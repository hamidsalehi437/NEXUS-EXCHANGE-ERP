"""Currency administration (PART 9, API_CONTRACT §9.2).

What these tests protect:

* **``code`` is identity.** It can never change — the frozen DDL raises ``NEX06`` — so the
  API does not even accept it in an update body (``extra="forbid"`` → 422).
* **Exactly one base currency.** Promoting a currency demotes the previous base in the
  same transaction and audits both rows; demoting the base outright is refused.
* **Deactivation, never deletion.** Part 25 applies to master data too, and the base
  currency cannot be deactivated at all.
* **Every write is audited** with the operator's identity, and reads are never audited
  (an audit trail full of GETs is a trail nobody can read).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from tests.helpers import execute_sql, fetch_all, fetch_scalar
from tests.masterdata_helpers import (
    CURRENCIES,
    create_currency,
    unique_currency_code,
)

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]

API = "/api/v1"


def _audit_rows(database: str, entity_id: str) -> list[tuple[object, ...]]:
    return fetch_all(
        database,
        "SELECT action, user_id, old_data, new_data FROM audit_logs "
        "WHERE entity_type = 'currency' AND entity_id = :entity_id ORDER BY seq",
        entity_id=entity_id,
    )


class TestCurrencyReads:
    def test_the_seeded_catalogue_is_listed_in_display_order(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        response = api_client.get(CURRENCIES, params={"limit": 200}, headers=admin_headers)
        assert response.status_code == 200, response.text
        body = response.json()
        codes = [row["code"] for row in body["items"]]
        assert "AFN" in codes and "USD" in codes, codes
        assert body["total"] == len(body["items"])
        # Ordering is (display_order, code): stable pickers, deterministic pagination.
        pairs = [(row["display_order"], row["code"]) for row in body["items"]]
        assert pairs == sorted(pairs), pairs

    def test_read_of_one_currency_and_unknown_id(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        listing = api_client.get(CURRENCIES, params={"code_hint": "AFN"}, headers=admin_headers)
        assert listing.status_code == 200
        afn = next(row for row in listing.json()["items"] if row["code"] == "AFN")

        found = api_client.get(f"{CURRENCIES}/{afn['id']}", headers=admin_headers)
        assert found.status_code == 200
        assert found.json()["is_base"] is True

        missing = api_client.get(f"{CURRENCIES}/{uuid.uuid4()}", headers=admin_headers)
        assert missing.status_code == 404
        assert missing.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_reads_do_not_write_audit_rows(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        api_client.get(CURRENCIES, headers=admin_headers)
        api_client.get(f"{CURRENCIES}", params={"is_active": True}, headers=admin_headers)
        assert fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") == before


class TestCurrencyCreation:
    def test_create_returns_201_and_is_audited(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        admin_user_id: str,
    ) -> None:
        code = unique_currency_code()
        response = create_currency(api_client, admin_headers, code=code, name="Test Krona")
        body = response.json()
        assert body["code"] == code
        assert body["is_base"] is False
        assert body["is_active"] is True

        rows = _audit_rows(main_database, body["id"])
        assert [row[0] for row in rows] == ["CURRENCY_CREATED"]
        assert str(rows[0][1]) == admin_user_id
        assert rows[0][3]["code"] == code

    def test_duplicate_code_is_refused_with_409(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        code = unique_currency_code()
        create_currency(api_client, admin_headers, code=code)
        duplicate = create_currency(api_client, admin_headers, code=code, expect=409)
        error = duplicate.json()["error"]
        assert error["code"] == "DUPLICATE_RESOURCE"
        assert error["details"]["fields"] == [{"field": "code", "code": "duplicate"}]

    @pytest.mark.parametrize("code", ["afn2", "A", "ABCDEFGHIJK", "A1N", "AF N"])
    def test_code_format_is_validated(
        self, api_client: TestClient, admin_headers: dict[str, str], code: str
    ) -> None:
        response = api_client.post(
            CURRENCIES, json={"code": code, "name": "Bad code"}, headers=admin_headers
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_lowercase_code_is_normalised_not_rejected(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        code = unique_currency_code()
        response = create_currency(api_client, admin_headers, code=code.lower())
        assert response.json()["code"] == code

    def test_decimal_places_bounds(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        for decimals in (-1, 7):
            response = create_currency(
                api_client, admin_headers, decimal_places=decimals, expect=422
            )
            assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_unknown_field_is_rejected(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.post(
            CURRENCIES,
            json={"code": unique_currency_code(), "name": "X", "is_base_currency": True},
            headers=admin_headers,
        )
        assert response.status_code == 422


class TestBaseCurrencyRules:
    def test_creating_a_second_base_demotes_the_previous_one_atomically(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        code = unique_currency_code()
        response = create_currency(api_client, admin_headers, code=code, is_base=True)
        assert response.status_code == 201, response.text
        new_id = response.json()["id"]

        bases = fetch_all(
            main_database, "SELECT id, code FROM currencies WHERE is_base ORDER BY code"
        )
        assert len(bases) == 1, bases
        assert bases[0][1] == code

        # Both sides of the change are recorded: the promoted and the demoted currency.
        promoted = _audit_rows(main_database, new_id)
        assert promoted[0][0] == "CURRENCY_CREATED"
        demotions = fetch_all(
            main_database,
            "SELECT entity_id, old_data, new_data FROM audit_logs "
            "WHERE action = 'CURRENCY_UPDATED' AND new_data->>'is_base' = 'false' "
            "ORDER BY seq DESC LIMIT 1",
        )
        assert demotions, "the replaced base currency was not audited"
        assert demotions[0][2]["reason"]

    def test_the_base_currency_cannot_be_demoted(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        listing = api_client.get(CURRENCIES, headers=admin_headers).json()["items"]
        base = next(row for row in listing if row["is_base"])
        response = api_client.patch(
            f"{CURRENCIES}/{base['id']}", json={"is_base": False}, headers=admin_headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_the_base_currency_cannot_be_deactivated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        listing = api_client.get(CURRENCIES, headers=admin_headers).json()["items"]
        base = next(row for row in listing if row["is_base"])
        response = api_client.patch(
            f"{CURRENCIES}/{base['id']}", json={"is_active": False}, headers=admin_headers
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "CONFLICT"

    def test_promotion_can_be_done_through_patch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_currency(api_client, admin_headers).json()
        response = api_client.patch(
            f"{CURRENCIES}/{created['id']}", json={"is_base": True}, headers=admin_headers
        )
        assert response.status_code == 200, response.text
        assert response.json()["is_base"] is True
        assert fetch_scalar(main_database, "SELECT count(*) FROM currencies WHERE is_base") == 1


class TestCurrencyUpdates:
    def test_patch_updates_and_audits_the_field_diff(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_currency(api_client, admin_headers, name="Before").json()
        response = api_client.patch(
            f"{CURRENCIES}/{created['id']}",
            json={"name": "After", "symbol": "T$", "display_order": 7},
            headers=admin_headers,
        )
        assert response.status_code == 200, response.text
        assert response.json()["name"] == "After"

        rows = _audit_rows(main_database, created["id"])
        update = next(row for row in rows if row[0] == "CURRENCY_UPDATED")
        assert update[2]["name"] == "Before"
        assert update[3]["name"] == "After"
        assert update[3]["symbol"] == "T$"

    def test_a_no_op_patch_writes_no_audit_row(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        created = create_currency(api_client, admin_headers, name="Unchanged").json()
        before = len(_audit_rows(main_database, created["id"]))
        response = api_client.patch(
            f"{CURRENCIES}/{created['id']}", json={"name": "Unchanged"}, headers=admin_headers
        )
        assert response.status_code == 200
        assert len(_audit_rows(main_database, created["id"])) == before

    def test_the_code_cannot_be_changed_through_the_api(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = create_currency(api_client, admin_headers).json()
        response = api_client.patch(
            f"{CURRENCIES}/{created['id']}", json={"code": "XXX"}, headers=admin_headers
        )
        # Rejected by the schema (the field does not exist in the update model) — so the
        # request never reaches the trigger that would raise NEX06.
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_the_database_also_refuses_a_direct_code_change(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Defence in depth: even a raw SQL update is stopped by the frozen DDL."""
        created = create_currency(api_client, admin_headers).json()
        with pytest.raises(Exception) as excinfo:
            execute_sql(
                main_database,
                "UPDATE currencies SET code = 'ZZZ' WHERE id = :currency_id",
                currency_id=created["id"],
            )
        assert "NEX06" in str(excinfo.value) or "immutable" in str(excinfo.value).lower()

    def test_update_of_an_unknown_currency_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        response = api_client.patch(
            f"{CURRENCIES}/{uuid.uuid4()}", json={"name": "Nope"}, headers=admin_headers
        )
        assert response.status_code == 404

    def test_a_currency_can_be_deactivated_and_reactivated(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        created = create_currency(api_client, admin_headers).json()
        off = api_client.patch(
            f"{CURRENCIES}/{created['id']}", json={"is_active": False}, headers=admin_headers
        )
        assert off.status_code == 200 and off.json()["is_active"] is False
        on = api_client.patch(
            f"{CURRENCIES}/{created['id']}", json={"is_active": True}, headers=admin_headers
        )
        assert on.status_code == 200 and on.json()["is_active"] is True


class TestCurrencyAuthorization:
    def test_listing_requires_authentication(self, api_client: TestClient) -> None:
        response = api_client.get(CURRENCIES)
        assert response.status_code == 401

    def test_a_cashier_may_read_but_not_write(
        self, api_client: TestClient, make_user: object
    ) -> None:
        from tests.auth_helpers import login

        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        tokens = login(api_client, str(user["username"]), str(user["password"])).json()
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        assert api_client.get(CURRENCIES, headers=headers).status_code == 200

        denied = api_client.post(
            CURRENCIES, json={"code": unique_currency_code(), "name": "X"}, headers=headers
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["details"]["required_permission"] == "settings.manage"

    def test_an_accountant_cannot_manage_currencies(
        self, api_client: TestClient, accountant_headers: dict[str, str]
    ) -> None:
        """ACCOUNTANT lacks ``settings.manage``: currencies are business settings."""
        denied = api_client.post(
            CURRENCIES,
            json={"code": unique_currency_code(), "name": "X"},
            headers=accountant_headers,
        )
        assert denied.status_code == 403, denied.text
        assert denied.json()["error"]["code"] == "PERMISSION_DENIED"
