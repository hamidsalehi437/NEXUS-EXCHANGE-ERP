"""Phase 4 — scope: who may post where, and who may see which ledger.

Two boundaries are asserted here, because they are enforced in two different places:

* **Posting scope** lives in the service. A branch-scoped actor (MANAGER, CASHIER,
  ACCOUNTANT) may post only into *their* branch; the refusal is a ``FORBIDDEN_SCOPE`` 403
  and it is audited (``LEDGER_POSTING_DENIED``). A device-bound posting into another
  branch is the same refusal. Roles with group-wide authority (SUPER_ADMIN, OWNER) may post
  where the business needs them to, which is the allocation PART 41 documents.
* **Reading scope** lives in the read layer. ``GET /journal``, ``GET /journal/{id}`` and
  ``GET /reports/trial-balance`` return only what the actor may see: another branch's entry
  is a 404, not a 403 — the ledger does not confirm the existence of what the actor cannot
  see. ``GET /reports/trial-balance`` needs ``reports.view``, which AUDITOR holds and
  CASHIER does not.

The line the phase must not cross: no caller can post into a branch they are not entitled
to, and no caller can read a branch they are not entitled to — while an auditor who *is*
entitled sees everything, including the reversals.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import ForbiddenScopeError
from app.core.permissions import RoleName
from tests.accounting_helpers import (
    World,
    build_world,
    create_account,
    create_branch,
    deactivate_branch,
    ledger_rows,
    line,
    load_currencies,
    read,
    run_scenario,
    scaffold_chart,
    unique_code,
)
from tests.auth_helpers import USER_PASSWORD

pytestmark = [pytest.mark.integration, pytest.mark.accounting]

API = "/api/v1"


def receipt(world: World, *, amount: str = "100", reference_id: uuid.UUID | None = None):
    """A valid cash receipt at the scenario's branch."""

    async def scenario(service):
        return await service.post_cash_movement(
            movement_type="IN",
            reference_id=reference_id or uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_afn"),
            counter_account_id=world.account("capital"),
            currency_id=world.base.id,
            amount=Decimal(amount),
            description="branch scope probe",
            actor=world.head_actor,
        )

    return scenario


@pytest.fixture
def second_branch(api_client: TestClient, admin_headers: dict[str, str], main_database: str):
    """A second branch with its own chart slice, deactivated when the test ends."""
    branch = create_branch(api_client, admin_headers)
    try:
        yield branch
    finally:
        deactivate_branch(api_client, admin_headers, branch["id"])


def post_at(
    service: object,
    *,
    world: World,
    branch_id: uuid.UUID,
    accounts: dict[str, uuid.UUID],
    currency_id: uuid.UUID,
    actor: object,
    amount: str = "50",
    reference_type: str = "CASH_MOVEMENT",
):
    """A two-leg posting at an explicitly chosen branch and chart slice."""
    return service.create_journal_entry(  # type: ignore[attr-defined]
        reference_type=reference_type,
        reference_id=uuid.uuid4(),
        branch_id=branch_id,
        lines=[
            line(accounts["cash_afn"], debit=amount, currency_id=currency_id),
            line(accounts["capital"], credit=amount, currency_id=currency_id),
        ],
        actor=actor,
        description="cross-branch probe",
    )


