"""Exchange rates: append-only publication and resolution (PART 13, PART 62).

The rules this suite pins down:

* **A published quote is permanent.** There is no PATCH and no DELETE; a correction is a
  newer quote, and the append-only trigger refuses an ``UPDATE``/``DELETE`` even by a
  direct SQL client (the test attempts one through the repository's own transaction).
* **One quote per pair, branch and instant.** Re-publishing the same instant would make
  "the rate in force" ambiguous, so it is refused (409) rather than silently replaced.
* **Resolution has one definition.** ``resolve_exchange_rate`` decides: the branch quote
  wins over the global one, then the newest instant not in the future. The API calls that
  function; these tests call the API.
* **Rates are money-shaped.** They arrive as decimal strings, never floats, and come back
  as decimal strings — a float would already have lost precision before validation.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.auth_helpers import bearer, login
from tests.helpers import execute_sql, fetch_all, fetch_scalar
from tests.masterdata_helpers import RATES, create_currency

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]


def pair(
    client: TestClient, headers: dict[str, str]
) -> tuple[dict[str, object], dict[str, object]]:
    """Two freshly created tradable currencies — the suite never depends on the seed."""
    return create_currency(client, headers).json(), create_currency(client, headers).json()


def publish(
    client: TestClient,
    headers: dict[str, str],
    source: dict[str, object],
    target: dict[str, object],
    *,
    buy: str = "72.5000000000",
    sell: str = "73.2500000000",
    effective_at: str | None = None,
    branch_id: str | None = None,
    rate_source: str = "MANUAL",
    expect: int | None = 201,
):
    body: dict[str, object] = {
        "from_currency_id": source["id"],
        "to_currency_id": target["id"],
        "buy_rate": buy,
        "sell_rate": sell,
        "source": rate_source,
    }
    if effective_at is not None:
        body["effective_at"] = effective_at
    if branch_id is not None:
        body["branch_id"] = branch_id
    response = client.post(RATES, json=body, headers=headers)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def resolve(
    client: TestClient,
    headers: dict[str, str],
    source: dict[str, object],
    target: dict[str, object],
    *,
    branch_id: str | None = None,
    at: str | None = None,
    expect: int | None = 200,
):
    params: dict[str, object] = {
        "from_currency_id": source["id"],
        "to_currency_id": target["id"],
    }
    if branch_id is not None:
        params["branch_id"] = branch_id
    if at is not None:
        params["at"] = at
    response = client.get(f"{RATES}/resolve", params=params, headers=headers)
    if expect is not None:
        assert response.status_code == expect, response.text
    return response


def at(offset_minutes: int = 0) -> str:
    """A UTC instant offset from now, as a wire-format string."""
    return (dt.datetime.now(tz=dt.UTC) + dt.timedelta(minutes=offset_minutes)).isoformat()


class TestRatePublication:
    def test_a_published_quote_keeps_its_exact_decimal_value(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        created = publish(
            api_client,
            admin_headers,
            source,
            target,
            buy="72.1234567891",
            sell="73.9876543219",
        ).json()

        # On the wire the value is a decimal string: JSON numbers are floats and would
        # round-trip through binary floating point (PART 62).
        assert created["buy_rate"] == "72.1234567891"
        assert created["sell_rate"] == "73.9876543219"
        stored = fetch_scalar(
            main_database,
            "SELECT buy_rate FROM exchange_rates WHERE id = :rate_id",
            rate_id=created["id"],
        )
        assert stored == Decimal("72.1234567891")

    def test_a_json_float_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        response = api_client.post(
            RATES,
            json={
                "from_currency_id": source["id"],
                "to_currency_id": target["id"],
                "buy_rate": 72.5,
                "sell_rate": 73.25,
            },
            headers=admin_headers,
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    @pytest.mark.parametrize("value", ["0", "-1", "0.0000000000"])
    def test_a_non_positive_rate_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], value: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        response = publish(api_client, admin_headers, source, target, buy=value, expect=None)
        assert response.status_code == 422, f"{value!r}: {response.text}"

    def test_a_quote_needs_two_different_currencies(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        currency = create_currency(api_client, admin_headers).json()
        response = publish(api_client, admin_headers, currency, currency, expect=422)
        assert response.json()["error"]["details"]["fields"][0]["code"] == "same_as_source"

    def test_every_publication_writes_exactly_one_audit_row(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        created = publish(api_client, admin_headers, source, target).json()

        rows = fetch_all(
            main_database,
            "SELECT action, old_data, new_data FROM audit_logs "
            "WHERE entity_type = 'exchange_rate' AND entity_id = :rate_id",
            rate_id=created["id"],
        )
        assert [row[0] for row in rows] == ["RATE_CREATED"]
        assert rows[0][1] is None  # appending has no old value
        payload = rows[0][2]
        assert payload["from_currency"] == source["code"]
        assert payload["to_currency"] == target["code"]
        assert payload["buy_rate"] == "72.5000000000"
        assert payload["branch_id"] is None
        assert fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") == before + 1

    def test_a_correction_is_a_new_row_that_supersedes_the_previous_one(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        first = publish(
            api_client, admin_headers, source, target, buy="70", effective_at=at(-60)
        ).json()
        second = publish(
            api_client, admin_headers, source, target, buy="71", effective_at=at(-30)
        ).json()
        assert first["id"] != second["id"]
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM exchange_rates "
                "WHERE from_currency_id = :source AND to_currency_id = :target",
                source=source["id"],
                target=target["id"],
            )
            == 2
        )
        resolved = resolve(api_client, admin_headers, source, target).json()
        assert resolved["exchange_rate_id"] == second["id"]
        assert resolved["buy_rate"] == "71.0000000000"

    def test_the_same_instant_cannot_be_published_twice(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        moment = at(-5)
        publish(api_client, admin_headers, source, target, effective_at=moment)
        duplicate = publish(
            api_client, admin_headers, source, target, buy="99", effective_at=moment, expect=409
        )
        assert duplicate.json()["error"]["code"] == "DUPLICATE_RESOURCE"
        assert duplicate.json()["error"]["details"]["fields"][0]["field"] == "effective_at"

    def test_the_same_instant_is_free_for_another_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        """Uniqueness is per (pair, branch, instant) — COALESCE(branch) is part of the key."""
        source, target = pair(api_client, admin_headers)
        branch = branch_factory()  # type: ignore[operator]
        moment = at(-5)
        publish(api_client, admin_headers, source, target, effective_at=moment)
        other = publish(
            api_client,
            admin_headers,
            source,
            target,
            effective_at=moment,
            branch_id=branch["id"],
        )
        assert other.status_code == 201, other.text

    def test_an_inactive_currency_cannot_be_quoted(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        api_client.patch(
            f"/api/v1/currencies/{target['id']}",
            json={"is_active": False},
            headers=admin_headers,
        )
        response = publish(api_client, admin_headers, source, target, expect=422)
        assert response.json()["error"]["code"] == "CURRENCY_INACTIVE"
        assert response.json()["error"]["details"]["fields"][0]["field"] == "to_currency_id"

    def test_a_non_tradable_currency_cannot_be_quoted(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        api_client.patch(
            f"/api/v1/currencies/{target['id']}",
            json={"is_tradable": False},
            headers=admin_headers,
        )
        response = publish(api_client, admin_headers, source, target, expect=409)
        assert response.json()["error"]["code"] == "CONFLICT"
        assert response.json()["error"]["details"]["fields"][0]["code"] == "not_tradable"

    def test_an_inactive_branch_cannot_carry_a_quote(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        source, target = pair(api_client, admin_headers)
        branch = branch_factory()  # type: ignore[operator]
        api_client.patch(
            f"/api/v1/branches/{branch['id']}", json={"is_active": False}, headers=admin_headers
        )
        response = publish(
            api_client, admin_headers, source, target, branch_id=branch["id"], expect=422
        )
        assert response.json()["error"]["code"] == "BRANCH_INACTIVE"

    def test_an_unknown_currency_is_a_404(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, _ = pair(api_client, admin_headers)
        response = api_client.post(
            RATES,
            json={
                "from_currency_id": source["id"],
                "to_currency_id": str(uuid.uuid4()),
                "buy_rate": "1",
                "sell_rate": "1",
            },
            headers=admin_headers,
        )
        assert response.status_code == 404, response.text

    @pytest.mark.parametrize("source_name", ["CENTRAL_BANK", "partner"])
    def test_a_source_is_normalised_and_validated(
        self, api_client: TestClient, admin_headers: dict[str, str], source_name: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        created = publish(api_client, admin_headers, source, target, rate_source=source_name).json()
        assert created["source"] == source_name.strip().upper()

    def test_an_unknown_source_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        response = publish(
            api_client, admin_headers, source, target, rate_source="TELEPATHY", expect=422
        )
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_a_naive_effective_at_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """An instant without a timezone is a different instant in every branch."""
        source, target = pair(api_client, admin_headers)
        response = publish(
            api_client,
            admin_headers,
            source,
            target,
            effective_at="2026-01-01T10:00:00",
            expect=422,
        )
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestRateAppendOnly:
    """A quote is published once and never edited.

    The approved Phase 0 DDL has no append-only trigger on ``exchange_rates`` (unlike
    ``journal_lines`` or ``audit_logs``), and it grants ``UPDATE`` to the runtime role — so
    the guarantee is structural: there is no route, no service method and no repository
    method that can change a row, and a correction is a new quote. These tests pin all
    three statements down, so if any layer grows an edit path the build fails. The direct
    ``UPDATE`` below documents the boundary honestly: a privileged client *can* run one,
    and the table's ``change_log`` trigger records it.
    """

    def test_the_api_exposes_no_way_to_change_a_quote(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """Read the real route table: the app documentation UI is disabled in test mode."""
        routes = {
            (route.path, method)
            for route in api_client.app.routes  # type: ignore[attr-defined]
            for method in getattr(route, "methods", set())
            if route.path.startswith("/api/v1/rates")
        }
        assert routes == {
            ("/api/v1/rates", "GET"),
            ("/api/v1/rates", "POST"),
            ("/api/v1/rates/resolve", "GET"),
            ("/api/v1/rates/history", "GET"),
        }, routes

        source, target = pair(api_client, admin_headers)
        created = publish(api_client, admin_headers, source, target).json()
        for call in (
            api_client.patch(
                f"{RATES}/{created['id']}", json={"buy_rate": "1"}, headers=admin_headers
            ),
            api_client.put(
                f"{RATES}/{created['id']}", json={"buy_rate": "1"}, headers=admin_headers
            ),
            api_client.delete(f"{RATES}/{created['id']}", headers=admin_headers),
        ):
            assert call.status_code in {404, 405}, call.text

    def test_the_repository_and_service_offer_no_mutation(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        from app.repositories.ledger_master import ExchangeRateRepository
        from app.services.ledger_master_service import RateService

        for name in ("update", "delete", "remove", "set_rate", "correct"):
            assert not hasattr(ExchangeRateRepository, name), name
            assert not hasattr(RateService, name), name

    def test_a_direct_privileged_update_is_possible_and_recorded(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The honest boundary: the DDL grants UPDATE, so here is what happens if it runs.

        It is *recorded* (``change_log`` keeps the row's before/after image for the sync
        engine) but it does not go through the API, and the audit chain has no entry for
        it — which is exactly why no application path exists. Closing this at the database
        level needs a schema change and is recommended for the phase that owns the schema,
        not smuggled in here.
        """
        source, target = pair(api_client, admin_headers)
        created = publish(api_client, admin_headers, source, target).json()
        before = fetch_scalar(main_database, "SELECT count(*) FROM change_log")

        execute_sql(
            main_database,
            "UPDATE exchange_rates SET buy_rate = 1 WHERE id = :rate_id",
            rate_id=created["id"],
        )
        assert fetch_scalar(main_database, "SELECT count(*) FROM change_log") == before + 1
        assert fetch_scalar(
            main_database,
            "SELECT buy_rate FROM exchange_rates WHERE id = :rate_id",
            rate_id=created["id"],
        ) == Decimal("1")

    def test_the_runtime_role_may_not_delete_a_quote(self, main_database: str) -> None:
        grants = {
            row[0]
            for row in fetch_all(
                main_database,
                "SELECT privilege_type FROM information_schema.role_table_grants "
                "WHERE table_name = 'exchange_rates' AND grantee = 'nexus_app'",
            )
        }
        assert grants == {"SELECT", "INSERT", "UPDATE"}, grants
        assert "DELETE" not in grants


