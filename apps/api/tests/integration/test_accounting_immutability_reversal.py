"""Phase 4 — posted journals are immutable, and corrections are reversals.

Two independent guarantees, asserted separately because they fail differently:

* **The database refuses mutation.** ``journal_lines`` is append-only (no ``UPDATE``, no
  ``DELETE``), ``journal_entries`` refuses deletion outright and refuses any change to its
  posting fields (``NEXUS_IMMUTABLE_FIELD``, SQLSTATE ``NEX06`` — only the narrative
  ``description`` may be annotated, as the frozen schema documents), and a raw ``INSERT``
  of a line that does not balance is refused at ``COMMIT`` by the deferred
  ``ct_journal_lines_balanced_*`` constraint (SQLSTATE ``NEX02``). The runtime role
  ``nexus_app`` additionally has no ``DELETE``/``UPDATE`` grant on the ledger at all.
* **The service corrects by reversal.** A reversal is a mirror entry that keeps every
  account, currency and rate and swaps debit against credit, so it restores the
  *quantities* as well as the functional amounts. The original is never touched, the link
  is structural (``reversal_of_id`` plus the ``REVERSAL``/original-id reference), a second
  reversal is refused, and a reversal cannot itself be reversed (PART 22: "cancel, never
  delete"; a correction is a new document).

The tests read refusals straight off the driver, so what is asserted is the behaviour of
the deployed schema — not of a Python wrapper around it.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.exceptions import (
    AlreadyReversedError,
    ForbiddenScopeError,
    PermissionDeniedError,
    ResourceNotFoundError,
    ReversalError,
    ValidationError,
    sqlstate_of,
)
from app.core.permissions import RoleName
from tests.accounting_helpers import (
    World,
    build_world,
    count,
    ledger_rows,
    read,
    read_one,
    run_scenario,
    scalar,
)
from tests.auth_helpers import USER_PASSWORD
from tests.helpers import database_dsn

pytestmark = [pytest.mark.integration, pytest.mark.accounting]

# The columns the frozen schema freezes once an entry is posted.
FROZEN_POSTING_FIELDS = (
    "reference_type",
    "reference_id",
    "transaction_date",
    "created_by",
    "branch_id",
    "reversal_of_id",
)


def db_refusal(database: str, sql: str, **params: object) -> BaseException:
    """Run a statement the database must refuse and return the driver's exception."""
    engine = create_engine(database_dsn(database), future=True)
    try:
        # The ``with`` block ends in a COMMIT, and that is where the *deferred* balance
        # constraint fires - so the whole block, not just the statement, is under test.
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except Exception as error:
            return error
    finally:
        engine.dispose()
    raise AssertionError(f"the database accepted a statement it should refuse:\n{sql}")


def _nets(database: str) -> dict[uuid.UUID, Decimal]:
    """Per-account net of every account that has ever been posted to."""
    return {
        row["account_id"]: Decimal(str(row["net"]))
        for row in read(
            database,
            """
            SELECT account_id, COALESCE(SUM(debit - credit), 0) AS net
              FROM journal_lines GROUP BY account_id
            """,
        )
    }


def balances_of(database: str, account_id: uuid.UUID) -> list[Decimal]:
    """The account's per-currency balances as the ledger view reports them."""
    return [
        Decimal(str(row["balance"]))
        for row in read(
            database,
            """
            SELECT balance FROM v_account_balances
             WHERE account_id = :account ORDER BY currency_code
            """,
            account=account_id,
        )
    ]


def post_movement(world: World, *, amount: str, reference_id: uuid.UUID | None = None) -> object:
    """A simple, valid two-leg entry: Dr Cash AFN / Cr Capital."""

    async def scenario(service):
        return await service.post_cash_movement(
            movement_type="IN",
            reference_id=reference_id or uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_afn"),
            counter_account_id=world.account("capital"),
            currency_id=world.base.id,
            amount=Decimal(amount),
            description="counter receipt",
            actor=world.head_actor,
        )

    return scenario


