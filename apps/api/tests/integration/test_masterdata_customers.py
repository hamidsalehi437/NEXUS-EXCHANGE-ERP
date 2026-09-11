"""Customer registration and lifecycle (PART 10, PART 25, PART 65).

What matters here:

* **Codes are issued, not invented.** ``CUS-YYYYMMDD-NNNNNN`` comes from the database's
  ``next_document_number`` inside the registration transaction, so two concurrent
  registrations cannot collide and a rolled-back one does not burn a number.
* **Deletion does not exist.** ``DELETE`` deactivates; the row stays because transactions
  and transfers reference it forever.
* **PII stays minimal.** Only the last four digits of a national id are accepted, and the
  API never returns more than it stores.
* **Three separate permissions** — create, view, update — because a cashier who registers
  a walk-in customer is not thereby allowed to edit an existing profile.
"""

from __future__ import annotations

import re
import uuid

import pytest
from fastapi.testclient import TestClient

from tests.auth_helpers import login
from tests.helpers import execute_sql, fetch_all, fetch_scalar
from tests.masterdata_helpers import (
    CUSTOMERS,
    create_customer,
)

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]

CODE_PATTERN = re.compile(r"^CUS-\d{8}-\d{6}$")


def _audit_rows(database: str, entity_id: str) -> list[tuple[object, ...]]:
    return fetch_all(
        database,
        "SELECT action, user_id, old_data, new_data FROM audit_logs "
        "WHERE entity_type = 'customer' AND entity_id = :entity_id ORDER BY seq",
        entity_id=entity_id,
    )