class TestRateResolution:
    def test_a_branch_quote_overrides_the_global_one(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        source, target = pair(api_client, admin_headers)
        branch = branch_factory()  # type: ignore[operator]
        publish(
            api_client,
            admin_headers,
            source,
            target,
            buy="70",
            sell="71",
            effective_at=at(-120),
        )
        publish(
            api_client,
            admin_headers,
            source,
            target,
            buy="75",
            sell="76",
            branch_id=branch["id"],
            effective_at=at(-60),
        )

        global_quote = resolve(api_client, admin_headers, source, target).json()
        assert global_quote["buy_rate"] == "70.0000000000"
        assert global_quote["is_branch_quote"] is False
        assert global_quote["branch_id"] is None

        branch_quote = resolve(
            api_client, admin_headers, source, target, branch_id=branch["id"]
        ).json()
        assert branch_quote["buy_rate"] == "75.0000000000"
        assert branch_quote["is_branch_quote"] is True
        assert branch_quote["branch_id"] == branch["id"]

        # A branch without its own quote falls back to the global one.
        other = branch_factory()  # type: ignore[operator]
        fallback = resolve(api_client, admin_headers, source, target, branch_id=other["id"]).json()
        assert fallback["buy_rate"] == "70.0000000000"
        assert fallback["is_branch_quote"] is False

    def test_the_newest_effective_quote_wins(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        publish(api_client, admin_headers, source, target, buy="68", effective_at=at(-180))
        publish(api_client, admin_headers, source, target, buy="69", effective_at=at(-120))
        publish(api_client, admin_headers, source, target, buy="70", effective_at=at(-60))
        assert resolve(api_client, admin_headers, source, target).json()["buy_rate"] == (
            "70.0000000000"
        )

    def test_a_future_quote_is_not_in_force_yet(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        publish(api_client, admin_headers, source, target, buy="80", effective_at=at(-30))
        publish(api_client, admin_headers, source, target, buy="90", effective_at=at(120))

        assert resolve(api_client, admin_headers, source, target).json()["buy_rate"] == (
            "80.0000000000"
        )
        # The documented use of ``at``: the counter asks for the instant the transaction
        # is being recorded at, so a rate can be reproduced exactly later.
        assert (
            resolve(api_client, admin_headers, source, target, at=at(-15)).json()["buy_rate"]
            == "80.0000000000"
        )

    def test_a_pair_with_no_quote_is_a_rate_not_found(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, target = pair(api_client, admin_headers)
        response = resolve(api_client, admin_headers, source, target, expect=422)
        assert response.json()["error"]["code"] == "RATE_NOT_FOUND"
        details = response.json()["error"]["details"]
        assert details["from_currency_id"] == source["id"]
        assert details["to_currency_id"] == target["id"]

    def test_resolution_never_invents_an_inverse_rate(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        """A quote for A→B says nothing about B→A; the desks publish both."""
        source, target = pair(api_client, admin_headers)
        publish(api_client, admin_headers, source, target)
        resolve(api_client, admin_headers, source, target)
        resolve(api_client, admin_headers, target, source, expect=422)

    def test_current_quotes_show_the_quote_in_force_per_pair(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        source, target = pair(api_client, admin_headers)
        branch = branch_factory()  # type: ignore[operator]
        publish(api_client, admin_headers, source, target, buy="70", effective_at=at(-90))
        publish(
            api_client,
            admin_headers,
            source,
            target,
            buy="75",
            branch_id=branch["id"],
            effective_at=at(-45),
        )

        def quote_at(branch_id: str | None) -> dict[str, object]:
            params: dict[str, object] = {
                "from_currency_id": source["id"],
                "to_currency_id": target["id"],
                "at": at(),
            }
            if branch_id is not None:
                params["branch_id"] = branch_id
            response = api_client.get(RATES, params=params, headers=admin_headers)
            assert response.status_code == 200, response.text
            items = response.json()["items"]
            assert len(items) == 1, items  # one row per pair, never one per candidate
            return items[0]

        assert quote_at(None)["buy_rate"] == "70.0000000000"  # the global quote
        scoped = quote_at(branch["id"])
        assert scoped["buy_rate"] == "75.0000000000"  # the branch quote wins
        assert scoped["branch_code"] == branch["code"]

        # A branch with no quote of its own falls back to the global row.
        other = branch_factory()  # type: ignore[operator]
        assert quote_at(other["id"])["buy_rate"] == "70.0000000000"

    def test_a_pair_filter_needs_both_currencies(
        self, api_client: TestClient, admin_headers: dict[str, str]
    ) -> None:
        source, _ = pair(api_client, admin_headers)
        response = api_client.get(
            RATES, params={"from_currency_id": source["id"]}, headers=admin_headers
        )
        assert response.status_code == 422, response.text
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"


class TestRateHistory:
    def test_history_lists_every_quote_newest_first_and_filterable(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        source, target = pair(api_client, admin_headers)
        branch = branch_factory()  # type: ignore[operator]
        older = publish(
            api_client, admin_headers, source, target, buy="70", effective_at=at(-90)
        ).json()
        newer = publish(
            api_client, admin_headers, source, target, buy="71", effective_at=at(-30)
        ).json()
        scoped = publish(
            api_client,
            admin_headers,
            source,
            target,
            buy="72",
            effective_at=at(-60),
            branch_id=branch["id"],
        ).json()

        history = api_client.get(
            RATES + "/history",
            params={"from_currency_id": source["id"], "to_currency_id": target["id"], "limit": 50},
            headers=admin_headers,
        ).json()
        ids = [row["id"] for row in history["items"]]
        assert ids[:3] == [newer["id"], scoped["id"], older["id"]]
        assert history["total"] >= 3

        global_only = api_client.get(
            RATES + "/history",
            params={
                "from_currency_id": source["id"],
                "to_currency_id": target["id"],
                "branch_id": branch["id"],
                "include_global": False,
            },
            headers=admin_headers,
        ).json()
        assert [row["id"] for row in global_only["items"]] == [scoped["id"]]

    def test_a_branch_history_is_scoped_to_that_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], branch_factory: object
    ) -> None:
        source, target = pair(api_client, admin_headers)
        first = branch_factory()  # type: ignore[operator]
        second = branch_factory()  # type: ignore[operator]
        publish(
            api_client, admin_headers, source, target, branch_id=first["id"], effective_at=at(-50)
        )
        publish(
            api_client, admin_headers, source, target, branch_id=second["id"], effective_at=at(-40)
        )
        rows = api_client.get(
            RATES + "/history",
            params={"branch_id": first["id"], "include_global": False},
            headers=admin_headers,
        ).json()["items"]
        assert [row["branch_id"] for row in rows] == [first["id"]]

    def test_reading_history_writes_no_audit_row(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        source, target = pair(api_client, admin_headers)
        publish(api_client, admin_headers, source, target)
        before = fetch_scalar(main_database, "SELECT count(*) FROM audit_logs")
        api_client.get(RATES, headers=admin_headers)
        api_client.get(RATES + "/history", headers=admin_headers)
        resolve(api_client, admin_headers, source, target)
        assert fetch_scalar(main_database, "SELECT count(*) FROM audit_logs") == before


class TestRateAuthorization:
    def test_a_cashier_reads_quotes_but_cannot_publish(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        make_user: object,
        branch_id: str,
    ) -> None:
        """``exchange.view`` lets the counter see the rate; ``rates.manage`` publishes it."""
        user = make_user(roles=("CASHIER",))  # type: ignore[operator]
        tokens = login(
            api_client,
            str(user["username"]),
            str(user["password"]),
            branch_id=uuid.UUID(branch_id),
        ).json()
        headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

        source, target = pair(api_client, admin_headers)
        publish(api_client, admin_headers, source, target)

        read = api_client.get(
            RATES,
            params={"from_currency_id": source["id"], "to_currency_id": target["id"]},
            headers=headers,
        )
        assert read.status_code == 200, read.text
        resolved = api_client.get(
            f"{RATES}/resolve",
            params={"from_currency_id": source["id"], "to_currency_id": target["id"]},
            headers=headers,
        )
        assert resolved.status_code == 200, resolved.text

        denied = publish(api_client, headers, source, target, expect=403)
        assert denied.json()["error"]["details"]["required_permission"] == "rates.manage"

    def test_the_accountant_reads_quotes_but_cannot_publish(
        self,
        api_client: TestClient,
        accountant_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        source, target = pair(api_client, admin_headers)
        # Reads are open to every authenticated role that may see the exchange surface.
        read = api_client.get(
            RATES,
            params={"from_currency_id": source["id"], "to_currency_id": target["id"]},
            headers=accountant_headers,
        )
        assert read.status_code == 200, read.text

        denied = publish(api_client, accountant_headers, source, target, expect=403)
        assert denied.json()["error"]["details"]["required_permission"] == "rates.manage"

    def test_an_auditor_reads_but_never_writes(
        self,
        api_client: TestClient,
        make_user: object,
        admin_headers: dict[str, str],
        provisioned_device: object,
    ) -> None:
        user = make_user(roles=("AUDITOR",))  # type: ignore[operator]
        device_uuid = provisioned_device()  # type: ignore[operator]
        tokens = login(
            api_client,
            str(user["username"]),
            str(user["password"]),
            device_uuid=device_uuid,
        ).json()
        headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

        source, target = pair(api_client, admin_headers)
        assert (
            api_client.get(
                RATES,
                params={"from_currency_id": source["id"], "to_currency_id": target["id"]},
                headers=headers,
            ).status_code
            == 200
        )
        assert publish(api_client, headers, source, target, expect=None).status_code == 403

    def test_reads_require_authentication(self, api_client: TestClient) -> None:
        assert api_client.get(RATES).status_code == 401
        assert (
            api_client.get(
                f"{RATES}/resolve",
                params={"from_currency_id": str(uuid.uuid4()), "to_currency_id": str(uuid.uuid4())},
            ).status_code
            == 401
        )
