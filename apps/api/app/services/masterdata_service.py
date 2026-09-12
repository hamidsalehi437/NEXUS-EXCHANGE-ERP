"""Currencies, branches and customers — the master data every later phase depends on.

Rules this service owns, and why each one is here rather than in a route or a trigger:

* **One base currency.** The database has a partial unique index, but replacing the base
  is a two-row operation (clear the old, set the new) that must happen in one
  transaction with an audit row naming both sides. The service does that and refuses to
  remove the base designation while other currencies are still quoted against it.
* **Codes are identity.** ``currencies.code`` can never change. ``branches.code`` changes
  only while the branch has no financial history. Both rules are audited, and both are
  enforced *before* the write so the caller gets a contract error instead of a trigger
  exception.
* **Deactivation is not deletion.** A currency, branch or customer rows are never
  deleted (PART 25); they are deactivated, and the exit is refused when the entity is
  still referenced by something that must keep working (a base currency, the last active
  branch, a device still bound to it).
* **Customer codes are issued, not guessed.** ``CUS-YYYYMMDD-NNNNNN`` comes from the
  database sequence function inside the same transaction as the INSERT, so concurrent
  registrations cannot collide and a rolled-back request does not burn a code that a
  later document would then reuse.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    BranchInactiveError,
    ConflictError,
    DuplicateResourceError,
    ImmutableFieldError,
    ResourceNotFoundError,
    ValidationError,
)
from app.repositories.masterdata import BranchRepository, CurrencyRepository, CustomerRepository
from app.services.audit_service import ActorContext, AuditService

# Fields whose change must be visible in the audit trail's old/new payloads.
_CURRENCY_AUDITED_FIELDS = (
    "name",
    "symbol",
    "decimal_places",
    "is_base",
    "is_active",
    "is_tradable",
    "display_order",
)
_BRANCH_AUDITED_FIELDS = ("code", "name", "address", "phone", "is_active", "timezone")
_CUSTOMER_AUDITED_FIELDS = (
    "full_name",
    "phone",
    "address",
    "notes",
    "national_id_last4",
    "branch_id",
    "is_active",
)


def _diff(
    before: Mapping[str, Any], after: Mapping[str, Any], fields: Sequence[str]
) -> dict[str, Any]:
    """Return ``{field: {"old": …, "new": …}}`` for the fields that actually changed.

    Only real changes are recorded: an audit trail where every PATCH of an unchanged
    resource produces twelve identical entries is a trail nobody reads.
    """
    changed: dict[str, Any] = {}
    for field in fields:
        old_value = before.get(field)
        new_value = after.get(field)
        if isinstance(old_value, uuid.UUID) or isinstance(new_value, uuid.UUID):
            old_value = str(old_value) if old_value is not None else None
            new_value = str(new_value) if new_value is not None else None
        if isinstance(old_value, dt.datetime):
            old_value = old_value.isoformat()
        if isinstance(new_value, dt.datetime):
            new_value = new_value.isoformat()
        if old_value != new_value:
            changed[field] = {"old": old_value, "new": new_value}
    return changed


class MasterDataService:
    """Currencies, branches and customers, audited and deny-by-default."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings

    # ------------------------------------------------------------------ currencies
    async def list_currencies(
        self,
        *,
        is_active: bool | None = None,
        is_tradable: bool | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[Sequence[Any], int]:
        async with self._database.session() as session:
            return await CurrencyRepository(session).list_currencies(
                is_active=is_active, is_tradable=is_tradable, limit=limit, offset=offset
            )

    async def get_currency(self, currency_id: uuid.UUID) -> Any:
        async with self._database.session() as session:
            currency = await CurrencyRepository(session).get(currency_id)
            if currency is None:
                raise ResourceNotFoundError(
                    "That currency does not exist.",
                    details={"resource": "currency", "id": str(currency_id)},
                )
            return currency

    async def create_currency(
        self,
        *,
        code: str,
        name: str,
        symbol: str | None,
        decimal_places: int,
        is_base: bool,
        is_active: bool,
        is_tradable: bool,
        display_order: int,
        actor: ActorContext,
    ) -> Any:
        """Insert a currency; promoting it to base demotes the previous base atomically."""
        async with self._database.transaction() as session:
            currencies = CurrencyRepository(session)
            audit = AuditService(session)

            if await currencies.code_exists(code):
                raise DuplicateResourceError(
                    "That currency code already exists.",
                    details={"fields": [{"field": "code", "code": "duplicate"}]},
                )

            previous_base = await currencies.base_currency() if is_base else None
            if is_base and previous_base is None and self._settings.base_currency_code != code:
                # The configured base is the deployment's expectation; creating a
                # *different* base before the configured one exists is almost always a
                # mistake (a typo in the code), so it is refused rather than silently
                # re-pointing the books.
                raise ValidationError(
                    "The configured base currency does not exist yet.",
                    details={
                        "fields": [
                            {
                                "field": "is_base",
                                "code": "configured_base_missing",
                                "hint": (
                                    f"BASE_CURRENCY_CODE is "
                                    f"{self._settings.base_currency_code!r}; create it first or "
                                    "change the setting"
                                ),
                            }
                        ]
                    },
                )

            currency = _new_currency(
                code=code,
                name=name,
                symbol=symbol,
                decimal_places=decimal_places,
                is_base=is_base,
                is_active=is_active,
                is_tradable=is_tradable,
                display_order=display_order,
            )
            currencies.add(currency)

            demoted_id: uuid.UUID | None = None
            if is_base and previous_base is not None and previous_base.code != code:
                previous_base.is_base = False
                demoted_id = previous_base.id
            await currencies.flush()

            audit.record(
                action=AuditAction.CURRENCY_CREATED,
                entity_type="currency",
                entity_id=currency.id,
                new_data={
                    "code": currency.code,
                    "name": currency.name,
                    "decimal_places": currency.decimal_places,
                    "is_base": currency.is_base,
                    "is_active": currency.is_active,
                    "is_tradable": currency.is_tradable,
                },
                actor=actor,
            )
            if demoted_id is not None:
                # Two rows changed meaning; the trail must name both.
                audit.record(
                    action=AuditAction.CURRENCY_UPDATED,
                    entity_type="currency",
                    entity_id=demoted_id,
                    old_data={"is_base": True},
                    new_data={"is_base": False, "reason": "replaced by a new base currency"},
                    actor=actor,
                )
            return currency

    async def update_currency(
        self,
        *,
        currency_id: uuid.UUID,
        changes: Mapping[str, Any],
        actor: ActorContext,
    ) -> Any:
        """Apply a partial update to a currency (never to its ``code``)."""
        async with self._database.transaction() as session:
            currencies = CurrencyRepository(session)
            audit = AuditService(session)

            currency = await currencies.get(currency_id, for_update=True)
            if currency is None:
                raise ResourceNotFoundError(
                    "That currency does not exist.",
                    details={"resource": "currency", "id": str(currency_id)},
                )

            wanted = {key: value for key, value in changes.items() if value is not None}
            if not wanted:
                return currency

            if "is_base" in wanted:
                await self._apply_base_change(
                    currencies,
                    audit,
                    currency=currency,
                    make_base=bool(wanted["is_base"]),
                    actor=actor,
                )
                wanted.pop("is_base")

            if wanted.get("is_active") is False:
                await self._refuse_deactivating_a_currency_in_use(currency)

            before = {field: getattr(currency, field) for field in _CURRENCY_AUDITED_FIELDS}
            for field, value in wanted.items():
                setattr(currency, field, value)
            await currencies.flush()
            after = {field: getattr(currency, field) for field in _CURRENCY_AUDITED_FIELDS}

            diff = _diff(before, after, _CURRENCY_AUDITED_FIELDS)
            if diff:
                audit.record(
                    action=AuditAction.CURRENCY_UPDATED,
                    entity_type="currency",
                    entity_id=currency.id,
                    old_data={field: change["old"] for field, change in diff.items()},
                    new_data={field: change["new"] for field, change in diff.items()},
                    actor=actor,
                )
            return currency

    async def _apply_base_change(
        self,
        currencies: CurrencyRepository,
        audit: AuditService,
        *,
        currency: Any,
        make_base: bool,
        actor: ActorContext,
    ) -> None:
        """Promote or demote the base flag, keeping exactly one base currency."""
        if make_base:
            current = await currencies.base_currency()
            if current is not None and current.id != currency.id:
                current.is_base = False
                await currencies.flush()
                audit.record(
                    action=AuditAction.CURRENCY_UPDATED,
                    entity_type="currency",
                    entity_id=current.id,
                    old_data={"is_base": True},
                    new_data={"is_base": False, "reason": "replaced by an explicit promotion"},
                    actor=actor,
                )
            currency.is_base = True
            return

        if currency.is_base:
            # Demoting the base currency would leave the books without a reporting
            # currency, which every report in Phase 7 assumes exists.
            raise ConflictError(
                "The base currency cannot be demoted; promote another currency instead.",
                details={"fields": [{"field": "is_base", "code": "base_required"}]},
            )

    async def _refuse_deactivating_a_currency_in_use(self, currency: Any) -> None:
        if currency.is_base:
            raise ConflictError(
                "The base currency cannot be deactivated.",
                details={"fields": [{"field": "is_active", "code": "base_currency"}]},
            )

    # -------------------------------------------------------------------- branches
    async def list_branches(
        self, *, is_active: bool | None = None, limit: int = 100, offset: int = 0
    ) -> tuple[Sequence[Any], int]:
        async with self._database.session() as session:
            return await BranchRepository(session).list_branches(
                is_active=is_active, limit=limit, offset=offset
            )

    async def get_branch(self, branch_id: uuid.UUID) -> Any:
        async with self._database.session() as session:
            branch = await BranchRepository(session).get(branch_id)
            if branch is None:
                raise ResourceNotFoundError(
                    "That branch does not exist.",
                    details={"resource": "branch", "id": str(branch_id)},
                )
            return branch

    async def create_branch(
        self,
        *,
        code: str,
        name: str,
        address: str | None,
        phone: str | None,
        is_active: bool,
        timezone: str,
        actor: ActorContext,
    ) -> Any:
        async with self._database.transaction() as session:
            branches = BranchRepository(session)
            audit = AuditService(session)

            if await branches.code_exists(code):
                raise DuplicateResourceError(
                    "That branch code already exists.",
                    details={"fields": [{"field": "code", "code": "duplicate"}]},
                )

            branch = _new_branch(
                code=code,
                name=name,
                address=address,
                phone=phone,
                is_active=is_active,
                timezone=timezone,
            )
            branches.add(branch)
            await branches.flush()

            audit.record(
                action=AuditAction.BRANCH_CREATED,
                entity_type="branch",
                entity_id=branch.id,
                new_data={
                    "code": branch.code,
                    "name": branch.name,
                    "timezone": branch.timezone,
                    "is_active": branch.is_active,
                },
                actor=actor,
            )
            return branch

    async def update_branch(
        self, *, branch_id: uuid.UUID, changes: Mapping[str, Any], actor: ActorContext
    ) -> Any:
        """Update a branch; ``code`` only while the branch has no financial history."""
        async with self._database.transaction() as session:
            branches = BranchRepository(session)
            audit = AuditService(session)

            branch = await branches.get(branch_id, for_update=True)
            if branch is None:
                raise ResourceNotFoundError(
                    "That branch does not exist.",
                    details={"resource": "branch", "id": str(branch_id)},
                )

            wanted = {key: value for key, value in changes.items() if value is not None}
            if not wanted:
                return branch

            new_code = wanted.get("code")
            if new_code is not None and new_code != branch.code:
                if await branches.has_financial_history(branch.id):
                    raise ImmutableFieldError(
                        "This branch already carries financial history; its code cannot change.",
                        details={"fields": [{"field": "code", "code": "immutable"}]},
                    )
                if await branches.code_exists(new_code):
                    raise DuplicateResourceError(
                        "That branch code already exists.",
                        details={"fields": [{"field": "code", "code": "duplicate"}]},
                    )

            if wanted.get("is_active") is False:
                if not branch.is_active:
                    wanted.pop("is_active")
                else:
                    await self._refuse_closing_the_last_branch(branches)

            before = {field: getattr(branch, field) for field in _BRANCH_AUDITED_FIELDS}
            for field, value in wanted.items():
                setattr(branch, field, value)
            await branches.flush()
            after = {field: getattr(branch, field) for field in _BRANCH_AUDITED_FIELDS}

            diff = _diff(before, after, _BRANCH_AUDITED_FIELDS)
            if diff:
                audit.record(
                    action=AuditAction.BRANCH_UPDATED,
                    entity_type="branch",
                    entity_id=branch.id,
                    old_data={field: change["old"] for field, change in diff.items()},
                    new_data={field: change["new"] for field, change in diff.items()},
                    actor=actor,
                )
            return branch

    async def _refuse_closing_the_last_branch(self, branches: BranchRepository) -> None:
        if await branches.active_count() <= 1:
            # Every device, cash session and document is branch-scoped: closing the last
            # active branch would leave the business unable to operate.
            raise ConflictError(
                "The last active branch cannot be deactivated.",
                details={"fields": [{"field": "is_active", "code": "last_active_branch"}]},
            )

    # ------------------------------------------------------------------- customers
    async def list_customers(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        shared_only: bool = False,
        is_active: bool | None = None,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[Sequence[Any], int]:
        async with self._database.session() as session:
            return await CustomerRepository(session).list_customers(
                branch_id=branch_id,
                branch_id_is_null=shared_only,
                is_active=is_active,
                search=search,
                limit=limit,
                offset=offset,
            )

    async def get_customer(self, customer_id: uuid.UUID) -> Any:
        async with self._database.session() as session:
            customer = await CustomerRepository(session).get(customer_id)
            if customer is None:
                raise ResourceNotFoundError(
                    "That customer does not exist.",
                    details={"resource": "customer", "id": str(customer_id)},
                )
            return customer

    async def create_customer(
        self,
        *,
        full_name: str,
        phone: str | None,
        address: str | None,
        notes: str | None,
        national_id_last4: str | None,
        branch_id: uuid.UUID | None,
        customer_code: str | None,
        is_active: bool,
        actor: ActorContext,
    ) -> Any:
        """Register a customer, issuing ``CUS-YYYYMMDD-NNNNNN`` when no code is given."""
        async with self._database.transaction() as session:
            customers = CustomerRepository(session)
            branches = BranchRepository(session)
            audit = AuditService(session)

            if branch_id is not None:
                branch = await branches.get(branch_id)
                if branch is None:
                    raise ResourceNotFoundError(
                        "That branch does not exist.",
                        details={"fields": [{"field": "branch_id", "code": "not_found"}]},
                    )
                if not branch.is_active:
                    raise BranchInactiveError(
                        details={"fields": [{"field": "branch_id", "code": "inactive"}]}
                    )

            code = customer_code or await customers.next_customer_code(
                prefix=self._settings.numbering_prefix_customer,
                width=self._settings.numbering_width,
                on=dt.datetime.now(tz=dt.UTC),
            )
            if await customers.code_exists(code):
                raise DuplicateResourceError(
                    "That customer code already exists.",
                    details={"fields": [{"field": "customer_code", "code": "duplicate"}]},
                )

            customer = _new_customer(
                customer_code=code,
                full_name=full_name,
                phone=phone,
                address=address,
                notes=notes,
                national_id_last4=national_id_last4,
                branch_id=branch_id,
                is_active=is_active,
                created_by=actor.user_id,
            )
            customers.add(customer)
            await customers.flush()

            audit.record(
                action=AuditAction.CUSTOMER_CREATED,
                entity_type="customer",
                entity_id=customer.id,
                new_data={
                    "customer_code": customer.customer_code,
                    "full_name": customer.full_name,
                    "phone": customer.phone,
                    "branch_id": str(customer.branch_id) if customer.branch_id else None,
                    "is_active": customer.is_active,
                    "national_id_recorded": bool(customer.national_id_last4),
                },
                actor=actor,
            )
            return customer

    async def update_customer(
        self, *, customer_id: uuid.UUID, changes: Mapping[str, Any], actor: ActorContext
    ) -> Any:
        """Update a customer, auditing the field-level diff including deactivation."""
        async with self._database.transaction() as session:
            customers = CustomerRepository(session)
            branches = BranchRepository(session)
            audit = AuditService(session)

            customer = await customers.get(customer_id, for_update=True)
            if customer is None:
                raise ResourceNotFoundError(
                    "That customer does not exist.",
                    details={"resource": "customer", "id": str(customer_id)},
                )

            wanted = dict(changes)
            if wanted.get("branch_id") is not None:
                branch = await branches.get(wanted["branch_id"])
                if branch is None:
                    raise ResourceNotFoundError(
                        "That branch does not exist.",
                        details={"fields": [{"field": "branch_id", "code": "not_found"}]},
                    )
                if not branch.is_active:
                    raise BranchInactiveError(
                        details={"fields": [{"field": "branch_id", "code": "inactive"}]}
                    )

            before = {field: getattr(customer, field) for field in _CUSTOMER_AUDITED_FIELDS}
            for field, value in wanted.items():
                setattr(customer, field, value)
            customer.updated_by = actor.user_id
            customer.updated_at = dt.datetime.now(tz=dt.UTC)
            await customers.flush()
            after = {field: getattr(customer, field) for field in _CUSTOMER_AUDITED_FIELDS}

            diff = _diff(before, after, _CUSTOMER_AUDITED_FIELDS)
            if diff:
                deactivated = (
                    diff.get("is_active", {}).get("new") is False
                    and before.get("is_active") is True
                )
                audit.record(
                    action=(
                        AuditAction.CUSTOMER_DEACTIVATED
                        if deactivated
                        else AuditAction.CUSTOMER_UPDATED
                    ),
                    entity_type="customer",
                    entity_id=customer.id,
                    old_data={field: change["old"] for field, change in diff.items()},
                    new_data={field: change["new"] for field, change in diff.items()},
                    actor=actor,
                )
            return customer

    async def deactivate_customer(self, *, customer_id: uuid.UUID, actor: ActorContext) -> Any:
        """``DELETE /customers/{id}`` — deactivation, never deletion (PART 25)."""
        return await self.update_customer(
            customer_id=customer_id, changes={"is_active": False}, actor=actor
        )


# --------------------------------------------------------------------------- helpers
def _new_currency(
    *,
    code: str,
    name: str,
    symbol: str | None,
    decimal_places: int,
    is_base: bool,
    is_active: bool,
    is_tradable: bool,
    display_order: int,
) -> Any:
    from app.models.currency import Currency

    return Currency(
        code=code,
        name=name.strip(),
        symbol=symbol,
        decimal_places=decimal_places,
        is_base=is_base,
        is_active=is_active,
        is_tradable=is_tradable,
        display_order=display_order,
    )


def _new_branch(
    *,
    code: str,
    name: str,
    address: str | None,
    phone: str | None,
    is_active: bool,
    timezone: str,
) -> Any:
    from app.models.branch import Branch

    return Branch(
        code=code,
        name=name.strip(),
        address=address,
        phone=phone,
        is_active=is_active,
        timezone=timezone,
    )


def _new_customer(
    *,
    customer_code: str,
    full_name: str,
    phone: str | None,
    address: str | None,
    notes: str | None,
    national_id_last4: str | None,
    branch_id: uuid.UUID | None,
    is_active: bool,
    created_by: uuid.UUID | None,
) -> Any:
    from app.models.customer import Customer

    return Customer(
        customer_code=customer_code,
        full_name=full_name,
        phone=phone,
        address=address,
        notes=notes,
        national_id_last4=national_id_last4,
        branch_id=branch_id,
        is_active=is_active,
        created_by=created_by,
    )


def build_master_data_service(*, database: Database, settings: Settings) -> MasterDataService:
    """Construct the service for a request (cheap: it holds no state beyond handles)."""
    return MasterDataService(database=database, settings=settings)
