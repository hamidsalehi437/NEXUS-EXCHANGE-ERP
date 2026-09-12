"""Shared scaffolding for the Phase 5 exchange suites (PART 48, PART 49).

The exchange engine is a *document* engine: what has to be true after a call is not only
"the service returned 201" but "the row, the balanced entry, the physical movements, the
number and the audit row all exist, and nothing exists without them". So these helpers

* build real state — a real branch's drawers funded with a real opening posting (journal
  **and** the physical ``cash_movements`` row, exactly as ``ACCOUNTING_MODEL.md`` §6.1
  requires), real quotes through ``POST /rates``, real customers through ``POST /customers``;
* drive writes through the services (the only writers: PART 46) and reads through SQL, so an
  assertion never depends on the answer the service just gave;
* run each scenario on its own async engine (`run_scenario`/`run_race`), because one async
  engine belongs to one event loop — and the suites must run sequentially and in parallel.

The seeded chart is the fixture's chart: ``1000`` AFN, ``1001`` USD, ``1002`` EUR (the cash
band), ``4000`` FX result, ``4010`` commission income, ``6000`` opening offset. That is what
the engine resolves in production too, so the suites exercise the real resolution path rather
than a private one.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.database import Database
from app.core.exceptions import NexusError
from app.core.permissions import RoleName
from app.services.accounting_service import AccountingService, CashMovementSpec
from app.services.audit_service import ActorContext
from app.services.exchange_service import (
    ExchangeRequest,
    ExchangeResult,
    ExchangeService,
    build_exchange_service,
)
from tests.accounting_helpers import (
    BASE_CODE,
    Money,
    actor_for,
    count,
    read,
    read_one,
    scalar,
)
from tests.accounting_helpers import (
    identifier as _identifier,
)
from tests.accounting_helpers import (
    load_currencies as _load_currencies,
)
from tests.auth_helpers import ADMIN_USERNAME, bearer, login, register_device
from tests.helpers import DEV_ADMIN_PASSWORD, database_dsn, settings_for_database

EXCHANGE = "/api/v1/exchange"
BRANCHES = "/api/v1/branches"
REFERENCE_TYPE_EXCHANGE = "EXCHANGE_TRANSACTION"
REFERENCE_TYPE_REVERSAL = "REVERSAL"
RATES = "/api/v1/rates"
CUSTOMERS = "/api/v1/customers"

# The seeded cash-inventory band and the accounts the accounting model names.
DRAWER_CODES: dict[str, str] = {"AFN": "1000", "USD": "1001", "EUR": "1002"}
COMMISSION_ACCOUNT_CODE = "4010"
FX_RESULT_ACCOUNT_CODE = "4000"
OPENING_OFFSET_CODE = "6000"

# Foreign currencies the suites fund and trade.
FOREIGN_CODES = ("USD", "EUR")

# The rate the seeded quotes use: the house buys USD at 70 AFN and sells it at 71.
USD_BUY_RATE = Decimal("70")
USD_SELL_RATE = Decimal("71")


# --------------------------------------------------------------------------- reading
def account_id_of(database: str, code: str) -> uuid.UUID:
    return _identifier(database, "SELECT id FROM accounts WHERE code = :code", code=code)


def currency_id_of(database: str, code: str) -> uuid.UUID:
    return _identifier(database, "SELECT id FROM currencies WHERE code = :code", code=code)


def branch_id_of(database: str, code: str = "MAIN") -> uuid.UUID:
    return _identifier(database, "SELECT id FROM branches WHERE code = :code", code=code)


def document_row(database: str, transaction_id: uuid.UUID) -> dict[str, Any]:
    return read_one(
        database,
        "SELECT * FROM exchange_transactions WHERE id = :id",
        id=transaction_id,
    )


def documents_for_branch(database: str, branch_id: uuid.UUID) -> list[dict[str, Any]]:
    return read(
        database,
        """
        SELECT id, transaction_number, status, transaction_type, reversal_of_id,
               journal_entry_id, reversal_journal_entry_id, from_amount, to_amount,
               exchange_rate, commission, origin, client_event_id
          FROM exchange_transactions
         WHERE branch_id = :branch
         ORDER BY created_at, transaction_number
        """,
        branch=branch_id,
    )


def movements_of(
    database: str, *, reference_type: str, reference_id: uuid.UUID
) -> list[dict[str, Any]]:
    """The stored cash movements of one reference, with their signed amounts.

    Ordered "money in, then money out" — the order a cashier would count it — because every
    row a single transaction writes shares one ``created_at`` (PostgreSQL's transaction
    timestamp), so the clock cannot order them.
    """
    return read(
        database,
        """
        SELECT m.id, m.movement_type, m.amount, m.signed_amount, m.currency_id,
               c.code AS currency_code, m.account_id, a.code AS account_code,
               m.reference_type, m.reference_id, m.journal_entry_id, m.branch_id
          FROM cash_movements m
          JOIN currencies c ON c.id = m.currency_id
          JOIN accounts a ON a.id = m.account_id
         WHERE m.reference_type = :reference_type AND m.reference_id = :reference_id
         ORDER BY (m.signed_amount < 0), m.created_at, m.id
        """,
        reference_type=reference_type,
        reference_id=reference_id,
    )


def branch_cash(database: str, *, branch_id: uuid.UUID, currency_code: str) -> Decimal:
    """The branch's physical position for one currency (``v_cash_position``)."""
    value = scalar(
        database,
        """
        SELECT COALESCE((
            SELECT balance FROM v_cash_position
             WHERE branch_id = :branch AND currency_code = :code
        ), 0)
        """,
        branch=branch_id,
        code=currency_code,
    )
    return Decimal(str(value))


