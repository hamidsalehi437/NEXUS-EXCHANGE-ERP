"""Exchange document reads and writes (Phase 5, ``API_CONTRACT.md`` §9.3).

Everything SQL about ``exchange_transactions`` lives here so the service above it reads
like business rules: resolve a document, lock it for a lifecycle move, find one by the
offline event that produced it, page the branch's book, allocate the next document number
through the frozen Phase 0 function, and read back the revenue lines a posted document
produced.

Nothing in this module writes the ledger: a journal entry is posted by
:class:`app.services.accounting_service.AccountingService` (PART 46), and a cash movement is
recorded through it too. The document row is the only thing this repository inserts, and the
database's own triggers police the rest (``NEX06`` immutability, ``NEX03`` status machine,
``NEX04`` reversal mirroring).
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.exchange_transaction import ExchangeTransaction

# The document-number scope of an exchange transaction. The period is the branch's business
# date, so a counter that runs past midnight rolls into the next day's series instead of
# continuing yesterday's (``next_document_number``, Phase 0 schema).
NUMBER_PREFIX = "NX"
NUMBER_SCOPE = "exchange_transaction"

# Columns every read of a document needs, joined to the codes and names a receipt or a list
# row must show. The write path never depends on this projection.
_VIEW_COLUMNS = """
        e.id, e.transaction_number, e.status, e.transaction_type, e.origin,
        e.branch_id, e.device_id, e.cashier_id, e.customer_id,
        e.from_currency_id, e.from_amount, e.to_currency_id, e.to_amount,
        e.exchange_rate, e.commission, e.created_at, e.updated_at,
        e.reversal_of_id, e.reversal_reason, e.reversed_by, e.reversed_at,
        e.journal_entry_id, e.reversal_journal_entry_id, e.version, e.client_event_id,
        e.cash_session_id,
        b.code AS branch_code, b.name AS branch_name, b.address AS branch_address,
        b.phone AS branch_phone, b.timezone AS branch_timezone,
        u.username AS cashier_username,
        c.customer_code AS customer_code, c.full_name AS customer_name,
        fc.code AS from_currency_code, fc.decimal_places AS from_decimal_places,
        tc.code AS to_currency_code, tc.decimal_places AS to_decimal_places,
        rev.id AS reversal_transaction_id,
        rev.transaction_number AS reversal_transaction_number,
        rev.reversal_reason AS reversal_document_reason,
        rex.description AS reversal_entry_description,
        (SELECT COALESCE(SUM(l.debit), 0) FROM journal_lines l
          WHERE l.journal_entry_id = e.journal_entry_id) AS journal_total_debit,
        (SELECT COALESCE(SUM(l.credit), 0) FROM journal_lines l
          WHERE l.journal_entry_id = e.journal_entry_id) AS journal_total_credit
"""

_VIEW_JOINS = """
        JOIN branches b ON b.id = e.branch_id
        JOIN users u ON u.id = e.cashier_id
        JOIN currencies fc ON fc.id = e.from_currency_id
        JOIN currencies tc ON tc.id = e.to_currency_id
        LEFT JOIN customers c ON c.id = e.customer_id
        LEFT JOIN exchange_transactions rev ON rev.reversal_of_id = e.id
        LEFT JOIN journal_entries rex ON rex.id = e.reversal_journal_entry_id
"""

# One filter fragment, used by both the page query and the count query of a list call: a
# second copy of the conditions is how a page and its total start disagreeing.
_VIEW_FILTERS = """
        (CAST(:cashier_id AS UUID) IS NULL OR e.cashier_id = CAST(:cashier_id AS UUID))
    AND (CAST(:customer_id AS UUID) IS NULL OR e.customer_id = CAST(:customer_id AS UUID))
    AND (CAST(:transaction_type AS TEXT) IS NULL
         OR e.transaction_type = CAST(:transaction_type AS TEXT))
    AND (CAST(:status AS TEXT) IS NULL OR e.status = CAST(:status AS TEXT))
    AND (CAST(:number_query AS TEXT) IS NULL
         OR e.transaction_number ILIKE CAST(:number_query AS TEXT))
    AND (CAST(:from_at AS TIMESTAMPTZ) IS NULL
         OR e.created_at >= CAST(:from_at AS TIMESTAMPTZ))
    AND (CAST(:to_at AS TIMESTAMPTZ) IS NULL OR e.created_at < CAST(:to_at AS TIMESTAMPTZ))
