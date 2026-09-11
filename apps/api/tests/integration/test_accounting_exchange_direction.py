"""Phase 4 — Gate Review regression: an exchange direction that cannot exist is refused.

**Vulnerability (Hole A).** ``post_exchange`` validated the *amounts* of a deal — rate,
quantity, commission, date — but never the *direction* the document describes. An invalid
direction therefore reached financial posting:

* ``SELL`` with ``from_currency`` = the **functional** currency **posted a nonsense entry**
  (it treated the afghani as a foreign currency, crediting the AFN drawer at a carrying rate
  of 1 and debiting the foreign drawer with ``amount x rate``);
* ``BUY``/``SELL`` naming **one currency twice posted too** (a self-cancelling pair of lines
  on the same drawer);
* ``BUY`` with the functional currency delivered was refused only by accident — the disposal
  guard happened to read the *other* drawer's position and raised ``INSUFFICIENT_BALANCE``,
  a message about a missing position for a request whose real problem was its direction.
  With a funded drawer that refusal disappeared as well.

**Root cause.** ``ACCOUNTING_MODEL.md`` §6.2/§6.3 define both exchange types with the
**delivered** currency foreign ("business acquires foreign currency" / "business disposes
foreign currency"), but the service never checked that the currencies it was handed could
describe such a deal. Nothing downstream caught it: the entry is *balanced* (the arithmetic
of a nonsense deal is still arithmetic), so neither the validator nor the deferred constraint
trigger had anything to object to.

**Fix.** ``_assert_exchange_direction`` runs as soon as both currencies are loaded and
refuses the two impossible shapes with their own code, ``EXCHANGE_DIRECTION_INVALID`` (422),
naming the offending field:

* ``SAME_CURRENCY`` — the same currency on both sides (nothing is exchanged);
* ``FUNCTIONAL_CURRENCY_NOT_DELIVERABLE`` — the functional currency as the delivered side (a
  customer buying foreign currency from us is a SELL of that foreign currency, never a BUY
  of the afghani).

**Why the previous tests did not catch it.** The suite tested the *arithmetic* of valid
directions (§6.2/§6.3 worked examples, three-currency entries, FX results) and the refusals
of *values* (zero, negative, unbalanced, unquoted). No test asked what happens when the
currency pair itself is impossible, and the one test that touched the case — "a currency
without a quote cannot be valued" — was written with ``from_currency_id=base`` and passed
because ``RATE_NOT_FOUND`` fired on the way; it was passing for the wrong reason and has been
rewritten to reach the intended boundary with a valid direction.

**Invariants protected.** No invalid financial state may be committed (an entry that cannot
describe a real deal must not exist), Σdebit = Σcredit, posted history stays reproducible,
and a refused request leaves no journal entry, no line, no position change and no finalized
idempotency key. Every assertion below reads the database, not the service's return value.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import ExchangeDirectionError
from tests.accounting_helpers import (
    FinancialState,
    World,
    build_world,
    count,
    financial_state,
    idempotency_rows,
    publish_rate,
    read,
    run_scenario,
)

pytestmark = [pytest.mark.integration, pytest.mark.accounting]

USD, EUR = "USD", "EUR"


def open_drawer(world: World, *, currency: str, amount: str, rate: str) -> None:
    """Fund one drawer with an audited opening balance, so no refusal can be about cash."""

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
    usd: str = "1000",
    eur: str = "500",
) -> World:
    """A branch with published quotes, a funded AFN drawer and funded USD/EUR drawers.

    Everything that *could* refuse a valid deal is satisfied up front: the quotes exist
    (``build_world`` publishes USD/AFN; EUR is published here), the drawers hold currency,
    so what is left to refuse a posting is the direction itself.
    """
    world = build_world(api_client, admin_headers, database, quotes=True)
    publish_rate(
        api_client,
        admin_headers,
        from_currency_id=str(world.money(EUR).id),
        to_currency_id=str(world.base.id),
        buy_rate="75",
        sell_rate="76",
    )
    open_drawer(world, currency="AFN", amount="10000000", rate="1")
    open_drawer(world, currency=USD, amount=usd, rate="70")
    open_drawer(world, currency=EUR, amount=eur, rate="75")
    return world


def exchange_args(world: World, **overrides: object) -> dict[str, object]:
    """A valid BUY of USD paid with afghanis, overridable field by field."""
    arguments: dict[str, object] = {
        "transaction_type": "BUY",
        "reference_id": uuid.uuid4(),
        "branch_id": world.branch_id,
        "from_currency_id": world.money(USD).id,
        "to_currency_id": world.base.id,
        "from_amount": Decimal("100"),
        "exchange_rate": Decimal("70"),
        "from_cash_account_id": world.account("cash_usd"),
        "to_cash_account_id": world.account("cash_afn"),
        "fx_account_id": world.account("fx"),
        "commission": Decimal("0"),
        "actor": world.head_actor,
    }
    arguments.update(overrides)
    return arguments


def guarded(scenario):
    """Run a scenario that must fail, returning the refusal instead of raising it."""

    async def runner(service):
        try:
            return await scenario(service)
        except ExchangeDirectionError as refusal:
            return refusal

    return runner


def snapshot(world: World, *extra: str) -> FinancialState:
    return financial_state(
        world.database,
        [
            world.account(name)
            for name in ("cash_afn", "cash_usd", "cash_eur", "capital", "fx", *extra)
        ],
    )


class TestInvalidDirectionsAreRefused:
    """The three impossible shapes, each proved with everything else about the deal valid."""

    def test_buy_rejects_functional_currency_as_from_currency(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A BUY cannot deliver the functional currency: §6.2 acquires *foreign* currency."""
        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    from_currency_id=world.base.id,
                    to_currency_id=world.money(USD).id,
                    from_cash_account_id=world.account("cash_afn"),
                    to_cash_account_id=world.account("cash_usd"),
                )
            )

        refusal = run_scenario(main_database, guarded(scenario))
        assert isinstance(refusal, ExchangeDirectionError), refusal
        assert refusal.code == "EXCHANGE_DIRECTION_INVALID"
        assert refusal.http_status == 422
        assert refusal.details["reason"] == "FUNCTIONAL_CURRENCY_NOT_DELIVERABLE"
        assert refusal.details["transaction_type"] == "BUY"
        assert refusal.details["functional_currency_code"] == world.base.code
        assert refusal.details["fields"] == [
            {"field": "from_currency_id", "code": "functional_currency"}
        ]

    def test_sell_rejects_functional_currency_as_from_currency(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A SELL cannot deliver the functional currency: §6.3 disposes *foreign* currency.

        This is the shape that used to **post** - the delivered leg was priced at a carrying
        rate of 1 (the functional currency is always 1 to itself) and the received leg at
        ``amount x rate``, producing a balanced entry for a deal that cannot exist.
        """
        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    transaction_type="SELL",
                    from_currency_id=world.base.id,
                    to_currency_id=world.money(USD).id,
                    from_cash_account_id=world.account("cash_afn"),
                    to_cash_account_id=world.account("cash_usd"),
                )
            )

        refusal = run_scenario(main_database, guarded(scenario))
        assert isinstance(refusal, ExchangeDirectionError), refusal
        assert refusal.details["reason"] == "FUNCTIONAL_CURRENCY_NOT_DELIVERABLE"
        assert refusal.details["transaction_type"] == "SELL"
        assert refusal.details["fields"] == [
            {"field": "from_currency_id", "code": "functional_currency"}
        ]

    @pytest.mark.parametrize("transaction_type", ["BUY", "SELL"])
    def test_exchange_rejects_same_currency(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        transaction_type: str,
    ) -> None:
        """One currency on both sides exchanges nothing, in either direction."""
        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    transaction_type=transaction_type,
                    from_currency_id=world.money(USD).id,
                    to_currency_id=world.money(USD).id,
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_usd"),
                )
            )

        refusal = run_scenario(main_database, guarded(scenario))
        assert isinstance(refusal, ExchangeDirectionError), refusal
        assert refusal.details["reason"] == "SAME_CURRENCY"
        assert refusal.details["transaction_type"] == transaction_type
        assert refusal.details["from_currency_id"] == refusal.details["to_currency_id"]
        assert refusal.details["fields"] == [{"field": "to_currency_id", "code": "same_currency"}]

    def test_a_foreign_to_foreign_same_pair_is_not_the_functional_shortcut(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The refusal is about the pair, not about which currency happens to be base.

        EUR delivered for USD is a legitimate SELL (both foreign, §6.2's ``T = base or
        other``); EUR delivered for EUR is not. The two differ only in ``to_currency_id``,
        which is what makes this a direction test rather than a rate test.
        """
        world = funded_world(api_client, admin_headers, main_database)

        async def rejected(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    transaction_type="SELL",
                    from_currency_id=world.money(EUR).id,
                    to_currency_id=world.money(EUR).id,
                    from_cash_account_id=world.account("cash_eur"),
                    to_cash_account_id=world.account("cash_eur"),
                )
            )

        refusal = run_scenario(main_database, guarded(rejected))
        assert isinstance(refusal, ExchangeDirectionError), refusal
        assert refusal.details["reason"] == "SAME_CURRENCY"