def ledger_position(
    database: str, *, branch_id: uuid.UUID, account_ids: Sequence[uuid.UUID]
) -> Decimal:
    """What the ledger says one branch's own cash accounts hold.

    Summed over exactly the accounts this scenario's drawers are bound to — not over a code
    band — because that is the question the assertion is really asking: *did the engine post
    to this branch's account?* A band query would happily sum a neighbour branch's cash (or
    the group account) and still pass, which is the failure mode this suite exists to catch.
    Debits add, credits subtract, and ``foreign_amount`` is generated from the line's own
    amount and rate, so the total is the currency quantity, not the functional value.
    """
    value = scalar(
        database,
        """
        SELECT COALESCE(SUM(CASE WHEN l.debit > 0 THEN l.foreign_amount
                                 ELSE -l.foreign_amount END), 0)
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch
           AND l.account_id = ANY(CAST(:accounts AS UUID[]))
        """,
        branch=branch_id,
        accounts=[str(account_id) for account_id in account_ids],
    )
    return Decimal(str(value))


def entry_row(database: str, journal_entry_id: uuid.UUID) -> dict[str, Any]:
    return read_one(
        database,
        """
        SELECT id, reference_type, reference_id, branch_id, description,
               transaction_date, created_by, device_id, reversal_of_id, created_at
          FROM journal_entries
         WHERE id = :id
        """,
        id=journal_entry_id,
    )


def entry_lines(database: str, journal_entry_id: uuid.UUID) -> list[dict[str, Any]]:
    """The ledger lines of one entry: what it debited, what it credited, in what currency."""
    return read(
        database,
        """
        SELECT l.id, l.account_id, a.code AS account_code, a.name AS account_name,
               a.account_type,
               c.code AS currency_code, l.debit, l.credit, l.foreign_amount,
               l.exchange_rate, l.description
          FROM journal_lines l
          JOIN accounts a ON a.id = l.account_id
          JOIN currencies c ON c.id = l.currency_id
         WHERE l.journal_entry_id = :id
         ORDER BY l.id
        """,
        id=journal_entry_id,
    )


def audit_rows_for_entity(database: str, entity_id: uuid.UUID) -> list[dict[str, Any]]:
    """Every audit row that names this entity, in chain order (``seq``)."""
    return read(
        database,
        """
        SELECT seq, action, entity_type, entity_id, user_id, device_id, ip_address,
               request_id, old_data, new_data, created_at
          FROM audit_logs
         WHERE entity_id = :id
         ORDER BY seq
        """,
        id=entity_id,
    )


def audit_actions(database: str, entity_id: uuid.UUID) -> list[str]:
    return [
        str(row["action"])
        for row in read(
            database,
            "SELECT action FROM audit_logs WHERE entity_id = :id ORDER BY seq",
            id=entity_id,
        )
    ]


def audit_rows_for_action(database: str, action: str) -> list[dict[str, Any]]:
    return read(
        database,
        """
        SELECT seq, action, entity_type, entity_id, user_id, new_data
          FROM audit_logs
         WHERE action = :action
         ORDER BY seq
        """,
        action=action,
    )


def idempotency_row(database: str, key: uuid.UUID) -> dict[str, Any]:
    return read_one(database, "SELECT * FROM idempotency_keys WHERE key = :key", key=key)


@dataclass(frozen=True, slots=True)
class ExchangeState:
    """Everything a refused exchange call must leave exactly as it was.

    ``documents`` and ``movements`` are the phase's own tables (absent before, absent after);
    the ledger counts and totals prove the refusal reached no posting; ``ledger_quantities``
    and ``cash_quantities`` are the positions — two independent readings of the same fact,
    which is what a test that says "the money did not move" has to check.
    """

    documents: int
    movements: int
    entries: int
    lines: int
    audit_rows: int
    idempotency_rows: int
    sequences: int
    total_debit: Decimal
    total_credit: Decimal
    ledger_quantities: dict[str, Decimal]
    cash_quantities: dict[str, Decimal]

    @property
    def balanced(self) -> bool:
        return self.total_debit == self.total_credit

    @property
    def book(self) -> tuple[object, ...]:
        """Everything except the ``Idempotency-Key`` rows.

        A replay legitimately writes one new claim row (a fresh key on a recorded event), so
        an assertion that "the books did not move" must leave those rows out — while still
        checking every document, movement, entry, line, audit row and position.
        """
        return (
            self.documents,
            self.movements,
            self.entries,
            self.lines,
            self.audit_rows,
            self.sequences,
            self.total_debit,
            self.total_credit,
            tuple(sorted(self.ledger_quantities.items())),
            tuple(sorted(self.cash_quantities.items())),
        )