"""

_VIEW_ORDER = "ORDER BY e.created_at DESC, e.transaction_number DESC"

# The point-read query, assembled once from the fragments above. Kept as a named constant so
# the statement handed to the driver is a plain string: the only substitution is an ordinary
# bind parameter, and a reader can see that without untangling nested f-strings.
_VIEW_BY_ID = (
    f"SELECT {_VIEW_COLUMNS} FROM exchange_transactions e {_VIEW_JOINS} "  # noqa: S608
    "WHERE e.id = :transaction_id"
)


class ExchangeTransactionRepository:
    """The document store of the exchange engine."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    # ------------------------------------------------------------------- writes
    def add(self, transaction: ExchangeTransaction) -> None:
        self._session.add(transaction)

    async def flush(self) -> None:
        await self._session.flush()

    async def next_transaction_number(self, *, period: str) -> str:
        """``NX-YYYYMMDD-NNNNNN`` from the frozen atomic counter (concurrency-safe).

        The counter row is locked for the rest of the transaction, which is what lets two
        simultaneous counters receive two different numbers without an advisory lock or a
        retry loop. The period is the branch's **business** date, so the series follows the
        operator's working day rather than UTC.
        """
        result = await self._session.execute(
            text("SELECT next_document_number(:prefix, :scope, :period)"),
            {"prefix": NUMBER_PREFIX, "scope": NUMBER_SCOPE, "period": period},
        )
        return str(result.scalar_one())

    # -------------------------------------------------------------------- reads
    async def get(
        self, transaction_id: uuid.UUID, *, for_update: bool = False
    ) -> ExchangeTransaction | None:
        statement = select(ExchangeTransaction).where(ExchangeTransaction.id == transaction_id)
        if for_update:
            # A lifecycle move (cancel/reverse) is decided from the document's own state and
            # from whether a reversal already exists. Locking the row serialises two
            # simultaneous attempts, so the second one sees the first one's committed state
            # instead of both deciding from the same stale snapshot.
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def view(self, transaction_id: uuid.UUID) -> dict[str, Any] | None:
        """One document with every code and name a response needs (or ``None``)."""
        row = (
            (
                await self._session.execute(
                    text(_VIEW_BY_ID),
                    {"transaction_id": transaction_id},
                )
            )
            .mappings()
            .one_or_none()
        )
        return dict(row) if row is not None else None

    async def find_by_client_event(self, client_event_id: uuid.UUID) -> ExchangeTransaction | None:
        """The document an offline-origin event already produced (PART 34)."""
        return (
            await self._session.execute(
                select(ExchangeTransaction).where(
                    ExchangeTransaction.client_event_id == client_event_id
                )
            )
        ).scalar_one_or_none()

    async def reversal_of(self, original_id: uuid.UUID) -> ExchangeTransaction | None:
        """The mirror document that reverses ``original_id`` (unique by index)."""
        return (
            await self._session.execute(
                select(ExchangeTransaction).where(ExchangeTransaction.reversal_of_id == original_id)
            )
        ).scalar_one_or_none()

    async def list_exchanges(
        self,
        *,
        branch_ids: Sequence[uuid.UUID] | None,
        cashier_id: uuid.UUID | None = None,
        customer_id: uuid.UUID | None = None,
        transaction_type: str | None = None,
        status: str | None = None,
        number_query: str | None = None,
        from_: dt.datetime | None = None,
        to: dt.datetime | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """One page of the book, newest first, with the total for the same filter.

        ``branch_ids`` is the caller's scope: ``None`` means every branch (a group-wide
        role) and an empty sequence means none — the same convention the ledger uses for a
        read scope, so a caller cannot receive another branch's documents by omitting a
        parameter.
        """
        if branch_ids is not None and not branch_ids:
            return [], 0

        params: dict[str, Any] = {
            "cashier_id": cashier_id,
            "customer_id": customer_id,
            "transaction_type": transaction_type,
            "status": status,
            "number_query": f"%{number_query.strip()}%" if number_query else None,
            "from_at": from_,
            "to_at": to,
        }
        filters = _VIEW_FILTERS
        if branch_ids is not None:
            # One named placeholder per branch, so the scope arrives as ordinary binds and the
            # page and its total can never disagree about which branches they cover.
            names = [f"branch_{index}" for index in range(len(branch_ids))]
            for name, value in zip(names, branch_ids, strict=True):
                params[name] = value
            filters = f"e.branch_id IN ({', '.join(f':{name}' for name in names)}) AND " + filters

        # The interpolated parts are module constants and generated bind names only — every
        # operator value is bound — so the statements are parameterised by construction and the
        # annotation records that check for the next reader.
        rows_statement = text(
            f"SELECT {_VIEW_COLUMNS} FROM exchange_transactions e {_VIEW_JOINS} "  # noqa: S608
            f"WHERE {filters} {_VIEW_ORDER} LIMIT :limit OFFSET :offset"
        )
        total_statement = text(
            f"SELECT COUNT(*) AS count FROM exchange_transactions e WHERE {filters}"  # noqa: S608
        )

        total = int((await self._session.execute(total_statement, params)).scalar_one())
        if total == 0:
            return [], 0

        rows = (
            (
                await self._session.execute(
                    rows_statement, {**params, "limit": limit, "offset": offset}
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows], total

    async def cash_movements(
        self, *, reference_type: str, reference_id: uuid.UUID
    ) -> list[dict[str, Any]]:
        """The physical movements one document produced, in a fixed, readable order.

        A cancelled document has two sets — the movements of the deal and the movements of
        its reversal — and both belong to the document's history, which is why this reads by
        reference rather than by "the last one".

        The order is part of the answer: the deal's own movements first, then the ones that
        undid it (``reference_type`` ascending), and inside each group money **in** before
        money **out**. Ordering by ``created_at`` alone would not do it: every row written by
        one transaction carries the same ``now()`` (PostgreSQL's transaction timestamp), so
        the insertion order is not recoverable from the clock. ``id`` closes the ordering, so
        two reads of the same document always return the same list.
        """
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT m.id, m.movement_type, m.amount, m.signed_amount,
                               m.reference_type, m.reference_id,
                               m.currency_id, cur.code AS currency_code,
                               m.account_id, acc.code AS account_code,
                               m.journal_entry_id, m.created_at
                          FROM cash_movements m
                          JOIN accounts acc ON acc.id = m.account_id
                          JOIN currencies cur ON cur.id = m.currency_id
                         WHERE m.reference_type = :reference_type
                           AND m.reference_id = :reference_id
                         ORDER BY m.reference_type, (m.signed_amount < 0),
                                  m.created_at, m.id
                        """
                    ),
                    {"reference_type": reference_type, "reference_id": reference_id},
                )
            )
            .mappings()
            .all()
        )
        return [dict(row) for row in rows]

    async def cash_movements_by_document(
        self, document_ids: Sequence[uuid.UUID]
    ) -> dict[uuid.UUID, list[dict[str, Any]]]:
        """The movements of many documents at once, keyed by the document they belong to.

        A list endpoint that issued one query per row would be an N+1 against the table that
        records physical money; this reads the page's movements in one statement and groups
        them in Python. Both the deal's own movements and a cancellation's reversing movements
        are included, because both belong to the document's history.
        """
        if not document_ids:
            return {}
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT m.id, m.movement_type, m.amount, m.signed_amount,
                               m.reference_type, m.reference_id,
                               m.currency_id, cur.code AS currency_code,
                               m.account_id, acc.code AS account_code,
                               m.journal_entry_id, m.created_at
                          FROM cash_movements m
                          JOIN accounts acc ON acc.id = m.account_id
                          JOIN currencies cur ON cur.id = m.currency_id
                         WHERE m.reference_id = ANY(CAST(:document_ids AS UUID[]))
                           AND m.reference_type IN ('EXCHANGE_TRANSACTION', 'REVERSAL')
                         ORDER BY m.reference_type, (m.signed_amount < 0),
                                  m.created_at, m.id
                        """
                    ),
                    {"document_ids": list(document_ids)},
                )
            )
            .mappings()
            .all()
        )
        grouped: dict[uuid.UUID, list[dict[str, Any]]] = {}
        for row in rows:
            grouped.setdefault(uuid.UUID(str(row["reference_id"])), []).append(dict(row))
        return grouped

    async def open_cash_session_id(
        self, *, branch_id: uuid.UUID, device_id: uuid.UUID | None
    ) -> uuid.UUID | None:
        """The drawer shift this device is working in, when one is open (Phase 6 owns shifts).

        Phase 5 links the session that already exists so a later close reconciles the shift's
        own trades; it never *requires* one — a trade recorded offline, or at a counter that
        has not opened a shift, is still a real trade and must not be invented away. The
        newest open session wins, so the answer is deterministic even if a device somehow has
        two.
        """
        if device_id is None:
            return None
        row = (
            await self._session.execute(
                text(
                    """
                    SELECT id
                      FROM cash_sessions
                     WHERE branch_id = :branch_id
                       AND device_id = :device_id
                       AND status = 'OPEN'
                     ORDER BY opened_at DESC, id DESC
                     LIMIT 1
                    """
                ),
                {"branch_id": branch_id, "device_id": device_id},
            )
        ).scalar_one_or_none()
        return uuid.UUID(str(row)) if row is not None else None

    async def revenue_lines(self, journal_entry_id: uuid.UUID | None) -> list[dict[str, Any]]:
        """The revenue accounts a document's journal entry moved, with their amounts.

        ``account_type`` is a first-class column, so classifying "what this deal earned" needs
        no chart-code convention: every line of the entry that lands on a REVENUE account is
        part of the result (the FX result account and the commission income account in the
        seeded chart). The sign follows the account's normal side — a credit is income, a
        debit is a loss — which is what makes a losing exchange report a negative result.
        """
        if journal_entry_id is None:
            return []
        rows = (
            (
                await self._session.execute(
                    text(
                        """
                        SELECT a.code AS account_code, a.name AS account_name,
                               COALESCE(SUM(l.credit - l.debit), 0) AS amount
                          FROM journal_lines l
                          JOIN accounts a ON a.id = l.account_id
                         WHERE l.journal_entry_id = :entry AND a.account_type = 'REVENUE'
                         GROUP BY a.code, a.name
                         ORDER BY a.code
                        """
                    ),
                    {"entry": journal_entry_id},
                )
            )
            .mappings()
            .all()
        )
        return [
            {
                "account_code": str(row["account_code"]),
                "account_name": str(row["account_name"]),
                "amount": Decimal(str(row["amount"])),
            }
            for row in rows
        ]


__all__ = ["NUMBER_PREFIX", "NUMBER_SCOPE", "ExchangeTransactionRepository"]
