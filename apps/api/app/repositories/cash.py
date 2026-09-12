"""Cash reads and writes (Phase 6, ``API_CONTRACT.md`` §9.4).

Everything SQL about ``cash_sessions``, ``cash_session_lines`` and ``cash_movements`` lives
here, so the service above it reads like the business rules it implements: lock a drawer's
day, open a shift, record what came in and went out, reconcile a count against the
movements that actually happened, and read the position back.

Two boundaries this module keeps:

* **It never writes the ledger.** A cash movement's journal entry is posted by
  :class:`app.services.accounting_service.AccountingService`; the rows written here are the
  session, its reconciliation lines and the reads the service needs. The physical movement
  rows themselves are written by the accounting service too (``record_cash_movements``),
  so an entry and the movements it belongs to are always written by the same code, in the
  same transaction, for the same reference.
* **It never deletes or mutates history.** ``cash_movements`` is append-only (the database
  refuses UPDATE and DELETE outright); a session row moves ``OPEN`` -> ``CLOSED`` once, and
  its reconciliation lines are written at close. A correction is a new movement.

Every list query is built from one filter fragment (:data:`_SESSION_FILTERS`,
:data:`_MOVEMENT_FILTERS`) that the page query and its ``COUNT(*)`` share, so a page and its
total can never be answering different questions. Branch scope is an expanding ``IN`` list:
``None`` means "every branch the caller may see" and an empty list means "none", which is
the same convention the ledger and the exchange book use.

The position reads come from the two places the accounting model names as authoritative:
``v_cash_position`` (the physical position, built from immutable movements) and the journal
itself (the value and the quantity the books carry for the branch's cash accounts, in the
chart's 1000-1099 inventory band — ``accounting_service.CASH_ACCOUNT_CODE_PATTERN``).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.cash import CashMovement, CashSession, CashSessionLine

# The document a standalone cash movement belongs to. ``OPENING_BALANCE`` is the ledger's
# own reference type for a shift opening (§6.1) and ``CASH_MOVEMENT`` for in/out/adjustment
# (§6.4); both are written by ``AccountingService.post_cash_movement``.
REFERENCE_TYPE_CASH = "CASH_MOVEMENT"
REFERENCE_TYPE_OPENING = "OPENING_BALANCE"
REFERENCE_TYPE_REVERSAL = "REVERSAL"
# The documents a single movement can be reversed on its own. A shift opening belongs to its
# shift, and an exchange deal is cancelled or reversed as a whole; reversing one of their
# legs separately would leave the document half-undone.
REVERSIBLE_REFERENCE_TYPES = (REFERENCE_TYPE_CASH,)

STATUS_OPEN = "OPEN"
STATUS_CLOSED = "CLOSED"

# Statement prefixes. Each is a **pure literal** — columns, joins and the ``WHERE``
# keyword — so a query is composed from named constants plus bound parameters and never
# interpolates SQL text: the parts a caller chooses (filters, caller scope, order) are
# constants defined below, and every value travels as a bind parameter.
_SESSION_SELECT = """SELECT
        s.id, s.branch_id, b.code AS branch_code, b.name AS branch_name,
        b.timezone AS branch_timezone, b.is_active AS branch_is_active,
        s.device_id, d.device_uuid, d.device_name, d.platform,
        s.opened_by, opener.username AS opened_by_username,
        s.opened_at, s.closed_by, closer.username AS closed_by_username,
        s.closed_at, s.status, s.notes
        FROM cash_sessions s
        JOIN branches b ON b.id = s.branch_id
        LEFT JOIN devices d ON d.id = s.device_id
        LEFT JOIN users opener ON opener.id = s.opened_by
        LEFT JOIN users closer ON closer.id = s.closed_by
        WHERE """

_SESSION_COUNT = """SELECT COUNT(*) AS count FROM cash_sessions s WHERE """

_LINE_SELECT = """SELECT
        l.id, l.cash_session_id, l.currency_id, c.code AS currency_code,
        c.decimal_places AS currency_decimal_places, c.name AS currency_name,
        l.opening_declared, l.expected_amount, l.counted_amount, l.difference
        FROM cash_session_lines l
        JOIN currencies c ON c.id = l.currency_id
        WHERE """

_MOVEMENT_SELECT = """SELECT
        m.id, m.branch_id, b.code AS branch_code,
        m.account_id, a.code AS account_code,
        m.currency_id, c.code AS currency_code, c.decimal_places AS currency_decimal_places,
        m.movement_type, m.amount, m.signed_amount, m.adjustment_sign,
        m.reference_type, m.reference_id, m.description,
        m.created_by, u.username AS created_by_username, m.created_at,
        m.cash_session_id, s.status AS session_status,
        m.device_id, m.journal_entry_id, m.client_event_id
        FROM cash_movements m
        JOIN branches b ON b.id = m.branch_id
        JOIN accounts a ON a.id = m.account_id
        JOIN currencies c ON c.id = m.currency_id
        LEFT JOIN users u ON u.id = m.created_by
        LEFT JOIN cash_sessions s ON s.id = m.cash_session_id
        WHERE """

_MOVEMENT_COUNT = """SELECT COUNT(*) AS count FROM cash_movements m WHERE """

_POSITION_SELECT = """SELECT v.branch_id, v.branch_code, v.currency_id, v.currency_code,
       v.balance, v.last_movement_at
  FROM v_cash_position v
 WHERE """

_POSITION_TAIL = """
 ORDER BY v.branch_code, v.currency_code