def exchange_state(
    database: str,
    *,
    branch_id: uuid.UUID,
    drawers: Mapping[str, uuid.UUID],
) -> ExchangeState:
    totals_row = read_one(
        database,
        "SELECT COALESCE(SUM(debit), 0) AS d, COALESCE(SUM(credit), 0) AS c FROM journal_lines",
    )
    return ExchangeState(
        documents=count(database, "exchange_transactions"),
        movements=count(database, "cash_movements"),
        entries=count(database, "journal_entries"),
        lines=count(database, "journal_lines"),
        audit_rows=count(database, "audit_logs"),
        idempotency_rows=count(database, "idempotency_keys"),
        sequences=count(database, "sequences"),
        total_debit=Decimal(str(totals_row["d"])),
        total_credit=Decimal(str(totals_row["c"])),
        ledger_quantities={
            code: ledger_position(database, branch_id=branch_id, account_ids=[account_id])
            for code, account_id in drawers.items()
        },
        cash_quantities={
            code: branch_cash(database, branch_id=branch_id, currency_code=code) for code in drawers
        },
    )


# --------------------------------------------------------------- funding primitives
def ledger_of(service: Any) -> AccountingService:
    """The :class:`AccountingService` behind an exchange or accounting service handle."""
    if isinstance(service, ExchangeService):
        return service.accounting
    return service  # type: ignore[no-any-return]


async def fund_drawer(
    service: Any,
    *,
    world: ExchangeWorld,
    currency_code: str,
    amount: Decimal | str,
    rate: Decimal | str = Decimal(1),
    actor: ActorContext | None = None,
) -> uuid.UUID:
    """Open a drawer with a real posting: the journal **and** the physical movement.

    ``ACCOUNTING_MODEL.md`` §6.1 is the only documented way to give a branch cash: an
    ``OPENING_BALANCE`` entry (``Dr Cash-<CUR>`` / ``Cr 6000``) plus a ``cash_movements`` row
    of type ``OPENING``. Doing it any other way would let a suite start from a state
    production cannot reach — and, because ``NEX01`` is the physical authority, a test that
    skipped the movement would fail for the wrong reason.
    """
    quantity = Decimal(str(amount))
    price = Decimal(str(rate))
    reference_id = uuid.uuid4()
    who = actor or world.actor()
    ledger = ledger_of(service)
    async with ledger.document_transaction() as session:
        entry = await ledger.post_cash_movement(
            movement_type="OPENING",
            reference_id=reference_id,
            branch_id=world.branch_id,
            cash_account_id=world.drawer(currency_code),
            counter_account_id=world.account("opening"),
            currency_id=world.money(currency_code).id,
            amount=quantity,
            exchange_rate=price,
            description=f"Opening {currency_code} drawer",
            actor=who,
            session=session,
        )
        await ledger.record_cash_movements(
            session,
            reference_type="OPENING_BALANCE",
            reference_id=reference_id,
            branch_id=world.branch_id,
            movements=[
                CashMovementSpec(
                    account_id=world.drawer(currency_code),
                    currency_id=world.money(currency_code).id,
                    movement_type="OPENING",
                    amount=quantity,
                    description=f"Opening {currency_code} drawer",
                )
            ],
            actor=who,
            journal_entry_id=entry.id,
        )
    return entry.id


# ------------------------------------------------------------------- service runs
def make_exchange_engine(database: str) -> tuple[Any, Database, ExchangeService]:
    """``(settings, database, exchange_service)`` on a fresh async engine."""
    settings = settings_for_database(database)
    handle = Database(settings)  # type: ignore[arg-type]
    service = build_exchange_service(database=handle, settings=settings)  # type: ignore[arg-type]
    return settings, handle, service


ExchangeScenario = Callable[[ExchangeService], Awaitable[Any]]


def run_exchange(database: str, scenario: ExchangeScenario) -> Any:
    """Run one exchange scenario on its own engine and event loop."""

    async def _main() -> Any:
        _, handle, service = make_exchange_engine(database)
        try:
            return await scenario(service)
        finally:
            await handle.dispose()

    return asyncio.run(_main())


def run_exchange_race(database: str, *scenarios: ExchangeScenario) -> list[Any]:
    """Run scenarios in parallel, each on its own engine sharing one event loop.

    Exceptions come back as results, so a race can be examined from both sides: who won, and
    *how* the loser was refused.
    """

    async def _main() -> list[Any]:
        handles: list[Database] = []
        services: list[ExchangeService] = []
        try:
            for _ in scenarios:
                _, handle, service = make_exchange_engine(database)
                handles.append(handle)
                services.append(service)
            return list(
                await asyncio.gather(
                    *(
                        scenario(service)
                        for scenario, service in zip(scenarios, services, strict=True)
                    ),
                    return_exceptions=True,
                )
            )
        finally:
            for handle in handles:
                await handle.dispose()

    return asyncio.run(_main())