class TestAnInvalidDirectionNeverReachesTheLedger:
    """The state before and after a refused deal, read from the database."""

    def test_invalid_exchange_direction_with_valid_quote_never_posts(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Valid quote, funded drawers, valid amount - refused *only* for its direction.

        Every precondition of a postable deal is asserted to be satisfied first, so the
        refusal cannot be explained by a missing rate or an empty drawer.
        """
        world = funded_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        before = snapshot(world)

        # A quote for the pair is in force at this branch (Phase 0's resolver, through the
        # API the operator uses) and the drawers are funded.
        quote = api_client.get(
            "/api/v1/rates/resolve",
            params={
                "from_currency_id": str(world.money(USD).id),
                "to_currency_id": str(world.base.id),
                "branch_id": str(world.branch_id),
            },
            headers=admin_headers,
        )
        assert quote.status_code == 200, quote.text
        assert Decimal(quote.json()["buy_rate"]) > 0
        assert before.quantities[str(world.account("cash_usd"))] == Decimal("1000.0000000000")

        async def scenario(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    from_currency_id=world.base.id,
                    to_currency_id=world.money(USD).id,
                    reference_id=reference_id,
                    from_cash_account_id=world.account("cash_afn"),
                    to_cash_account_id=world.account("cash_usd"),
                )
            )

        refusal = run_scenario(main_database, guarded(scenario))
        assert isinstance(refusal, ExchangeDirectionError), refusal

        after = snapshot(world)
        assert after == before  # counts, totals, positions: not one row moved
        assert after.balanced
        assert (
            count(main_database, "journal_entries", where="reference_id = :id", id=reference_id)
            == 0
        )
        # The refusal is not even an auditable *denial*: it is a malformed document, decided
        # before anything was written. A permission refusal would add a row; this must not.
        assert after.audit_rows == before.audit_rows
        assert (
            read(
                main_database,
                "SELECT count(*) AS n FROM journal_lines WHERE account_id = :account",
                account=world.account("cash_usd"),
            )[0]["n"]
            > 0
        )  # the drawer's opening balance is the only history it has

    def test_a_rejected_direction_does_not_finalize_the_idempotency_key(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A refused direction must not burn the key, and must not answer from a stored body.

        The refusal happens before the key is claimed, so no ``idempotency_keys`` row exists
        at all - the strongest form of "not incorrectly finalized". Correcting the direction
        and retrying with the *same* key then posts.
        """
        world = funded_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def scenario(service):
            bad = await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    reference_id=reference_id,
                    idempotency_key=key,
                    from_currency_id=world.base.id,
                    to_currency_id=world.money(USD).id,
                    from_cash_account_id=world.account("cash_afn"),
                    to_cash_account_id=world.account("cash_usd"),
                )
            )
            good = await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    reference_id=reference_id,
                    idempotency_key=key,
                )
            )
            return bad, good

        async def runner(service):
            try:
                return await scenario(service)
            except ExchangeDirectionError as refusal:
                return refusal

        outcome = run_scenario(main_database, runner)
        assert isinstance(outcome, ExchangeDirectionError), outcome

        # The key is untouched: no row, therefore neither COMPLETED nor IN_PROGRESS.
        assert idempotency_rows(main_database, key=key) == []
        assert (
            count(main_database, "journal_entries", where="reference_id = :id", id=reference_id)
            == 0
        )

        # ... and the corrected request with the same key posts exactly one entry.
        async def corrected(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    reference_id=reference_id,
                    idempotency_key=key,
                )
            )

        view = run_scenario(main_database, corrected)
        assert view.reference_id == reference_id
        rows = idempotency_rows(main_database, key=key)
        assert len(rows) == 1
        assert str(rows[0]["status"]).lower() in {"completed", "succeeded"}
        assert (
            count(main_database, "journal_entries", where="reference_id = :id", id=reference_id)
            == 1
        )

    def test_a_refused_direction_leaves_the_branch_positions_untouched(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Both drawers keep exactly the quantities they held: no partial movement."""
        world = funded_world(api_client, admin_headers, main_database)
        before = snapshot(world)

        async def scenario(service):
            return await service.post_exchange(
                **exchange_args(  # type: ignore[arg-type]
                    world,
                    transaction_type="SELL",
                    from_currency_id=world.base.id,
                    to_currency_id=world.money(EUR).id,
                    from_cash_account_id=world.account("cash_afn"),
                    to_cash_account_id=world.account("cash_eur"),
                )
            )

        refusal = run_scenario(main_database, guarded(scenario))
        assert isinstance(refusal, ExchangeDirectionError), refusal
        after = snapshot(world)
        assert after.quantities[str(world.account("cash_afn"))] == Decimal("10000000.0000000000")
        assert after.quantities[str(world.account("cash_eur"))] == Decimal("500.0000000000")
        assert after == before

    def test_an_unknown_transaction_type_is_still_refused_first(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Direction validation did not displace the existing type/amount rules."""
        from app.core.exceptions import ValidationError

        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            try:
                await service.post_exchange(
                    **exchange_args(  # type: ignore[arg-type]
                        world,
                        transaction_type="SWAP",
                        from_currency_id=world.base.id,
                        to_currency_id=world.base.id,
                    )
                )
            except ValidationError as refusal:
                return refusal
            return None

        refusal = run_scenario(main_database, scenario)
        assert isinstance(refusal, ValidationError), refusal
        assert refusal.details["fields"] == [{"field": "transaction_type", "code": "unsupported"}]


class TestValidExchangesStillPost:
    """The fix must not close a legitimate door — asserted line by line."""

    def test_valid_foreign_to_functional_exchange_still_posts(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """SELL 100 USD at 71 (the house's sell rate) into the AFN drawer."""
        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money(USD).id,
                to_currency_id=world.base.id,
                from_amount=Decimal("100"),
                exchange_rate=Decimal("71"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        assert view.total_debit == Decimal("7100.0000000000")
        assert view.total_credit == view.total_debit
        stored = {
            row["account_code"]: row
            for row in read(
                main_database,
                """
            SELECT a.code AS account_code, l.debit, l.credit, l.foreign_amount
              FROM journal_lines l JOIN accounts a ON a.id = l.account_id
             WHERE l.journal_entry_id = :entry
        """,
                entry=view.id,
            )
        }
        assert stored[world.codes["cash_usd"]]["foreign_amount"] == Decimal("100.0000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("7000.0000000000")
        assert stored[world.codes["cash_afn"]]["debit"] == Decimal("7100.0000000000")

        after = snapshot(world)
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("900.0000000000")
        assert after.balanced

    def test_valid_buy_of_foreign_currency_still_posts(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """BUY 100 USD at 70 paying afghanis: the acquired leg enters at its price."""
        world = funded_world(api_client, admin_headers, main_database)

        view = run_scenario(
            main_database,
            lambda service: service.post_exchange(**exchange_args(world)),  # type: ignore[arg-type]
        )
        after = snapshot(world)
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("1100.0000000000")
        assert after.functional[str(world.account("cash_afn"))] == Decimal(
            "9993000.0000000000"  # 10,000,000 - 7,000
        )
        assert view.is_balanced and after.balanced

    def test_valid_foreign_to_foreign_exchange_still_posts(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """SELL 100 EUR for USD: neither side is the functional currency (§6.2's *other*)."""
        world = funded_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money(EUR).id,
                to_currency_id=world.money(USD).id,
                from_amount=Decimal("100"),
                exchange_rate=Decimal("1.5"),  # 1.5 USD per EUR
                from_cash_account_id=world.account("cash_eur"),
                to_cash_account_id=world.account("cash_usd"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        after = snapshot(world)
        assert view.is_balanced and after.balanced
        assert after.quantities[str(world.account("cash_eur"))] == Decimal("400.0000000000")
        assert after.quantities[str(world.account("cash_usd"))] == Decimal("1150.0000000000")
        # The realized FX result: 150 USD received valued at 70 (10,500) against the 7,500
        # the EUR cost, so a gain of 3,000 - credited, which leaves the raw debit-minus-credit
        # of account 4000 negative (it is credit-normal, §6.3).
        assert after.functional[str(world.account("fx"))] == Decimal("-3000.0000000000")


class TestTheAuditedTestThatPassedForTheWrongReason:
    """The direction rules replaced an accidental refusal - this pins the replacement."""

    def test_an_unquoted_received_currency_is_still_refused_with_rate_not_found(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The intended boundary of the rewritten rate test, reached with a *valid* direction.

        A SELL delivering USD and receiving an unquoted currency must fail on the missing
        quote (``RATE_NOT_FOUND``), not on its direction: the delivery is a legitimate
        foreign-currency disposal with a funded drawer behind it.
        """
        from app.core.exceptions import RateNotFoundError
        from tests.accounting_helpers import create_account, create_currency
        from tests.masterdata_helpers import unique_currency_code

        world = funded_world(api_client, admin_headers, main_database)
        exotic = create_currency(api_client, admin_headers, code=unique_currency_code())
        exotic_cash = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=f"T{uuid.uuid4().hex[:8].upper()}",
                name="Cash in an unquoted currency",
                account_type="ASSET",
                currency_id=exotic["id"],
            )["id"]
        )
        before = snapshot(world)

        async def scenario(service):
            with pytest.raises(RateNotFoundError) as refusal:
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money(USD).id,
                    to_currency_id=uuid.UUID(exotic["id"]),
                    from_amount=Decimal("100"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=exotic_cash,
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["currency_code"] == exotic["code"]
        assert refusal.details["functional_currency_code"] == world.base.code
        assert snapshot(world) == before
