"""Branch administration (PART 7, PART 9, API_CONTRACT §9.1).

The interesting rules are the ones that protect history and operability:

* A branch **code is frozen once the branch carries financial history**. The code appears
  on receipts, reconciled statements and journal entries; letting it change would rewrite
  the past (PART 22).
* **The last active branch cannot be deactivated.** Devices, cash sessions and documents
  are branch-scoped, so closing the final one would leave the business unable to operate.
* Branch reads are administrative data: they sit behind ``branch.manage``, not behind the
  operational ``exchange.view``.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
from fastapi.testclient import TestClient

from app.core.database import Database
from app.core.exceptions import ImmutableFieldError
from app.services.audit_service import ActorContext
from app.services.masterdata_service import MasterDataService, build_master_data_service
from tests.helpers import fetch_all, fetch_scalar, settings_for_database
from tests.masterdata_helpers import BRANCHES, create_branch, unique_branch_code

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]


def _audit_rows(database: str, entity_id: str) -> list[tuple[object, ...]]:
    return fetch_all(
        database,
        "SELECT action, user_id, old_data, new_data FROM audit_logs "
        "WHERE entity_type = 'branch' AND entity_id = :entity_id ORDER BY seq",
        entity_id=entity_id,
    )


class TestBranchReads:
    def test_the_bootstrap_branch_is_visible(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        response = api_client.get(BRANCHES, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        codes = [row["code"] for row in body["items"]]
        assert "MAIN" in codes
        ids = [row["id"] for row in body["items"]]
        assert branch_id in ids

    def test_unknown_branch_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{BRANCHES}/{uuid.uuid4()}", headers=admin_headers)
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestBranchCreation:
    def test_create_is_audited_with_the_operator(
        self,
        main_database: str,
        admin_user_id: str,
        branch_factory: object,
    ) -> None:
        body = branch_factory(name="Kabul Main")  # type: ignore[operator]
        assert body["timezone"] == "Asia/Kabul"

        rows = _audit_rows(main_database, body["id"])
        assert [row[0] for row in rows] == ["BRANCH_CREATED"]
        assert str(rows[0][1]) == admin_user_id
        assert rows[0][3]["code"] == body["code"]

    def test_duplicate_code_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        code = unique_branch_code()
        branch_factory(code=code)  # type: ignore[operator]
        duplicate = create_branch(api_client, admin_headers, code=code, expect=409)
        assert duplicate.json()["error"]["code"] == "DUPLICATE_RESOURCE"

    @pytest.mark.parametrize("code", ["A", "has space", "with_underscore", "x" * 21, "-LEADING"])
    def test_code_format_is_validated(
        self, api_client: TestClient, admin_headers: dict[str, str], code: str
    ) -> None:
        response = api_client.post(
            BRANCHES, json={"code": code, "name": "Bad"}, headers=admin_headers
        )
        assert response.status_code == 422, response.text

    def test_timezone_format_is_validated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            BRANCHES,
            json={"code": unique_branch_code(), "name": "Bad tz", "timezone": "Kabul"},
            headers=admin_headers,
        )
        assert response.status_code == 422


class TestBranchUpdates:
    def test_rename_is_audited(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        branch_factory: object,
    ) -> None:
        created = branch_factory(name="Before")  # type: ignore[operator]
        response = api_client.patch(
            f"{BRANCHES}/{created['id']}",
            json={"name": "After", "address": "Kabul, Afghanistan", "phone": "+93 20 000 0000"},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text

        rows = _audit_rows(main_database, created["id"])
        update = next(row for row in rows if row[0] == "BRANCH_UPDATED")
        assert update[2]["name"] == "Before"
        assert update[3]["name"] == "After"
        assert update[3]["phone"] == "+93 20 000 0000"

    def test_the_code_may_change_while_the_branch_has_no_history(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        created = branch_factory()  # type: ignore[operator]
        new_code = unique_branch_code()
        response = api_client.patch(
            f"{BRANCHES}/{created['id']}", json={"code": new_code}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["code"] == new_code

    def test_the_code_is_frozen_once_the_branch_has_financial_history(
        self, scratch_database: object
    ) -> None:
        """A branch referenced by the ledger may no longer be renamed.

        This runs against the test's **own** database: the probe row is a real journal
        entry and the schema is append-only, so it could never be removed from the shared
        database afterwards. Leaving financial history behind in the database every other
        test shares is exactly the hidden coupling the suite must not have.

        Everything runs inside one event loop: an async engine (asyncpg) is bound to the
        loop that created it, so a second ``asyncio.run`` against the same handle is a bug.
        """
        database_name = scratch_database("nexus_test_branch_freeze")  # type: ignore[operator]
        settings = settings_for_database(database_name)
        # One event loop for the whole scenario *including* disposal: an asyncpg
        # connection belongs to the loop that opened it, so handing it to a second
        # ``asyncio.run`` (or to the garbage collector) leaks the socket.
        asyncio.run(_freeze_scenario(settings, database_name))


async def _freeze_scenario(settings: object, database_name: str) -> None:
    """Seed a branch, prove it can be renamed, give it history, prove it cannot."""
    database = Database(settings)  # type: ignore[arg-type]
    service = build_master_data_service(database=database, settings=settings)  # type: ignore[arg-type]
    try:
        await _freeze_scenario_body(service, database)
    finally:
        await database.dispose()


async def _freeze_scenario_body(service: MasterDataService, database: Database) -> None:
    """The assertions of :func:`_freeze_scenario`, with the engine already open."""
    from sqlalchemy import text

    actor = ActorContext()
    branch_id = uuid.uuid4()
    admin_id = uuid.uuid4()

    # The scenario needs its own actors and branch rows; two statements, no fixtures.
    async with database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO users (id, username, password_hash, full_name) "
                "VALUES (:id, :username, 'x', 'Probe Admin')"
            ),
            {"id": admin_id, "username": f"probe-{uuid.uuid4().hex[:8]}"},
        )
        await session.execute(
            text("INSERT INTO branches (id, code, name) VALUES (:id, :code, 'Probe branch')"),
            {"id": branch_id, "code": unique_branch_code()},
        )

    # Without financial history the code may change — the rule is about history, not age.
    moved = await service.update_branch(
        branch_id=branch_id, changes={"code": unique_branch_code()}, actor=actor
    )
    assert moved.code.startswith("T")

    async with database.transaction() as session:
        await session.execute(
            text(
                "INSERT INTO journal_entries (id, reference_type, description, branch_id, "
                "created_by) VALUES (:id, 'MANUAL_ADJUSTMENT', 'freeze probe', :branch, :user)"
            ),
            {"id": uuid.uuid4(), "branch": branch_id, "user": admin_id},
        )

    with pytest.raises(ImmutableFieldError) as refusal:
        await service.update_branch(
            branch_id=branch_id, changes={"code": unique_branch_code()}, actor=actor
        )
    assert refusal.value.http_status == 409
    assert refusal.value.details["fields"] == [{"field": "code", "code": "immutable"}]

    # The name stays editable: only identity is frozen.
    renamed = await service.update_branch(
        branch_id=branch_id, changes={"name": "Renamed"}, actor=actor
    )
    assert renamed.name == "Renamed"

    def test_the_last_active_branch_cannot_be_deactivated(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        branch_id = fetch_scalar(
            main_database, "SELECT id FROM branches WHERE is_active = TRUE LIMIT 1"
        )
        active = fetch_scalar(main_database, "SELECT count(*) FROM branches WHERE is_active = TRUE")
        if int(active or 0) > 1:
            pytest.skip("this database has more than one active branch; covered by the guard test")
        response = api_client.patch(
            f"{BRANCHES}/{branch_id}", json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_deactivating_a_branch_is_allowed_while_another_stays_open(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        created = branch_factory()  # type: ignore[operator]
        response = api_client.patch(
            f"{BRANCHES}/{created['id']}", json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_active"] is False

    def test_an_unknown_branch_cannot_be_updated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{BRANCHES}/{uuid.uuid4()}", json={"name": "Ghost"}, headers=admin_headers
        )
        assert response.status_code == 404


class TestBranchAuthorization:
    def test_reads_require_branch_manage(self, api_client: TestClient, make_user: object) -> None:
        """A cashier holds ``exchange.view`` but not ``branch.manage``."""
        from tests.auth_helpers import login

        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        tokens = login(api_client, str(user["username"]), str(user["password"])).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        denied = api_client.get(BRANCHES, headers=headers)
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["required_permission"] == "branch.manage"

    def test_a_manager_cannot_manage_branches(
        self, api_client: TestClient, make_user: object
    ) -> None:
        from tests.auth_helpers import login

        user = make_user(roles=("MANAGER",))  # type: ignore[operator]
        tokens = login(api_client, str(user["username"]), str(user["password"])).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        denied = api_client.post(
            BRANCHES, json={"code": unique_branch_code(), "name": "X"}, headers=headers
        )
        assert denied.status_code == 403, denied.text