# ------------------------------------------------------------------------ scaffolding
@dataclass(slots=True)
class ExchangeWorld:
    """One scenario's own counter: a branch, its drawers, the chart accounts and an actor.

    The branch is **created by the test** (through ``POST /branches``) and its drawers are
    created **branch-bound**, so no assertion in this suite depends on what another test left
    in the seeded ``MAIN`` branch — and the resolution path under test is the production one
    (a branch's own account beats the group chart, §10).
    """

    database: str
    branch_id: uuid.UUID
    branch_code: str
    branch_timezone: str
    head_user_id: uuid.UUID
    currencies: dict[str, Money]
    drawers: dict[str, uuid.UUID]
    accounts: dict[str, uuid.UUID]
    ids: dict[str, uuid.UUID] = field(default_factory=dict)
    device_id: uuid.UUID | None = None
    seeded: bool = False
    # The session HTTP-level tests use: a device of **this** branch, logged in for the admin.
    headers: dict[str, str] = field(default_factory=dict)
    _funded: dict[str, Decimal] = field(default_factory=dict)

    # ----------------------------------------------------------------- identity
    @property
    def base(self) -> Money:
        return self.currencies[BASE_CODE]

    def money(self, code: str) -> Money:
        """A currency by code, read from the catalogue when the world did not load it.

        A scenario that adds a currency mid-test (the "no drawer can hold this" case) must
        still be able to name it; the read is against the same catalogue the service uses.
        """
        if code not in self.currencies:
            self.currencies.update(_load_currencies(self.database, (code,)))
        return self.currencies[code]

    def drawer(self, code: str) -> uuid.UUID:
        return self.drawers[code]

    def account(self, name: str) -> uuid.UUID:
        return self.accounts[name]

    def actor(
        self,
        *,
        roles: Sequence[str] = (RoleName.SUPER_ADMIN,),
        branch_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        permissions: Iterable[str] | None = None,
        user_id: uuid.UUID | None = None,
    ) -> ActorContext:
        """An actor with the effective authority of ``roles`` (the branch by default)."""
        return actor_for(
            user_id or self.head_user_id,
            branch_id=self.branch_id if branch_id is None else branch_id,
            roles=roles,
            permissions=permissions,
            device_id=device_id,
        )

    # ---------------------------------------------------------------- positions
    def cash(self, currency_code: str) -> Decimal:
        """The branch's physical position (``v_cash_position``)."""
        return branch_cash(self.database, branch_id=self.branch_id, currency_code=currency_code)

    def ledger(self, currency_code: str) -> Decimal:
        """What the ledger holds in this branch's own drawer for the currency."""
        return ledger_position(
            self.database, branch_id=self.branch_id, account_ids=[self.drawer(currency_code)]
        )

    def document(self, transaction_id: uuid.UUID) -> dict[str, Any]:
        return document_row(self.database, transaction_id)

    def documents(self) -> list[dict[str, Any]]:
        return documents_for_branch(self.database, self.branch_id)

    def movements(self, *, reference_type: str, reference_id: uuid.UUID) -> list[dict[str, Any]]:
        return movements_of(self.database, reference_type=reference_type, reference_id=reference_id)

    def audit(self, entity_id: uuid.UUID) -> list[dict[str, Any]]:
        return audit_rows_for_entity(self.database, entity_id)

    def entry(self, journal_entry_id: uuid.UUID) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return (
            entry_row(self.database, journal_entry_id),
            entry_lines(self.database, journal_entry_id),
        )

    def idempotency(self, key: uuid.UUID) -> dict[str, Any]:
        return idempotency_row(self.database, key)

    def state(self) -> ExchangeState:
        return exchange_state(self.database, branch_id=self.branch_id, drawers=self.drawers)

    # ------------------------------------------------------------------ funding
    def fund(self, *specs: tuple[str, str, str]) -> None:
        """Fund drawers with real opening postings: ``(currency_code, amount, rate)``."""

        async def _fund(service: AccountingService) -> None:
            for code, amount, rate in specs:
                await fund_drawer(
                    service,
                    world=self,
                    currency_code=code,
                    amount=Decimal(amount),
                    rate=Decimal(rate),
                    actor=self.actor(),
                )

        run_exchange(self.database, _fund)
        for code, amount, _rate in specs:
            self._funded[code] = self._funded.get(code, Decimal(0)) + Decimal(amount)

    def fund_all(self) -> None:
        """A comfortably stocked counter: AFN for payouts, USD and EUR for deliveries."""
        self.fund(
            ("AFN", "5000000", "1"),
            ("USD", "20000", str(USD_BUY_RATE)),
            ("EUR", "20000", "75"),
        )

    # ------------------------------------------------------------------- quoting
    def quote(
        self,
        client: TestClient,
        headers: Mapping[str, str],
        *,
        from_code: str = "USD",
        to_code: str = BASE_CODE,
        buy_rate: str = str(USD_BUY_RATE),
        sell_rate: str = str(USD_SELL_RATE),
        effective_at: dt.datetime | None = None,
    ) -> dict[str, Any]:
        """Publish a quote **for this branch**, so no other test's global quote can win."""
        return publish_quote(
            client,
            headers,
            from_currency_id=self.money(from_code).id,
            to_currency_id=self.money(to_code).id,
            buy_rate=buy_rate,
            sell_rate=sell_rate,
            branch_id=self.branch_id,
            effective_at=effective_at,
        )

    # ---------------------------------------------------------------------- calls
    def request(
        self,
        *,
        transaction_type: str = "BUY",
        from_code: str = "USD",
        to_code: str = BASE_CODE,
        from_amount: str = "1000",
        exchange_rate: str | None = None,
        commission: str | None = None,
        to_amount: str | None = None,
        customer_id: uuid.UUID | None = None,
        device_id: uuid.UUID | None = None,
        client_event_id: uuid.UUID | None = None,
        transaction_date: dt.datetime | None = None,
        description: str | None = None,
        branch_id: uuid.UUID | None = None,
    ) -> ExchangeRequest:
        """An :class:`ExchangeRequest` with currency ids resolved from their codes.

        The world's own device is the default: a counter that was provisioned a device records
        its deals from it, and a scenario that wants to exercise the device rules passes one
        explicitly (or ``uuid.uuid4()`` for one that does not exist).
        """
        rate = (
            Decimal(exchange_rate)
            if exchange_rate is not None
            else (USD_BUY_RATE if transaction_type == "BUY" else USD_SELL_RATE)
        )
        return ExchangeRequest(
            transaction_type=transaction_type,
            branch_id=branch_id or self.branch_id,
            from_currency_id=self.money(from_code).id,
            to_currency_id=self.money(to_code).id,
            from_amount=Decimal(from_amount),
            exchange_rate=rate,
            commission=Decimal(commission) if commission is not None else Decimal(0),
            to_amount=Decimal(to_amount) if to_amount is not None else None,
            customer_id=customer_id,
            device_id=device_id or self.device_id,
            client_event_id=client_event_id,
            transaction_date=transaction_date,
            description=description,
        )

    def create(
        self,
        *,
        actor: ActorContext | None = None,
        idempotency_key: uuid.UUID | None = None,
        **fields: Any,
    ) -> ExchangeResult:
        """Record a deal on its own engine and event loop (raising on refusal)."""
        return run_exchange(
            self.database,
            self.create_scenario(actor=actor, idempotency_key=idempotency_key, **fields),
        )

    def try_create(
        self,
        *,
        actor: ActorContext | None = None,
        idempotency_key: uuid.UUID | None = None,
        **fields: Any,
    ) -> ExchangeResult | BaseException:
        """Record a deal, returning the refusal instead of raising it."""
        return self._attempt(
            self.create_scenario(actor=actor, idempotency_key=idempotency_key, **fields)
        )

    def create_scenario(
        self,
        *,
        actor: ActorContext | None = None,
        idempotency_key: uuid.UUID | None = None,
        **fields: Any,
    ) -> ExchangeScenario:
        """The create call as a scenario, so it can be raced against another one."""
        request = self.request(**fields)
        if not isinstance(request, ExchangeRequest):  # pragma: no cover - typing guard
            raise AssertionError("request() must build an ExchangeRequest")
        resolved_actor = actor or self.actor()
        key = idempotency_key or uuid.uuid4()

        async def _create(service: ExchangeService) -> ExchangeResult:
            return await service.create_exchange(
                actor=resolved_actor, request=request, idempotency_key=key
            )

        return _create

    def race(self, *scenarios: ExchangeScenario) -> list[Any]:
        """Run scenarios together on the real database (PART 48, §11/§15).

        Each gets its own engine and connection on one event loop, so the contention is
        resolved by PostgreSQL — the only place a race can be resolved for real.
        """
        return list(run_exchange_race(self.database, *scenarios))

    def cancel(self, transaction_id: uuid.UUID, **kw: Any) -> ExchangeResult:
        """Undo a completed deal: the ledger's mirror entry and the money back in the drawer."""
        return run_exchange(self.database, self.cancel_scenario(transaction_id, **kw))

    def try_cancel(self, transaction_id: uuid.UUID, **kw: Any) -> ExchangeResult | BaseException:
        """``cancel_exchange`` with its refusal returned instead of raised."""
        return self._attempt(self.cancel_scenario(transaction_id, **kw))

    def reverse(self, transaction_id: uuid.UUID, **kw: Any) -> ExchangeResult:
        """Undo a completed deal **with a mirror document** (``NEX04``)."""
        return run_exchange(self.database, self.reverse_scenario(transaction_id, **kw))

    def try_reverse(self, transaction_id: uuid.UUID, **kw: Any) -> ExchangeResult | BaseException:
        """``reverse_exchange`` with its refusal returned instead of raised."""
        return self._attempt(self.reverse_scenario(transaction_id, **kw))

    def cancel_scenario(
        self,
        transaction_id: uuid.UUID,
        *,
        reason: str = "operator correction",
        actor: ActorContext | None = None,
        idempotency_key: uuid.UUID | None = None,
    ) -> ExchangeScenario:
        """The cancel call as a scenario, so it can be raced against another undo."""
        resolved_actor = actor or self.actor()
        key = idempotency_key or uuid.uuid4()

        async def _cancel(service: ExchangeService) -> ExchangeResult:
            return await service.cancel_exchange(
                actor=resolved_actor,
                transaction_id=transaction_id,
                reason=reason,
                idempotency_key=key,
            )

        return _cancel

    def reverse_scenario(
        self,
        transaction_id: uuid.UUID,
        *,
        reason: str = "customer returned the money",
        actor: ActorContext | None = None,
        idempotency_key: uuid.UUID | None = None,
    ) -> ExchangeScenario:
        """The reverse call as a scenario, so it can be raced against another undo."""
        resolved_actor = actor or self.actor()
        key = idempotency_key or uuid.uuid4()

        async def _reverse(service: ExchangeService) -> ExchangeResult:
            return await service.reverse_exchange(
                actor=resolved_actor,
                transaction_id=transaction_id,
                reason=reason,
                idempotency_key=key,
            )

        return _reverse

    def _attempt(self, scenario: ExchangeScenario) -> Any:
        """Run a lifecycle scenario and hand back the domain refusal instead of raising it.

        An expected refusal comes back as its exception object (what the API layer would turn
        into an error envelope); anything else is a genuine bug and keeps its traceback.
        """
        try:
            return run_exchange(self.database, scenario)
        except NexusError as refusal:
            return refusal

    def try_view(self, transaction_id: uuid.UUID, *, actor: ActorContext | None = None) -> Any:
        """``get_exchange`` with its refusal returned instead of raised."""
        resolved = actor or self.actor()

        async def _view(service: ExchangeService) -> Any:
            return await service.get_exchange(actor=resolved, transaction_id=transaction_id)

        return self._attempt(_view)

    def list(
        self,
        *,
        status: str | None = None,
        transaction_type: str | None = None,
        customer_id: uuid.UUID | None = None,
        cashier_id: uuid.UUID | None = None,
        number_query: str | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
        branch_id: uuid.UUID | None = None,
        actor: ActorContext | None = None,
    ) -> tuple[list[Any], int]:
        """The branch's book through the service, with the same filters as the endpoint."""

        async def _list(service: ExchangeService) -> tuple[list[Any], int]:
            return await service.list_exchanges(
                actor=actor or self.actor(),
                branch_id=branch_id if branch_id is not None else self.branch_id,
                cashier_id=cashier_id,
                customer_id=customer_id,
                transaction_type=transaction_type,
                status=status,
                number_query=number_query,
                from_=from_,
                to=to,
                limit=limit,
                offset=offset,
            )

        views, total = run_exchange(self.database, _list)
        return list(views), int(total)

    def receipt(self, transaction_id: uuid.UUID) -> dict[str, Any]:
        async def _receipt(service: ExchangeService) -> dict[str, Any]:
            return await service.build_receipt(actor=self.actor(), transaction_id=transaction_id)

        return run_exchange(self.database, _receipt)

    def view(self, transaction_id: uuid.UUID) -> Any:
        async def _view(service: ExchangeService) -> Any:
            return await service.get_exchange(actor=self.actor(), transaction_id=transaction_id)

        return run_exchange(self.database, _view)


