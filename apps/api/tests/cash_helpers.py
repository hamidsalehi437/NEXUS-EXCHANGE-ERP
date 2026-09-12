"""Cash-control test scaffolding (Phase 6).

Two harnesses in one module, because the cash module is tested at both doors:

* **HTTP** — ``post_open`` / ``post_in`` / ``post_out`` / ``post_adjustment`` / ``post_close`` /
  ``post_reverse`` and the read endpoints drive ``/api/v1/cash`` through the real session,
  device and permission dependencies. These are the calls that prove the contract.
* **Service** — ``run_cash`` / ``run_cash_race`` build a :class:`CashService` on its own async
  engine, which is how a *race* can be run: two scenarios on two engines sharing one event
  loop, meeting inside PostgreSQL rather than inside the loop.

Every scenario works on its **own branch** (``build_world`` creates one through
``POST /branches``, with branch-bound drawers), so no assertion depends on what another test
left behind and the multi-branch rules have something real to refuse.

Readers are deliberately written against the tables and views the accounting model names —
``cash_sessions``, ``cash_movements``, ``v_cash_position``, ``journal_lines`` — so a test that
passes on a cached total would not compile here in the first place.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.database import Database
from app.services.audit_service import ActorContext
from app.services.cash_service import (
    CashCount,
    CashMovementRequest,
    CashOpening,
    CashResult,
    CashService,
    CloseSessionRequest,
    OpenSessionRequest,
    build_cash_service,
)
from tests.exchange_helpers import (
    ExchangeWorld,
    attach_session,
    build_world,
    publish_quote,
)
from tests.exchange_helpers import (
    fund_drawer as _fund_drawer,
)
from tests.helpers import database_dsn, settings_for_database

CASH = "/api/v1/cash"
OPEN = f"{CASH}/open"
CASH_IN = f"{CASH}/in"
CASH_OUT = f"{CASH}/out"
ADJUSTMENT = f"{CASH}/adjustment"
CLOSE = f"{CASH}/close"
BALANCE = f"{CASH}/balance"
MOVEMENTS = f"{CASH}/movements"
SESSIONS = f"{CASH}/sessions"
CURRENT = f"{CASH}/sessions/current"

# The chart account the accounting model names for a shift's short/over (§6.4).
SHORT_OVER_CODE = "5090"
OPENING_OFFSET_CODE = "6000"

Scenario = Callable[[CashService], Awaitable[Any]]


# --------------------------------------------------------------------------- headers
def cash_headers(headers: Mapping[str, str], key: uuid.UUID | None = None) -> dict[str, str]:
    """Authorization plus an ``Idempotency-Key`` (a fresh one when the test does not care)."""
    return {**dict(headers), "Idempotency-Key": str(key or uuid.uuid4())}


# ------------------------------------------------------------------------ HTTP doors
def post_open(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str,
    openings: Sequence[Mapping[str, Any]] = (),
    device_id: uuid.UUID | str | None = None,
    notes: str | None = None,
    idempotency_key: uuid.UUID | None = None,
    with_key: bool = True,
    expect: int | None = 201,
) -> Any:
    """``POST /cash/open`` with the contract's body, overridable per field."""
    body: dict[str, Any] = {
        "branch_id": str(branch_id),
        "openings": [dict(item) for item in openings],
    }
    if device_id is not None:
        body["device_id"] = str(device_id)
    if notes is not None:
        body["notes"] = notes
    sent = cash_headers(headers, idempotency_key) if with_key else dict(headers)
    response = client.post(OPEN, headers=sent, json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def post_in(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str,
    currency_id: uuid.UUID | str,
    amount: str,
    source_account_id: uuid.UUID | str,
    session_id: uuid.UUID | str | None = None,
    device_id: uuid.UUID | str | None = None,
    client_event_id: uuid.UUID | str | None = None,
    transaction_date: str | None = None,
    description: str | None = None,
    idempotency_key: uuid.UUID | None = None,
    expect: int | None = 201,
) -> Any:
    """``POST /cash/in`` (``source_account_id`` is what §6.4 requires)."""
    body: dict[str, Any] = {
        "branch_id": str(branch_id),
        "currency_id": str(currency_id),
        "amount": amount,
        "source_account_id": str(source_account_id),
    }
    return _post_movement(
        client,
        CASH_IN,
        headers,
        body,
        session_id,
        device_id,
        client_event_id,
        idempotency_key,
        expect,
        transaction_date,
        description,
    )


def post_out(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str,
    currency_id: uuid.UUID | str,
    amount: str,
    target_account_id: uuid.UUID | str,
    session_id: uuid.UUID | str | None = None,
    device_id: uuid.UUID | str | None = None,
    client_event_id: uuid.UUID | str | None = None,
    transaction_date: str | None = None,
    idempotency_key: uuid.UUID | None = None,
    expect: int | None = 201,
) -> Any:
    """``POST /cash/out`` (``target_account_id`` is what §6.4 requires)."""
    body: dict[str, Any] = {
        "branch_id": str(branch_id),
        "currency_id": str(currency_id),
        "amount": amount,
        "target_account_id": str(target_account_id),
    }
    return _post_movement(
        client,
        CASH_OUT,
        headers,
        body,
        session_id,
        device_id,
        client_event_id,
        idempotency_key,
        expect,
        transaction_date,
    )


def post_adjustment(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str,
    currency_id: uuid.UUID | str,
    amount: str,
    adjustment_sign: int,
    reason: str,
    session_id: uuid.UUID | str | None = None,
    device_id: uuid.UUID | str | None = None,
    idempotency_key: uuid.UUID | None = None,
    expect: int | None = 201,
) -> Any:
    """``POST /cash/adjustment`` (direction and reason are mandatory)."""
    body: dict[str, Any] = {
        "branch_id": str(branch_id),
        "currency_id": str(currency_id),
        "amount": amount,
        "adjustment_sign": adjustment_sign,
        "reason": reason,
    }
    return _post_movement(
        client, ADJUSTMENT, headers, body, session_id, device_id, None, idempotency_key, expect
    )


def _post_movement(
    client: TestClient,
    path: str,
    headers: Mapping[str, str],
    body: dict[str, Any],
    session_id: uuid.UUID | str | None,
    device_id: uuid.UUID | str | None,
    client_event_id: uuid.UUID | str | None,
    idempotency_key: uuid.UUID | None,
    expect: int | None,
    transaction_date: str | None = None,
    description: str | None = None,
) -> Any:
    if session_id is not None:
        body["session_id"] = str(session_id)
    if device_id is not None:
        body["device_id"] = str(device_id)
    if client_event_id is not None:
        body["client_event_id"] = str(client_event_id)
    if transaction_date is not None:
        body["transaction_date"] = transaction_date
    if description is not None:
        body["description"] = description
    response = client.post(path, headers=cash_headers(headers, idempotency_key), json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def post_close(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    session_id: uuid.UUID | str,
    counted: Sequence[Mapping[str, Any]],
    notes: str | None = None,
    idempotency_key: uuid.UUID | None = None,
    expect: int | None = 200,
) -> Any:
    """``POST /cash/close`` with the counts the operator actually made."""
    body: dict[str, Any] = {
        "session_id": str(session_id),
        "counted": [dict(item) for item in counted],
    }
    if notes is not None:
        body["notes"] = notes
    response = client.post(CLOSE, headers=cash_headers(headers, idempotency_key), json=body)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def post_reverse(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    movement_id: uuid.UUID | str,
    reason: str,
    idempotency_key: uuid.UUID | None = None,
    with_key: bool = False,
    expect: int | None = 200,
) -> Any:
    """``POST /cash/movements/{id}/reverse`` (a key is optional here, and honoured when sent)."""
    sent = cash_headers(headers, idempotency_key) if with_key else dict(headers)
    response = client.post(
        f"{MOVEMENTS}/{movement_id}/reverse", headers=sent, json={"reason": reason}
    )
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_balance(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str | None = None,
    expect: int | None = 200,
) -> Any:
    params = {"branch_id": str(branch_id)} if branch_id is not None else {}
    response = client.get(BALANCE, headers=dict(headers), params=params)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_movements(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str | None = None,
    currency_id: uuid.UUID | str | None = None,
    movement_type: str | None = None,
    session_id: uuid.UUID | str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    expect: int | None = 200,
) -> Any:
    params: dict[str, Any] = {}
    if branch_id is not None:
        params["branch_id"] = str(branch_id)
    if currency_id is not None:
        params["currency_id"] = str(currency_id)
    if movement_type is not None:
        params["movement_type"] = movement_type
    if session_id is not None:
        params["session_id"] = str(session_id)
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    response = client.get(MOVEMENTS, headers=dict(headers), params=params)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_movement(
    client: TestClient,
    headers: Mapping[str, str],
    movement_id: uuid.UUID | str,
    *,
    expect: int | None = 200,
) -> Any:
    response = client.get(f"{MOVEMENTS}/{movement_id}", headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_sessions(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str | None = None,
    status: str | None = None,
    limit: int | None = None,
    offset: int | None = None,
    expect: int | None = 200,
) -> Any:
    params: dict[str, Any] = {}
    if branch_id is not None:
        params["branch_id"] = str(branch_id)
    if status is not None:
        params["status"] = status
    if limit is not None:
        params["limit"] = limit
    if offset is not None:
        params["offset"] = offset
    response = client.get(SESSIONS, headers=dict(headers), params=params)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_session(
    client: TestClient,
    headers: Mapping[str, str],
    session_id: uuid.UUID | str,
    *,
    expect: int | None = 200,
) -> Any:
    response = client.get(f"{SESSIONS}/{session_id}", headers=dict(headers))
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def get_current(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | str | None = None,
    device_id: uuid.UUID | str | None = None,
    expect: int | None = 200,
) -> Any:
    params: dict[str, Any] = {}
    if branch_id is not None:
        params["branch_id"] = str(branch_id)
    if device_id is not None:
        params["device_id"] = str(device_id)
    response = client.get(CURRENT, headers=dict(headers), params=params)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def counted(*items: tuple[Any, str]) -> list[dict[str, str]]:
    """``[(currency_id, "1000.00"), …]`` as the close body's ``counted`` array."""
    return [{"currency_id": str(currency_id), "amount": amount} for currency_id, amount in items]


# -------------------------------------------------------------------------- readers
def movement_rows(
    database: str,
    *,
    branch_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    currency_code: str | None = None,
    movement_type: str | None = None,
) -> list[dict[str, Any]]:
    """The branch's movements, filtered the way a report would filter them."""
    clauses = ["m.branch_id = :branch_id"]
    params: dict[str, Any] = {"branch_id": branch_id}
    if session_id is not None:
        clauses.append("m.cash_session_id = :session_id")
        params["session_id"] = session_id
    if currency_code is not None:
        clauses.append("c.code = :currency_code")
        params["currency_code"] = currency_code
    if movement_type is not None:
        clauses.append("m.movement_type = :movement_type")
        params["movement_type"] = movement_type
    return read(
        database,
        f"""
        SELECT m.id, m.branch_id, m.account_id, m.currency_id, c.code AS currency_code,
               m.movement_type, m.amount, m.signed_amount, m.adjustment_sign, m.reference_type,
               m.reference_id, m.cash_session_id, m.journal_entry_id, m.client_event_id,
               m.created_by, m.created_at, a.code AS account_code
          FROM cash_movements m
          JOIN currencies c ON c.id = m.currency_id
          JOIN accounts a ON a.id = m.account_id
         WHERE {" AND ".join(clauses)}
         ORDER BY m.created_at, m.id
        """,  # noqa: S608 - the clauses are constants chosen above, never caller input
        **params,
    )


def movement_row(database: str, movement_id: uuid.UUID | str) -> dict[str, Any]:
    return read_one(
        database,
        """
        SELECT m.*, c.code AS currency_code
          FROM cash_movements m
          JOIN currencies c ON c.id = m.currency_id
         WHERE m.id = :movement_id
        """,
        movement_id=movement_id,
    )


def movements_for_reference(
    database: str, *, reference_type: str, reference_id: uuid.UUID
) -> list[dict[str, Any]]:
    """Every movement written for one document reference (a movement, a deal, a shift)."""
    return read(
        database,
        """
        SELECT m.*, c.code AS currency_code
          FROM cash_movements m
          JOIN currencies c ON c.id = m.currency_id
         WHERE m.reference_type = :reference_type AND m.reference_id = :reference_id
         ORDER BY m.created_at, m.id
        """,
        reference_type=reference_type,
        reference_id=reference_id,
    )


def session_row(database: str, session_id: uuid.UUID | str) -> dict[str, Any]:
    return read_one(
        database,
        """
        SELECT s.*, b.code AS branch_code
          FROM cash_sessions s
          JOIN branches b ON b.id = s.branch_id
         WHERE s.id = :session_id
        """,
        session_id=session_id,
    )


def session_lines(database: str, session_id: uuid.UUID | str) -> list[dict[str, Any]]:
    return read(
        database,
        """
        SELECT l.*, c.code AS currency_code
          FROM cash_session_lines l
          JOIN currencies c ON c.id = l.currency_id
         WHERE l.cash_session_id = :session_id
         ORDER BY c.code
        """,
        session_id=session_id,
    )


def position(database: str, *, branch_id: uuid.UUID, currency_code: str) -> Decimal:
    """The branch's physical position in one currency (``v_cash_position``)."""
    row = read(
        database,
        """
        SELECT balance FROM v_cash_position
         WHERE branch_id = :branch_id AND currency_code = :currency_code
        """,
        branch_id=branch_id,
        currency_code=currency_code,
    )
    return Decimal(str(row[0]["balance"])) if row else Decimal(0)


def ledger_quantity(database: str, *, branch_id: uuid.UUID, account_id: uuid.UUID) -> Decimal:
    """What the journal holds in one drawer, in the drawer's own currency."""
    row = read(
        database,
        """
        SELECT COALESCE(
                   SUM(CASE WHEN l.debit > 0 THEN l.foreign_amount ELSE -l.foreign_amount END), 0
               ) AS quantity
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=branch_id,
        account_id=account_id,
    )
    return Decimal(str(row[0]["quantity"])) if row else Decimal(0)


def entry_with_lines(
    database: str, entry_id: uuid.UUID | str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """A posted entry and its lines (the immutable record of what the books say)."""
    entry = read_one(database, "SELECT * FROM journal_entries WHERE id = :id", id=entry_id)
    lines = read(
        database,
        """
        SELECT l.*, a.code AS account_code, c.code AS currency_code
          FROM journal_lines l
          JOIN accounts a ON a.id = l.account_id
          LEFT JOIN currencies c ON c.id = l.currency_id
         WHERE l.journal_entry_id = :id
         ORDER BY l.id
        """,
        id=entry_id,
    )
    return entry, lines


def entry_balance(lines: Sequence[Mapping[str, Any]]) -> tuple[Decimal, Decimal]:
    """``(Σdebit, Σcredit)`` of an entry's lines — equal for a posted entry (I-1)."""
    debit = sum((Decimal(str(line["debit"])) for line in lines), Decimal(0))
    credit = sum((Decimal(str(line["credit"])) for line in lines), Decimal(0))
    return debit, credit


def audit_rows(
    database: str,
    *,
    entity_id: uuid.UUID | None = None,
    action: str | None = None,
    entity_type: str | None = None,
) -> list[dict[str, Any]]:
    clauses = ["TRUE"]
    params: dict[str, Any] = {}
    if entity_id is not None:
        clauses.append("entity_id = :entity_id")
        params["entity_id"] = entity_id
    if action is not None:
        clauses.append("action = :action")
        params["action"] = action
    if entity_type is not None:
        clauses.append("entity_type = :entity_type")
        params["entity_type"] = entity_type
    return read(
        database,
        f"SELECT * FROM audit_logs WHERE {' AND '.join(clauses)} ORDER BY seq",  # noqa: S608
        **params,
    )


def idempotency_rows(database: str, key: uuid.UUID | str) -> list[dict[str, Any]]:
    """Every claim of one key — the store is scoped ``(user, endpoint, key)``, so a key
    reused on a second endpoint is a second row, not a replay."""
    return read(
        database,
        "SELECT * FROM idempotency_keys WHERE key = :key ORDER BY created_at, endpoint",
        key=key,
    )


def idempotency_row(
    database: str, key: uuid.UUID | str, *, endpoint: str | None = None
) -> dict[str, Any] | None:
    """The row one key holds, optionally for one endpoint (the scope's own dimension)."""
    rows = idempotency_rows(database, key)
    if endpoint is not None:
        rows = [row for row in rows if row["endpoint"] == endpoint]
    return rows[0] if rows else None


def branch_entries(
    database: str, *, branch_id: uuid.UUID, reference_type: str | None = None
) -> list[dict[str, Any]]:
    """The branch's posted entries (optionally of one document type), oldest first."""
    clause = "AND e.reference_type = :reference_type" if reference_type else ""
    params: dict[str, Any] = {"branch_id": branch_id}
    if reference_type:
        params["reference_type"] = reference_type
    return read(
        database,
        f"""
        SELECT e.* FROM journal_entries e
         WHERE e.branch_id = :branch_id {clause}
         ORDER BY e.created_at, e.id
        """,  # noqa: S608 - the clause is a constant chosen above
        **params,
    )


def trial_balance(
    database: str, *, branch_id: uuid.UUID, account_ids: Sequence[uuid.UUID]
) -> tuple[Decimal, Decimal]:
    """``(Σdebit, Σcredit)`` over the named accounts: the branch's own slice of the ledger."""
    rows = read(
        database,
        """
        SELECT COALESCE(SUM(l.debit), 0) AS debit, COALESCE(SUM(l.credit), 0) AS credit
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = ANY(:account_ids)
        """,
        branch_id=branch_id,
        account_ids=list(account_ids),
    )
    return Decimal(str(rows[0]["debit"])), Decimal(str(rows[0]["credit"]))


def read(database: str, sql: str, **params: object) -> list[dict[str, Any]]:
    engine = create_engine(database_dsn(database), future=True)
    try:
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(text(sql), params).mappings().all()]
    finally:
        engine.dispose()


def read_one(database: str, sql: str, **params: object) -> dict[str, Any]:
    rows = read(database, sql, **params)
    assert rows, f"query returned no rows:\n{sql}"
    return rows[0]


# --------------------------------------------------------------------- service runs
def make_cash_engine(database: str) -> tuple[Any, Database, CashService]:
    """``(settings, database handle, cash service)`` on a fresh async engine."""
    settings = settings_for_database(database)
    handle = Database(settings)  # type: ignore[arg-type]
    service = build_cash_service(database=handle, settings=settings)  # type: ignore[arg-type]
    return settings, handle, service


async def dispose(handle: Database) -> None:
    await handle.dispose()


def run_cash(database: str, scenario: Scenario) -> Any:
    """Run one cash scenario on its own engine and event loop."""

    async def _main() -> Any:
        _, handle, service = make_cash_engine(database)
        try:
            return await scenario(service)
        finally:
            await dispose(handle)

    return asyncio.run(_main())


def run_cash_race(database: str, *scenarios: Scenario) -> list[Any]:
    """Run cash scenarios in parallel — one engine each, meeting inside PostgreSQL.

    The scenarios cannot see each other through the event loop, so the only thing that can
    decide the race is the database: the session lock, the account lock, the deferred
    position constraint and the unique indexes. Exceptions are returned rather than raised,
    so the loser of a race does not hide the winner's result.
    """

    async def _main() -> list[Any]:
        handles: list[Database] = []
        services: list[CashService] = []
        try:
            for _ in scenarios:
                _, handle, service = make_cash_engine(database)
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
                await dispose(handle)

    return asyncio.run(_main())


# ------------------------------------------------------------- service-level doors
async def service_open(
    cash: CashService,
    world: ExchangeWorld,
    *,
    openings: Mapping[str, str] = {},
    actor: ActorContext | None = None,
    notes: str | None = None,
    key: uuid.UUID | None = None,
) -> CashResult:
    """Open a shift through the service (the same call the endpoint makes)."""
    return await cash.open_session(
        actor=actor or world.actor(),
        request=OpenSessionRequest(
            branch_id=world.branch_id,
            device_id=world.device_id,
            openings=[
                CashOpening(currency_id=world.money(code).id, amount=Decimal(amount))
                for code, amount in openings.items()
            ],
            notes=notes,
        ),
        idempotency_key=key,
    )


async def service_open_session_id(
    cash: CashService,
    world: ExchangeWorld,
    *,
    openings: Mapping[str, str] = {},
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
) -> uuid.UUID:
    """Open a shift and return its id (what most scenarios want)."""
    result = await service_open(cash, world, openings=openings, actor=actor, key=key)
    assert result.session_id is not None, "an opened session always reports its id"
    return result.session_id


async def service_confirmed_open_id(
    cash: CashService,
    world: ExchangeWorld,
    *,
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
) -> uuid.UUID:
    """Open a shift that the books already fund, so it posts no opening entry.

    A drawer the ledger already carries must be counted at exactly the carried amount; this
    helper funds nothing and declares nothing, which is the common production case for a
    branch whose cash was posted by an earlier shift.
    """
    return await service_open_session_id(cash, world, openings={}, actor=actor, key=key)


async def service_in(
    cash: CashService,
    world: ExchangeWorld,
    *,
    code: str,
    amount: str,
    counter_account_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
    client_event_id: uuid.UUID | None = None,
) -> CashResult:
    return await cash.record_in(
        actor=actor or world.actor(),
        request=CashMovementRequest(
            branch_id=world.branch_id,
            currency_id=world.money(code).id,
            amount=Decimal(amount),
            session_id=session_id,
            device_id=world.device_id,
            counter_account_id=counter_account_id or world.account("opening"),
            client_event_id=client_event_id,
        ),
        idempotency_key=key or uuid.uuid4(),
    )


async def service_out(
    cash: CashService,
    world: ExchangeWorld,
    *,
    code: str,
    amount: str,
    counter_account_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
) -> CashResult:
    return await cash.record_out(
        actor=actor or world.actor(),
        request=CashMovementRequest(
            branch_id=world.branch_id,
            currency_id=world.money(code).id,
            amount=Decimal(amount),
            session_id=session_id,
            device_id=world.device_id,
            counter_account_id=counter_account_id or world.account("opening"),
        ),
        idempotency_key=key or uuid.uuid4(),
    )


async def service_adjust(
    cash: CashService,
    world: ExchangeWorld,
    *,
    code: str,
    amount: str,
    sign: int,
    reason: str = "Count correction",
    session_id: uuid.UUID | None = None,
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
) -> CashResult:
    return await cash.record_adjustment(
        actor=actor or world.actor(),
        request=CashMovementRequest(
            branch_id=world.branch_id,
            currency_id=world.money(code).id,
            amount=Decimal(amount),
            session_id=session_id,
            device_id=world.device_id,
            adjustment_sign=sign,
            reason=reason,
        ),
        idempotency_key=key,
    )


async def service_close(
    cash: CashService,
    world: ExchangeWorld,
    *,
    session_id: uuid.UUID,
    counted: Mapping[str, str],
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
    notes: str | None = None,
) -> CashResult:
    """Close a shift with the counts the operator made (``code -> amount``)."""
    return await cash.close_session(
        actor=actor or world.actor(),
        request=CloseSessionRequest(
            session_id=session_id,
            counted=[
                CashCount(currency_id=world.money(code).id, amount=Decimal(amount))
                for code, amount in counted.items()
            ],
            notes=notes,
        ),
        idempotency_key=key or uuid.uuid4(),
    )


async def service_reverse(
    cash: CashService,
    world: ExchangeWorld,
    *,
    movement_id: uuid.UUID,
    reason: str = "Recorded in error",
    actor: ActorContext | None = None,
    key: uuid.UUID | None = None,
) -> CashResult:
    return await cash.reverse_movement(
        actor=actor or world.actor(),
        movement_id=movement_id,
        reason=reason,
        idempotency_key=key,
    )


async def fund(
    cash: CashService,
    world: ExchangeWorld,
    *,
    code: str,
    amount: str,
    rate: str = "1",
) -> uuid.UUID:
    """Give a drawer real cash through the documented opening-balance path (§6.1)."""
    return await _fund_drawer(
        cash.accounting, world=world, currency_code=code, amount=amount, rate=rate
    )


def publish(
    world: ExchangeWorld,
    client: TestClient,
    headers: Mapping[str, str],
    code: str,
    *,
    buy: str,
    sell: str,
) -> None:
    """Publish a quote for ``code`` against the base currency (append-only history)."""
    publish_quote(
        client,
        headers,
        from_currency_id=world.money(code).id,
        to_currency_id=world.money("AFN").id,
        buy_rate=buy,
        sell_rate=sell,
        branch_id=world.branch_id,
    )


__all__ = [
    "ADJUSTMENT",
    "BALANCE",
    "CASH",
    "CASH_IN",
    "CASH_OUT",
    "CLOSE",
    "CURRENT",
    "MOVEMENTS",
    "OPEN",
    "OPENING_OFFSET_CODE",
    "SESSIONS",
    "SHORT_OVER_CODE",
    "Scenario",
    "attach_session",
    "audit_rows",
    "branch_entries",
    "build_world",
    "cash_headers",
    "counted",
    "dispose",
    "entry_balance",
    "entry_with_lines",
    "fund",
    "get_balance",
    "get_current",
    "get_movement",
    "get_movements",
    "get_session",
    "get_sessions",
    "idempotency_row",
    "idempotency_rows",
    "ledger_quantity",
    "make_cash_engine",
    "movement_row",
    "movement_rows",
    "movements_for_reference",
    "position",
    "post_adjustment",
    "post_close",
    "post_in",
    "post_open",
    "post_out",
    "post_reverse",
    "publish",
    "read",
    "read_one",
    "run_cash",
    "run_cash_race",
    "service_adjust",
    "service_close",
    "service_confirmed_open_id",
    "service_in",
    "service_open",
    "service_open_session_id",
    "service_out",
    "service_reverse",
    "session_lines",
    "session_row",
    "trial_balance",
]