"""

_LEDGER_POSITION_SELECT = """SELECT e.branch_id,
       l.currency_id,
       COALESCE(SUM(l.debit - l.credit), 0) AS functional_balance,
       COALESCE(SUM(CASE WHEN l.debit > 0
                         THEN l.foreign_amount
                         ELSE -l.foreign_amount END), 0) AS quantity
  FROM journal_lines l
  JOIN journal_entries e ON e.id = l.journal_entry_id
  JOIN accounts a ON a.id = l.account_id
 WHERE a.account_type = 'ASSET'
   AND a.is_active
   AND a.is_postable
   AND (a.branch_id = e.branch_id
        OR (a.branch_id IS NULL AND a.code ~ :band_pattern))
   AND """

_LEDGER_POSITION_TAIL = """
 GROUP BY e.branch_id, l.currency_id
"""

# One filter fragment shared by the page query and its count. ``currency_id``,
# ``movement_type``, ``session_id`` and the window are optional; the branch list is added by
# the caller from its scope, either as "any branch" (``branch_ids=None``) or as an expanding
# ``IN`` list.
_MOVEMENT_FILTERS = """
        (CAST(:currency_id AS UUID) IS NULL OR m.currency_id = CAST(:currency_id AS UUID))
    AND (CAST(:movement_type AS TEXT) IS NULL
         OR m.movement_type = CAST(:movement_type AS TEXT))
    AND (CAST(:session_id AS UUID) IS NULL
         OR m.cash_session_id = CAST(:session_id AS UUID))
    AND (CAST(:from_at AS TIMESTAMPTZ) IS NULL
         OR m.created_at >= CAST(:from_at AS TIMESTAMPTZ))
    AND (CAST(:to_at AS TIMESTAMPTZ) IS NULL OR m.created_at < CAST(:to_at AS TIMESTAMPTZ))
"""

_MOVEMENT_ORDER = "ORDER BY m.created_at DESC, m.id DESC"

_SESSION_COLUMNS = """
        s.id, s.branch_id, b.code AS branch_code, b.name AS branch_name,
        b.timezone AS branch_timezone, b.is_active AS branch_is_active,
        s.device_id, d.device_uuid, d.device_name, d.platform,
        s.opened_by, opener.username AS opened_by_username,
        s.opened_at, s.closed_by, closer.username AS closed_by_username,
        s.closed_at, s.status, s.notes
"""

_SESSION_JOINS = """
        JOIN branches b ON b.id = s.branch_id
        LEFT JOIN devices d ON d.id = s.device_id
        LEFT JOIN users opener ON opener.id = s.opened_by
        LEFT JOIN users closer ON closer.id = s.closed_by
"""

_SESSION_FILTERS = """
        (CAST(:status AS TEXT) IS NULL OR s.status = CAST(:status AS TEXT))
    AND (CAST(:device_id AS UUID) IS NULL OR s.device_id = CAST(:device_id AS UUID))
    AND (CAST(:opened_by AS UUID) IS NULL OR s.opened_by = CAST(:opened_by AS UUID))
    AND (CAST(:from_at AS TIMESTAMPTZ) IS NULL
         OR s.opened_at >= CAST(:from_at AS TIMESTAMPTZ))
    AND (CAST(:to_at AS TIMESTAMPTZ) IS NULL OR s.opened_at < CAST(:to_at AS TIMESTAMPTZ))