class TestPostedJournalsAreImmutable:
    def test_no_posting_field_of_a_posted_entry_can_change(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Every frozen column is frozen — including the reversal link."""
        world = build_world(api_client, admin_headers, main_database)
        from tests.accounting_helpers import create_branch, deactivate_branch

        entry = run_scenario(main_database, post_movement(world, amount="100"))
        other = create_branch(api_client, admin_headers)
        other_branch = other["id"]
        # A *different* user: writing the same value back is a legal no-op, so it proves
        # nothing about immutability.
        from tests.helpers import create_user

        other_user_id = create_user(
            main_database,
            username=f"immutability-{uuid.uuid4().hex[:8]}",
            password=USER_PASSWORD,
        )

        replacements = {
            "reference_type": "'MANUAL_ADJUSTMENT'",
            "reference_id": f"'{uuid.uuid4()}'",
            "transaction_date": "transaction_date + interval '1 day'",
            "created_by": f"'{other_user_id}'",
            "branch_id": f"'{other_branch}'",
            "reversal_of_id": f"'{uuid.uuid4()}'",
        }
        try:
            for column in FROZEN_POSTING_FIELDS:
                # The column name comes from FROZEN_POSTING_FIELDS above, never from a
                # caller of the API.
                statement = (
                    # Column and value come from FROZEN_POSTING_FIELDS, never from the API.
                    f"UPDATE journal_entries SET {column} = {replacements[column]}"  # noqa: S608
                    " WHERE id = :entry"
                )
                error = db_refusal(main_database, statement, entry=entry.id)
                assert sqlstate_of(error) == "NEX06", column
                assert "NEXUS_IMMUTABLE_FIELD" in str(error), column
            # Nothing moved.
            row = read_one(
                main_database,
                """
                SELECT reference_type, reference_id, branch_id, created_by
                  FROM journal_entries WHERE id = :entry
                """,
                entry=entry.id,
            )
            assert row["reference_type"] == "CASH_MOVEMENT"
            assert row["branch_id"] == world.branch_id
            assert row["created_by"] == world.head_user_id
        finally:
            deactivate_branch(api_client, admin_headers, other["id"])

    def test_the_narrative_may_be_annotated_but_the_money_cannot(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The frozen schema allows a description edit; amounts and dates stay untouched."""
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))
        before = read_one(
            main_database,
            "SELECT transaction_date, created_at FROM journal_entries WHERE id = :entry",
            entry=entry.id,
        )

        from tests.helpers import execute_sql

        assert (
            execute_sql(
                main_database,
                "UPDATE journal_entries SET description = 'annotated by the auditor'"
                " WHERE id = :entry",
                entry=entry.id,
            )
            == 1
        )
        after = read_one(
            main_database,
            """
            SELECT description, transaction_date, created_at
              FROM journal_entries WHERE id = :entry
            """,
            entry=entry.id,
        )
        assert after["description"] == "annotated by the auditor"
        assert after["transaction_date"] == before["transaction_date"]
        assert after["created_at"] == before["created_at"]

    def test_deleting_a_posted_entry_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """PART 22: a journal entry is never deleted, not even by an administrator."""
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))

        error = db_refusal(
            main_database, "DELETE FROM journal_entries WHERE id = :entry", entry=entry.id
        )
        assert sqlstate_of(error) == "P0001"
        assert "NEXUS_APPEND_ONLY" in str(error)
        assert count(main_database, "journal_entries", where="id = :entry", entry=entry.id) == 1

    def test_a_journal_line_cannot_be_updated(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))
        row = read_one(
            main_database,
            "SELECT id FROM journal_lines WHERE journal_entry_id = :entry LIMIT 1",
            entry=entry.id,
        )

        error = db_refusal(
            main_database,
            "UPDATE journal_lines SET debit = debit + 1 WHERE id = :line",
            line=row["id"],
        )
        assert sqlstate_of(error) == "P0001"
        assert "NEXUS_APPEND_ONLY" in str(error)

    def test_a_journal_line_cannot_be_deleted(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))
        row = read_one(
            main_database,
            "SELECT id FROM journal_lines WHERE journal_entry_id = :entry LIMIT 1",
            entry=entry.id,
        )
        before = count(main_database, "journal_lines")

        error = db_refusal(
            main_database, "DELETE FROM journal_lines WHERE id = :line", line=row["id"]
        )
        assert sqlstate_of(error) == "P0001"
        assert count(main_database, "journal_lines") == before

    def test_a_line_that_breaks_the_balance_is_refused_at_commit(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The deferred constraint catches raw SQL the service never wrote (§I-1)."""
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))

        error = db_refusal(
            main_database,
            """
            INSERT INTO journal_lines
                (journal_entry_id, account_id, currency_id, debit, credit, exchange_rate,
                 description)
            VALUES (:entry, :account, :currency, 5, 0, 1, 'smuggled line')
            """,
            entry=entry.id,
            account=world.account("cash_afn"),
            currency=world.base.id,
        )
        assert sqlstate_of(error) == "NEX02"
        assert "NEXUS_JOURNAL_UNBALANCED" in str(error)
        assert (
            count(main_database, "journal_lines", where="journal_entry_id = :entry", entry=entry.id)
            == 2
        )

    def test_a_negative_line_is_refused_at_commit(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Signing is carried on the side, not in the amount (ck_*_non_negative)."""
        world = build_world(api_client, admin_headers, main_database)
        entry = run_scenario(main_database, post_movement(world, amount="100"))

        error = db_refusal(
            main_database,
            """
            UPDATE journal_lines SET debit = -debit WHERE journal_entry_id = :entry
            """,
            entry=entry.id,
        )
        # Both defences fire here: the table is append-only *and* the amount must be >= 0.
        assert sqlstate_of(error) in {"P0001", "NEX02", "23514"}
        assert (
            count(
                main_database,
                "journal_lines",
                where="journal_entry_id = :entry AND debit < 0",
                entry=entry.id,
            )
            == 0
        )

    def test_the_runtime_role_has_no_delete_or_update_on_the_ledger(
        self, main_database: str
    ) -> None:
        """Defence in depth: the API cannot mutate historical journals even if it tried."""
        grants = {
            (row["table_name"], row["privilege_type"])
            for row in read(
                main_database,
                """
                SELECT table_name, privilege_type
                  FROM information_schema.role_table_grants
                 WHERE grantee = 'nexus_app'
                   AND table_name IN ('journal_entries', 'journal_lines', 'audit_logs')
                """,
            )
        }
        assert ("journal_entries", "DELETE") not in grants
        assert ("journal_lines", "DELETE") not in grants
        assert ("journal_lines", "UPDATE") not in grants
        assert ("audit_logs", "DELETE") not in grants
        assert ("audit_logs", "UPDATE") not in grants


class TestReversalLifecycle:
    def test_a_reversal_mirrors_the_original_line_for_line(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Same accounts, same currencies, same rates; debit and credit swapped."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        original = run_scenario(main_database, post_movement(world, amount="1234.56"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="posted against the wrong counter account",
                actor=world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        source = {row["account_code"]: row for row in ledger_rows(main_database, original.id)}
        mirror = {row["account_code"]: row for row in ledger_rows(main_database, reversal.id)}
        assert set(source) == set(mirror)
        for code, row in source.items():
            assert mirror[code]["debit"] == row["credit"]
            assert mirror[code]["credit"] == row["debit"]
            assert mirror[code]["currency_code"] == row["currency_code"]
            assert mirror[code]["exchange_rate"] == row["exchange_rate"]
            assert mirror[code]["foreign_amount"] == row["foreign_amount"]
        assert reversal.total_debit == original.total_credit
        assert reversal.total_credit == original.total_debit

    def test_a_reversal_is_linked_to_what_it_reverses(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="500"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="duplicate counter receipt",
                actor=world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        row = read_one(
            main_database,
            """
            SELECT reference_type, reference_id, reversal_of_id
              FROM journal_entries WHERE id = :entry
            """,
            entry=reversal.id,
        )
        assert row["reversal_of_id"] == original.id
        assert row["reference_type"] == "REVERSAL"
        # The REVERSAL reference is the original id, which the unique reference index
        # allows exactly once — the structural half of "reverse only once".
        assert row["reference_id"] == original.id
        assert (
            count(
                main_database, "journal_entries", where="reversal_of_id = :entry", entry=original.id
            )
            == 1
        )
        # The original is not marked in the ledger; the link is the reversal's.
        assert (
            read_one(
                main_database,
                "SELECT reversal_of_id FROM journal_entries WHERE id = :entry",
                entry=original.id,
            )["reversal_of_id"]
            is None
        )

    def test_the_original_is_untouched_by_its_reversal(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="777"))
        before_entry = read_one(
            main_database, "SELECT * FROM journal_entries WHERE id = :entry", entry=original.id
        )
        before_lines = ledger_rows(main_database, original.id)

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="correction", actor=world.head_actor
            )

        run_scenario(main_database, scenario)
        assert (
            read_one(
                main_database, "SELECT * FROM journal_entries WHERE id = :entry", entry=original.id
            )
            == before_entry
        )
        assert ledger_rows(main_database, original.id) == before_lines

    def test_a_second_reversal_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """409 ``ALREADY_REVERSED``: reversing twice would silently double the correction."""
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="300"))
        entries_before = count(main_database, "journal_entries")

        async def scenario(service):
            first = await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="first correction", actor=world.head_actor
            )
            with pytest.raises(AlreadyReversedError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=original.id,
                    reason="second correction",
                    actor=world.head_actor,
                )
            return first, refusal.value

        first, refusal = run_scenario(main_database, scenario)
        assert refusal.details["reversal_journal_entry_id"] == str(first.id)
        assert count(main_database, "journal_entries") == entries_before + 1

    def test_a_reversal_cannot_itself_be_reversed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A correction is a new document, not another reversal (PART 22)."""
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="300"))

        async def scenario(service):
            reversal = await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="correction", actor=world.head_actor
            )
            with pytest.raises(ReversalError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=reversal.id, reason="undo the undo", actor=world.head_actor
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["reference_type"] == "REVERSAL"

    def test_a_reversal_needs_a_reason(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="300"))

        async def scenario(service):
            for reason in ("", "   "):
                with pytest.raises(ValidationError) as refusal:
                    await service.reverse_journal_entry(
                        journal_entry_id=original.id, reason=reason, actor=world.head_actor
                    )
                assert refusal.value.details["fields"] == [{"field": "reason", "code": "required"}]
            return True

        assert run_scenario(main_database, scenario) is True

    def test_a_reversal_cannot_be_dated_before_the_entry_it_reverses(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="300"))

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=original.id,
                    reason="too early",
                    actor=world.head_actor,
                    transaction_date=original.transaction_date
                    - __import__("datetime").timedelta(days=1),
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"] == [
            {"field": "transaction_date", "code": "before_original"}
        ]

    def test_a_reversal_restores_the_account_balances(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        original = run_scenario(main_database, post_movement(world, amount="1500"))
        assert balances_of(main_database, world.account("cash_afn")) == [Decimal("1500.0000000000")]

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="cash was misposted", actor=world.head_actor
            )

        run_scenario(main_database, scenario)
        assert balances_of(main_database, world.account("cash_afn")) == [Decimal("0.0000000000")]
        assert balances_of(main_database, world.account("capital")) == [Decimal("0.0000000000")]

    def test_a_reversal_of_a_foreign_position_returns_the_quantity(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Mirroring the *rate* is what makes the currency quantity come back exactly."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def opening(service):
            return await service.post_cash_movement(
                movement_type="OPENING",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("capital"),
                currency_id=world.money("USD").id,
                amount=Decimal("1000"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )

        original = run_scenario(main_database, opening)
        quantity_before = read_one(
            main_database,
            """
            SELECT COALESCE(SUM(foreign_amount) FILTER (WHERE debit > 0), 0)
                 - COALESCE(SUM(foreign_amount) FILTER (WHERE credit > 0), 0) AS quantity
              FROM journal_lines WHERE account_id = :account
            """,
            account=world.account("cash_usd"),
        )["quantity"]
        assert Decimal(str(quantity_before)) == Decimal("1000.0000000000")

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="opening was double counted",
                actor=world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        mirrored = {row["account_code"]: row for row in ledger_rows(main_database, reversal.id)}
        assert mirrored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("70.0000000000")
        assert mirrored[world.codes["cash_usd"]]["credit"] == Decimal("70000.0000000000")
        # The quantity therefore comes back exactly, not approximately.
        assert mirrored[world.codes["cash_usd"]]["foreign_amount"] == Decimal("1000.0000000000")
        quantity_after = read_one(
            main_database,
            """
            SELECT COALESCE(SUM(foreign_amount) FILTER (WHERE debit > 0), 0)
                 - COALESCE(SUM(foreign_amount) FILTER (WHERE credit > 0), 0) AS quantity
              FROM journal_lines WHERE account_id = :account
            """,
            account=world.account("cash_usd"),
        )["quantity"]
        assert Decimal(str(quantity_after)) == Decimal("0.0000000000")

    def test_the_trial_balance_returns_to_where_it_started(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§8's reconciliation identity, before and after a correction."""
        world = build_world(api_client, admin_headers, main_database)
        before = _nets(main_database)
        original = run_scenario(main_database, post_movement(world, amount="999.99"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="wrong branch's counter",
                actor=world.head_actor,
            )

        run_scenario(main_database, scenario)
        after = _nets(main_database)
        assert {account: after[account] for account in before} == before
        assert all(value == 0 for account, value in after.items() if account not in before), (
            "an account first posted to by this correction must not be left with a balance"
        )
        totals = read_one(
            main_database,
            "SELECT SUM(debit) AS debit, SUM(credit) AS credit FROM journal_lines",
        )
        assert totals["debit"] == totals["credit"]

    def test_both_the_entry_and_its_reversal_stay_visible(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Nothing is hidden from the trial balance: reversal is history, not erasure."""
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="250"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="correction", actor=world.head_actor
            )

        reversal = run_scenario(main_database, scenario)
        rows = read(
            main_database,
            """
            SELECT account_code, total_debit, total_credit, net_debit
              FROM v_trial_balance WHERE account_id = :account
            """,
            account=world.account("cash_afn"),
        )
        assert rows, "the trial balance view must report the account"
        assert rows[0]["net_debit"] == Decimal("0.0000000000")  # 250 posted, 250 reversed
        assert (
            count(
                main_database,
                "journal_entries",
                where="id IN (:a, :b)",
                a=original.id,
                b=reversal.id,
            )
            == 2
        )
        # The API reads both entries and shows the link.
        detail = api_client.get(f"/api/v1/journal/{reversal.id}", headers=admin_headers)
        assert detail.status_code == 200
        assert detail.json()["reversal_of_id"] == str(original.id)
        assert detail.json()["reference_type"] == "REVERSAL"

    def test_reversing_a_document_by_reference_finds_its_journal(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        run_scenario(main_database, post_movement(world, amount="420", reference_id=reference_id))

        async def scenario(service):
            return await service.reverse_transaction(
                reference_type="CASH_MOVEMENT",
                reference_id=reference_id,
                reason="the document was cancelled",
                actor=world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        assert reversal.reference_type == "REVERSAL"
        original_entry_id = scalar(
            main_database,
            """
            SELECT id FROM journal_entries
             WHERE reference_type = 'CASH_MOVEMENT' AND reference_id = :reference_id
            """,
            reference_id=reference_id,
        )
        assert (
            read_one(
                main_database,
                "SELECT reversal_of_id FROM journal_entries WHERE id = :entry",
                entry=reversal.id,
            )["reversal_of_id"]
            == original_entry_id
        )

    def test_reversing_a_document_that_has_no_journal_is_a_not_found(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)

        async def scenario(service):
            with pytest.raises(ResourceNotFoundError) as refusal:
                await service.reverse_transaction(
                    reference_type="CASH_MOVEMENT",
                    reference_id=uuid.uuid4(),
                    reason="nothing to reverse",
                    actor=world.head_actor,
                )
            return refusal.value

        assert run_scenario(main_database, scenario).http_status == 404

    def test_reversing_by_reference_twice_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        run_scenario(main_database, post_movement(world, amount="80", reference_id=reference_id))

        async def scenario(service):
            await service.reverse_transaction(
                reference_type="CASH_MOVEMENT",
                reference_id=reference_id,
                reason="first",
                actor=world.head_actor,
            )
            with pytest.raises(AlreadyReversedError):
                await service.reverse_transaction(
                    reference_type="CASH_MOVEMENT",
                    reference_id=reference_id,
                    reason="second",
                    actor=world.head_actor,
                )
            return True

        assert run_scenario(main_database, scenario) is True


class TestReversalAuthorization:
    def test_an_actor_without_the_reversal_authority_is_refused_and_audited(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A cash movement is reversed by ``cash.adjust``; a CASHIER does not hold it."""
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="90"))
        cashier = world.actor(roles=(RoleName.CASHIER,))

        async def scenario(service):
            with pytest.raises(PermissionDeniedError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=original.id, reason="till was short", actor=cashier
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["required_permission"] == "cash.adjust"
        denied = read(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE action = :action ORDER BY seq DESC LIMIT 1
            """,
            action="LEDGER_POSTING_DENIED",
        )
        assert denied and denied[0]["new_data"]["required_permission"] == "cash.adjust"

    def test_a_branch_scoped_actor_cannot_reverse_another_branchs_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        from tests.accounting_helpers import (
            create_branch,
            deactivate_branch,
            load_currencies,
            scaffold_chart,
        )

        world = build_world(api_client, admin_headers, main_database)
        branch = create_branch(api_client, admin_headers)
        try:
            currencies = load_currencies(main_database, ["AFN"])
            accounts, _ = scaffold_chart(
                api_client, admin_headers, branch_id=uuid.UUID(branch["id"]), currencies=currencies
            )

            async def foreign_side(service):
                return await service.post_cash_movement(
                    movement_type="IN",
                    reference_id=uuid.uuid4(),
                    branch_id=uuid.UUID(branch["id"]),
                    cash_account_id=accounts["cash_afn"],
                    counter_account_id=accounts["capital"],
                    currency_id=currencies["AFN"].id,
                    amount=Decimal("60"),
                    actor=world.head_actor,
                )

            original = run_scenario(main_database, foreign_side)
            manager = world.actor(roles=(RoleName.MANAGER,))

            async def scenario(service):
                with pytest.raises(ForbiddenScopeError) as refusal:
                    await service.reverse_journal_entry(
                        journal_entry_id=original.id, reason="not my branch", actor=manager
                    )
                return refusal.value

            refusal = run_scenario(main_database, scenario)
            assert refusal.details["reason"] == "ANOTHER_BRANCH"
            assert refusal.details["target_branch_id"] == branch["id"]
        finally:
            deactivate_branch(api_client, admin_headers, branch["id"])

    def test_an_entry_without_an_author_cannot_be_reversed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        from tests.accounting_helpers import actor_for

        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="60"))
        anonymous = actor_for(None, branch_id=world.branch_id, roles=(RoleName.OWNER,))

        async def scenario(service):
            with pytest.raises(PermissionDeniedError) as refusal:
                await service.reverse_journal_entry(
                    journal_entry_id=original.id, reason="who am I", actor=anonymous
                )
            return refusal.value

        assert run_scenario(main_database, scenario).details["reason"] == "ACTOR_REQUIRED"

    def test_the_reversal_is_audited_with_its_reason_and_link(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="640"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="counter account was the wrong branch's",
                actor=world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        audited = read(
            main_database,
            """
            SELECT new_data, user_id, action FROM audit_logs
             WHERE entity_id = :entry AND action = 'JOURNAL_REVERSED'
            """,
            entry=reversal.id,
        )
        assert len(audited) == 1
        data = audited[0]["new_data"]
        assert data["reversal_of_id"] == str(original.id)
        assert data["reversal_reason"] == "counter account was the wrong branch's"
        assert data["original_reference_type"] == "CASH_MOVEMENT"
        assert data["mirrored_lines"] == 2
        assert data["total_debit"] == "640.0000000000"
        assert data["total_credit"] == "640.0000000000"
        assert audited[0]["user_id"] == world.head_user_id
        # Exactly one row per event: the reversal is audited as JOURNAL_REVERSED, not
        # twice (once as a posting and once as a reversal), so the chain stays readable.
        assert (
            count(main_database, "audit_logs", where="entity_id = :entry", entry=reversal.id) == 1
        )

    def test_a_reversal_survives_an_inactive_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Retiring a branch must not make its history uncorrectable."""
        from tests.accounting_helpers import create_branch, deactivate_branch

        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="70"))
        # A second branch keeps MAIN from being the only active one, which the API refuses
        # to retire.
        spare = create_branch(api_client, admin_headers)
        deactivate_branch(api_client, admin_headers, str(world.branch_id))
        try:

            async def scenario(service):
                return await service.reverse_journal_entry(
                    journal_entry_id=original.id,
                    reason="branch closed with an error on the books",
                    actor=world.head_actor,
                )

            reversal = run_scenario(main_database, scenario)
            assert reversal.reference_type == "REVERSAL"
            assert reversal.branch_id == world.branch_id
        finally:
            from tests.helpers import execute_sql

            execute_sql(
                main_database,
                "UPDATE branches SET is_active = TRUE WHERE id = :branch",
                branch=world.branch_id,
            )
            deactivate_branch(api_client, admin_headers, spare["id"])


class TestLedgerHistoryStaysQueryable:
    def test_the_journal_api_shows_the_reversal_chain(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="310"))

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="correction", actor=world.head_actor
            )

        reversal = run_scenario(main_database, scenario)
        listing = api_client.get(
            "/api/v1/journal",
            headers=admin_headers,
            params={"reference_type": "REVERSAL"},
        )
        assert listing.status_code == 200
        ids = [row["id"] for row in listing.json()["items"]]
        assert str(reversal.id) in ids

    def test_a_reversed_entry_cannot_be_resurrected(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """No API path mutates the ledger; the only write is a new posting."""
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(main_database, post_movement(world, amount="55"))

        assert (
            api_client.delete(f"/api/v1/journal/{original.id}", headers=admin_headers).status_code
            == 405
        )
        assert (
            api_client.patch(
                f"/api/v1/journal/{original.id}", headers=admin_headers, json={}
            ).status_code
            == 405
        )
        assert (
            api_client.put(
                f"/api/v1/journal/{original.id}", headers=admin_headers, json={}
            ).status_code
            == 405
        )
        assert (
            scalar(
                main_database,
                "SELECT count(*) FROM journal_entries WHERE id = :entry",
                entry=original.id,
            )
            == 1
        )
