"""Chart of accounts and exchange-rate services (PART 11, PART 13).

Two invariants this module protects:

* **The chart stays a forest.** An account may not be its own parent, may not be moved
  under one of its own descendants, and a parent account must be declared as a grouping
  account (``is_postable = false``) before it can receive children — a postable parent
  would put journal lines on a header and double-count every subtotal above it. The
  recursive check happens in SQL (see
  :meth:`AccountRepository.is_descendant_of`) so two concurrent re-parentings cannot
  create a cycle between them.
* **Quotes are append-only.** There is no code path that updates or deletes a rate: the
  service exposes publication and reads only, the API has no ``PATCH``/``DELETE`` route,
  and a correction is a *new* quote. Every publication is audited with the resolved pair,
  the branch, the source and the decimal-exact values. (The approved Phase 0 DDL installs
  no append-only trigger on ``exchange_rates`` — see PHASE3_REPORT.md, Limitation L-2 —
  so a direct privileged ``UPDATE`` is possible; it would be captured by the table's
  ``change_log`` trigger, and the application cannot do it at all.) The resolution order
  (branch quote wins, newest instant wins) is the Phase 0 function
  ``resolve_exchange_rate``, never a second implementation here.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, NamedTuple

from sqlalchemy.engine import RowMapping

from app.core.audit_actions import AuditAction
from app.core.config import Settings
from app.core.database import Database
from app.core.exceptions import (
    BranchInactiveError,
    ConflictError,
    CurrencyInactiveError,
    DuplicateResourceError,
    ImmutableFieldError,
    RateNotFoundError,
    ResourceNotFoundError,
    ValidationError,
)
from app.core.money import format_decimal
from app.models.account import Account
from app.models.branch import Branch
from app.models.currency import Currency
from app.models.exchange_rate import ExchangeRate
from app.repositories.ledger_master import AccountRepository, ExchangeRateRepository
from app.repositories.masterdata import BranchRepository, CurrencyRepository
from app.services.audit_service import ActorContext, AuditService

# ``normal_balance`` is a property of the account type, not an operator choice: an asset
# account that reports a credit normal balance would invert every balance report.
NORMAL_BALANCE_BY_TYPE: dict[str, str] = {
    "ASSET": "DEBIT",
    "EXPENSE": "DEBIT",
    "LIABILITY": "CREDIT",
    "EQUITY": "CREDIT",
    "REVENUE": "CREDIT",
}


class AccountView(NamedTuple):
    """An account plus the two facts that live outside it.

    ``has_children`` and ``currency_code`` are what a chart editor needs to render a tree
    row; both come from the same query as the account (or the same request), so a client
    never has to issue a follow-up call or guess.
    """

    account: Account
    has_children: bool
    currency_code: str | None


_ACCOUNT_AUDITED_FIELDS = (
    "code",
    "name",
    "account_type",
    "normal_balance",
    "currency_id",
    "branch_id",
    "parent_id",
    "is_active",
    "is_postable",
)


class AccountService:
    """Chart-of-accounts administration."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings

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
    ) -> tuple[Sequence[AccountView], int]:
        """Accounts with their tree/currency facts (see the repository)."""
        async with self._database.session() as session:
            rows, total = await AccountRepository(session).list_accounts(
                account_type=account_type,
                branch_id=branch_id,
                currency_id=currency_id,
                parent_id=parent_id,
                is_active=is_active,
                is_postable=is_postable,
                limit=limit,
                offset=offset,
            )
        return [
            AccountView(account, has_children, currency_code)
            for account, has_children, currency_code in rows
        ], total

    async def get_account(self, account_id: uuid.UUID) -> AccountView:
        """One account plus its tree/currency facts."""
        async with self._database.session() as session:
            row = await AccountRepository(session).get_view(account_id)
            if row is None:
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={"resource": "account", "id": str(account_id)},
                )
            account, has_children, currency_code = row
            return AccountView(account, has_children, currency_code)

    async def create_account(
        self,
        *,
        code: str,
        name: str,
        account_type: str,
        currency_id: uuid.UUID | None,
        branch_id: uuid.UUID | None,
        parent_id: uuid.UUID | None,
        is_active: bool,
        is_postable: bool,
        actor: ActorContext,
    ) -> AccountView:
        """Create an account; ``normal_balance`` is derived from ``account_type``."""
        async with self._database.transaction() as session:
            accounts = AccountRepository(session)
            currencies = CurrencyRepository(session)
            branches = BranchRepository(session)
            audit = AuditService(session)

            if await accounts.code_exists(code):
                raise DuplicateResourceError(
                    "That account code already exists.",
                    details={"fields": [{"field": "code", "code": "duplicate"}]},
                )

            currency: Currency | None = None
            if currency_id is not None:
                currency = await currencies.get(currency_id)
                if currency is None:
                    raise ResourceNotFoundError(
                        "That currency does not exist.",
                        details={"fields": [{"field": "currency_id", "code": "not_found"}]},
                    )
                if not currency.is_active:
                    raise CurrencyInactiveError(
                        details={"fields": [{"field": "currency_id", "code": "inactive"}]}
                    )

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

            parent: Account | None = None
            if parent_id is not None:
                parent = await accounts.get(parent_id)
                if parent is None:
                    raise ResourceNotFoundError(
                        "That parent account does not exist.",
                        details={"fields": [{"field": "parent_id", "code": "not_found"}]},
                    )
                # The same rule as on update: a child must share the parent's type, and a
                # grouping account must be marked as such *before* it receives a child.
                self._check_parent_eligibility(parent=parent, account_type=account_type)
            account = Account(
                code=code,
                name=name.strip(),
                account_type=account_type,
                normal_balance=NORMAL_BALANCE_BY_TYPE[account_type],
                currency_id=currency_id,
                branch_id=branch_id,
                parent_id=parent.id if parent is not None else None,
                is_active=is_active,
                is_postable=is_postable,
                created_by=actor.user_id,
            )
            accounts.add(account)
            await accounts.flush()

            audit.record(
                action=AuditAction.ACCOUNT_CREATED,
                entity_type="account",
                entity_id=account.id,
                new_data={
                    "code": account.code,
                    "name": account.name,
                    "account_type": account.account_type,
                    "normal_balance": account.normal_balance,
                    "currency_id": str(account.currency_id) if account.currency_id else None,
                    "branch_id": str(account.branch_id) if account.branch_id else None,
                    "parent_id": str(account.parent_id) if account.parent_id else None,
                    "is_postable": account.is_postable,
                },
                actor=actor,
            )
            # A brand-new account has no children by construction, so the flag is False
            # without a second query.
            return AccountView(account, False, currency.code if currency else None)

    async def update_account(
        self, *, account_id: uuid.UUID, changes: Mapping[str, Any], actor: ActorContext
    ) -> AccountView:
        """Rename/re-parent/deactivate an account; its identity freezes once it has lines."""
        async with self._database.transaction() as session:
            accounts = AccountRepository(session)
            audit = AuditService(session)

            account = await accounts.get(account_id, for_update=True)
            if account is None:
                raise ResourceNotFoundError(
                    "That account does not exist.",
                    details={"resource": "account", "id": str(account_id)},
                )

            wanted = {key: value for key, value in changes.items() if value is not None}
            if not wanted:
                # Nothing to change is not an error: the caller gets the current state and
                # no audit row is written (an empty PATCH is not an event).
                return await self._view(accounts, account)

            has_lines = await accounts.has_journal_lines(account.id)

            new_code = wanted.get("code")
            if new_code is not None and new_code != account.code:
                if has_lines:
                    raise ImmutableFieldError(
                        "This account carries journal lines; its code cannot change.",
                        details={"fields": [{"field": "code", "code": "immutable"}]},
                    )
                if await accounts.code_exists(new_code):
                    raise DuplicateResourceError(
                        "That account code already exists.",
                        details={"fields": [{"field": "code", "code": "duplicate"}]},
                    )

            if "currency_id" in changes and changes["currency_id"] is not None:
                currency = await CurrencyRepository(session).get(changes["currency_id"])
                if currency is None:
                    raise ResourceNotFoundError(
                        "That currency does not exist.",
                        details={"fields": [{"field": "currency_id", "code": "not_found"}]},
                    )
                if not currency.is_active:
                    raise CurrencyInactiveError(
                        details={"fields": [{"field": "currency_id", "code": "inactive"}]}
                    )

            new_type = wanted.get("account_type")
            if new_type is not None and new_type != account.account_type:
                if has_lines:
                    # Changing the type would silently reclassify history and every report
                    # built on it. A correction is a new account plus a reclassification
                    # entry, which is an accounting decision, not an edit.
                    raise ImmutableFieldError(
                        "This account carries journal lines; its type cannot change.",
                        details={"fields": [{"field": "account_type", "code": "immutable"}]},
                    )
                account.normal_balance = NORMAL_BALANCE_BY_TYPE[new_type]

            if "parent_id" in changes:
                parent_id = changes["parent_id"]
                await self._validate_parent(accounts, account=account, parent_id=parent_id)
                account.parent_id = parent_id

            if wanted.get("is_postable") is True and await accounts.has_children(account.id):
                # ACCOUNTING_MODEL.md: a parent account groups a subtree and is not posted
                # to. Letting it become postable would put journal lines on a header and
                # double-count every subtotal built on top of it.
                raise ConflictError(
                    "An account with children groups a subtree and cannot be postable.",
                    details={"fields": [{"field": "is_postable", "code": "has_children"}]},
                )

            before = {field: getattr(account, field) for field in _ACCOUNT_AUDITED_FIELDS}
            for field, value in wanted.items():
                if field == "parent_id":
                    continue  # already applied above, including the None case
                setattr(account, field, value)
            await accounts.flush()
            after = {field: getattr(account, field) for field in _ACCOUNT_AUDITED_FIELDS}

            diff = _diff(before, after, _ACCOUNT_AUDITED_FIELDS)
            if diff:
                audit.record(
                    action=AuditAction.ACCOUNT_UPDATED,
                    entity_type="account",
                    entity_id=account.id,
                    old_data={field: change["old"] for field, change in diff.items()},
                    new_data={field: change["new"] for field, change in diff.items()},
                    actor=actor,
                )
            return await self._view(accounts, account)

    @staticmethod
    async def _view(accounts: AccountRepository, account: Account) -> AccountView:
        """Load the account's tree/currency facts — one extra query, only on write paths."""
        currency_code: str | None = None
        if account.currency_id is not None:
            currency = await CurrencyRepository(accounts.session).get(account.currency_id)
            currency_code = currency.code if currency is not None else None
        return AccountView(account, await accounts.has_children(account.id), currency_code)

    @staticmethod
    def _check_parent_eligibility(*, parent: Account, account_type: str) -> None:
        """A parent must share the child's type and be marked as a grouping account.

        Runs on creation *and* on re-parenting, so a tree that could only have been built
        by going through the API is always consistent. The type is checked first: it is
        the mistake an operator actually made, and it is the one that would corrupt a
        subtotal if it slipped through.
        """
        if parent.account_type != account_type:
            # A cost account under a revenue parent would break every subtotal in the
            # chart; the tree mirrors the accounting classification.
            raise ValidationError(
                "A parent account must have the same account type.",
                details={"fields": [{"field": "parent_id", "code": "type_mismatch"}]},
            )
        AccountService._check_parent_grouping(parent=parent)

    @staticmethod
    def _check_parent_grouping(*, parent: Account) -> None:
        """A parent is a grouping account: ``is_postable`` must already be false."""
        if parent.is_postable:
            raise ConflictError(
                "A parent account groups a subtree and must be non-postable "
                "(set is_postable=false on the parent first).",
                details={"fields": [{"field": "parent_id", "code": "postable_parent"}]},
            )

    async def _validate_parent(
        self, accounts: AccountRepository, *, account: Account, parent_id: uuid.UUID | None
    ) -> None:
        """Reject a parent that is unknown, the account itself, or one of its descendants."""
        if parent_id is None:
            return
        if parent_id == account.id:
            raise ValidationError(
                "An account cannot be its own parent.",
                details={"fields": [{"field": "parent_id", "code": "self_parent"}]},
            )
        parent = await accounts.get(parent_id)
        if parent is None:
            raise ResourceNotFoundError(
                "That parent account does not exist.",
                details={"fields": [{"field": "parent_id", "code": "not_found"}]},
            )
        if parent.account_type != account.account_type:
            raise ValidationError(
                "A parent account must have the same account type.",
                details={"fields": [{"field": "parent_id", "code": "type_mismatch"}]},
            )
        if await accounts.is_descendant_of(parent_id, account.id):
            # Order matters: a cycle detaches a whole subtree from the chart — the more
            # serious problem — so it is reported before the grouping rule, which would
            # otherwise send the operator off to "fix" the parent's postable flag.
            raise ValidationError(
                "An account cannot be moved under one of its own descendants.",
                details={"fields": [{"field": "parent_id", "code": "cycle"}]},
            )
        self._check_parent_grouping(parent=parent)


