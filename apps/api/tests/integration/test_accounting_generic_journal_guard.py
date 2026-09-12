"""Phase 4 — Gate Review regression: the generic journal door cannot create a position.

**Vulnerability (Hole B).** ``create_journal_entry`` — the generic door into the ledger —
validated accounts, currencies, postability, branches and the balance proof, but never the
**physical positions** those lines moved. A manual entry that credited an inventory account
therefore drove it negative: the probe recorded in `docs/phases/PHASE4_REPORT.md` disposed of
500 USD from a drawer holding nothing and left the account at ``-7.1428571429`` units, with a
balanced, immutable, fully audited journal entry to prove it. Nothing refused it — not the
service, not a trigger: the deferred ``ct_journal_lines_balanced_*`` constraint only checks
Σdebit = Σcredit, and ``ct_cash_movements_non_negative`` (``NEX01``) guards the *cash
movements* table, which a manual journal never writes.

**Root cause.** The disposal guard lived in ``_carrying_rate``, which only the *exchange*
path calls, and it was expressed as a rate rather than as a quantity (a document posts a
quantity and the ledger prices it). A manual entry states its own rate, so it never reached
that guard; the door that any future phase may use for corrections had no physical rule at
all.

**Fix.** The generic door sets ``guard_inventory`` on its posting plan, and ``_post`` runs
``_assert_inventory_positions`` after the shared context validation and before the first
insert. For every **inventory** account (an asset account bound to a currency — the model's
definition of a place that holds a position, §2) it sums the lines' physical contributions,
``(debit - credit) / rate`` — exactly the expression PostgreSQL uses to generate
``foreign_amount`` — and refuses a negative outcome with the same vocabulary the exchange
door uses: ``NO_POSITION`` when the drawer is empty, ``QUANTITY_EXCEEDED`` with the shortfall
when it is not. Document paths keep their own rules: they are backed by a business document
and by the ``cash_movements`` row whose ``NEX01`` constraint is the physical authority for
them.

**Why the previous tests did not catch it.** Every test of the generic door posted *into*
cash (a debit, the balance-testing vehicle) or moved currency-less accounts. The suites
tested the exchange door's disposal guard but never asked whether the same rule was
reachable through the journal door — and the two doors shared validation, which made the
assumption look safe.

**Invariants protected.** Invariant I-5 ("cash position never negative") now holds for every
posting path that writes the ledger, Σdebit = Σcredit is unchanged, posted history stays
immutable, and a refused manual entry leaves no entry, no line, no position change and no
finalized idempotency key.

Locking: the accounts are locked by ``_check_ledger_context`` through production code
(``JournalEntryRepository.lock_accounts``, sorted by id — the same order the lines are
inserted in), *before* any position is read, so the concurrency test at the end of this file
is a real race resolved by PostgreSQL row locks rather than by a mock.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import InsufficientBalanceError
from app.core.permissions import RoleName
from tests.accounting_helpers import (
    FinancialState,
    World,
    account_id_of,
    build_world,
    count,
    create_account,
    create_branch,
    deactivate_branch,
    financial_state,
    idempotency_rows,
    line,
    read,
    run_race,
    run_scenario,
)

pytestmark = [pytest.mark.integration, pytest.mark.accounting]

USD, EUR = "USD", "EUR"

# The rates this file uses: one unit is worth what the drawer paid for it.
USD_RATE, EUR_RATE = "70", "75"


def open_drawer(world: World, *, currency: str, amount: str, rate: str) -> None:
    """Fund a drawer with an opening balance, so a position exists to be guarded."""

    async def scenario(service):
        return await service.post_cash_movement(
            movement_type="OPENING",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account(f"cash_{currency.lower()}"),
            counter_account_id=world.account("capital"),
            currency_id=world.money(currency).id,
            amount=Decimal(amount),
            exchange_rate=Decimal(rate),
            actor=world.head_actor,
        )

    run_scenario(world.database, scenario)


def funded_world(
    api_client: TestClient,
    admin_headers: dict[str, str],
    database: str,
    *,
    usd: str | None = "1000",
    eur: str | None = "500",
) -> World:
    """A branch whose USD (and optionally EUR) drawer holds a known position."""
    world = build_world(api_client, admin_headers, database, quotes=True)
    if usd is not None:
        open_drawer(world, currency=USD, amount=usd, rate=USD_RATE)
    if eur is not None:
        open_drawer(world, currency=EUR, amount=eur, rate=EUR_RATE)
    return world


def disposal(
    world: World,
    *,
    account: str,
    currency: str,
    functional: str,
    rate: str,
    counter: str = "capital",
    reference_id: uuid.UUID | None = None,
    idempotency_key: uuid.UUID | None = None,
    branch_id: uuid.UUID | None = None,
    actor: object | None = None,
) -> object:
    """A manual entry that *delivers* ``functional`` worth of ``currency`` from a drawer."""

    async def scenario(service):
        return await service.create_journal_entry(
            reference_type="MANUAL_ADJUSTMENT",
            reference_id=reference_id or uuid.uuid4(),
            branch_id=branch_id or world.branch_id,
            lines=[
                line(
                    world.account(counter),
                    debit=functional,
                    currency_id=world.base.id,
                ),
                line(
                    world.account(account),
                    credit=functional,
                    currency_id=world.money(currency).id,
                    exchange_rate=rate,
                ),
            ],
            actor=actor or world.head_actor,
            description="manual disposal under test",
            idempotency_key=idempotency_key,
        )

    return scenario


def refusal(scenario):
    """Run a scenario that must be refused, returning the refusal instead of raising it."""

    async def runner(service):
        try:
            return await scenario(service)
        except InsufficientBalanceError as error:
            return error

    return runner


def snapshot(world: World, *extra: uuid.UUID) -> FinancialState:
    """The chart slice plus any account the test created itself."""
    return financial_state(
        world.database,
        [world.account(name) for name in ("cash_afn", "cash_usd", "cash_eur", "capital", "payable")]
        + list(extra),
    )


def usd_position(world: World) -> Decimal:
    return snapshot(world).quantities[str(world.account("cash_usd"))]


class TestTheGenericDoorRefusesToDeliverWhatItDoesNotHold:
    """The guard, case by case: empty, short, exact and ordinary."""

    def test_generic_journal_rejects_cash_disposal_from_empty_position(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """An unfunded drawer holds nothing, so a manual delivery of it is refused."""
        world = funded_world(api_client, admin_headers, main_database, usd=None, eur=None)
        before = snapshot(world)
        assert before.quantities[str(world.account("cash_usd"))] == Decimal("0")

        outcome = run_scenario(
            main_database,
            refusal(
                disposal(
                    world,
                    account="cash_usd",
                    currency=USD,
                    functional="700",
                    rate=USD_RATE,
                )
            ),
        )
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.code == "INSUFFICIENT_BALANCE"
        assert outcome.details["reason"] == "NO_POSITION"
        assert outcome.details["foreign_quantity"] == "0.0000000000"
        assert outcome.details["disposing_quantity"] == "10.0000000000"
        assert outcome.details["branch_id"] == str(world.branch_id)

        after = snapshot(world)
        assert after == before
        assert after.balanced

    def test_generic_journal_rejects_cash_disposal_above_position(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """1,100 units out of a 1,000-unit drawer: refused, with the shortfall named."""
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)
        assert before.quantities[str(world.account("cash_usd"))] == Decimal("1000.0000000000")

        outcome = run_scenario(
            main_database,
            refusal(
                disposal(
                    world,
                    account="cash_usd",
                    currency=USD,
                    # 1,100 units at 70 = 77,000 functional
                    functional="77000",
                    rate=USD_RATE,
                )
            ),
        )
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.details["reason"] == "QUANTITY_EXCEEDED"
        assert outcome.details["foreign_quantity"] == "1000.0000000000"
        assert outcome.details["disposing_quantity"] == "1100.0000000000"
        assert outcome.details["shortfall"] == "100.0000000000"
        assert outcome.details["account_id"] == str(world.account("cash_usd"))

        after = snapshot(world)
        assert after == before
        assert after.balanced

    def test_generic_journal_allows_exact_cash_disposal(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Delivering exactly what is held is legitimate and leaves the drawer at zero."""
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)

        async def scenario(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(world.account("capital"), debit="70000", currency_id=world.base.id),
                    line(
                        world.account("cash_usd"),
                        credit="70000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                ],
                actor=world.head_actor,
                description="the whole position, delivered",
            )

        view = run_scenario(main_database, scenario)
        stored = {
            row["account_code"]: row
            for row in read(
                main_database,
                """
            SELECT a.code AS account_code, l.credit, l.foreign_amount
              FROM journal_lines l JOIN accounts a ON a.id = l.account_id
             WHERE l.journal_entry_id = :entry
            """,
                entry=view.id,
            )
        }
        assert stored[world.codes["cash_usd"]]["foreign_amount"] == Decimal("1000.0000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("70000.0000000000")

        after = snapshot(world)
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("0.0000000000")
        assert after.entries == before.entries + 1
        assert after.lines == before.lines + 2
        assert after.balanced

        # ... and the drawer is now empty, so the next delivery is refused: the guard bites
        # at zero rather than allowing the position to cross into the negative.
        outcome = run_scenario(
            main_database,
            refusal(
                disposal(
                    world,
                    account="cash_usd",
                    currency=USD,
                    functional="70",
                    rate=USD_RATE,
                )
            ),
        )
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.details["reason"] == "NO_POSITION"
        assert usd_position(world) == Decimal("0.0000000000")

    def test_generic_journal_cannot_create_negative_inventory(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The guard is per *plan*: two lines that are each acceptable together are not.

        Neither 600-unit line exceeds the 1,000-unit position on its own, but the entry
        delivers 1,200 and must be refused as a whole - a guard that checked line by line
        would wave this through and leave the drawer 200 units short.
        """
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)

        async def scenario(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(world.account("capital"), debit="84000", currency_id=world.base.id),
                    line(
                        world.account("cash_usd"),
                        credit="42000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                    line(
                        world.account("cash_usd"),
                        credit="42000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                ],
                actor=world.head_actor,
                description="two lines, one over-delivery",
            )

        outcome = run_scenario(main_database, refusal(scenario))
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.details["reason"] == "QUANTITY_EXCEEDED"
        assert outcome.details["disposing_quantity"] == "1200.0000000000"
        assert outcome.details["shortfall"] == "200.0000000000"
        assert snapshot(world) == before
        assert usd_position(world) == Decimal("1000.0000000000")

    def test_generic_journal_normal_accounts_remain_allowed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The guard is scoped to inventory accounts, so ordinary journals are untouched.

        A currency-less control account has no position to run out of, and a currency-bound
        **liability** that goes into debit is a receivable - not a drawer missing banknotes.
        Both must keep posting, or the guard would refuse legitimate accounting.
        """
        world = funded_world(api_client, admin_headers, main_database)
        branch_lender = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=f"T{uuid.uuid4().hex[:8].upper()}",
                name="Payable in USD (per-currency control account)",
                account_type="LIABILITY",
                currency_id=str(world.money(USD).id),
            )["id"]
        )

        async def scenario(service):
            first = await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(world.account("payable"), debit="5000", currency_id=world.base.id),
                    line(world.account("capital"), credit="5000", currency_id=world.base.id),
                ],
                actor=world.head_actor,
                description="currency-less control accounts",
            )
            # A liability account with a currency, drawn below zero: allowed by design.
            second = await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(
                        branch_lender,
                        debit="7000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                    line(world.account("capital"), credit="7000", currency_id=world.base.id),
                ],
                actor=world.head_actor,
                description="a payable drawn into debit",
            )
            # A debit *into* an inventory account is an increase, never a disposal.
            third = await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(
                        world.account("cash_usd"),
                        debit="14000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                    line(world.account("capital"), credit="14000", currency_id=world.base.id),
                ],
                actor=world.head_actor,
                description="a deposit into the drawer",
            )
            return first, second, third

        views = run_scenario(main_database, scenario)
        assert all(view.is_balanced for view in views)
        after = snapshot(world, "cash_usd")
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("1200.0000000000")
        assert after.balanced

    def test_generic_journal_multicurrency_position_guard(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Each currency is guarded by its own position: a full USD drawer cannot cover EUR."""
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)

        # 500 USD out of 1,000 (fine) beside 600 EUR out of 500 (not fine).
        async def over(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(world.account("capital"), debit="80000", currency_id=world.base.id),
                    line(
                        world.account("cash_usd"),
                        credit="35000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                    line(
                        world.account("cash_eur"),
                        credit="45000",
                        currency_id=world.money(EUR).id,
                        exchange_rate=EUR_RATE,
                    ),
                ],
                actor=world.head_actor,
                description="one leg within, one leg beyond",
            )

        outcome = run_scenario(main_database, refusal(over))
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.details["account_id"] == str(world.account("cash_eur"))
        assert outcome.details["reason"] == "QUANTITY_EXCEEDED"
        assert outcome.details["shortfall"] == "100.0000000000"
        after = snapshot(world)
        assert after == before  # neither currency moved, not even the acceptable leg

        # The same entry with the EUR leg inside its position posts and moves both drawers.
        async def within(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(world.account("capital"), debit="42500", currency_id=world.base.id),
                    line(
                        world.account("cash_usd"),
                        credit="35000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                    line(
                        world.account("cash_eur"),
                        credit="7500",
                        currency_id=world.money(EUR).id,
                        exchange_rate=EUR_RATE,
                    ),
                ],
                actor=world.head_actor,
                description="both legs within their positions",
            )

        run_scenario(main_database, within)
        funded = snapshot(world)
        assert funded.quantities[str(world.account("cash_usd"))] == Decimal("500.0000000000")
        assert funded.quantities[str(world.account("cash_eur"))] == Decimal("400.0000000000")
        assert funded.balanced

    def test_generic_journal_rejection_rolls_back_all_financial_state(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Nothing survives a refused manual entry: no entry, no line, no cache, no audit."""
        world = funded_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        before = snapshot(world)

        outcome = run_scenario(
            main_database,
            refusal(
                disposal(
                    world,
                    account="cash_usd",
                    currency=USD,
                    functional="77000",
                    rate=USD_RATE,
                    reference_id=reference_id,
                )
            ),
        )
        assert isinstance(outcome, InsufficientBalanceError), outcome

        after = snapshot(world)
        assert after.entries == before.entries
        assert after.lines == before.lines
        assert after.audit_rows == before.audit_rows
        assert after.idempotency_rows == before.idempotency_rows
        assert after.total_debit == before.total_debit == after.total_credit == before.total_credit
        assert after == before
        assert (
            count(main_database, "journal_entries", where="reference_id = :id", id=reference_id)
            == 0
        )
        # No balance cache row was created for the account either.
        assert (
            count(
                main_database,
                "account_balances",
                where="account_id = :account",
                account=world.account("cash_usd"),
            )
            == 1  # the funded opening balance, and nothing from the refused entry
        )

    def test_generic_journal_position_guard_is_branch_scoped(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Another branch's cash is not yours to deliver, even from a group-wide account.

        The drawer is group-wide on purpose (a control account may legitimately be posted to
        from any branch), so the only thing separating the two branches is the position:
        ``position()`` counts the entries of the branch that is trading, and the branch that
        holds nothing cannot deliver. The branch created here is deactivated before the test
        ends, because a second *active* branch makes device registration ambiguous for every
        later login fixture.
        """
        world = funded_world(api_client, admin_headers, main_database)
        other = create_branch(api_client, admin_headers)
        other_branch_id = uuid.UUID(other["id"])
        offset_account = account_id_of(main_database, "6000")  # group-wide opening offset

        # The account itself is group-wide (so it may legitimately be posted to from any
        # branch); only the *position* is branch-scoped, and the other branch has none.
        shared_cash = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=f"T{uuid.uuid4().hex[:8].upper()}",
                name="Group-wide USD drawer",
                account_type="ASSET",
                currency_id=str(world.money(USD).id),
            )["id"]
        )

        async def funded(service):
            return await service.post_cash_movement(
                movement_type="OPENING",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=shared_cash,
                counter_account_id=world.account("capital"),
                currency_id=world.money(USD).id,
                amount=Decimal("1000"),
                exchange_rate=Decimal(USD_RATE),
                actor=world.head_actor,
            )

        run_scenario(main_database, funded)
        before = snapshot(world, shared_cash)
        assert before.quantities[str(shared_cash)] == Decimal("1000.0000000000")

        # A global-scope actor posting into the other branch, whose drawer holds nothing.
        actor = world.actor(branch_id=None, roles=(RoleName.SUPER_ADMIN,))

        async def scenario(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=other_branch_id,
                lines=[
                    line(offset_account, debit="7000", currency_id=world.base.id),
                    line(
                        shared_cash,
                        credit="7000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                ],
                actor=actor,
                description="delivering another branch's USD",
            )

        try:
            outcome = run_scenario(main_database, refusal(scenario))
            after = snapshot(world, shared_cash)
        finally:
            # Deactivating the branch is itself audited, so the snapshot above is taken
            # while the extra branch is still active.
            deactivate_branch(api_client, admin_headers, other["id"])
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert outcome.details["reason"] == "NO_POSITION"
        assert outcome.details["branch_id"] == str(other_branch_id)
        assert after == before
        assert after.quantities[str(shared_cash)] == Decimal("1000.0000000000")
        assert usd_position(world) == Decimal("1000.0000000000")


class TestRejectedGenericJournalAndTheIdempotencyKey:
    """The protocol rule: a refusal must not finalize (or burn) the caller's key."""

    def test_rejected_generic_journal_does_not_finalize_idempotency(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The key is claimed and released with the rolled-back transaction - then works."""
        world = funded_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        outcome = run_scenario(
            main_database,
            refusal(
                disposal(
                    world,
                    account="cash_usd",
                    currency=USD,
                    functional="77000",
                    rate=USD_RATE,
                    reference_id=reference_id,
                    idempotency_key=key,
                )
            ),
        )
        assert isinstance(outcome, InsufficientBalanceError), outcome
        assert idempotency_rows(main_database, key=key) == []

        # The corrected request (the whole 1,000-unit position) posts under the same key.
        async def corrected(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=reference_id,
                branch_id=world.branch_id,
                lines=[
                    line(world.account("capital"), debit="70000", currency_id=world.base.id),
                    line(
                        world.account("cash_usd"),
                        credit="70000",
                        currency_id=world.money(USD).id,
                        exchange_rate=USD_RATE,
                    ),
                ],
                actor=world.head_actor,
                description="corrected after the refusal",
                idempotency_key=key,
            )

        view = run_scenario(main_database, corrected)
        rows = idempotency_rows(main_database, key=key)
        assert len(rows) == 1
        assert str(rows[0]["status"]).lower() in {"completed", "succeeded"}
        assert rows[0]["resource_id"] == view.id
        assert usd_position(world) == Decimal("0.0000000000")


class TestConcurrentGenericDisposals:
    """Two manual deliveries of one drawer, resolved by PostgreSQL row locks."""

    def test_concurrent_generic_cash_disposals_cannot_make_position_negative(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Both callers try to empty the same 1,000-unit drawer; exactly one can.

        The loser reads the position *after* the winner's commit (the accounts are locked
        for the whole transaction), so it is refused rather than allowed to overdraw - the
        guard is read-then-write only because the read happens under the production lock.
        """
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)
        assert before.quantities[str(world.account("cash_usd"))] == Decimal("1000.0000000000")

        outcomes = run_race(
            main_database,
            disposal(
                world,
                account="cash_usd",
                currency=USD,
                functional="70000",
                rate=USD_RATE,
            ),
            disposal(
                world,
                account="cash_usd",
                currency=USD,
                functional="70000",
                rate=USD_RATE,
            ),
        )
        winners = [item for item in outcomes if not isinstance(item, Exception)]
        losers = [item for item in outcomes if isinstance(item, Exception)]
        assert len(winners) == 1, outcomes
        assert len(losers) == 1, outcomes
        assert isinstance(losers[0], InsufficientBalanceError), losers[0]
        assert losers[0].details["reason"] in {"NO_POSITION", "QUANTITY_EXCEEDED"}

        after = snapshot(world)
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("0.0000000000")
        assert after.quantities[str(world.account("cash_usd"))] >= 0
        assert after.entries == before.entries + 1
        assert after.lines == before.lines + 2
        assert after.balanced

    def test_concurrent_partial_disposals_never_oversell_the_drawer(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """600 + 600 units against a 1,000-unit drawer: one posts, the other is refused."""
        world = funded_world(api_client, admin_headers, main_database)

        outcomes = run_race(
            main_database,
            disposal(
                world,
                account="cash_usd",
                currency=USD,
                functional="42000",  # 600 units
                rate=USD_RATE,
            ),
            disposal(
                world,
                account="cash_usd",
                currency=USD,
                functional="42000",  # 600 units
                rate=USD_RATE,
            ),
        )
        winners = [item for item in outcomes if not isinstance(item, Exception)]
        losers = [item for item in outcomes if isinstance(item, Exception)]
        assert len(winners) == 1, outcomes
        assert isinstance(losers[0], InsufficientBalanceError), losers[0]
        assert losers[0].details["reason"] == "QUANTITY_EXCEEDED"
        assert losers[0].details["shortfall"] == "200.0000000000"

        after = snapshot(world)
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("400.0000000000")
        assert after.balanced
