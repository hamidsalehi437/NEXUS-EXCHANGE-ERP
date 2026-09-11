"""Shared scaffolding for the Phase 4 accounting suites (PART 48, PART 49).

The ledger is a database shape more than it is a function: a balanced entry, an
append-only line, a branch-scoped read. So these helpers build *real* state — real
accounts through the real HTTP surface, real quotes through ``POST /rates``, real rows
read back with SQL — and the suites then drive the engine the way the application does:
through :class:`AccountingService` for writes (the only writer, PART 46) and through the
HTTP surface for reads.

Two deliberate choices:

* **Nothing is shared between tests.** Each scenario scaffolds its own chart slice with
  unique codes, its own quotes and its own document ids, so a balance assertion can never
  depend on what another test happened to post before it, and the suite is order
  independent. The rows stay in the database afterwards — that is what an append-only
  ledger *is*, and the Phase 0 invariants are re-asserted over the accumulated history by
  ``test_accounting_integrity.py``.
* **The engine runs on its own engine per scenario.** One async engine belongs to one
  event loop, so every scenario opens a :class:`Database`, runs on :func:`asyncio.run`'s
  loop and disposes it. Concurrency scenarios get two.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import uuid
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence

# ``Scenario`` is the shape every suite hands to :func:`run_scenario` / :func:`run_race`.
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.database import Database
from app.core.permissions import ROLE_PERMISSIONS, Permission, RoleName
from app.services.accounting_service import AccountingService, build_accounting_service
from app.services.audit_service import ActorContext
from tests.helpers import database_dsn, fetch_all, fetch_scalar, settings_for_database

API = "/api/v1"
ACCOUNTS = f"{API}/accounts"
BRANCHES = f"{API}/branches"
RATES = f"{API}/rates"


# The seeded functional currency and the two foreign currencies the suites exercise.
BASE_CODE = "AFN"
FOREIGN_CODES = ("USD", "EUR")

# The chart slice every scenario scaffolds. Names are the keys tests use; the values are
# (account_type, currency_code or None, extra kwargs). ``cash_*`` accounts are bound to one
# currency (that is what makes a currency/account mismatch detectable), the rest are
# currency-less group accounts that accept any currency, exactly as the seeded chart's
# control accounts do.
DEFAULT_ACCOUNTS: dict[str, tuple[str, str | None, dict[str, Any]]] = {
    "cash_afn": ("ASSET", "AFN", {}),
    "cash_usd": ("ASSET", "USD", {}),
    "cash_eur": ("ASSET", "EUR", {}),
    "capital": ("EQUITY", None, {}),
    "commission": ("REVENUE", None, {}),
    "fx": ("REVENUE", None, {}),
    "expense": ("EXPENSE", None, {}),
    "receivable": ("ASSET", None, {}),
    "payable": ("LIABILITY", None, {}),
    "grouping": ("ASSET", None, {"is_postable": False}),
}


# --------------------------------------------------------------------------- reading
def read(database: str, sql: str, **params: object) -> list[dict[str, Any]]:
    """Run a query and return mapping rows (the assertion-friendly form)."""
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


def scalar(database: str, sql: str, **params: object) -> Any:
    """A single value from a query (``fetch_scalar`` with the same signature)."""
    return fetch_scalar(database, sql, **params)


def count(database: str, table: str, *, where: str = "TRUE", **params: object) -> int:
    """Row count for a table named by the test itself (never by a caller of the API)."""
    statement = f"SELECT count(*) FROM {table} WHERE {where}"  # noqa: S608 - test-only literal
    return int(scalar(database, statement, **params) or 0)


def identifier(database: str, sql: str, **params: object) -> uuid.UUID:
    """A UUID from a query, asserted to exist."""
    value = fetch_scalar(database, sql, **params)
    assert value is not None, f"no row for:\n{sql}"
    return uuid.UUID(str(value))


def account_id_of(database: str, code: str) -> uuid.UUID:
    return identifier(database, "SELECT id FROM accounts WHERE code = :code", code=code)


def currency_id_of(database: str, code: str) -> uuid.UUID:
    return identifier(database, "SELECT id FROM currencies WHERE code = :code", code=code)


def user_id_of(database: str, username: str) -> uuid.UUID:
    return identifier(
        database, "SELECT id FROM users WHERE username = :username", username=username
    )


# ------------------------------------------------------------------------ scaffolding
@dataclass(frozen=True, slots=True)
class Money:
    """A currency as the suites need it: identity, code and physical precision."""

    id: uuid.UUID
    code: str
    decimal_places: int


# Sentinel for "the scenario's branch" in :meth:`World.actor`, so that an explicitly
# unbound actor (``branch_id=None``) is expressible and cannot be confused with the default.
INHERIT_BRANCH = "inherit"


@dataclass(slots=True)
class World:
    """One scenario's scaffold: a branch, a chart slice and the currencies involved."""

    database: str
    branch_id: uuid.UUID
    branch_code: str
    head_user_id: uuid.UUID
    head_actor: ActorContext
    currencies: dict[str, Money]
    accounts: dict[str, uuid.UUID]
    codes: dict[str, str]
    user_ids: dict[str, uuid.UUID]
    # Ids a scenario wants to hand to a later assertion (an entry it posted, a currency it
    # created): the suites rarely need it, and threading it through every helper would
    # obscure more than it explains.
    ids: dict[str, uuid.UUID] = field(default_factory=dict)

    @property
    def base(self) -> Money:
        return self.currencies[BASE_CODE]

    def account(self, name: str) -> uuid.UUID:
        return self.accounts[name]

    def money(self, code: str) -> Money:
        return self.currencies[code]

    def user(self, name: str = "head") -> uuid.UUID:
        return self.user_ids[name]

    def actor(
        self,
        *,
        roles: Sequence[str] = (RoleName.OWNER,),
        user: str = "head",
        branch_id: uuid.UUID | str | None = INHERIT_BRANCH,
        permissions: Iterable[str] | None = None,
        device_id: uuid.UUID | None = None,
        ip_address: str | None = "127.0.0.1",
        request_id: str | None = None,
    ) -> ActorContext:
        """An actor with the *effective* authority of ``roles``.

        ``branch_id`` defaults to the scenario's branch (the state a device-bound user is
        in). ``branch_id=None`` is the *unbound* case and is therefore a distinct value from
        the default — passing ``None`` no longer means "whatever the scenario has".
        """
        resolved = self.branch_id if branch_id == INHERIT_BRANCH else branch_id
        return actor_for(
            self.user(user),
            branch_id=resolved,
            roles=roles,
            permissions=permissions,
            device_id=device_id,
            ip_address=ip_address,
            request_id=request_id,
        )