def build_world(
    client: TestClient,
    headers: Mapping[str, str],
    database: str,
    *,
    currencies: Sequence[str] = (BASE_CODE, *FOREIGN_CODES),
    drawers: bool = True,
    seeded_drawers: bool = False,
    timezone: str | None = None,
    address: str | None = None,
    phone: str | None = None,
    branch: str | None = None,
    device_id: uuid.UUID | None = None,
) -> ExchangeWorld:
    """Create a branch with its own drawers and resolve the chart accounts it posts to.

    The branch is created through the public API (``POST /branches``) — the same door an
    operator uses — and each drawer is a **branch-bound** ``ASSET`` account for its currency,
    which is the multi-branch rule of ``ACCOUNTING_MODEL.md`` §10 made concrete: this branch
    can never spend another branch's cash.

    ``branch`` reuses a seeded branch (``"MAIN"``) instead of creating one. That is what the
    HTTP-level tests need: a session is bound to the device it logged in from, and a *new*
    branch has no signed-in device, so a request that carried one would be refused for the
    right reason (a device belongs to one counter) and the test would prove nothing about the
    endpoint.
    """
    from tests.accounting_helpers import create_account, create_branch, unique_code

    if branch is not None:
        created = read_one(
            database,
            "SELECT id, code, timezone FROM branches WHERE code = :code",
            code=branch,
        )
        seeded = True
    else:
        seeded = False
        if timezone is None:
            created = create_branch(client, headers)
        else:
            created = create_branch_with_timezone(
                client, headers, timezone=timezone, address=address, phone=phone
            )
    branch_id = uuid.UUID(str(created["id"]))
    rows = _load_currencies(database, currencies)
    drawer_ids: dict[str, uuid.UUID] = {}
    if seeded_drawers:
        # The chart's own cash accounts (``1000`` + the band): one per currency, group-wide,
        # which is what a seeded branch like ``MAIN`` actually trades out of.
        for code in currencies:
            drawer_ids[code] = account_id_of(database, DRAWER_CODES[code])
    elif drawers:
        for code in currencies:
            account = create_account(
                client,
                headers,
                code=unique_code(),
                name=f"Cash {code} {unique_code()}",
                account_type="ASSET",
                currency_id=str(rows[code].id),
                branch_id=str(branch_id),
            )
            drawer_ids[code] = uuid.UUID(str(account["id"]))
    return ExchangeWorld(
        seeded=seeded,
        device_id=device_id,
        database=database,
        branch_id=branch_id,
        branch_code=str(created["code"]),
        branch_timezone=str(created.get("timezone") or "UTC"),
        head_user_id=_identifier(database, "SELECT id FROM users WHERE username = 'admin'"),
        currencies=rows,
        drawers=drawer_ids,
        accounts={
            "commission": account_id_of(database, COMMISSION_ACCOUNT_CODE),
            "fx": account_id_of(database, FX_RESULT_ACCOUNT_CODE),
            "opening": account_id_of(database, OPENING_OFFSET_CODE),
        },
    )


