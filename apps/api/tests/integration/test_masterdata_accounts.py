"""Chart of accounts (PART 11, PART 12, PART 46).

Three properties are load-bearing for everything Phase 5+ will build on this table:

* **``normal_balance`` is derived, never supplied.** It follows from ``account_type``;
  letting a client choose it would let a client invert a balance report.
* **The chart is a forest.** A parent must exist, must share the account type, and can
  never be the account itself or one of its own descendants.
* **Identity freezes at first use.** Once an account carries journal lines, its code and
  type are permanent; a correction is a new account, not a silent reclassification of
  history. The rename and the parent move stay available.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.database import Database
from app.core.exceptions import ImmutableFieldError
from app.services.audit_service import ActorContext
from app.services.ledger_master_service import AccountService, build_account_service
from tests.helpers import fetch_all, fetch_scalar, settings_for_database
from tests.masterdata_helpers import (
    ACCOUNTS,
    create_account,
    create_currency,
    create_header,
    unique_account_code,
)

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]

NORMAL_BALANCE = {
    "ASSET": "DEBIT",
    "EXPENSE": "DEBIT",
    "LIABILITY": "CREDIT",
    "EQUITY": "CREDIT",
    "REVENUE": "CREDIT",
}


def _audit_rows(database: str, entity_id: str) -> list[tuple[object, ...]]:
    return fetch_all(
        database,
        "SELECT action, old_data, new_data FROM audit_logs "
        "WHERE entity_type = 'account' AND entity_id = :entity_id ORDER BY seq",
        entity_id=entity_id,
    )


class TestAccountCreation:
    @pytest.mark.parametrize(("account_type", "normal_balance"), sorted(NORMAL_BALANCE.items()))
    def test_the_normal_balance_follows_the_type(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        account_type: str,
        normal_balance: str,
    ) -> None:
        created = create_account(
            api_client, admin_headers, account_type=account_type, name=f"{account_type} probe"
        ).json()
        assert created["normal_balance"] == normal_balance
        # The column is CHAR(6) (frozen DDL), so raw SQL reports the blank padding that
        # PostgreSQL adds to CHAR. The API strips it; the database compares it as equal
        # because CHAR ignores trailing blanks — both are asserted here.
        stored = fetch_scalar(
            main_database,
            "SELECT normal_balance FROM accounts WHERE id = :account_id",
            account_id=created["id"],
        )
        assert isinstance(stored, str) and stored.strip() == normal_balance
        assert (
            fetch_scalar(
                main_database,
                "SELECT normal_balance = :expected FROM accounts WHERE id = :account_id",
                account_id=created["id"],
                expected=normal_balance,
            )
            is True
        )
        assert created["normal_balance"] == normal_balance  # no padding on the wire

    def test_a_client_cannot_choose_the_normal_balance(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            ACCOUNTS,
            json={
                "code": unique_account_code(),
                "name": "Spoofed",
                "account_type": "ASSET",
                "normal_balance": "CREDIT",
            },
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["fields"][0]["code"] == "extra_forbidden"

    @pytest.mark.parametrize("account_type", ["asset", "Equity", " revenue "])
    def test_a_type_is_normalised_before_it_is_validated(
        self, api_client: TestClient, admin_headers: dict[str, str], account_type: str
    ) -> None:
        """Case and surrounding blanks are noise, not a different account type."""
        created = create_account(
            api_client, admin_headers, account_type=account_type, name="Normalised"
        )
        assert created.status_code == 201, created.text
        assert created.json()["account_type"] == account_type.strip().upper()

    @pytest.mark.parametrize("account_type", ["CASH", " ", "asset-liability", "ASSETT"])
    def test_a_type_outside_the_chart_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], account_type: str
    ) -> None:
        response = create_account(api_client, admin_headers, account_type=account_type, expect=None)
        assert response.status_code == 422, f"{account_type!r}: {response.text}"

    def test_a_duplicate_code_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        code = unique_account_code()
        create_account(api_client, admin_headers, code=code)
        duplicate = create_account(api_client, admin_headers, code=code, expect=409)
        assert duplicate.json()["error"]["code"] == "DUPLICATE_RESOURCE"

    def test_creation_is_audited_with_the_derived_balance(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_account(
            api_client, admin_headers, account_type="LIABILITY", name="Payables probe"
        ).json()
        rows = _audit_rows(main_database, created["id"])
        assert [row[0] for row in rows] == ["ACCOUNT_CREATED"]
        assert rows[0][2]["normal_balance"] == "CREDIT"
        assert rows[0][2]["account_type"] == "LIABILITY"

    def test_a_scoped_account_records_its_branch_and_currency(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
    ) -> None:
        currency = create_currency(api_client, admin_headers).json()
        created = create_account(
            api_client,
            admin_headers,
            currency_id=currency["id"],
            branch_id=branch_id,
            name="Branch cash probe",
        ).json()
        assert created["currency_code"] == currency["code"]
        assert created["branch_id"] == branch_id

    def test_an_unknown_currency_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = create_account(
            api_client, admin_headers, currency_id=str(uuid.uuid4()), expect=404
        )
        assert response.json()["error"]["details"]["fields"][0]["field"] == "currency_id"

    def test_an_inactive_currency_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        currency = create_currency(api_client, admin_headers).json()
        api_client.patch(
            f"/api/v1/currencies/{currency['id']}",
            json={"is_active": False},
            headers=admin_headers,
        )
        response = create_account(api_client, admin_headers, currency_id=currency["id"], expect=422)
        assert response.json()["error"]["code"] == "CURRENCY_INACTIVE"

    def test_an_unknown_parent_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = create_account(
            api_client, admin_headers, parent_id=str(uuid.uuid4()), expect=404
        )
        assert response.json()["error"]["details"]["fields"][0]["field"] == "parent_id"


class TestAccountTree:
    def test_a_child_can_be_created_under_a_grouping_parent(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        parent = create_header(
            api_client, admin_headers, account_type="REVENUE", name="Revenue header"
        ).json()
        child = create_account(
            api_client,
            admin_headers,
            account_type="REVENUE",
            parent_id=parent["id"],
            name="Commission income",
        ).json()
        assert child["parent_id"] == parent["id"]
        assert child["normal_balance"] == "CREDIT"

        listing = api_client.get(
            ACCOUNTS, params={"parent_id": parent["id"]}, headers=admin_headers
        )
        assert [row["id"] for row in listing.json()["items"]] == [child["id"]]

    def test_a_postable_parent_must_be_declared_a_header_first(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """A parent groups a subtree; taking children while still postable is refused."""
        parent = create_account(
            api_client, admin_headers, account_type="ASSET", name="Still postable"
        ).json()
        assert parent["is_postable"] is True

        refused = create_account(
            api_client,
            admin_headers,
            account_type="ASSET",
            parent_id=parent["id"],
            name="Too early",
            expect=409,
        )
        assert refused.json()["error"]["code"] == "CONFLICT"
        assert refused.json()["error"]["details"]["fields"][0]["code"] == "postable_parent"

        # The documented path: mark the header, then add the child.
        api_client.patch(
            f"{ACCOUNTS}/{parent['id']}", json={"is_postable": False}, headers=admin_headers
        )
        child = create_account(
            api_client, admin_headers, account_type="ASSET", parent_id=parent["id"], name="Now"
        )
        assert child.status_code == 201, child.text

    def test_a_parent_of_another_type_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        parent = create_header(api_client, admin_headers, account_type="ASSET").json()
        response = create_account(
            api_client,
            admin_headers,
            account_type="REVENUE",
            parent_id=parent["id"],
            expect=422,
        )
        assert response.json()["error"]["details"]["fields"][0]["code"] == "type_mismatch"

    def test_an_account_cannot_be_its_own_parent(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        account = create_account(api_client, admin_headers).json()
        response = api_client.patch(
            f"{ACCOUNTS}/{account['id']}", json={"parent_id": account["id"]}, headers=admin_headers
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["fields"][0]["code"] == "self_parent"

    def test_an_account_cannot_be_moved_under_its_own_descendant(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        root = create_header(api_client, admin_headers, name="Root").json()
        child = create_header(api_client, admin_headers, parent_id=root["id"], name="Child").json()
        grandchild = create_account(
            api_client, admin_headers, parent_id=child["id"], name="Grandchild"
        ).json()

        response = api_client.patch(
            f"{ACCOUNTS}/{root['id']}",
            json={"parent_id": grandchild["id"]},
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["details"]["fields"][0]["code"] == "cycle"

        # Nothing moved, so the tree is still a tree.
        reread = api_client.get(f"{ACCOUNTS}/{root['id']}", headers=admin_headers).json()
        assert reread["parent_id"] is None

    def test_a_header_with_children_cannot_become_postable(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        parent = create_header(api_client, admin_headers, name="Header").json()
        create_account(api_client, admin_headers, parent_id=parent["id"], name="Detail")
        response = api_client.patch(
            f"{ACCOUNTS}/{parent['id']}", json={"is_postable": True}, headers=admin_headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "CONFLICT"
        assert response.json()["error"]["details"]["fields"][0]["code"] == "has_children"

    def test_the_listing_reports_who_has_children(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """Two filtered listings, not one global page.

        The session database accumulates the accounts every other suite creates (Phase 4's
        scenarios alone scaffold a chart each), so a global page cannot be relied on to
        contain this tree - the original form of this test asserted against
        ``?limit=500`` and a full run eventually pushed the root past the page. Listing by
        the parent the test itself created is deterministic *and* a stronger statement:
        the children reported are exactly this root's child, and the root reports that it
        has one.
        """
        root = create_header(api_client, admin_headers, name="Tree root").json()
        branch = create_header(
            api_client, admin_headers, parent_id=root["id"], name="Tree branch"
        ).json()
        leaf = create_account(
            api_client, admin_headers, parent_id=branch["id"], name="Tree leaf"
        ).json()

        children_of_root = api_client.get(
            ACCOUNTS, params={"parent_id": root["id"]}, headers=admin_headers
        ).json()["items"]
        assert [row["id"] for row in children_of_root] == [branch["id"]]
        assert children_of_root[0]["has_children"] is True
        assert children_of_root[0]["parent_id"] == root["id"]

        children_of_branch = api_client.get(
            ACCOUNTS, params={"parent_id": branch["id"]}, headers=admin_headers
        ).json()["items"]
        assert [row["id"] for row in children_of_branch] == [leaf["id"]]
        assert children_of_branch[0]["has_children"] is False
        assert children_of_branch[0]["parent_id"] == branch["id"]

        # The detail endpoint agrees with the listing's derived flag, and the root - which
        # has no parent to filter by - is read directly.
        single_root = api_client.get(f"{ACCOUNTS}/{root['id']}", headers=admin_headers).json()
        assert single_root["has_children"] is True
        single_leaf = api_client.get(f"{ACCOUNTS}/{leaf['id']}", headers=admin_headers).json()
        assert single_leaf["has_children"] is False


class TestAccountIdentity:
    """Identity (`code`, `account_type`) freezes at first use; grouping rules always apply.

    The freeze cannot be tested on the shared database: proving "this account has journal
    lines" means inserting real ledger rows, and the ledger is append-only — the probe
    could never be removed, so every later test in a randomly ordered run would be reading
    a database that had been quietly rewritten. The scenario therefore runs on its own
    migrated, seeded database through the service (the HTTP mapping of these errors to 409
    is covered by tests/unit/test_exceptions.py and by the branch freeze test).
    """

    @staticmethod
    def test_code_and_type_freeze_once_the_account_is_used(scratch_database: object) -> None:
        database_name = scratch_database("nexus_test_account_freeze")  # type: ignore[operator]
        asyncio.run(_identity_scenario(settings_for_database(database_name), database_name))

    def test_an_unused_account_may_be_recoded_and_retyped(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The other half of the rule: with no journal lines nothing is frozen yet."""
        account = create_account(api_client, admin_headers, account_type="ASSET").json()
        recoded = api_client.patch(
            f"{ACCOUNTS}/{account['id']}",
            json={"code": unique_account_code()},
            headers=admin_headers,
        )
        assert recoded.status_code == 200, recoded.text
        retyped = api_client.patch(
            f"{ACCOUNTS}/{account['id']}", json={"account_type": "EXPENSE"}, headers=admin_headers
        )
        assert retyped.status_code == 200, retyped.text
        assert retyped.json()["normal_balance"] == "DEBIT"