class RateService:
    """Append-only exchange-rate publication and resolution (PART 13)."""

    def __init__(self, *, database: Database, settings: Settings) -> None:
        self._database = database
        self._settings = settings

    async def publish_rate(
        self,
        *,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        buy_rate: Any,
        sell_rate: Any,
        effective_at: dt.datetime | None,
        branch_id: uuid.UUID | None,
        source: str,
        actor: ActorContext,
    ) -> dict[str, Any]:
        """Append one quote after validating both currencies, the branch and the pair."""
        async with self._database.transaction() as session:
            rates = ExchangeRateRepository(session)
            currencies = CurrencyRepository(session)
            branches = BranchRepository(session)
            audit = AuditService(session)

            if from_currency_id == to_currency_id:
                raise ValidationError(
                    "A rate needs two different currencies.",
                    details={"fields": [{"field": "to_currency_id", "code": "same_as_source"}]},
                )

            from_currency = await self._tradable_currency(
                currencies, currency_id=from_currency_id, field="from_currency_id"
            )
            to_currency = await self._tradable_currency(
                currencies, currency_id=to_currency_id, field="to_currency_id"
            )

            branch: Branch | None = None
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

            moment = effective_at or dt.datetime.now(tz=dt.UTC)
            if await rates.duplicate_instant_exists(
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
                branch_id=branch_id,
                effective_at=moment,
            ):
                raise DuplicateResourceError(
                    "A quote already exists for that pair, branch and instant.",
                    details={"fields": [{"field": "effective_at", "code": "duplicate"}]},
                )

            rate = ExchangeRate(
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
                buy_rate=buy_rate,
                sell_rate=sell_rate,
                effective_at=moment,
                branch_id=branch_id,
                source=source,
                created_by=actor.user_id,
            )
            rates.add(rate)
            await rates.flush()

            audit.record(
                action=AuditAction.RATE_CREATED,
                entity_type="exchange_rate",
                entity_id=rate.id,
                new_data={
                    "from_currency": from_currency.code,
                    "to_currency": to_currency.code,
                    "buy_rate": format_decimal(buy_rate),
                    "sell_rate": format_decimal(sell_rate),
                    "effective_at": moment.isoformat(),
                    "branch_id": str(branch_id) if branch_id else None,
                    "source": source,
                },
                actor=actor,
            )
            # The response shape of the read endpoints, assembled from the rows this
            # transaction already loaded (no second query, no un-filled field).
            return {
                "id": rate.id,
                "from_currency_id": rate.from_currency_id,
                "to_currency_id": rate.to_currency_id,
                "from_currency_code": from_currency.code,
                "to_currency_code": to_currency.code,
                "buy_rate": rate.buy_rate,
                "sell_rate": rate.sell_rate,
                "effective_at": rate.effective_at,
                "branch_id": rate.branch_id,
                "branch_code": branch.code if branch is not None else None,
                "source": rate.source,
                "created_at": rate.created_at,
                "created_by": rate.created_by,
            }

    @staticmethod
    async def _tradable_currency(
        currencies: CurrencyRepository, *, currency_id: uuid.UUID, field: str
    ) -> Currency:
        """Fetch a currency that may be quoted, or explain precisely why not.

        A rate names two currencies; "which side is wrong" is the first thing the operator
        needs to know, so the field name travels with the error.
        """
        currency = await currencies.get(currency_id)
        if currency is None:
            raise ResourceNotFoundError(
                "That currency does not exist.",
                details={"fields": [{"field": field, "code": "not_found"}]},
            )
        if not currency.is_active:
            raise CurrencyInactiveError(details={"fields": [{"field": field, "code": "inactive"}]})
        if not currency.is_tradable:
            raise ConflictError(
                "That currency is not tradable.",
                details={"fields": [{"field": field, "code": "not_tradable"}]},
            )
        return currency

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
        """Quote history with currency/branch codes resolved (see the repository)."""
        async with self._database.session() as session:
            return await ExchangeRateRepository(session).list_history(
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
                branch_id=branch_id,
                include_global=include_global,
                effective_from=effective_from,
                effective_to=effective_to,
                limit=limit,
                offset=offset,
            )

    async def current_quotes(
        self,
        *,
        branch_id: uuid.UUID | None = None,
        at: dt.datetime | None = None,
        from_currency_id: uuid.UUID | None = None,
        to_currency_id: uuid.UUID | None = None,
    ) -> Sequence[Any]:
        """The quote in force per pair (branch quote overriding the global one)."""
        async with self._database.session() as session:
            return await ExchangeRateRepository(session).latest_quotes(
                branch_id=branch_id,
                at=at,
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
            )

    async def resolve_rate(
        self,
        *,
        from_currency_id: uuid.UUID,
        to_currency_id: uuid.UUID,
        branch_id: uuid.UUID | None,
        at: dt.datetime | None = None,
    ) -> Any:
        """Resolve the applicable quote or raise ``RATE_NOT_FOUND`` (422).

        Phase 5 calls this with the transaction's own instant; exposing it as an endpoint
        now means the counter software and the server agree on the rate *before* a
        transaction is attempted, which is what makes ``RATE_OUT_OF_TOLERANCE`` rare
        rather than routine.
        """
        async with self._database.session() as session:
            row = await ExchangeRateRepository(session).resolve(
                from_currency_id=from_currency_id,
                to_currency_id=to_currency_id,
                branch_id=branch_id,
                at=at,
            )
        if row is None:
            raise RateNotFoundError(
                details={
                    "from_currency_id": str(from_currency_id),
                    "to_currency_id": str(to_currency_id),
                    "branch_id": str(branch_id) if branch_id else None,
                }
            )
        return row


def _diff(
    before: Mapping[str, Any], after: Mapping[str, Any], fields: Sequence[str]
) -> dict[str, Any]:
    """Field-level change map for the audit trail (only real changes are recorded)."""
    changed: dict[str, Any] = {}
    for field in fields:
        old_value = before.get(field)
        new_value = after.get(field)
        if isinstance(old_value, uuid.UUID) or isinstance(new_value, uuid.UUID):
            old_value = str(old_value) if old_value is not None else None
            new_value = str(new_value) if new_value is not None else None
        if old_value != new_value:
            changed[field] = {"old": old_value, "new": new_value}
    return changed


def build_account_service(*, database: Database, settings: Settings) -> AccountService:
    """Construct the chart-of-accounts service for a request."""
    return AccountService(database=database, settings=settings)


def build_rate_service(*, database: Database, settings: Settings) -> RateService:
    """Construct the rate service for a request."""
    return RateService(database=database, settings=settings)