"""

_SESSION_ORDER = "ORDER BY s.opened_at DESC, s.id DESC"

_LINE_COLUMNS = """
        l.id, l.cash_session_id, l.currency_id, c.code AS currency_code,
        c.decimal_places AS currency_decimal_places, c.name AS currency_name,
        l.opening_declared, l.expected_amount, l.counted_amount, l.difference
"""

_LINE_JOINS = """
        JOIN currencies c ON c.id = l.currency_id
"""

_EXPANDING_BRANCHES: Any = bindparam("branch_ids", expanding=True)


def _branch_clause(column: str, branch_ids: Sequence[uuid.UUID] | None) -> str:
    """The scope predicate for a list query (``None`` = every branch)."""
    if branch_ids is None:
        return "TRUE"
    return f"{column} IN :branch_ids"


class CashSessionRepository:
    """The shift store: sessions, their reconciliation lines and their locks."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ------------------------------------------------------------------- locks
    async def lock_branch(self, branch_id: uuid.UUID) -> Branch | None:
        """Lock the branch row for the duration of a session open.

        The frozen schema enforces *one open session per device* with a partial unique index,
        but the comment beside that index also promises "one per branch while no device is
        bound" - and a session without a device has no index to enforce it. Opening therefore
        serialises on the branch row itself: two simultaneous opens of a device-less shift
        queue here, and the second one reads the first one's committed row instead of racing
        it into two open shifts (the same discipline the ledger uses on accounts).
        """
        return (
            await self._session.execute(
                select(Branch).where(Branch.id == branch_id).with_for_update()
            )
        ).scalar_one_or_none()

    async def get(self, session_id: uuid.UUID, *, for_update: bool = False) -> CashSession | None:
        """One session, optionally locked.

        Every mutation of a shift takes this lock first: a cash movement, a close, and a
        second close of the same drawer queue here, so a movement can never land in a shift
        that is being closed at the same instant.
        """
        statement = select(CashSession).where(CashSession.id == session_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def find_open(
        self, *, branch_id: uuid.UUID, device_id: uuid.UUID | None
    ) -> CashSession | None:
        """The open shift of one drawer: its device when it has one, its branch otherwise."""
        device_clause = (
            CashSession.device_id.is_(None)
            if device_id is None
            else CashSession.device_id == device_id
        )
        statement = (
            select(CashSession)
            .where(
                CashSession.branch_id == branch_id,
                CashSession.status == STATUS_OPEN,
                device_clause,
            )
            .order_by(CashSession.opened_at.desc(), CashSession.id.desc())
            .limit(1)
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def open_sessions(self, *, branch_id: uuid.UUID) -> Sequence[CashSession]:
        """Every open shift at a branch, locked (used to explain an ``ALREADY_OPEN`` refusal)."""
        rows = await self._session.execute(
            select(CashSession)
            .where(CashSession.branch_id == branch_id, CashSession.status == STATUS_OPEN)
            .order_by(CashSession.opened_at)
            .with_for_update()
        )
        return rows.scalars().all()

    # ------------------------------------------------------------------ writes
    def add(self, session: CashSession) -> None:
        self._session.add(session)

    def add_line(self, line: CashSessionLine) -> None:
        self._session.add(line)

    async def flush(self) -> None:
        await self._session.flush()

    # ------------------------------------------------------------------- reads
    async def row(self, session_id: uuid.UUID) -> dict[str, Any] | None:
        """The session header as a plain row (one statement, joined to its codes)."""
        statement = text(f"{_SESSION_SELECT}s.id = :session_id")
        row = (
            (await self._session.execute(statement, {"session_id": session_id}))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    async def rows(self, session_ids: Sequence[uuid.UUID]) -> list[dict[str, Any]]:
        """Several session headers in one statement (no N+1 on a list page)."""
        if not session_ids:
            return []
        statement = text(f"{_SESSION_SELECT}s.id IN :session_ids").bindparams(
            bindparam("session_ids", expanding=True)
        )
        rows = (
            (await self._session.execute(statement, {"session_ids": list(session_ids)}))
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def lines(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """The reconciliation lines of one session, in chart order."""
        statement = text(f"{_LINE_SELECT}l.cash_session_id = :session_id ORDER BY c.code")
        rows = (await self._session.execute(statement, {"session_id": session_id})).mappings().all()
        return [dict(row) for row in rows]

    async def lines_for(
        self, session_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[dict[str, Any]]]:
        """The lines of many sessions, keyed by session (one statement for a whole page)."""
        if not session_ids:
            return {}
        statement = text(
            f"{_LINE_SELECT}l.cash_session_id IN :session_ids ORDER BY c.code"
        ).bindparams(bindparam("session_ids", expanding=True))
        rows = (
            (await self._session.execute(statement, {"session_ids": list(session_ids)}))
            .mappings()
            .all()
        )
        grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for row in rows:
            item = dict(row)
            grouped.setdefault(item["cash_session_id"], []).append(item)
        return grouped

    async def line_for_update(
        self, *, session_id: uuid.UUID, currency_id: uuid.UUID
    ) -> CashSessionLine | None:
        """One reconciliation line, locked (the session lock already serialises closes)."""
        statement = (
            select(CashSessionLine)
            .where(
                CashSessionLine.cash_session_id == session_id,
                CashSessionLine.currency_id == currency_id,
            )
            .with_for_update()
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def line_for(
        self, *, session_id: uuid.UUID, currency_id: uuid.UUID
    ) -> CashSessionLine | None:
        statement = select(CashSessionLine).where(
            CashSessionLine.cash_session_id == session_id,
            CashSessionLine.currency_id == currency_id,
        )
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_sessions(
        self,
        *,
        branch_ids: Sequence[uuid.UUID] | None,
        status: str | None = None,
        device_id: uuid.UUID | None = None,
        opened_by: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of shift history, newest first, with the total for the same filter."""
        if branch_ids is not None and not branch_ids:
            return [], 0
        params: dict[str, Any] = {
            "status": status,
            "device_id": device_id,
            "opened_by": opened_by,
            "from_at": from_,
            "to_at": to,
            "limit": limit,
            "offset": offset,
        }
        scope = _branch_clause("s.branch_id", branch_ids)
        if branch_ids is not None:
            params["branch_ids"] = list(branch_ids)
        page = text(
            f"{_SESSION_SELECT}{_SESSION_FILTERS} AND {scope} {_SESSION_ORDER} "
            f"LIMIT :limit OFFSET :offset"
        )
        counted = text(f"{_SESSION_COUNT}{_SESSION_FILTERS} AND {scope}")
        if branch_ids is not None:
            page = page.bindparams(_EXPANDING_BRANCHES)
            counted = counted.bindparams(_EXPANDING_BRANCHES)
        total = int((await self._session.execute(counted, params)).scalar_one())
        if total == 0:
            return [], 0
        rows = (await self._session.execute(page, params)).mappings().all()
        return [dict(row) for row in rows], total

    async def movement_totals(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Per currency: what the shift's movements did, split by type.

        This is the raw material of the expected balance and of the shift summary: the
        money a drawer *should* hold is what went in minus what went out, and both come
        from the immutable movement rows rather than from a cached total.
        """
        statement = text(
            """
            SELECT m.currency_id,
                   c.code AS currency_code,
                   COALESCE(SUM(m.signed_amount), 0) AS movement_sum,
                   COALESCE(SUM(m.signed_amount)
                            FILTER (WHERE m.movement_type = 'OPENING'), 0) AS opening_sum,
                   COALESCE(SUM(m.signed_amount)
                            FILTER (WHERE m.movement_type IN ('IN', 'EXPENSE', 'ADJUSTMENT')
                                    AND m.signed_amount > 0), 0) AS inflow,
                   COALESCE(SUM(m.signed_amount)
                            FILTER (WHERE m.movement_type = 'OUT'
                                    OR (m.movement_type IN ('EXPENSE', 'ADJUSTMENT')
                                        AND m.signed_amount < 0)), 0) AS outflow,
                   COUNT(*) FILTER (WHERE m.movement_type = 'IN') AS count_in,
                   COUNT(*) FILTER (WHERE m.movement_type = 'OUT') AS count_out,
                   COUNT(*) FILTER (WHERE m.movement_type = 'ADJUSTMENT') AS count_adjustment,
                   COUNT(*) FILTER (WHERE m.movement_type = 'OPENING') AS count_opening
              FROM cash_movements m
              JOIN currencies c ON c.id = m.currency_id
             WHERE m.cash_session_id = :session_id
             GROUP BY m.currency_id, c.code
             ORDER BY c.code
            """
        )
        rows = (await self._session.execute(statement, {"session_id": session_id})).mappings().all()
        return [dict(row) for row in rows]

    async def position_rows(
        self, *, branch_ids: Sequence[uuid.UUID] | None
    ) -> list[dict[str, Any]]:
        """The physical position per branch and currency (``v_cash_position``)."""
        if branch_ids is not None and not branch_ids:
            return []
        scope = _branch_clause("v.branch_id", branch_ids)
        statement = text(f"{_POSITION_SELECT}{scope}{_POSITION_TAIL}")
        params: dict[str, Any] = {}
        if branch_ids is not None:
            statement = statement.bindparams(_EXPANDING_BRANCHES)
            params["branch_ids"] = list(branch_ids)
        rows = (await self._session.execute(statement, params)).mappings().all()
        return [dict(row) for row in rows]

    async def ledger_position_rows(
        self, *, branch_ids: Sequence[uuid.UUID] | None, band_pattern: str
    ) -> list[dict[str, Any]]:
        """What the **ledger** carries for the branch's cash accounts, per currency.

        Read from ``journal_lines`` (the immutable entries), not from a balance cache — the
        accounting model's rule (§8): a report tells a reader what the books say, and the
        books are the lines. "The branch's cash accounts" is the same question
        ``AccountingService.inventory_account`` answers, in the same order: the branch's
        **own** asset accounts first (§10: a branch never spends another branch's cash), then
        the group-wide accounts in the chart's 1000-1099 inventory band. A group-wide asset
        account *outside* the band is not a drawer — ``1100`` is cash in transit and ``1200``
        is a receivable, and counting either as a till would make the position meaningless.
        """
        if branch_ids is not None and not branch_ids:
            return []
        scope = _branch_clause("e.branch_id", branch_ids)
        statement = text(f"{_LEDGER_POSITION_SELECT}{scope}{_LEDGER_POSITION_TAIL}")
        params: dict[str, Any] = {"band_pattern": band_pattern}
        if branch_ids is not None:
            statement = statement.bindparams(_EXPANDING_BRANCHES)
            params["branch_ids"] = list(branch_ids)
        rows = (await self._session.execute(statement, params)).mappings().all()
        return [dict(row) for row in rows]

    async def ledger_account_positions(
        self, *, branch_id: uuid.UUID, account_ids: Sequence[uuid.UUID]
    ) -> dict[str, Any]:
        """What the ledger carries in **named** accounts of one branch.

        :meth:`ledger_position_rows` answers a report's question ("what does this branch's
        cash add up to"); this answers the control question ("does the drawer this movement
        will post to carry what we think it carries"). The two differ whenever a chart has
        more than one candidate account for a currency, which is exactly when the difference
        must not be papered over.
        """
        if not account_ids:
            return {"functional_balance": 0, "quantity": 0}
        statement = text(
            """
            SELECT COALESCE(SUM(l.debit - l.credit), 0) AS functional_balance,
                   COALESCE(SUM(CASE WHEN l.debit > 0
                                     THEN l.foreign_amount
                                     ELSE -l.foreign_amount END), 0) AS quantity
              FROM journal_lines l
              JOIN journal_entries e ON e.id = l.journal_entry_id
             WHERE e.branch_id = :branch_id
               AND l.account_id = ANY(:account_ids)
            """
        )
        row = (
            (
                await self._session.execute(
                    statement,
                    {"branch_id": branch_id, "account_ids": list(account_ids)},
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


class CashMovementRepository:
    """Reads and the (few) writes of the physical movement table."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    def add(self, movement: CashMovement) -> None:
        self._session.add(movement)

    async def row(self, movement_id: uuid.UUID) -> dict[str, Any] | None:
        statement = text(f"{_MOVEMENT_SELECT}m.id = :movement_id")
        row = (
            (await self._session.execute(statement, {"movement_id": movement_id}))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    async def lock(self, movement_id: uuid.UUID) -> None:
        """Serialise two corrections aimed at the same movement.

        ``cash_movements`` rows are immutable, so this lock protects no update: it makes
        "has this movement already been reversed?" a question two concurrent callers cannot
        both answer from a snapshot that predates the other's commit.
        """
        await self._session.execute(
            text("SELECT id FROM cash_movements WHERE id = :movement_id FOR UPDATE"),
            {"movement_id": movement_id},
        )

    async def reversal_of(self, movement_id: uuid.UUID) -> dict[str, Any] | None:
        """The compensating movement written for one movement, if it exists."""
        statement = text(
            f"{_MOVEMENT_SELECT}m.reference_type = :kind AND m.reference_id = :movement_id"
        )
        row = (
            (
                await self._session.execute(
                    statement,
                    {"kind": REFERENCE_TYPE_REVERSAL, "movement_id": movement_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    async def movements_of(
        self, *, reference_type: str, reference_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """Every movement written for one document reference, in a stable order."""
        statement = text(
            f"{_MOVEMENT_SELECT}m.reference_type = :reference_type "
            f"AND m.reference_id = :reference_id ORDER BY m.movement_type, m.id"
        )
        rows = (
            (
                await self._session.execute(
                    statement,
                    {"reference_type": reference_type, "reference_id": reference_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def by_client_event(self, client_event_id: uuid.UUID) -> dict[str, Any] | None:
        """The movement a device's ``client_event_id`` already produced (offline replay).

        The frozen schema makes the event id unique across the table, so this is the one place
        a replayed offline receipt can be recognised before the money moves twice.
        """
        statement = text(f"{_MOVEMENT_SELECT}m.client_event_id = :client_event_id")
        row = (
            (await self._session.execute(statement, {"client_event_id": client_event_id}))
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    async def session_movements(self, session_id: uuid.UUID) -> list[dict[str, Any]]:
        """Every movement a shift recorded, oldest first (its own history)."""
        statement = text(
            f"{_MOVEMENT_SELECT}m.cash_session_id = :session_id ORDER BY m.created_at, m.id"
        )
        rows = (await self._session.execute(statement, {"session_id": session_id})).mappings().all()
        return [dict(row) for row in rows]

    async def adjustment_entries(self, session_ids: Sequence[uuid.UUID]) -> list[dict[str, Any]]:
        """Per session and currency: the journal entry each adjustment posted, if any.

        The contract's close response names an ``adjustment_journal_entry_id`` per line. The
        frozen schema stores that link on the *movement* (``journal_entry_id``), so this is a
        read, not a column: the evidence stays in one place.
        """
        if not session_ids:
            return []
        statement = text(
            """
            SELECT m.cash_session_id, m.currency_id, m.journal_entry_id, m.created_at
              FROM cash_movements m
             WHERE m.cash_session_id IN :session_ids
               AND m.movement_type = 'ADJUSTMENT'
               AND m.journal_entry_id IS NOT NULL
             ORDER BY m.created_at
            """
        ).bindparams(bindparam("session_ids", expanding=True))
        rows = (
            (await self._session.execute(statement, {"session_ids": list(session_ids)}))
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def entry_lines(self, entry_id: uuid.UUID) -> list[dict[str, Any]]:
        """The lines of a journal entry, in the ledger's canonical order.

        Used to mirror a correcting entry: the reversal has to post the accounts the
        original posted. A journal line carries **no timestamp of its own** (the frozen
        ``journal_lines`` table has no ``created_at``), so the order is content, exactly as
        ``JournalEntryRepository.lines_of`` defines it — ``(account, currency, debit,
        credit)`` — rather than an insertion order the schema never recorded.
        """
        statement = text(
            """
            SELECT l.id, l.account_id, l.currency_id, l.debit, l.credit, l.foreign_amount,
                   l.exchange_rate
              FROM journal_lines l
             WHERE l.journal_entry_id = :entry_id
             ORDER BY l.account_id, l.currency_id, l.debit DESC, l.credit DESC
            """
        )
        rows = (await self._session.execute(statement, {"entry_id": entry_id})).mappings().all()
        return [dict(row) for row in rows]

    async def list_movements(
        self,
        *,
        branch_ids: Sequence[uuid.UUID] | None,
        currency_id: uuid.UUID | None = None,
        movement_type: str | None = None,
        session_id: uuid.UUID | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of the movement book, newest first, with the total for the same filter."""
        if branch_ids is not None and not branch_ids:
            return [], 0
        params: dict[str, Any] = {
            "currency_id": currency_id,
            "movement_type": movement_type,
            "session_id": session_id,
            "from_at": from_,
            "to_at": to,
            "limit": limit,
            "offset": offset,
        }
        scope = _branch_clause("m.branch_id", branch_ids)
        if branch_ids is not None:
            params["branch_ids"] = list(branch_ids)
        page = text(
            f"{_MOVEMENT_SELECT}{_MOVEMENT_FILTERS} AND {scope} {_MOVEMENT_ORDER} "
            f"LIMIT :limit OFFSET :offset"
        )
        counted = text(f"{_MOVEMENT_COUNT}{_MOVEMENT_FILTERS} AND {scope}")
        if branch_ids is not None:
            page = page.bindparams(_EXPANDING_BRANCHES)
            counted = counted.bindparams(_EXPANDING_BRANCHES)
        total = int((await self._session.execute(counted, params)).scalar_one())
        if total == 0:
            return [], 0
        rows = (await self._session.execute(page, params)).mappings().all()
        return [dict(row) for row in rows], total
