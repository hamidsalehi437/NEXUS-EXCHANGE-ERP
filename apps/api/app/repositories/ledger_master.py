"""Chart-of-accounts and exchange-rate queries (PART 11, PART 13).

Two things live here that are easy to get wrong elsewhere:

* **The account tree.** The chart is a forest (``parent_id``), and both the cycle check
  and the "may this parent receive a child?" question need recursive SQL. Doing it in the
  repository means the service never walks a Python tree that could disagree with the
  database if two requests race.
* **Rate resolution.** ``resolve_exchange_rate`` is a database function written in
  Phase 0 and reviewed with the schema: branch quote first, then the newest
  ``effective_at <= at``. Calling it rather than re-expressing the ordering in Python
  keeps one definition of "which quote is in force".
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal

from sqlalchemy import Select, func, literal, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.account import Account
from app.models.branch import Branch
from app.models.currency import Currency
from app.models.exchange_rate import ExchangeRate


class AccountRepository:
    """Reads and writes for ``accounts`` (the chart of accounts)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        """The session this repository reads and writes through.

        Exposed so a service can read a sibling table (the account's currency) inside the
        same transaction without opening a second connection.
        """
        return self._session

    async def get(self, account_id: uuid.UUID, *, for_update: bool = False) -> Account | None:
        statement = select(Account).where(Account.id == account_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_code(self, code: str) -> Account | None:
        statement = select(Account).where(Account.code == code.strip())
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_accounts(
        self,
        *,
        account_type: str | None = None,
        branch_id: uuid.UUID | None = None,
        currency_id: uuid.UUID | None = None,
        parent_id: uuid.UUID | None = None,
        is_active: bool | None = None,
        is_postable: bool | None = None,
        limit: int = 200,
        offset: int = 0,
    ) -> tuple[Sequence[tuple[Account, bool, str | None]], int]:
        """Accounts ordered by code with the two facts a client cannot derive.

        ``has_children`` is a correlated ``EXISTS`` (the flag decides whether an account may
        be postable, so it must reflect committed rows, not whichever page was fetched) and
        ``currency_code`` comes from a left join, so a chart listing is one query instead of
        one query per row.
        """
        child = aliased(Account)
        has_children = (
            select(literal(1)).select_from(child).where(child.parent_id == Account.id).exists()
        )
        statement = (
            select(Account, has_children.label("has_children"), Currency.code)
            .outerjoin(Currency, Currency.id == Account.currency_id)
            .order_by(Account.code)
        )
        if account_type is not None:
            statement = statement.where(Account.account_type == account_type)
        if branch_id is not None:
            statement = statement.where(Account.branch_id == branch_id)
        if currency_id is not None:
            statement = statement.where(Account.currency_id == currency_id)
        if parent_id is not None:
            statement = statement.where(Account.parent_id == parent_id)
        if is_active is not None:
            statement = statement.where(Account.is_active.is_(is_active))
        if is_postable is not None:
            statement = statement.where(Account.is_postable.is_(is_postable))
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(statement.limit(limit).offset(offset))
        return [(row[0], bool(row[1]), row[2]) for row in rows.all()], int(total or 0)

    async def get_view(
        self, account_id: uuid.UUID, *, for_update: bool = False
    ) -> tuple[Account, bool, str | None] | None:
        """One account with ``has_children`` and ``currency_code``, or ``None``."""
        child = aliased(Account)
        has_children = (
            select(literal(1)).select_from(child).where(child.parent_id == Account.id).exists()
        )
        statement = (
            select(Account, has_children.label("has_children"), Currency.code)
            .outerjoin(Currency, Currency.id == Account.currency_id)
            .where(Account.id == account_id)
        )
        if for_update:
            statement = statement.with_for_update(of=Account)
        row = (await self._session.execute(statement)).first()
        return None if row is None else (row[0], bool(row[1]), row[2])

    async def code_exists(self, code: str) -> bool:
        statement = select(func.count()).select_from(Account).where(Account.code == code)
        return bool(await self._session.scalar(statement))

    async def has_children(self, account_id: uuid.UUID) -> bool:
        statement = select(func.count()).select_from(Account).where(Account.parent_id == account_id)
        return bool(await self._session.scalar(statement))

    async def is_descendant_of(self, candidate_id: uuid.UUID, ancestor_id: uuid.UUID) -> bool:
        """True when ``candidate_id`` sits below ``ancestor_id`` in the tree.

        A recursive walk in the database: re-parenting an account under one of its own
        descendants would detach that subtree from the chart, and the check has to see the
        committed tree, not a copy that another transaction may already have changed.
        """
        value = await self._session.scalar(
            text(
                """
                WITH RECURSIVE subtree AS (
                    SELECT id, parent_id FROM accounts WHERE parent_id = :ancestor
                    UNION ALL
                    SELECT a.id, a.parent_id
                      FROM accounts a
                      JOIN subtree s ON a.parent_id = s.id
                )
                SELECT EXISTS (SELECT 1 FROM subtree WHERE id = :candidate)
                """
            ),
            {"ancestor": ancestor_id, "candidate": candidate_id},
        )
        return bool(value)

    async def has_journal_lines(self, account_id: uuid.UUID) -> bool:
        """True once the account carries ledger movements (it then stays structurally frozen)."""
        found = await self._session.scalar(
            text("SELECT 1 FROM journal_lines WHERE account_id = :account_id LIMIT 1"),
            {"account_id": account_id},
        )
        return found is not None

    def add(self, account: Account) -> None:
        self._session.add(account)

    async def flush(self) -> None:
        await self._session.flush()


class ExchangeRateRepository:
    """Append-only reads and writes for ``exchange_rates`` (PART 13)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, rate_id: uuid.UUID, *, for_update: bool = False) -> ExchangeRate | None:
        statement = select(ExchangeRate).where(ExchangeRate.id == rate_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_history(
        self,
        *,
        from_currency_id: uuid.UUID | None = None,
        to_currency_id: uuid.UUID | None = None,
        branch_id: uuid.UUID | None = None,
        include_global: bool = True,
        effective_from: dt.datetime | None = None,
        effective_to: dt.datetime | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[RowMapping], int]:
        """Quote rows newest first — the audit view of who published what, when.

        Returns mappings rather than ORM objects because a quote is read by humans and by
        the counter: the currency codes and the branch code are part of the answer, and
        resolving them one query at a time in the serializer would be an N+1 the caller
        never asked for.
        """
        source_currency = aliased(Currency)
        target_currency = aliased(Currency)
        branch = aliased(Branch)
        statement = (
            select(
                ExchangeRate.id,
                ExchangeRate.from_currency_id,
                ExchangeRate.to_currency_id,
                source_currency.code.label("from_currency_code"),
                target_currency.code.label("to_currency_code"),
                ExchangeRate.buy_rate,
                ExchangeRate.sell_rate,
                ExchangeRate.effective_at,
                ExchangeRate.branch_id,
                branch.code.label("branch_code"),
                ExchangeRate.source,
                ExchangeRate.created_at,
                ExchangeRate.created_by,
            )
            .select_from(ExchangeRate)
            .outerjoin(source_currency, source_currency.id == ExchangeRate.from_currency_id)
            .outerjoin(target_currency, target_currency.id == ExchangeRate.to_currency_id)
            .outerjoin(branch, branch.id == ExchangeRate.branch_id)
            .order_by(ExchangeRate.effective_at.desc(), ExchangeRate.created_at.desc())
        )
        if from_currency_id is not None:
            statement = statement.where(ExchangeRate.from_currency_id == from_currency_id)
        if to_currency_id is not None:
            statement = statement.where(ExchangeRate.to_currency_id == to_currency_id)
        if branch_id is not None:
            statement = statement.where(
                ExchangeRate.branch_id == branch_id
                if not include_global
                else (ExchangeRate.branch_id == branch_id) | ExchangeRate.branch_id.is_(None)
            )
        if effective_from is not None:
            statement = statement.where(ExchangeRate.effective_at >= effective_from)
        if effective_to is not None:
            statement = statement.where(ExchangeRate.effective_at <= effective_to)
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(statement.limit(limit).offset(offset))
        return list(rows.mappings().all()), int(total or 0)

    async def latest_quotes(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        at: dt.datetime | None = None,
        from_currency_id: uuid.UUID | None = None,
        to_currency_id: uuid.UUID | None = None,
    ) -> Sequence[object]:
        """The quote in force per currency pair at ``at``, under the resolution rules.

        **One row per pair**, chosen exactly the way ``resolve_exchange_rate`` chooses:
        a branch quote beats a global one, then the newest instant wins. Without
        ``branch_id`` the answer is the *global* quote — a branch quote cannot be applied
        to a branch that was not named, and showing all of them side by side would leave
        the caller to re-implement the precedence rule.

        The parameter is cast because ``:branch_id IS NULL`` gives PostgreSQL nothing to
        infer a type from (asyncpg then refuses to bind it — a defect this query had until
        the resolution tests exercised it, together with a missing global fallback for a
        branch that has published no quote of its own).
        """
        moment = at or dt.datetime.now(tz=dt.UTC)
        statement = text(
            """
            SELECT DISTINCT ON (r.from_currency_id, r.to_currency_id)
                   r.id,
                   r.from_currency_id,
                   r.to_currency_id,
                   source.code AS from_currency_code,
                   target.code AS to_currency_code,
                   r.buy_rate,
                   r.sell_rate,
                   r.effective_at,
                   r.branch_id,
                   branch.code AS branch_code,
                   r.source,
                   r.created_at,
                   r.created_by
              FROM exchange_rates r
              JOIN currencies source ON source.id = r.from_currency_id
              JOIN currencies target ON target.id = r.to_currency_id
              LEFT JOIN branches branch ON branch.id = r.branch_id
             WHERE r.effective_at <= :at
               AND (
                    -- Global view: only global quotes are in force.
                    (CAST(:branch_id AS UUID) IS NULL AND r.branch_id IS NULL)
                    -- Branch view: the branch's own quote, or the global fallback.
                 OR (CAST(:branch_id AS UUID) IS NOT NULL
                     AND (r.branch_id IS NULL OR r.branch_id = CAST(:branch_id AS UUID)))
               )
               AND (CAST(:from_currency AS UUID) IS NULL
                    OR r.from_currency_id = CAST(:from_currency AS UUID))
               AND (CAST(:to_currency AS UUID) IS NULL
                    OR r.to_currency_id = CAST(:to_currency AS UUID))
             ORDER BY r.from_currency_id, r.to_currency_id,
                      (r.branch_id IS NOT NULL) DESC,
                      r.effective_at DESC, r.created_at DESC
            """
        )
        rows = await self._session.execute(
            statement,
            {
                "at": moment,
                "branch_id": branch_id,
                "from_currency": from_currency_id,
                "to_currency": to_currency_id,
            },
        )
        return list(rows.mappings().all())

    async def resolve(
        self,
        *,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        at: dt.datetime | None = None,
    ) -> object | None:
        """The quote a transaction would use right now (branch wins over global)."""
        moment = at or dt.datetime.now(tz=dt.UTC)
        return (
            (
                await self._session.execute(
                    text(
                        "SELECT exchange_rate_id, buy_rate, sell_rate, effective_at, branch_id "
                        "FROM resolve_exchange_rate(:from_currency, :to_currency, :branch, :at)"
                    ),
                    {
                        "from_currency": from_currency_id,
                        "to_currency": to_currency_id,
                        "branch": branch_id,
                        "at": moment,
                    },
                )
            )
            .mappings()
            .one_or_none()
        )

    async def duplicate_instant_exists(
        self,
        *,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        effective_at: dt.datetime,
    ) -> bool:
        """Whether a quote already exists for this pair, branch and exact instant.

        The unique index is the real guarantee (it also covers concurrent inserts); this
        check exists so the caller gets ``DUPLICATE_RESOURCE`` with the offending fields
        instead of a driver error when the collision is predictable.
        """
        statement = (
            select(func.count())
            .select_from(ExchangeRate)
            .where(
                ExchangeRate.from_currency_id == from_currency_id,
                ExchangeRate.to_currency_id == to_currency_id,
                ExchangeRate.effective_at == effective_at,
                ExchangeRate.branch_id.is_(None)
                if branch_id is None
                else ExchangeRate.branch_id == branch_id,
            )
        )
        return bool(await self._session.scalar(statement))

    def add(self, rate: ExchangeRate) -> None:
        self._session.add(rate)

    async def flush(self) -> None:
        await self._session.flush()


def decimal_or_none(value: object) -> Decimal | None:
    """Narrow a driver value to :class:`Decimal` without ever going through ``float``."""
    if value is None:
        return None
    return value if isinstance(value, Decimal) else Decimal(str(value))


__all__ = ["AccountRepository", "ExchangeRateRepository", "Select", "decimal_or_none"]