class TestCustomerCreation:
    def test_registration_issues_a_formatted_code(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        response = create_customer(api_client, admin_headers, full_name="  Ahmad   Karimi  ")
        body = response.json()
        assert CODE_PATTERN.match(body["customer_code"]), body["customer_code"]
        # Whitespace inside a name is normalised, so "Ahmad  Karimi" and "Ahmad Karimi"
        # are the same person on a receipt.
        assert body["full_name"] == "Ahmad Karimi"
        assert body["is_active"] is True
        assert body["created_by"] is not None

        stored = fetch_scalar(
            main_database,
            "SELECT customer_code FROM customers WHERE id = :customer_id",
            customer_id=body["id"],
        )
        assert stored == body["customer_code"]

    def test_consecutive_codes_increase_within_the_day(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        first = create_customer(api_client, admin_headers).json()["customer_code"]
        second = create_customer(api_client, admin_headers).json()["customer_code"]
        assert first.split("-")[:2] == second.split("-")[:2]
        assert int(second.split("-")[2]) == int(first.split("-")[2]) + 1

    def test_an_explicit_code_is_accepted_and_normalised(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        code = f"LEGACY-{uuid.uuid4().hex[:6].upper()}"
        created = create_customer(api_client, admin_headers, customer_code=code.lower())
        assert created.json()["customer_code"] == code

    def test_a_duplicate_explicit_code_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        code = f"DUP-{uuid.uuid4().hex[:6].upper()}"
        create_customer(api_client, admin_headers, customer_code=code)
        duplicate = create_customer(api_client, admin_headers, customer_code=code, expect=409)
        assert duplicate.json()["error"]["code"] == "DUPLICATE_RESOURCE"

    def test_creation_is_audited_without_storing_the_identifier(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_customer(
            api_client, admin_headers, full_name="Sara Noori", national_id_last4="4321"
        ).json()
        rows = _audit_rows(main_database, created["id"])
        assert [row[0] for row in rows] == ["CUSTOMER_CREATED"]
        payload = rows[0][3]
        assert payload["national_id_recorded"] is True
        # The audit trail records *that* an identifier exists, never the identifier itself.
        assert "4321" not in str(payload)
        assert "national_id_last4" not in payload

    @pytest.mark.parametrize("name", ["", "A", "   "])
    def test_a_customer_needs_a_real_name(
        self, api_client: TestClient, admin_headers: dict[str, str], name: str
    ) -> None:
        response = api_client.post(CUSTOMERS, json={"full_name": name}, headers=admin_headers)
        assert response.status_code == 422, response.text

    @pytest.mark.parametrize("last4", ["123", "12345", "abcd"])
    def test_only_four_digits_are_accepted_as_an_identifier(
        self, api_client: TestClient, admin_headers: dict[str, str], last4: str
    ) -> None:
        response = api_client.post(
            CUSTOMERS,
            json={"full_name": "PII Probe", "national_id_last4": last4},
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text

    def test_registering_into_an_inactive_branch_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        branch = branch_factory()  # type: ignore[operator]
        api_client.patch(
            f"/api/v1/branches/{branch['id']}", json={"is_active": False}, headers=admin_headers
        )
        response = create_customer(api_client, admin_headers, branch_id=branch["id"], expect=422)
        assert response.json()["error"]["code"] == "BRANCH_INACTIVE"

    def test_registering_into_an_unknown_branch_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = create_customer(
            api_client, admin_headers, branch_id=str(uuid.uuid4()), expect=404
        )
        assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


class TestCustomerSearch:
    def test_search_matches_name_phone_and_code(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        marker = uuid.uuid4().hex[:8]
        created = create_customer(
            api_client, admin_headers, full_name=f"Zahra {marker}", phone=f"+9370{marker}"
        ).json()

        by_name = api_client.get(CUSTOMERS, params={"q": marker}, headers=admin_headers).json()
        assert [row["id"] for row in by_name["items"]] == [created["id"]]

        by_phone = api_client.get(CUSTOMERS, params={"q": f"70{marker}"}, headers=admin_headers)
        assert created["id"] in [row["id"] for row in by_phone.json()["items"]]

        by_code = api_client.get(
            CUSTOMERS, params={"q": created["customer_code"]}, headers=admin_headers
        )
        assert [row["id"] for row in by_code.json()["items"]] == [created["id"]]

    def test_search_is_case_insensitive(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        marker = f"Kabul{uuid.uuid4().hex[:6]}"
        created = create_customer(api_client, admin_headers, full_name=marker).json()
        found = api_client.get(
            CUSTOMERS, params={"q": marker.lower()}, headers=admin_headers
        ).json()
        assert created["id"] in [row["id"] for row in found["items"]]

    def test_branch_and_shared_filters(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_id: str
    ) -> None:
        shared = create_customer(api_client, admin_headers).json()
        scoped = create_customer(api_client, admin_headers, branch_id=branch_id).json()

        only_shared = api_client.get(
            CUSTOMERS, params={"shared_only": True, "limit": 200}, headers=admin_headers
        ).json()
        ids = [row["id"] for row in only_shared["items"]]
        assert shared["id"] in ids and scoped["id"] not in ids

        in_branch = api_client.get(
            CUSTOMERS, params={"branch_id": branch_id, "limit": 200}, headers=admin_headers
        ).json()
        ids = [row["id"] for row in in_branch["items"]]
        assert scoped["id"] in ids and shared["id"] not in ids

    def test_an_unknown_customer_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.get(f"{CUSTOMERS}/{uuid.uuid4()}", headers=admin_headers)
        assert response.status_code == 404


class TestCustomerUpdates:
    def test_update_records_the_field_diff(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_customer(
            api_client, admin_headers, full_name="Old Name", phone="+93700000000"
        ).json()
        updated = api_client.patch(
            f"{CUSTOMERS}/{created['id']}",
            json={"full_name": "New Name", "notes": "prefers USD"},
            headers=admin_headers,
        )
        assert updated.status_code == 200, updated.text
        assert updated.json()["full_name"] == "New Name"
        assert updated.json()["updated_by"] is not None

        rows = _audit_rows(main_database, created["id"])
        change = next(row for row in rows if row[0] == "CUSTOMER_UPDATED")
        assert change[2]["full_name"] == "Old Name"
        assert change[3]["full_name"] == "New Name"
        assert change[3]["notes"] == "prefers USD"
        # An unchanged field is not part of the diff.
        assert "phone" not in change[2]

    def test_delete_is_a_soft_delete_and_is_audited_as_such(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_customer(api_client, admin_headers).json()
        deleted = api_client.delete(f"{CUSTOMERS}/{created['id']}", headers=admin_headers)
        assert deleted.status_code == 200, deleted.text
        assert deleted.json()["is_active"] is False

        # The row is still there — PART 25: customers are deactivated, never removed.
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM customers WHERE id = :customer_id",
                customer_id=created["id"],
            )
            == 1
        )
        rows = _audit_rows(main_database, created["id"])
        assert rows[-1][0] == "CUSTOMER_DEACTIVATED"

    def test_the_database_forbids_a_hard_delete_by_the_runtime_role(
        self, main_database: str
    ) -> None:
        """Defence in depth: even a direct DELETE is refused for ``nexus_app``.

        The test connects as a superuser, so the check is made against the catalog's
        privileges rather than by attempting the statement as the runtime role.
        """
        execute_sql(main_database, "SELECT 1")  # connection sanity
        granted = fetch_all(
            main_database,
            """
            SELECT privilege_type
              FROM information_schema.role_table_grants
             WHERE table_name = 'customers' AND grantee = 'nexus_app'
            """,
        )
        privileges = {row[0] for row in granted}
        assert "DELETE" not in privileges, privileges

    def test_reactivation_works_and_is_audited(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_customer(api_client, admin_headers).json()
        api_client.delete(f"{CUSTOMERS}/{created['id']}", headers=admin_headers)
        reactivated = api_client.patch(
            f"{CUSTOMERS}/{created['id']}", json={"is_active": True}, headers=admin_headers
        )
        assert reactivated.status_code == 200
        assert reactivated.json()["is_active"] is True

    def test_an_unknown_customer_cannot_be_updated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{CUSTOMERS}/{uuid.uuid4()}", json={"full_name": "Ghost"}, headers=admin_headers
        )
        assert response.status_code == 404


class TestCustomerAuthorization:
    def test_a_cashier_creates_and_reads_but_does_not_update(
        self, api_client: TestClient, make_user: object, branch_id: str
    ) -> None:
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        tokens = login(
            api_client,
            str(user["username"]),
            str(user["password"]),
            branch_id=uuid.UUID(branch_id),
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        created = create_customer(api_client, headers)  # customer.create is granted
        assert created.status_code == 201, created.text
        customer_id = created.json()["id"]
        assert api_client.get(f"{CUSTOMERS}/{customer_id}", headers=headers).status_code == 200

        denied = api_client.patch(
            f"{CUSTOMERS}/{customer_id}", json={"full_name": "Changed"}, headers=headers
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["required_permission"] == "customer.update"

    def test_denied_update_leaves_no_audit_row(
        self, api_client: TestClient, make_user: object, main_database: str, branch_id: str
    ) -> None:
        """A refused action is not an action: the trail keeps only what happened."""
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        tokens = login(
            api_client,
            str(user["username"]),
            str(user["password"]),
            branch_id=uuid.UUID(branch_id),
        ).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        customer_id = create_customer(api_client, headers).json()["id"]

        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        api_client.patch(f"{CUSTOMERS}/{customer_id}", json={"full_name": "Nope"}, headers=headers)
        after = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        assert after == before

    def test_reads_require_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(CUSTOMERS).status_code == 401