def attach_session(
    world: ExchangeWorld,
    client: TestClient,
    admin_headers: Mapping[str, str],
    *,
    username: str = ADMIN_USERNAME,
    password: str = DEV_ADMIN_PASSWORD,
) -> dict[str, str]:
    """Give a world a session that belongs to its **own** branch.

    HTTP-level tests need this and nothing less. A session is bound to the device it logged in
    from and a device belongs to exactly one branch (§16), so a test that posted a deal on a
    freshly created branch while holding the seeded administrator's ``MAIN`` session would be
    refused for the right reason — a device belongs to one counter — and would prove nothing
    about the endpoint. Provisioning a device for the world's own branch keeps the endpoint
    reachable *and* keeps the request honest: the document is attributed to a device that
    really stands at this counter.

    The session belongs to the development administrator (a group-wide actor), so it is
    authority-neutral: what a test is exercising is the endpoint, not a role.
    """
    device = register_device(client, admin_headers, branch_id=str(world.branch_id))
    tokens = login(
        client,
        username,
        password,
        device_uuid=uuid.UUID(str(device["device_uuid"])),
        branch_id=world.branch_id,
    ).json()
    world.device_id = uuid.UUID(str(tokens["device"]["id"]))
    world.headers = bearer(str(tokens["access_token"]), str(world.device_id))
    return dict(world.headers)


