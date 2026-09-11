"""Ledger queries and writes (PART 12, PART 46, PART 49).

Three rules shape this module:

* **``journal_lines`` is the source of truth.** Every balance, total and trial balance in
  the system is derived from the immutable lines — never from the ``account_balances``
  cache, which is a rebuildable convenience (invariant I-2, ``ACCOUNTING_MODEL.md`` §7).
* **Nothing here decides anything.** Repositories read and write rows; the posting rules,
  the branch scope and the authorization checks live in
  :class:`app.services.accounting_service.AccountingService`. A second place that could
  decide "is this entry balanced" is a second place that could be wrong.
* **Reads are shaped for the API.** A journal entry without its account codes and currency
  codes forces the caller to fetch them row by row, and a listing without its totals makes
  a reader sum money in Python. Both come back from one query each.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.models.account import Account
from app.models.branch import Branch
from app.models.journal import JournalEntry, JournalLine
from app.models.user import User

# The filters a journal query accepts. Kept in one tuple so the repository, the service
# and the endpoint cannot drift apart about which filters exist.
JOURNAL_ENTRY_FILTERS: tuple[str, ...] = (
    "reference_type",
    "reference_id",
    "branch_id",
    "from_",
    "to",
)


class JournalEntryRepository:
    """Reads and writes for ``journal_entries`` and ``journal_lines``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ------------------------------------------------------------------ writes
    def add_entry(self, entry: JournalEntry) -> None:
        self._session.add(entry)

    def add_lines(self, lines: Sequence[JournalLine]) -> None:
        self._session.add_all(list(lines))

    async def flush(self) -> None:
        """Send pending INSERTs so the entry, its lines and the cache trigger run."""
        await self._session.flush()

    async def lock_accounts(self, account_ids: Sequence[uuid.UUID]) -> None:
        """Take a row lock on each account a posting touches, in a deterministic order.

        A posting changes derived state — the account's functional balance *and* the
        quantity of currency it holds — and some rules are decided from that state (a
        disposal may not deliver more than the position holds, §6.3). Read-then-write is
        only safe if the read is protected until the write commits, so the accounts are
        locked here, ordered by id.

        The order is the *same* order the lines are inserted in (and therefore the order
        the balance-cache trigger upserts its rows in), which is what keeps two postings
        that touch the same pair of accounts from deadlocking.
        """
        if not account_ids:
            return
        await self._session.execute(
            select(Account.id)
            .where(Account.id.in_(sorted(set(account_ids), key=str)))
            .order_by(Account.id)
            .with_for_update()
        )

    # ------------------------------------------------------------------- reads
    async def get_entry(
        self, entry_id: uuid.UUID, *, for_update: bool = False
    ) -> JournalEntry | None:
        statement = select(JournalEntry).where(JournalEntry.id == entry_id)
        if for_update:
            # A reversal is decided from the entry's own state and from the existence of a
            # reversal row. Locking the original serialises two simultaneous reversal
            # attempts, so the second one sees the first one's committed row instead of
            # inserting a mirror of a mirror.
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def find_by_reference(
        self, *, reference_type: str, reference_id: uuid.UUID
    ) -> JournalEntry | None:
        """The entry already posted for a business document (at most one can exist)."""
        statement = select(JournalEntry).where(
            JournalEntry.reference_type == reference_type,
            JournalEntry.reference_id == reference_id,
        )
        return (await self._session.execute(statement)).scalars().first()

    async def find_reversal_of(self, entry_id: uuid.UUID) -> JournalEntry | None:
        """The reversal bound to this entry, if it has one."""
        statement = select(JournalEntry).where(JournalEntry.reversal_of_id == entry_id)
        return (await self._session.execute(statement)).scalars().first()

    async def lines_of(self, entry_id: uuid.UUID) -> Sequence[JournalLine]:
        """The entry's lines in their canonical order.

        Canonical is *content*, not insertion: ``(account, currency, debit, credit)``.
        Mirroring a reversal and reading an entry back therefore agree on the order
        without depending on the random UUID a row received.
        """
        statement = (
            select(JournalLine)
            .where(JournalLine.journal_entry_id == entry_id)
            .order_by(
                JournalLine.account_id,
                JournalLine.currency_id,
                JournalLine.debit.desc(),
                JournalLine.credit.desc(),
            )
        )
        return list((await self._session.execute(statement)).scalars().all())

    async def line_count(self, entry_id: uuid.UUID) -> int:
        statement = (
            select(func.count())
            .select_from(JournalLine)
            .where(JournalLine.journal_entry_id == entry_id)
        )
        return int(await self._session.scalar(statement) or 0)

    async def entry_row(self, entry_id: uuid.UUID) -> RowMapping | None:
        """One entry with its branch code, reversal linkage and totals."""
        return (
            (await self._session.execute(_entry_select().where(JournalEntry.id == entry_id)))
            .mappings()
            .one_or_none()
        )

    async def list_entries(
        self,
        *,
        reference_type: str | None = None,
        reference_id: uuid.UUID | None = None,
        branch_ids: Sequence[uuid.UUID] | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[RowMapping], int]:
        """Entries newest first, with the totals a reader must not compute itself."""
        statement: Select[Any] = _entry_select()
        if reference_type is not None:
            statement = statement.where(JournalEntry.reference_type == reference_type)
        if reference_id is not None:
            statement = statement.where(JournalEntry.reference_id == reference_id)
        if branch_ids is not None:
            # ``branch_ids`` is the *effective* scope. An empty sequence means "no branch
            # is visible to this actor", which must return nothing — never everything.
            statement = statement.where(JournalEntry.branch_id.in_(list(branch_ids)))
        if from_ is not None:
            statement = statement.where(JournalEntry.transaction_date >= from_)
        if to is not None:
            statement = statement.where(JournalEntry.transaction_date < to)

        # Counting grouped rows needs a subquery: ``COUNT(*)`` over a GROUP BY statement
        # would answer per group instead of once.
        total = int(
            await self._session.scalar(
                select(func.count()).select_from(statement.order_by(None).subquery())
            )
            or 0
        )
        rows = await self._session.execute(
            statement.order_by(
                JournalEntry.transaction_date.desc(),
                JournalEntry.created_at.desc(),
                JournalEntry.id,
            )
            .limit(limit)
            .offset(offset)
        )
        return list(rows.mappings().all()), total

    async def line_rows(self, entry_id: uuid.UUID) -> Sequence[RowMapping]:
        """An entry's lines with the account and currency facts a client cannot derive."""
        result = await self._session.execute(
            text(
                """
                SELECT l.id,
                       l.journal_entry_id,
                       l.account_id,
                       a.code            AS account_code,
                       a.name            AS account_name,
                       a.account_type,
                       l.currency_id,
                       c.code            AS currency_code,
                       l.debit,
                       l.credit,
                       l.exchange_rate,
                       l.foreign_amount,
                       l.description
                  FROM journal_lines l
                  JOIN accounts a        ON a.id = l.account_id
                  LEFT JOIN currencies c ON c.id = l.currency_id
                 WHERE l.journal_entry_id = :entry_id
                 ORDER BY a.code, l.debit DESC, l.id
                """
            ),
            {"entry_id": entry_id},
        )
        return list(result.mappings().all())