def actor_for(
    user_id: uuid.UUID | None,
    *,
    branch_id: uuid.UUID | None,
    roles: Sequence[str] = (RoleName.OWNER,),
    permissions: Iterable[str] | None = None,
    device_id: uuid.UUID | None = None,
    ip_address: str | None = "127.0.0.1",
    request_id: str | None = None,
) -> ActorContext:
    """Build an :class:`ActorContext` carrying the permissions the roles really grant."""
    granted: frozenset[str]
    if permissions is not None:
        granted = frozenset(str(permission) for permission in permissions)
    else:
        granted = frozenset(
            str(permission)
            for role in roles
            for permission in ROLE_PERMISSIONS[RoleName(str(role))]
        )
    return ActorContext(
        user_id=user_id,
        device_id=device_id,
        ip_address=ip_address,
        request_id=request_id,
        branch_id=branch_id,
        permissions=granted,
        roles=tuple(str(role) for role in roles),
    )


def create_currency(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    code: str,
    decimal_places: int = 2,
    is_base: bool = False,
) -> dict[str, Any]:
    response = client.post(
        f"{API}/currencies",
        headers=dict(headers),
        json={
            "code": code,
            "name": f"Accounting test {code}",
            "decimal_places": decimal_places,
            "is_base": is_base,
            "is_tradable": True,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def create_account(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    code: str,
    name: str,
    account_type: str,
    currency_id: str | None = None,
    branch_id: str | None = None,
    is_postable: bool = True,
    is_active: bool = True,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "code": code,
        "name": name,
        "account_type": account_type,
        "is_postable": is_postable,
        "is_active": is_active,
    }
    if currency_id is not None:
        body["currency_id"] = currency_id
    if branch_id is not None:
        body["branch_id"] = branch_id
    response = client.post(ACCOUNTS, headers=dict(headers), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def create_branch(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    code: str | None = None,
    name: str | None = None,
) -> dict[str, Any]:
    branch_code = code or f"T{uuid.uuid4().hex[:8].upper()}"
    response = client.post(
        BRANCHES,
        headers=dict(headers),
        json={
            "code": branch_code,
            "name": name or f"Branch {branch_code}",
            "timezone": "Asia/Kabul",
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def deactivate_branch(client: TestClient, headers: Mapping[str, str], branch_id: str) -> None:
    """Retire a branch created by a scenario (branches are never deleted, PART 22)."""
    response = client.patch(
        f"{BRANCHES}/{branch_id}", headers=dict(headers), json={"is_active": False}
    )
    assert response.status_code == 200, response.text


def publish_rate(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    from_currency_id: str,
    to_currency_id: str,
    buy_rate: str,
    sell_rate: str,
    branch_id: str | None = None,
    effective_at: str | None = None,
) -> dict[str, Any]:
    """One quote through ``POST /rates`` (append-only: nothing is ever replaced)."""
    body: dict[str, Any] = {
        "from_currency_id": from_currency_id,
        "to_currency_id": to_currency_id,
        "buy_rate": buy_rate,
        "sell_rate": sell_rate,
        "source": "MANUAL",
    }
    if branch_id is not None:
        body["branch_id"] = branch_id
    if effective_at is not None:
        body["effective_at"] = effective_at
    response = client.post(RATES, headers=dict(headers), json=body)
    assert response.status_code == 201, response.text
    return response.json()


def unique_code(prefix: str = "T") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10].upper()}"


def scaffold_chart(
    client: TestClient,
    headers: Mapping[str, str],
    *,
    branch_id: uuid.UUID | None,
    currencies: Mapping[str, Money],
    chart: Mapping[str, tuple[str, str | None, dict[str, Any]]] | None = None,
) -> tuple[dict[str, uuid.UUID], dict[str, str]]:
    """Create one chart slice (fresh codes) through ``POST /accounts``.

    Called once per scenario per branch: a scenario that needs two branches scaffolds two
    slices, so neither branch can see the other's accounts and the cross-branch rules have
    something real to refuse.
    """
    chosen = dict(chart or DEFAULT_ACCOUNTS)
    # A scenario may scaffold with only the currencies it has (a one-currency branch needs
    # no ``cash_usd``), so entries whose currency is absent are skipped rather than failing.
    accounts: dict[str, uuid.UUID] = {}
    codes: dict[str, str] = {}
    for name, (account_type, currency_code, extra) in chosen.items():
        if currency_code is not None and currency_code not in currencies:
            continue
        code = unique_code()
        created = create_account(
            client,
            headers,
            code=code,
            name=f"{name} {code}",
            account_type=account_type,
            currency_id=str(currencies[currency_code].id) if currency_code else None,
            branch_id=str(branch_id) if branch_id else None,
            is_postable=bool(extra.get("is_postable", True)),
            is_active=bool(extra.get("is_active", True)),
        )
        accounts[name] = uuid.UUID(str(created["id"]))
        codes[name] = code
    return accounts, codes


def load_currencies(database: str, codes: Sequence[str]) -> dict[str, Money]:
    """The currency rows a scenario needs, read once."""
    rows: dict[str, Money] = {}
    for code in codes:
        row = read_one(
            database,
            "SELECT id, code, decimal_places FROM currencies WHERE code = :code",
            code=code,
        )
        rows[code] = Money(
            id=uuid.UUID(str(row["id"])),
            code=str(row["code"]),
            decimal_places=int(row["decimal_places"]),
        )
    return rows


def build_world(
    client: TestClient,
    headers: Mapping[str, str],
    database: str,
    *,
    branch_id: str | None = None,
    branch_code: str = "MAIN",
    accounts: Mapping[str, tuple[str, str | None, dict[str, Any]]] | None = None,
    currencies: Sequence[str] = (BASE_CODE, *FOREIGN_CODES),
    quotes: bool = False,
    buy_rate: str = "70",
    sell_rate: str = "71",
) -> World:
    """Scaffold a branch + chart slice + (optionally) quotes for one scenario.

    ``branch_id`` defaults to the seeded ``MAIN`` branch. When it is given (a scenario that
    created its own branch), the chart slice is created **branch-bound**, which is what
    triggers the cross-branch rules.
    """
    chosen = dict(accounts or DEFAULT_ACCOUNTS)
    resolved_branch = uuid.UUID(branch_id) if branch_id else None
    if resolved_branch is None:
        resolved_branch = identifier(
            database, "SELECT id FROM branches WHERE code = :code", code=branch_code
        )
        branch_code = str(
            scalar(database, "SELECT code FROM branches WHERE id = :id", id=resolved_branch) or ""
        )

    currency_rows = load_currencies(database, currencies)

    account_ids, codes = scaffold_chart(
        client,
        headers,
        branch_id=resolved_branch,
        currencies=currency_rows,
        chart=chosen,
    )

    if quotes:
        for code in currencies:
            if code == BASE_CODE:
                continue
            publish_rate(
                client,
                headers,
                from_currency_id=str(currency_rows[code].id),
                to_currency_id=str(currency_rows[BASE_CODE].id),
                buy_rate=buy_rate,
                sell_rate=sell_rate,
            )

    head_user_id = user_id_of(database, "admin")
    return World(
        database=database,
        branch_id=resolved_branch,
        branch_code=branch_code,
        head_user_id=head_user_id,
        head_actor=actor_for(
            head_user_id, branch_id=resolved_branch, roles=(RoleName.SUPER_ADMIN,)
        ),
        currencies=currency_rows,
        accounts=account_ids,
        codes=codes,
        user_ids={"head": head_user_id},
    )


# ------------------------------------------------------------------------- async runs
def make_engine(database: str) -> tuple[Any, Database, AccountingService]:
    """``(settings, database, service)`` on a fresh async engine for this event loop."""
    settings = settings_for_database(database)
    handle = Database(settings)  # type: ignore[arg-type]
    service = build_accounting_service(database=handle, settings=settings)  # type: ignore[arg-type]
    return settings, handle, service


async def dispose(handle: Database) -> None:
    await handle.dispose()


Scenario = Callable[[AccountingService], Awaitable[Any]]


def run_scenario(database: str, scenario: Scenario) -> Any:
    """Run one accounting scenario on its own engine and event loop."""

    async def _main() -> Any:
        _, handle, service = make_engine(database)
        try:
            return await scenario(service)
        finally:
            await dispose(handle)

    return asyncio.run(_main())


def run_race(database: str, *scenarios: Scenario) -> list[Any]:
    """Run scenarios in parallel — each on its own engine sharing one event loop.

    The scenarios meet inside PostgreSQL, not inside the event loop, so a race can only be
    resolved by the database (unique constraints, row locks, the deferred balance
    assertion) — exactly what a concurrent posting test must exercise. Exceptions are
    returned rather than raised, so one loser in the race does not hide the winner's
    result.
    """

    async def _main() -> list[Any]:
        handles: list[Database] = []
        services: list[AccountingService] = []
        try:
            for _ in scenarios:
                _, handle, service = make_engine(database)
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


# ------------------------------------------------------------------------ posting help
def line(
    account_id: uuid.UUID,
    *,
    debit: str | Decimal = "0",
    credit: str | Decimal = "0",
    currency_id: uuid.UUID,
    exchange_rate: str | Decimal = "1",
    description: str | None = None,
) -> Any:
    """A :class:`PostingLine` built from decimal *strings* (never floats, PART 62)."""
    from app.services.accounting_service import PostingLine

    return PostingLine(
        account_id=account_id,
        currency_id=currency_id,
        debit=Decimal(debit),
        credit=Decimal(credit),
        exchange_rate=Decimal(exchange_rate),
        description=description,
    )


def line_totals(entry: Any) -> tuple[Decimal, Decimal]:
    """``(SUM(debit), SUM(credit))`` of an entry's lines, summed the way the ledger does.

    ``money_sum`` and not ``sum()``: a test that adds up 30-digit values in the default
    decimal context (28 digits) would round its own expectation and then report a false
    failure — or, worse, agree with a defective ledger.
    """
    from app.core.money import money_sum

    return (
        money_sum(Decimal(line.debit) for line in entry.lines),
        money_sum(Decimal(line.credit) for line in entry.lines),
    )


def at_utc(*, days_ago: int = 0, hours_ago: int = 0, minutes_ago: int = 0) -> dt.datetime:
    return dt.datetime.now(tz=dt.UTC) - dt.timedelta(
        days=days_ago, hours=hours_ago, minutes=minutes_ago
    )


def ledger_rows(database: str, entry_id: uuid.UUID) -> list[dict[str, Any]]:
    """The stored lines of one entry, ordered, straight from the immutable table."""
    return read(
        database,
        """
        SELECT l.id, l.account_id, a.code AS account_code, l.debit, l.credit,
               l.currency_id, c.code AS currency_code, l.exchange_rate, l.foreign_amount
          FROM journal_lines l
          JOIN accounts a ON a.id = l.account_id
          LEFT JOIN currencies c ON c.id = l.currency_id
         WHERE l.journal_entry_id = :entry
         ORDER BY a.code
        """,
        entry=entry_id,
    )


def entry_row(database: str, entry_id: uuid.UUID) -> dict[str, Any]:
    return read_one(
        database,
        "SELECT * FROM journal_entries WHERE id = :entry",
        entry=entry_id,
    )


def totals(database: str) -> tuple[Decimal, Decimal]:
    """The ledger's whole Σdebit/Σcredit, read from the immutable table."""
    row = read_one(
        database,
        "SELECT COALESCE(SUM(debit), 0) AS d, COALESCE(SUM(credit), 0) AS c FROM journal_lines",
    )
    return Decimal(str(row["d"])), Decimal(str(row["c"]))


@dataclass(frozen=True, slots=True)
class FinancialState:
    """Everything a *refused* posting must leave exactly as it was.

    Counts, ledger-wide totals and each account's position, captured before and after a
    posting that is supposed to be refused. A test that proves "nothing was written" has to
    read the database rather than the service's return value: ``entries`` and ``lines`` are
    the tables, ``audit_rows`` and ``idempotency_rows`` are the side effects a keyed or
    denied request could leave behind, and ``quantities``/``functional`` are the positions
    the money would have moved.
    """

    entries: int
    lines: int
    audit_rows: int
    idempotency_rows: int
    total_debit: Decimal
    total_credit: Decimal
    quantities: dict[str, Decimal]
    functional: dict[str, Decimal]

    @property
    def balanced(self) -> bool:
        """The ledger-wide invariant, so a test can assert it on the *after* snapshot."""
        return self.total_debit == self.total_credit


def financial_state(database: str, accounts: Iterable[uuid.UUID]) -> FinancialState:
    """Snapshot the ledger for the given accounts (every requested account is present)."""
    wanted = [str(account) for account in accounts]
    rows = read(
        database,
        """
        SELECT account_id,
               COALESCE(SUM(debit - credit), 0) AS functional_balance,
               COALESCE(
                   SUM(CASE WHEN debit > 0 THEN foreign_amount ELSE -foreign_amount END), 0
               ) AS foreign_quantity
          FROM journal_lines
         GROUP BY account_id
        """,
    )
    by_account = {str(row["account_id"]): row for row in rows}
    quantities = {key: Decimal("0") for key in wanted}
    functional = {key: Decimal("0") for key in wanted}
    for key in wanted:
        row = by_account.get(key)
        if row is None:
            continue
        quantities[key] = Decimal(str(row["foreign_quantity"]))
        functional[key] = Decimal(str(row["functional_balance"]))
    debit, credit = totals(database)
    return FinancialState(
        entries=count(database, "journal_entries"),
        lines=count(database, "journal_lines"),
        audit_rows=count(database, "audit_logs"),
        idempotency_rows=count(database, "idempotency_keys"),
        total_debit=debit,
        total_credit=credit,
        quantities=quantities,
        functional=functional,
    )


def audit_actions_for(database: str, entity_id: uuid.UUID) -> list[str]:
    rows = read(
        database,
        "SELECT action FROM audit_logs WHERE entity_id = :entity ORDER BY seq",
        entity=entity_id,
    )
    return [str(row["action"]) for row in rows]


def idempotency_rows(database: str, *, key: uuid.UUID) -> list[dict[str, Any]]:
    return read(database, "SELECT * FROM idempotency_keys WHERE key = :key", key=key)


def table_count(database: str, table: str) -> int:
    """Row count for a table named by the test itself (never by a caller of the API)."""
    statement = f"SELECT count(*) FROM {table}"  # noqa: S608 - test-only literal
    return int(fetch_scalar(database, statement) or 0)


def permission(name: str) -> str:
    """``Permission.EXCHANGE_CREATE`` → ``"exchange.create"`` for detail assertions."""
    return str(Permission(name))


def all_fetch(database: str, sql: str, **params: object) -> list[tuple[Any, ...]]:
    return fetch_all(database, sql, **params)