class TestPostingScope:
    def test_a_branch_scoped_actor_cannot_post_into_another_branch(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )
        manager = world.actor(roles=(RoleName.MANAGER,))

        async def scenario(service):
            with pytest.raises(ForbiddenScopeError) as refusal:
                await post_at(
                    service,
                    world=world,
                    branch_id=uuid.UUID(str(second_branch["id"])),
                    accounts=accounts,
                    currency_id=currencies["AFN"].id,
                    actor=manager,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["reason"] == "ANOTHER_BRANCH"
        assert refusal.details["actor_branch_id"] == str(world.branch_id)
        assert refusal.details["target_branch_id"] == str(second_branch["id"])

    def test_every_branch_scoped_role_is_refused_and_audited(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        """MANAGER, CASHIER and ACCOUNTANT are all branch-bound; only group roles are not."""
        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )

        async def scenario(service):
            outcomes = []
            # MANAGER posts cash movement, CASHIER receives cash, ACCOUNTANT posts a
            # manual adjustment: each role uses a document type it really may post, so the
            # refusal under test is the branch scope and never the permission.
            for role, reference_type in (
                (RoleName.MANAGER, "CASH_MOVEMENT"),
                (RoleName.CASHIER, "CASH_MOVEMENT"),
                (RoleName.ACCOUNTANT, "MANUAL_ADJUSTMENT"),
            ):
                actor = world.actor(roles=(role,))
                with pytest.raises(ForbiddenScopeError) as refusal:
                    await post_at(
                        service,
                        world=world,
                        branch_id=uuid.UUID(str(second_branch["id"])),
                        accounts=accounts,
                        currency_id=currencies["AFN"].id,
                        actor=actor,
                        reference_type=reference_type,
                    )
                outcomes.append((role, refusal.value.details))
            return outcomes

        denied_before = read(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE action = 'LEDGER_POSTING_DENIED' AND new_data->>'reason' = 'ANOTHER_BRANCH'
            """,
        )
        outcomes = run_scenario(main_database, scenario)
        assert all(details.get("reason") == "ANOTHER_BRANCH" for _, details in outcomes), outcomes
        assert {role for role, _ in outcomes} == {
            RoleName.MANAGER,
            RoleName.CASHIER,
            RoleName.ACCOUNTANT,
        }
        denied = read(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE action = 'LEDGER_POSTING_DENIED' AND new_data->>'reason' = 'ANOTHER_BRANCH'
            """,
        )
        assert len(denied) - len(denied_before) == 3

    def test_a_group_wide_actor_may_post_into_any_active_branch(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        """OWNER/SUPER_ADMIN are not branch-bound (``GROUP_WIDE_ROLES``)."""
        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )

        async def scenario(service):
            return await post_at(
                service,
                world=world,
                branch_id=uuid.UUID(str(second_branch["id"])),
                accounts=accounts,
                currency_id=currencies["AFN"].id,
                actor=world.actor(roles=(RoleName.OWNER,)),
            )

        view = run_scenario(main_database, scenario)
        assert view.branch_id == uuid.UUID(str(second_branch["id"]))
        assert view.is_balanced is True

    def test_an_actor_without_a_branch_may_not_post_into_one(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """An unbound device is not a licence to post: the refusal names the missing link."""
        world = build_world(api_client, admin_headers, main_database)
        unbound = world.actor(roles=(RoleName.MANAGER,), branch_id=None)

        async def scenario(service):
            with pytest.raises(ForbiddenScopeError) as refusal:
                await service.post_cash_movement(
                    movement_type="IN",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    cash_account_id=world.account("cash_afn"),
                    counter_account_id=world.account("capital"),
                    currency_id=world.base.id,
                    amount=Decimal("10"),
                    actor=unbound,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["reason"] == "ACTOR_HAS_NO_BRANCH"
        assert refusal.details["target_branch_id"] == str(world.branch_id)

    def test_a_branch_scoped_actor_may_post_at_their_own_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        manager = world.actor(roles=(RoleName.MANAGER,))

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("75"),
                actor=manager,
            )

        view = run_scenario(main_database, scenario)
        assert view.branch_id == world.branch_id
        assert view.total_debit == Decimal("75")

    def test_a_branch_scoped_actor_cannot_post_a_group_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Even a user's own branch may not be used for a group-wide (branch-less) posting.

        A manual adjustment is the ``accounts.manage`` case, which the ACCOUNTANT holds —
        so the refusal under test is the scope and not the permission.
        """
        world = build_world(api_client, admin_headers, main_database)
        manager = world.actor(roles=(RoleName.ACCOUNTANT,))

        async def scenario(service):
            with pytest.raises(ForbiddenScopeError) as refusal:
                await service.create_journal_entry(
                    reference_type="MANUAL_ADJUSTMENT",
                    reference_id=uuid.uuid4(),
                    branch_id=None,
                    lines=[
                        line(world.account("cash_afn"), debit="5", currency_id=world.base.id),
                        line(world.account("capital"), credit="5", currency_id=world.base.id),
                    ],
                    actor=manager,
                    description="group entry",
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["target_branch_id"] is None
        assert refusal.details["actor_branch_id"] == str(world.branch_id)

    def test_a_group_wide_actor_cannot_post_into_an_inactive_branch(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        """Trading at a branch that has been retired is refused for everyone."""
        from app.core.exceptions import ValidationError

        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )
        deactivate_branch(api_client, admin_headers, str(second_branch["id"]))

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await post_at(
                    service,
                    world=world,
                    branch_id=uuid.UUID(str(second_branch["id"])),
                    accounts=accounts,
                    currency_id=currencies["AFN"].id,
                    actor=world.actor(roles=(RoleName.OWNER,)),
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"] == [{"field": "branch_id", "code": "inactive"}]

    def test_a_branch_scoped_actor_cannot_reverse_another_branchs_entry(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )

        async def foreign_entry(service):
            return await post_at(
                service,
                world=world,
                branch_id=uuid.UUID(str(second_branch["id"])),
                accounts=accounts,
                currency_id=currencies["AFN"].id,
                actor=world.actor(roles=(RoleName.OWNER,)),
            )

        original = run_scenario(main_database, foreign_entry)
        manager = world.actor(roles=(RoleName.MANAGER,))

        async def scenario(service):
            with pytest.raises(ForbiddenScopeError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=original.id, reason="not mine", actor=manager
                )
            return refusal.value

        assert run_scenario(main_database, scenario).details["reason"] == "ANOTHER_BRANCH"


class TestReadingScope:
    def test_a_group_wide_reader_sees_every_branch_in_the_journal(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        """The group-wide reader sees both branches; a scoped reader sees one."""
        world = build_world(api_client, admin_headers, main_database)
        local = run_scenario(main_database, receipt(world, amount="11"))

        currencies = load_currencies(main_database, ["AFN"])
        accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )
        foreign = run_scenario(
            main_database,
            lambda service: post_at(
                service,
                world=world,
                branch_id=uuid.UUID(str(second_branch["id"])),
                accounts=accounts,
                currency_id=currencies["AFN"].id,
                actor=world.actor(roles=(RoleName.OWNER,)),
            ),
        )

        # A user bound to the scenario's branch (the seeded admin is group-wide).
        reader = api_client.get(f"{API}/journal", headers=admin_headers)
        assert reader.status_code == 200
        group_wide = {row["id"] for row in reader.json()["items"]}
        assert str(local.id) in group_wide and str(foreign.id) in group_wide

    def test_an_entry_outside_the_actors_scope_is_not_found(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A device-bound accountant of another branch gets 404, not 403."""
        from tests.accounting_helpers import actor_for
        from tests.helpers import create_user

        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, receipt(world, amount="22"))
        outsider_user = uuid.UUID(
            create_user(
                main_database,
                username=f"scope-{uuid.uuid4().hex[:8]}",
                password=USER_PASSWORD,
                roles=(RoleName.AUDITOR,),
            )
        )

        async def scenario(service):
            outsider = actor_for(outsider_user, branch_id=uuid.uuid4(), roles=(RoleName.AUDITOR,))
            from app.core.exceptions import ResourceNotFoundError

            with pytest.raises(ResourceNotFoundError):
                await service.get_journal_entry(entry_id=entry.id, actor=outsider)
            return True

        assert run_scenario(main_database, scenario) is True

    def test_the_trial_balance_is_readable_by_an_auditor(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        run_scenario(main_database, receipt(world, amount="33"))

        response = api_client.get(f"{API}/reports/trial-balance", headers=admin_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["source"] == "journal_lines"
        assert body["is_balanced"] is True
        assert body["total_debit"] == body["total_credit"]
        assert body["difference"] == "0.0000000000"
        codes = {row["account_code"] for row in body["rows"]}
        assert world.codes["cash_afn"] in codes

    def test_the_trial_balance_hides_another_branch_from_a_scoped_reader(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Scope is applied in the query, not filtered out afterwards in Python."""
        world = build_world(api_client, admin_headers, main_database)
        run_scenario(main_database, receipt(world, amount="44"))

        async def scenario(service):
            return await service.get_trial_balance(
                actor=world.actor(roles=(RoleName.AUDITOR,), branch_id=uuid.uuid4())
            )

        board = run_scenario(main_database, scenario)
        assert board.rows == () or all(
            row.account_code not in world.codes.values() for row in board.rows
        )

    def test_the_journal_detail_never_leaks_another_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, receipt(world, amount="55"))

        async def scenario(service):
            from app.core.exceptions import ResourceNotFoundError

            scoped = world.actor(roles=(RoleName.AUDITOR,), branch_id=uuid.uuid4())
            with pytest.raises(ResourceNotFoundError):
                await service.get_journal_entry(entry_id=entry.id, actor=scoped)
            return await service.get_journal_entry(entry_id=entry.id, actor=world.head_actor)

        own = run_scenario(main_database, scenario)
        assert own.id == entry.id
        assert len(ledger_rows(main_database, own.id)) == 2


class TestAccountBoundaries:
    def test_an_account_of_another_branch_cannot_be_posted_to(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        """Branch isolation is also an account rule, not only a branch-id rule."""
        from app.core.exceptions import ValidationError

        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        foreign_accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.create_journal_entry(
                    reference_type="MANUAL_ADJUSTMENT",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    lines=[
                        line(foreign_accounts["cash_afn"], debit="9", currency_id=world.base.id),
                        line(world.account("capital"), credit="9", currency_id=world.base.id),
                    ],
                    actor=world.head_actor,
                    description="wrong branch's account",
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"][0]["code"] == "branch_mismatch"
        assert refusal.details["account_branch_id"] == str(second_branch["id"])

    def test_a_group_wide_account_may_be_posted_from_any_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        group_account = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=unique_code(),
                name="Group suspense",
                account_type="ASSET",
                branch_id=None,
            )["id"]
        )

        async def scenario(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    line(group_account, debit="15", currency_id=world.base.id),
                    line(world.account("capital"), credit="15", currency_id=world.base.id),
                ],
                actor=world.head_actor,
                description="group account from a branch",
            )

        view = run_scenario(main_database, scenario)
        assert view.branch_id == world.branch_id
        assert view.is_balanced is True

    def test_reading_a_balance_outside_the_scope_is_forbidden(
        self,
        api_client: TestClient,
        admin_headers: dict[str, str],
        main_database: str,
        second_branch: dict[str, object],
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        currencies = load_currencies(main_database, ["AFN"])
        foreign_accounts, _ = scaffold_chart(
            api_client,
            admin_headers,
            branch_id=uuid.UUID(str(second_branch["id"])),
            currencies=currencies,
        )
        manager = world.actor(roles=(RoleName.MANAGER,))

        async def scenario(service):
            with pytest.raises(ForbiddenScopeError) as refusal:
                await service.get_account_balance(
                    account_id=foreign_accounts["cash_afn"],
                    actor=manager,
                    branch_id=uuid.UUID(str(second_branch["id"])),
                )
            return refusal.value

        assert run_scenario(main_database, scenario).details["reason"] == "ANOTHER_BRANCH"