class LedgerRepository:
    """Derived reads over the immutable lines: balances, totals and positions."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def account_totals(
        self,
        *,
        account_id: uuid.UUID,
        branch_ids: Sequence[uuid.UUID] | None = None,
        currency_id: uuid.UUID | None = None,
        as_of: dt.datetime | None = None,
    ) -> Sequence[RowMapping]:
        """Per-currency debit/credit totals for one account, straight from the lines.

        ``branch_ids`` is the effective branch scope (``None`` = every branch, ``[]`` =
        nothing visible). It is applied to the *entry's* branch, which is what makes a
        branch-bound reader see their own branch's share of a group-level account.
        """
        result = await self._session.execute(
            text(
                """
                SELECT l.currency_id,
                       c.code AS currency_code,
                       SUM(l.debit)  AS debit_total,
                       SUM(l.credit) AS credit_total,
                       COUNT(DISTINCT l.journal_entry_id) AS entry_count,
                       MAX(e.transaction_date) AS last_posted_at
                  FROM journal_lines l
                  JOIN journal_entries e ON e.id = l.journal_entry_id
                  LEFT JOIN currencies c ON c.id = l.currency_id
                 WHERE l.account_id = :account_id
                   AND (CAST(:currency_id AS UUID) IS NULL
                        OR l.currency_id = CAST(:currency_id AS UUID))
                   AND (CAST(:as_of AS TIMESTAMPTZ) IS NULL
                        OR e.transaction_date <= CAST(:as_of AS TIMESTAMPTZ))
                   AND (CAST(:branch_ids AS UUID[]) IS NULL
                        OR e.branch_id = ANY(CAST(:branch_ids AS UUID[])))
                 GROUP BY l.currency_id, c.code
                 ORDER BY c.code NULLS LAST
                """
            ),
            {
                "account_id": account_id,
                "branch_ids": list(branch_ids) if branch_ids is not None else None,
                "currency_id": currency_id,
                "as_of": as_of,
            },
        )
        return list(result.mappings().all())

    async def trial_balance(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        account_type: str | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        include_inactive: bool = True,
    ) -> Sequence[RowMapping]:
        """``v_trial_balance``'s shape, with the filters a report needs (and no cache)."""
        result = await self._session.execute(
            text(
                """
                SELECT a.id            AS account_id,
                       a.code          AS account_code,
                       a.name          AS account_name,
                       a.account_type,
                       a.normal_balance,
                       a.is_active,
                       l.currency_id,
                       c.code          AS currency_code,
                       SUM(l.debit)    AS total_debit,
                       SUM(l.credit)   AS total_credit,
                       SUM(l.debit) - SUM(l.credit) AS net_debit,
                       COUNT(DISTINCT l.journal_entry_id) AS entry_count
                  FROM journal_lines l
                  JOIN journal_entries e ON e.id = l.journal_entry_id
                  JOIN accounts a        ON a.id = l.account_id
                  LEFT JOIN currencies c ON c.id = l.currency_id
                 WHERE (CAST(:branch_id AS UUID) IS NULL
                        OR e.branch_id = CAST(:branch_id AS UUID))
                   AND (CAST(:account_type AS TEXT) IS NULL
                        OR a.account_type = CAST(:account_type AS TEXT))
                   AND (CAST(:from_date AS TIMESTAMPTZ) IS NULL
                        OR e.transaction_date >= CAST(:from_date AS TIMESTAMPTZ))
                   AND (CAST(:to_date AS TIMESTAMPTZ) IS NULL
                        OR e.transaction_date < CAST(:to_date AS TIMESTAMPTZ))
                   AND (CAST(:include_inactive AS BOOLEAN) OR a.is_active)
                 GROUP BY a.id, a.code, a.name, a.account_type, a.normal_balance, a.is_active,
                          l.currency_id, c.code
                 ORDER BY a.code, c.code NULLS LAST
                """
            ),
            {
                "branch_id": branch_id,
                "account_type": account_type,
                "from_date": from_,
                "to_date": to,
                "include_inactive": include_inactive,
            },
        )
        return list(result.mappings().all())

    async def ledger_totals(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
    ) -> RowMapping:
        """The ledger-wide Σdebit and Σcredit (must be equal — PART 49, invariant I-1)."""
        return (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(l.debit), 0)  AS total_debit,
                               COALESCE(SUM(l.credit), 0) AS total_credit,
                               COUNT(DISTINCT e.id)       AS entry_count,
                               COUNT(l.id)                AS line_count
                          FROM journal_lines l
                          JOIN journal_entries e ON e.id = l.journal_entry_id
                         WHERE (CAST(:branch_id AS UUID) IS NULL
                                OR e.branch_id = CAST(:branch_id AS UUID))
                           AND (CAST(:from_date AS TIMESTAMPTZ) IS NULL
                                OR e.transaction_date >= CAST(:from_date AS TIMESTAMPTZ))
                           AND (CAST(:to_date AS TIMESTAMPTZ) IS NULL
                                OR e.transaction_date < CAST(:to_date AS TIMESTAMPTZ))
                        """
                    ),
                    {"branch_id": branch_id, "from_date": from_, "to_date": to},
                )
            )
            .mappings()
            .one()
        )

    async def position(
        self, *, account_id: uuid.UUID, branch_id: uuid.UUID | None = None
    ) -> RowMapping:
        """An inventory account's functional value and physical quantity.

        ``functional_balance`` is the sum of (debit - credit); ``foreign_quantity`` is the
        **signed** sum of ``foreign_amount`` — a debit line adds units, a credit line
        removes them. The sign matters: holding 1,000 and having delivered 700 leaves 300
        units, and a quantity that added both sides would report 1,700, price the next
        disposal at a fraction of its true carrying rate, and let a drawer deliver money it
        does not have. ``foreign_amount`` itself is the generated column (line amount /
        line rate), so the units are counted exactly as the ledger stores them. Together
        the two sums give the **carrying rate** (functional value per unit held), which
        ``ACCOUNTING_MODEL.md`` §6.3 requires for a disposal and which no cache could be
        trusted to provide.

        ``branch_id`` scopes the position to the branch that is trading: a branch disposes
        the cash *it* holds, so its carrying rate is its own average, never the group's.
        """
        return (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT COALESCE(SUM(l.debit - l.credit), 0)
                                  AS functional_balance,
                               COALESCE(
                                   SUM(CASE WHEN l.debit > 0
                                            THEN l.foreign_amount
                                            ELSE -l.foreign_amount END),
                                   0
                               ) AS foreign_quantity
                          FROM journal_lines l
                          JOIN journal_entries e ON e.id = l.journal_entry_id
                         WHERE l.account_id = :account_id
                           AND (CAST(:branch_id AS UUID) IS NULL
                                OR e.branch_id = CAST(:branch_id AS UUID))
                        """
                    ),
                    {"account_id": account_id, "branch_id": branch_id},
                )
            )
            .mappings()
            .one()
        )

    async def balance_cache(self, *, account_id: uuid.UUID) -> Sequence[RowMapping]:
        """The cached totals for one account (used to prove the cache equals the ledger)."""
        result = await self._session.execute(
            text(
                """
                SELECT currency_id, debit_total, credit_total, updated_at
                  FROM account_balances
                 WHERE account_id = :account_id
                 ORDER BY currency_id
                """
            ),
            {"account_id": account_id},
        )
        return list(result.mappings().all())