async def _identity_scenario(settings: object, database_name: str) -> None:
    """Create an account, use it in the ledger, then try to change its identity."""
    database = Database(settings)  # type: ignore[arg-type]
    service = build_account_service(database=database, settings=settings)  # type: ignore[arg-type]
    try:
        await _identity_scenario_body(service, database)
    finally:
        await database.dispose()


async def _identity_scenario_body(service: AccountService, database: Database) -> None:
    """A scratch database has the schema but no seeds, so the scenario seeds itself."""
    actor = ActorContext()
    async with database.transaction() as session:
        base_currency = await session.scalar(
            text(
                "INSERT INTO currencies (code, name, is_base, is_tradable) "
                "VALUES ('AFN', 'Afghan Afghani', TRUE, TRUE) RETURNING id"
            )
        )
        admin = await session.scalar(
            text(
                "INSERT INTO users (username, password_hash, full_name) "
                "VALUES ('probe-admin', 'x', 'Probe Admin') RETURNING id"
            )
        )
        branch = await session.scalar(
            text("INSERT INTO branches (code, name) VALUES ('PROBE', 'Probe branch') RETURNING id")
        )

    used = await service.create_account(
        code=unique_account_code(),
        name="Used account",
        account_type="ASSET",
        currency_id=None,
        branch_id=None,
        parent_id=None,
        is_active=True,
        is_postable=True,
        actor=actor,
    )
    counter = await service.create_account(
        code=unique_account_code(),
        name="Counter account",
        account_type="EQUITY",
        currency_id=None,
        branch_id=None,
        parent_id=None,
        is_active=True,
        is_postable=True,
        actor=actor,
    )

    # A real, balanced journal entry: the deferred balance trigger validates it at COMMIT.
    async with database.transaction() as session:
        entry_id = uuid.uuid4()
        await session.execute(
            text(
                "INSERT INTO journal_entries (id, reference_type, description, branch_id, "
                "created_by) VALUES (:id, 'MANUAL_ADJUSTMENT', 'account identity probe', "
                ":branch, :user)"
            ),
            {"id": entry_id, "branch": branch, "user": admin},
        )
        for account_id, debit, credit in (
            (used.account.id, 100, 0),
            (counter.account.id, 0, 100),
        ):
            await session.execute(
                text(
                    "INSERT INTO journal_lines (id, journal_entry_id, account_id, debit, credit, "
                    "currency_id) VALUES (:id, :entry, :account, :debit, :credit, :currency)"
                ),
                {
                    "id": uuid.uuid4(),
                    "entry": entry_id,
                    "account": account_id,
                    "debit": debit,
                    "credit": credit,
                    "currency": base_currency,
                },
            )

    with pytest.raises(ImmutableFieldError) as code_refusal:
        await service.update_account(
            account_id=used.account.id, changes={"code": unique_account_code()}, actor=actor
        )
    assert code_refusal.value.http_status == 409
    assert code_refusal.value.details["fields"] == [{"field": "code", "code": "immutable"}]

    with pytest.raises(ImmutableFieldError) as type_refusal:
        await service.update_account(
            account_id=used.account.id, changes={"account_type": "EXPENSE"}, actor=actor
        )
    assert type_refusal.value.details["fields"] == [{"field": "account_type", "code": "immutable"}]

    # Renaming and deactivating stay available: only identity is frozen, and a chart that
    # could not be corrected would push operators to work around the system.
    renamed = await service.update_account(
        account_id=used.account.id,
        changes={"name": "Renamed while used", "is_active": False},
        actor=actor,
    )
    assert renamed.account.name == "Renamed while used"
    assert renamed.account.is_active is False
    assert renamed.has_children is False
