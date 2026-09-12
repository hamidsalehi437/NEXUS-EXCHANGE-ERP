"""The Phase 3 exit criteria, end to end (ROADMAP §"Phase 3 — Core master data").

> Exit criteria: a manager can create a currency, branch, customer and rate; every rate
> change produces an audit row; no edit path exists for ``currencies.code``.

This suite walks the three criteria as a workflow rather than as unit checks, because that
is the shape the reviewer asked for: a fresh administrative user sets up a business (a
currency it trades, a branch it trades in), a manager registers the first customer and
publishes the day's rate, the counter can resolve that rate, and the audit trail shows the
publication.

**Who may do what is the approved Phase 0 permission map** (`API_CONTRACT.md` §8), which
this phase deliberately did not change: currency and branch administration are
`settings.manage`/`branch.manage` (SUPER_ADMIN, OWNER), while daily master data —
customers and rates — belongs to MANAGER. The tests therefore use OWNER for the catalogue
half and MANAGER for the operating half, and one test pins that split so it cannot be
"fixed" by accident.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from tests.auth_helpers import bearer, login
from tests.helpers import fetch_all, fetch_scalar
from tests.masterdata_helpers import (
    ACCOUNTS,
    BRANCHES,
    CURRENCIES,
    RATES,
    create_account,
    create_branch,
    create_currency,
    create_customer,
    unique_account_code,
)

pytestmark = [pytest.mark.integration, pytest.mark.masterdata]


def _headers(client: TestClient, make_user: object, role: str, branch_id: str) -> dict[str, str]:
    """Headers for a freshly seeded user of ``role``, logged in on the main branch."""
    user = make_user(roles=(role,))  # type: ignore[operator]
    tokens = login(
        client,
        str(user["username"]),
        str(user["password"]),
        branch_id=uuid.UUID(branch_id),
    ).json()
    return bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))


class TestPhase3ExitCriteria:
    def test_the_catalogue_and_the_counters_can_be_set_up(
        self,
        api_client: TestClient,
        make_user: object,
        branch_id: str,
        main_database: str,
    ) -> None:
        """A currency, a branch, a customer and a rate — created through the API only."""
        owner = _headers(api_client, make_user, "OWNER", branch_id)
        manager = _headers(api_client, make_user, "MANAGER", branch_id)

        def tear_down_the_new_branch(new_branch_id: str) -> None:
            """Leave exactly one active branch behind.

            Device registration resolves "the" branch automatically only while one branch
            is active, so a suite that creates branches must deactivate them (never delete:
            the schema forbids it, and production cannot do it either).
            """
            api_client.patch(
                f"{BRANCHES}/{new_branch_id}", json={"is_active": False}, headers=owner
            )

        # 1. The business's settings authority opens the catalogue and the branch.
        traded_currency = create_currency(
            api_client, owner, name="Exit-criteria currency", is_tradable=True
        ).json()
        branch = create_branch(api_client, owner, name="Exit-criteria branch").json()
        assert branch["is_active"] is True

        # 2. The manager registers a customer (the code is issued by the server) …
        customer = create_customer(
            api_client, manager, full_name="Exit Criteria Customer", branch_id=branch["id"]
        ).json()
        assert customer["customer_code"].startswith("CUS-")

        # 3. … publishes the day's quote against the base currency …
        base_currency_id = fetch_scalar(
            main_database,
            "SELECT id FROM currencies WHERE is_base AND is_active LIMIT 1",
        )
        moment = (dt.datetime.now(tz=dt.UTC) - dt.timedelta(minutes=1)).isoformat()
        published = api_client.post(
            RATES,
            json={
                "from_currency_id": str(traded_currency["id"]),
                "to_currency_id": str(base_currency_id),
                "buy_rate": "72.5000000000",
                "sell_rate": "73.2500000000",
                "effective_at": moment,
                "branch_id": branch["id"],
                "source": "MANUAL",
            },
            headers=manager,
        )
        assert published.status_code == 201, published.text
        assert published.json()["branch_code"] == branch["code"]
        assert published.json()["buy_rate"] == "72.5000000000"

        # 4. … and the counter can ask the server what rate applies, before trading.
        resolved = api_client.get(
            f"{RATES}/resolve",
            params={
                "from_currency_id": str(traded_currency["id"]),
                "to_currency_id": str(base_currency_id),
                "branch_id": branch["id"],
            },
            headers=manager,
        )
        assert resolved.status_code == 200, resolved.text
        assert resolved.json()["exchange_rate_id"] == published.json()["id"]
        assert resolved.json()["is_branch_quote"] is True
        assert Decimal(resolved.json()["sell_rate"]) == Decimal("73.2500000000")

        # Every rate change produces an audit row — one, with the same transaction.
        audit = fetch_all(
            main_database,
            "SELECT action, new_data FROM audit_logs "
            "WHERE entity_type = 'exchange_rate' AND entity_id = :rate_id",
            rate_id=published.json()["id"],
        )
        assert [row[0] for row in audit] == ["RATE_CREATED"]
        assert audit[0][1]["buy_rate"] == "72.5000000000"
        assert audit[0][1]["branch_id"] == branch["id"]

        # A correction is another audited publication, never an edit.
        corrected = api_client.post(
            RATES,
            json={
                "from_currency_id": str(traded_currency["id"]),
                "to_currency_id": str(base_currency_id),
                "buy_rate": "72.7500000000",
                "sell_rate": "73.4000000000",
                "branch_id": branch["id"],
                "source": "MANUAL",
            },
            headers=manager,
        )
        assert corrected.status_code == 201, corrected.text
        assert (
            fetch_scalar(
                main_database,
                "SELECT count(*) FROM audit_logs WHERE entity_type = 'exchange_rate' "
                "AND entity_id = :rate_id",
                rate_id=corrected.json()["id"],
            )
            == 1
        )
        assert fetch_scalar(
            main_database,
            "SELECT buy_rate FROM exchange_rates WHERE id = :rate_id",
            rate_id=published.json()["id"],
        ) == Decimal("72.5000000000")  # the earlier quote is untouched

        tear_down_the_new_branch(str(branch["id"]))

    def test_there_is_no_edit_path_for_a_currency_code(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
    ) -> None:
        """Criterion 3, in the three places it is enforced."""
        from sqlalchemy.exc import DatabaseError

        from tests.helpers import execute_sql

        currency = create_currency(api_client, admin_headers).json()

        # (a) the API has no field for it
        refused = api_client.patch(
            f"{CURRENCIES}/{currency['id']}",
            json={"code": "ZZZ"},
            headers=admin_headers,
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["error"]["details"]["fields"][0]["code"] == "extra_forbidden"

        # (b) the database refuses the raw statement regardless of the layer above it —
        #     the trigger names the frozen rule, so a bypass attempt is self-documenting
        with pytest.raises(DatabaseError) as refusal:
            execute_sql(
                main_database,
                "UPDATE currencies SET code = 'ZZZ' WHERE id = :currency_id",
                currency_id=currency["id"],
            )
        assert "NEXUS_IMMUTABLE_FIELD" in str(refusal.value)
        assert (
            fetch_scalar(
                main_database,
                "SELECT code FROM currencies WHERE id = :currency_id",
                currency_id=currency["id"],
            )
            == currency["code"]
        )

    def test_the_chart_of_accounts_is_ready_for_posting(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        branch_id: str,
    ) -> None:
        """An accountant can extend the chart — the Phase 4 dependency — without schema work.

        The seeded chart is exercised (its accounts already carry the derived normal
        balance), a new branch-scoped cash account is added, and the tree rules hold.
        """
        seeded = api_client.get(ACCOUNTS, params={"limit": 500}, headers=admin_headers).json()[
            "items"
        ]
        assert seeded, "the seeded chart of accounts is empty"
        assert {row["normal_balance"] for row in seeded} <= {"DEBIT", "CREDIT"}

        cash = create_account(
            api_client,
            admin_headers,
            code=unique_account_code(),
            name="Exit-criteria branch cash",
            account_type="ASSET",
            branch_id=branch_id,
        ).json()
        assert cash["normal_balance"] == "DEBIT"
        assert cash["branch_id"] == branch_id
        assert cash["has_children"] is False


class TestPhase3PermissionSplit:
    """Pin the approved split so a later change is a decision, not an accident."""

    def test_a_manager_may_not_edit_the_catalogue(
        self,
        api_client: TestClient,
        make_user: object,
        branch_id: str,
        admin_headers: dict[str, str],
    ) -> None:
        manager = _headers(api_client, make_user, "MANAGER", branch_id)
        currency = create_currency(api_client, admin_headers).json()

        denied_currency = api_client.post(
            CURRENCIES,
            json={"code": "TZZ", "name": "Not the manager's call"},
            headers=manager,
        )
        assert denied_currency.status_code == 403, denied_currency.text
        assert (
            denied_currency.json()["error"]["details"]["required_permission"] == "settings.manage"
        )

        denied_branch = api_client.post(
            BRANCHES, json={"code": "MGR-1", "name": "Not the manager's call"}, headers=manager
        )
        assert denied_branch.status_code == 403, denied_branch.text
        assert denied_branch.json()["error"]["details"]["required_permission"] == "branch.manage"

        # Reading the catalogue is part of the job, changing it is not.
        assert api_client.get(CURRENCIES, headers=manager).status_code == 200
        assert (
            api_client.patch(
                f"{CURRENCIES}/{currency['id']}",
                json={"name": "Renamed by a manager"},
                headers=manager,
            ).status_code
            == 403
        )

    def test_an_accountant_may_run_the_chart_but_not_the_catalogue(
        self,
        api_client: TestClient,
        accountant_headers: dict[str, str],
        admin_headers: dict[str, str],
    ) -> None:
        account = create_account(api_client, accountant_headers, name="Accountant-owned account")
        assert account.status_code == 201, account.text
        assert (
            api_client.post(
                CURRENCIES,
                json={"code": "TAQ", "name": "Accountant currency"},
                headers=accountant_headers,
            ).status_code
            == 403
        )
        assert (
            api_client.post(
                BRANCHES,
                json={"code": "ACC-1", "name": "Accountant branch"},
                headers=accountant_headers,
            ).status_code
            == 403
        )
        assert api_client.get(CURRENCIES, headers=admin_headers).status_code == 200