def _entry_select() -> Select[Any]:
    """The shared entry projection: branch/user codes, reversal link and line totals.

    The joins are all 1:1 (a branch has one row, an entry has at most one reversal
    because ``ux_journal_entries_reversed_once`` says so, and one creator), so summing the
    lines here cannot multiply a total.
    """
    reversal = aliased(JournalEntry)
    return (
        select(
            JournalEntry.id,
            JournalEntry.reference_type,
            JournalEntry.reference_id,
            JournalEntry.description,
            JournalEntry.transaction_date,
            JournalEntry.created_at,
            JournalEntry.created_by,
            JournalEntry.branch_id,
            JournalEntry.device_id,
            JournalEntry.reversal_of_id,
            Branch.code.label("branch_code"),
            User.username.label("created_by_username"),
            reversal.id.label("reversed_by_entry_id"),
            func.count(JournalLine.id).label("line_count"),
            func.coalesce(func.sum(JournalLine.debit), 0).label("total_debit"),
            func.coalesce(func.sum(JournalLine.credit), 0).label("total_credit"),
        )
        .select_from(JournalEntry)
        .outerjoin(Branch, Branch.id == JournalEntry.branch_id)
        .outerjoin(User, User.id == JournalEntry.created_by)
        .outerjoin(reversal, reversal.reversal_of_id == JournalEntry.id)
        .outerjoin(JournalLine, JournalLine.journal_entry_id == JournalEntry.id)
        .group_by(
            JournalEntry.id,
            Branch.code,
            User.username,
            reversal.id,
        )
    )
