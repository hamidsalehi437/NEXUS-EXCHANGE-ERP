"""Currency, branch and customer queries (PART 9, PART 10).

The three entities are read together because they share one shape: a unique business
``code``, an ``is_active`` lifecycle flag, and a "may this still be used?" question that
the services ask before every write. Keeping the lookups here means the answer is
computed by exactly one SQL statement per question.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.branch import Branch
from app.models.currency import Currency
from app.models.customer import Customer


def _paginate(statement: Select[Any], *, limit: int, offset: int) -> Select[Any]:
    """Apply the contract's deterministic ordering and window to a list statement.

    Typed as ``Select[Any]``: the three callers select different models, and the only thing
    this helper does is add ``LIMIT``/``OFFSET`` — a Pep 695 generic parameter would say the
    same thing while making the module unparsable for the 3.11 interpreter the local
    verification environment has to run on.
    """
    return statement.limit(limit).offset(offset)


class CurrencyRepository:
    """Reads and writes for ``currencies``."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, currency_id: uuid.UUID, *, for_update: bool = False) -> Currency | None:
        statement = select(Currency).where(Currency.id == currency_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_code(self, code: str) -> Currency | None:
        statement = select(Currency).where(Currency.code == code.strip().upper())
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_currencies(
        self,
        *,
        is_active: bool | None = None,
        is_tradable: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[Currency], int]:
        """Currencies in display order (then code), so the counter's pickers are stable."""
        statement = select(Currency).order_by(Currency.display_order, Currency.code)
        if is_active is not None:
            statement = statement.where(Currency.is_active.is_(is_active))
        if is_tradable is not None:
            statement = statement.where(Currency.is_tradable.is_(is_tradable))
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(_paginate(statement, limit=limit, offset=offset))
        return list(rows.scalars().all()), int(total or 0)

    async def base_currency(self) -> Currency | None:
        """The single row with ``is_base`` (a partial unique index guarantees at most one)."""
        statement = select(Currency).where(Currency.is_base.is_(True))
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def code_exists(self, code: str) -> bool:
        statement = select(func.count()).select_from(Currency).where(Currency.code == code)
        return bool(await self._session.scalar(statement))

    def add(self, currency: Currency) -> None:
        self._session.add(currency)

    async def flush(self) -> None:
        """Send pending INSERT/UPDATE statements so same-transaction reads see them."""
        await self._session.flush()


class BranchRepository:
    """Reads and writes for ``branches`` plus the "does this branch have history?" probe."""

    # Tables that make a branch part of the financial record. A branch with rows in any
    # of them may not change its code: the code appears on printed documents and in
    # reconciled statements, so renaming it would silently rewrite history (PART 22).
    _ACTIVITY_TABLES: tuple[tuple[str, str], ...] = (
        ("exchange_transactions", "branch_id"),
        ("transfers", "branch_id"),
        ("cash_sessions", "branch_id"),
        ("journal_entries", "branch_id"),
    )

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, branch_id: uuid.UUID, *, for_update: bool = False) -> Branch | None:
        statement = select(Branch).where(Branch.id == branch_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_code(self, code: str) -> Branch | None:
        statement = select(Branch).where(Branch.code == code.strip().upper())
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_branches(
        self,
        *,
        is_active: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[Branch], int]:
        statement = select(Branch).order_by(Branch.code)
        if is_active is not None:
            statement = statement.where(Branch.is_active.is_(is_active))
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(_paginate(statement, limit=limit, offset=offset))
        return list(rows.scalars().all()), int(total or 0)

    async def code_exists(self, code: str) -> bool:
        statement = select(func.count()).select_from(Branch).where(Branch.code == code)
        return bool(await self._session.scalar(statement))

    async def active_count(self) -> int:
        statement = select(func.count()).select_from(Branch).where(Branch.is_active.is_(True))
        return int(await self._session.scalar(statement) or 0)

    async def has_financial_history(self, branch_id: uuid.UUID) -> bool:
        """True when any financial table already references this branch."""
        for table, column in self._ACTIVITY_TABLES:
            found = await self._session.scalar(
                text(f"SELECT 1 FROM {table} WHERE {column} = :branch_id LIMIT 1"),  # noqa: S608
                {"branch_id": branch_id},
            )
            if found is not None:
                return True
        return False

    async def device_count(self, branch_id: uuid.UUID) -> int:
        statement = text("SELECT count(*) FROM devices WHERE branch_id = :branch_id")
        return int(await self._session.scalar(statement, {"branch_id": branch_id}) or 0)

    def add(self, branch: Branch) -> None:
        self._session.add(branch)

    async def flush(self) -> None:
        await self._session.flush()


class CustomerRepository:
    """Reads and writes for ``customers``, including code generation (PART 10)."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(self, customer_id: uuid.UUID, *, for_update: bool = False) -> Customer | None:
        statement = select(Customer).where(Customer.id == customer_id)
        if for_update:
            statement = statement.with_for_update()
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def get_by_code(self, customer_code: str) -> Customer | None:
        statement = select(Customer).where(Customer.customer_code == customer_code.strip().upper())
        return (await self._session.execute(statement)).scalar_one_or_none()

    async def list_customers(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        branch_id_is_null: bool = False,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Customer], int]:
        """Search by name, phone or code; newest first so a fresh registration is on top.

        ``branch_id_is_null`` selects the customers shared across branches when the caller
        asks for ``branch_id=null`` explicitly, which is different from "no filter".
        """
        statement = select(Customer).order_by(Customer.created_at.desc(), Customer.customer_code)
        if branch_id is not None:
            statement = statement.where(Customer.branch_id == branch_id)
        elif branch_id_is_null:
            statement = statement.where(Customer.branch_id.is_(None))
        if is_active is not None:
            statement = statement.where(Customer.is_active.is_(is_active))
        if search:
            pattern = f"%{search.strip().lower()}%"
            statement = statement.where(
                or_(
                    func.lower(Customer.full_name).like(pattern),
                    func.lower(Customer.phone).like(pattern),
                    func.lower(Customer.customer_code).like(pattern),
                )
            )
        total = await self._session.scalar(select(func.count()).select_from(statement.subquery()))
        rows = await self._session.execute(_paginate(statement, limit=limit, offset=offset))
        return list(rows.scalars().all()), int(total or 0)

    async def code_exists(self, customer_code: str) -> bool:
        statement = (
            select(func.count())
            .select_from(Customer)
            .where(Customer.customer_code == customer_code)
        )
        return bool(await self._session.scalar(statement))

    async def next_customer_code(self, *, prefix: str, width: int, on: dt.datetime) -> str:
        """Issue the next ``PREFIX-YYYYMMDD-NNNNNN`` code atomically.

        ``next_document_number`` upserts the ``sequences`` row and returns its new value
        inside the caller's transaction, so two concurrent registrations can never receive
        the same code — and a rolled-back registration does not consume a number that a
        later document would reuse.
        """
        period = on.astimezone(dt.UTC).strftime("%Y%m%d")
        value = await self._session.scalar(
            text("SELECT next_document_number(:prefix, 'customer', :period, :width)"),
            {"prefix": prefix, "period": period, "width": width},
        )
        return str(value)

    def add(self, customer: Customer) -> None:
        self._session.add(customer)

    async def flush(self) -> None:
        await self._session.flush()