def retire(client: TestClient, headers: Mapping[str, str], world: ExchangeWorld) -> None:
    """Deactivate a scenario's branch (branches are retired, never deleted, PART 22).

    Why this matters beyond tidiness: a login resolves a device's branch automatically only
    while exactly one branch is active, so a test that left a second one behind would change
    how every later login in the session behaves.
    """
    if world.seeded:  # a seeded branch is not this scenario's to retire
        return
    response = client.patch(
        f"/api/v1/branches/{world.branch_id}", headers=dict(headers), json={"is_active": False}
    )
    assert response.status_code == 200, response.text


# --------------------------------------------------------------------------- quoting
def publish_quote(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    from_currency_id: uuid.UUID | str,
    to_currency_id: uuid.UUID | str,
    buy_rate: str,
    sell_rate: str,
    branch_id: uuid.UUID | str | None = None,
    effective_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """One quote through ``POST /rates`` (append-only: nothing is ever replaced)."""
    body: dict[str, Any] = {
        "from_currency_id": str(from_currency_id),
        "to_currency_id": str(to_currency_id),
        "buy_rate": buy_rate,
        "sell_rate": sell_rate,
        "source": "MANUAL",
    }
    if branch_id is not None:
        body["branch_id"] = str(branch_id)
    if effective_at is not None:
        body["effective_at"] = effective_at.isoformat()
    response = client.post(RATES, headers=dict(headers), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_customer(
    client: TestClient, headers: Mapping[str, str], *, full_name: str, branch_id: str | None = None
) -> dict[str, Any]:
    body: dict[str, Any] = {"full_name": full_name}
    if branch_id is not None:
        body["branch_id"] = branch_id
    response = client.post(CUSTOMERS, headers=dict(headers), json=body)
    assert response.status_code == 201, response.text
    return response.json()


# ------------------------------------------------------------------- HTTP helpers
def exchange_headers(headers: Mapping[str, str], key: uuid.UUID | None = None) -> dict[str, str]:
    """Authorization plus the ``Idempotency-Key`` a money-moving call must carry."""
    return {**dict(headers), "Idempotency-Key": str(key or uuid.uuid4())}


def post_exchange(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    transaction_type: str,
    branch_id: uuid.UUID | str,
    from_currency_id: uuid.UUID | str,
    from_amount: str,
    to_currency_id: uuid.UUID | str,
    exchange_rate: str,
    commission: str | None = None,
    to_amount: str | None = None,
    customer_id: uuid.UUID | str | None = None,
    device_id: uuid.UUID | str | None = None,
    client_event_id: uuid.UUID | str | None = None,
    idempotency_key: uuid.UUID | None = None,
    expect: int | None = 201,
) -> Any:
    """``POST /exchange`` with the contract's body, overridable per field."""
    body: dict[str, Any] = {
        "transaction_type": transaction_type,
        "branch_id": str(branch_id),
        "from_currency_id": str(from_currency_id),
        "from_amount": from_amount,
        "to_currency_id": str(to_currency_id),
        "exchange_rate": exchange_rate,
    }
    if commission is not None:
        body["commission"] = commission
    if to_amount is not None:
        body["to_amount"] = to_amount
    if customer_id is not None:
        body["customer_id"] = str(customer_id)
    if device_id is not None:
        body["device_id"] = str(device_id)
    if client_event_id is not None:
        body["client_event_id"] = str(client_event_id)
    response = client.post(EXCHANGE, headers=exchange_headers(headers, idempotency_key), json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def document_counter(database: str, *, period: str | None = None) -> int:
    """The value of the exchange number counter in ``sequences`` — one row per period.

    Numbers are issued by ``next_document_number`` inside the *posting* transaction, so the
    counter is the witness for "a refused deal consumed no number": if the transaction rolled
    back, the increment rolled back with it. ``period`` selects one day's counter (the row is
    ``exchange_transaction:YYYYMMDD``); without it the highest counter is reported.
    """
    if period is None:
        value = scalar(
            database,
            "SELECT COALESCE(MAX(current_value), 0) FROM sequences "
            "WHERE name LIKE 'exchange_transaction:%'",
        )
    else:
        value = scalar(
            database,
            "SELECT current_value FROM sequences WHERE name = :name",
            name=f"exchange_transaction:{period}",
        )
    return int(value or 0)


def period_of(transaction_number: str) -> str:
    """The period part of ``NX-YYYYMMDD-NNNNNN``."""
    return transaction_number.split("-")[1]


def number_suffix(transaction_number: str) -> int:
    """The numeric part of ``NX-YYYYMMDD-NNNNNN``."""
    return int(transaction_number.rsplit("-", 1)[-1])


def db_refusal(database: str, sql: str, **params: object) -> BaseException:
    """Run a statement the database must refuse and return the driver's exception.

    The ``with`` block ends in a COMMIT, and that is where the *deferred* constraints fire
    (``NEX01`` on cash movements, ``ct_exchange_reversal_bound`` on reversals), so the whole
    block is under test — not just the statement.
    """
    engine = create_engine(database_dsn(database), future=True)
    try:
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except Exception as error:
            return error
    finally:
        engine.dispose()
    raise AssertionError(f"the database accepted a statement it should refuse:\n{sql}")


def create_branch_with_timezone(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    timezone: str,
    address: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """A branch in a chosen timezone, through ``POST /branches``.

    The timezone is the difference between "the day it is in Kabul" and "the day it is in
    UTC", and document numbers roll over on the branch's day (§18) — so a suite that wants to
    prove the rule has to be able to pick the zone.
    """
    code = f"T{uuid.uuid4().hex[:8].upper()}"
    body: dict[str, Any] = {"code": code, "name": f"Branch {code}", "timezone": timezone}
    if address is not None:
        body["address"] = address
    if phone is not None:
        body["phone"] = phone
    response = client.post(BRANCHES, headers=dict(headers), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def error_details(response: Any) -> Mapping[str, Any]:
    return response.json()["error"]["details"]


__all__ = [
    "BASE_CODE",
    "BRANCHES",
    "COMMISSION_ACCOUNT_CODE",
    "DRAWER_CODES",
    "EXCHANGE",
    "FOREIGN_CODES",
    "FX_RESULT_ACCOUNT_CODE",
    "OPENING_OFFSET_CODE",
    "RATES",
    "REFERENCE_TYPE_EXCHANGE",
    "REFERENCE_TYPE_REVERSAL",
    "USD_BUY_RATE",
    "USD_SELL_RATE",
    "ExchangeResult",
    "ExchangeState",
    "ExchangeWorld",
    "account_id_of",
    "attach_session",
    "audit_actions",
    "audit_rows_for_action",
    "audit_rows_for_entity",
    "branch_cash",
    "branch_id_of",
    "build_world",
    "create_branch_with_timezone",
    "create_customer",
    "currency_id_of",
    "db_refusal",
    "document_counter",
    "document_row",
    "documents_for_branch",
    "entry_lines",
    "entry_row",
    "error_code",
    "error_details",
    "exchange_headers",
    "exchange_state",
    "fund_drawer",
    "idempotency_row",
    "ledger_position",
    "make_exchange_engine",
    "movements_of",
    "number_suffix",
    "period_of",
    "post_exchange",
    "publish_quote",
    "retire",
    "run_exchange",
    "run_exchange_race",
]
