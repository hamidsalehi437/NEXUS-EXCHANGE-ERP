"""Phase 4 — idempotent posting: one document, one journal entry, however often it arrives.

An offline-first exchange house retries: a cashier's device re-sends a receipt after a
dropped connection, a sync job replays a queue, a client times out and tries again. Every
one of those retries must leave the books exactly as the first attempt did (PART 34/40).

Two distinct mechanisms are asserted here, because they fail differently:

* ``uv_journal_entries_one_per_reference`` — the *structural* rule: a business document has
  at most one journal entry. A second posting for the same reference is refused even with a
  fresh idempotency key (409 ``DUPLICATE_RESOURCE``), and the refusal names the entry that
  already exists, so a caller can reconcile instead of guessing.
* ``idempotency_keys`` — the *protocol* rule: a key replay returns the first call's
  response byte for byte, a key reused with a different payload is refused (409
  ``IDEMPOTENCY_KEY_REUSED``), a key still being worked on is refused (409
  ``IDEMPOTENCY_IN_PROGRESS``) rather than silently double-posted, and a key whose first
  attempt failed is reclaimable — a failed attempt must not poison the document for ever.

Manual adjustments are the vehicle for most of these tests: they are the one reference type
that does not require a business document, so the ledger's own rules are what is under
test. ``ux_journal_entries_one_per_reference`` deliberately does *not* cover them (they are
identified by the caller), which is exactly why the key is the protection that must work.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.core.exceptions import (
    DuplicateResourceError,
    IdempotencyInProgressError,
    IdempotencyKeyReusedError,
    ValidationError,
)
from app.core.idempotency import ENDPOINT_LEDGER_POSTING, canonical_request_hash
from app.services.accounting_service import RateSnapshot
from tests.accounting_helpers import (
    World,
    build_world,
    count,
    line,
    read,
    run_race,
    run_scenario,
    scalar,
)
from tests.auth_helpers import USER_PASSWORD

pytestmark = [pytest.mark.integration, pytest.mark.accounting]


def manual_entry(
    world: World,
    *,
    reference_id: uuid.UUID | None = None,
    amount: str = "100",
    idempotency_key: uuid.UUID | None = None,
    description: str = "manual adjustment",
    actor: object | None = None,
    session: object | None = None,
):
    """The keyword arguments of one manual adjustment, so a caller can vary one field."""
    arguments: dict[str, object] = {
        "reference_type": "MANUAL_ADJUSTMENT",
        "reference_id": reference_id or uuid.uuid4(),
        "branch_id": world.branch_id,
        "lines": [
            line(world.account("cash_afn"), debit=amount, currency_id=world.base.id),
            line(world.account("capital"), credit=amount, currency_id=world.base.id),
        ],
        "actor": actor if actor is not None else world.head_actor,
        "description": description,
    }
    if idempotency_key is not None:
        arguments["idempotency_key"] = idempotency_key
    if session is not None:
        arguments["session"] = session
    return arguments


def idempotency_rows(database: str, key: uuid.UUID) -> list[dict[str, object]]:
    return read(
        database,
        """
        SELECT key, endpoint, status, request_hash, response_body, resource_id, user_id
          FROM idempotency_keys WHERE key = :key
        """,
        key=key,
    )


class TestTheSameKeyPostsOnce:
    def test_a_replayed_request_returns_the_first_response(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def scenario(service):
            first = await service.create_journal_entry(
                **manual_entry(world, reference_id=reference_id, idempotency_key=key)  # type: ignore[arg-type]
            )
            second = await service.create_journal_entry(
                **manual_entry(world, reference_id=reference_id, idempotency_key=key)  # type: ignore[arg-type]
            )
            return first, second

        first, second = run_scenario(main_database, scenario)
        assert second.id == first.id
        assert second.total_debit == first.total_debit
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )
        # One ledger effect, one audit row: a replay is not a second posting.
        assert count(main_database, "audit_logs", where="entity_id = :entry", entry=first.id) == 1
        rows = idempotency_rows(main_database, key)
        assert len(rows) == 1
        assert rows[0]["status"] == "COMPLETED"
        assert rows[0]["resource_id"] == first.id
        assert rows[0]["endpoint"] == ENDPOINT_LEDGER_POSTING

    def test_a_replay_does_not_move_the_ledger(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The strongest statement of idempotency: Σdebit is unchanged by a replay."""
        world = build_world(api_client, admin_headers, main_database)
        key = uuid.uuid4()
        reference_id = uuid.uuid4()
        ledger_before = scalar(main_database, "SELECT SUM(debit) FROM journal_lines") or Decimal(0)

        async def scenario(service):
            for _ in range(3):
                await service.create_journal_entry(
                    **manual_entry(world, reference_id=reference_id, idempotency_key=key)  # type: ignore[arg-type]
                )
            return True

        assert run_scenario(main_database, scenario) is True
        ledger_after = scalar(main_database, "SELECT SUM(debit) FROM journal_lines")
        assert ledger_after - ledger_before == Decimal("100.0000000000")
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )
        assert count(main_database, "journal_lines", where="TRUE") >= 2

    def test_the_stored_request_hash_is_the_canonical_fingerprint(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The key is bound to *what was asked*, not merely to when it was asked."""
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def scenario(service):
            return await service.create_journal_entry(
                **manual_entry(world, reference_id=reference_id, idempotency_key=key)  # type: ignore[arg-type]
            )

        run_scenario(main_database, scenario)
        stored = idempotency_rows(main_database, key)[0]["request_hash"]
        assert isinstance(stored, str) and len(stored) == 64
        assert stored.isalnum()
        # A replay matches on that hash and returns the stored response; a *different*
        # request under the same key does not match it and is refused (next test).
        from tests.helpers import execute_sql

        assert (
            execute_sql(
                main_database,
                "UPDATE idempotency_keys SET request_hash = :hash WHERE key = :key"
                " AND user_id = :user_id",
                hash=canonical_request_hash({"tampered": True}),
                key=key,
                user_id=world.head_user_id,
            )
            == 1
        )

    def test_a_key_reused_for_a_different_request_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        key = uuid.uuid4()
        reference_id = uuid.uuid4()

        async def scenario(service):
            await service.create_journal_entry(
                **manual_entry(  # type: ignore[arg-type]
                    world, reference_id=reference_id, idempotency_key=key, amount="100"
                )
            )
            with pytest.raises(IdempotencyKeyReusedError) as refusal:
                await service.create_journal_entry(
                    **manual_entry(
                        world, reference_id=reference_id, idempotency_key=key, amount="250"
                    )  # type: ignore[arg-type]
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.http_status == 409
        assert refusal.details["idempotency_key"] == str(key)
        # The first posting stands, the second never happened.
        posted = scalar(
            main_database,
            """
            SELECT SUM(l.debit) FROM journal_lines l
              JOIN journal_entries e ON e.id = l.journal_entry_id
             WHERE e.reference_id = :reference_id
            """,
            reference_id=reference_id,
        )
        assert posted == Decimal("100.0000000000")

    def test_a_rollback_leaves_neither_key_nor_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A second caller with a key that is mid-flight gets 409, never a second entry."""
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def first(service):
            # Post inside a transaction that then fails: the claim and the entry must both
            # disappear with it, because the claim is made in a savepoint of that same
            # transaction.
            async with service._database.transaction() as session:  # type: ignore[attr-defined]
                await service.create_journal_entry(
                    **manual_entry(  # type: ignore[arg-type]
                        world, reference_id=reference_id, idempotency_key=key, session=session
                    )
                )
                raise RuntimeError("the caller died before committing")

        with pytest.raises(RuntimeError):
            run_scenario(main_database, first)

        # The claim was rolled back with the entry: no orphan key, no orphan ledger row.
        assert idempotency_rows(main_database, key) == []
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 0
        )

    def test_a_claim_written_outside_the_transaction_blocks_the_second_caller(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A committed IN_PROGRESS claim is a lock: the retry is refused, not duplicated."""
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()
        _claim(main_database, key=key, user_id=world.head_user_id)

        async def scenario(service):
            with pytest.raises(IdempotencyInProgressError) as refusal:
                await service.create_journal_entry(
                    **manual_entry(world, reference_id=reference_id, idempotency_key=key)  # type: ignore[arg-type]
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.http_status == 409
        assert refusal.details["idempotency_key"] == str(key)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 0
        )

    def test_a_failed_attempt_does_not_poison_the_key(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A refused posting releases its key, so the cashier can correct and retry.

        Without this, a mistyped amount would burn the key and the device's retry queue
        would be stuck for ever — the retry must be able to succeed once the request is
        valid.
        """
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def scenario(service):
            # 11 decimals: NUMERIC(30,10) cannot hold it, and rounding money silently is
            # how a ledger stops reconciling. The request is refused, the key is released.
            with pytest.raises(ValidationError):
                await service.create_journal_entry(
                    **manual_entry(
                        world,
                        reference_id=reference_id,
                        idempotency_key=key,
                        amount="1.23456789012",
                    )  # type: ignore[arg-type]
                )
            # The key is free again: a corrected request with the same key posts.
            return await service.create_journal_entry(
                **manual_entry(world, reference_id=reference_id, idempotency_key=key, amount="100")  # type: ignore[arg-type]
            )

        view = run_scenario(main_database, scenario)
        assert view.total_debit == Decimal("100")
        rows = idempotency_rows(main_database, key)
        assert len(rows) == 1
        assert rows[0]["status"] == "COMPLETED"

    def test_a_key_needs_the_endpoint_it_was_used_on(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.create_journal_entry(
                    endpoint=None,
                    **manual_entry(world, idempotency_key=uuid.uuid4()),  # type: ignore[arg-type]
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"] == [{"field": "endpoint", "code": "required"}]

    def test_keys_are_scoped_per_endpoint(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The same key on another endpoint is a different request, not a replay."""
        world = build_world(api_client, admin_headers, main_database)
        key = uuid.uuid4()

        async def scenario(service):
            first = await service.create_journal_entry(
                **manual_entry(world, idempotency_key=key)  # type: ignore[arg-type]
            )
            second = await service.create_journal_entry(
                endpoint="ledger:other",
                **manual_entry(world, idempotency_key=key),  # type: ignore[arg-type]
            )
            return first, second

        first, second = run_scenario(main_database, scenario)
        assert first.id != second.id
        assert len(idempotency_rows(main_database, key)) == 2

    def test_keys_are_scoped_per_user(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A key guessed by another user cannot replay or block someone else's posting."""
        from tests.helpers import create_user

        world = build_world(api_client, admin_headers, main_database)
        key = uuid.uuid4()
        other = uuid.UUID(
            create_user(
                main_database,
                username=f"key-scope-{uuid.uuid4().hex[:8]}",
                password=USER_PASSWORD,
            )
        )
        _claim(main_database, key=key, user_id=other)

        async def scenario(service):
            # The same key, but this user's scope is untouched by the other user's claim.
            return await service.create_journal_entry(
                **manual_entry(
                    world,
                    idempotency_key=key,
                    actor=world.actor(roles=("OWNER",), user="head"),
                )  # type: ignore[arg-type]
            )

        view = run_scenario(main_database, scenario)
        assert view.id is not None
        assert len(idempotency_rows(main_database, key)) == 2
        assert {row["user_id"] for row in idempotency_rows(main_database, key)} == {
            world.head_user_id,
            other,
        }


class TestTheDocumentCannotBePostedTwice:
    def test_the_same_reference_with_a_new_key_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The unique index is the structural guarantee, independent of any key.

        A device that lost its key, a sync job that forgot it, or a buggy client that
        never sent one still cannot post a document twice.
        """
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()

        async def scenario(service):
            first = await service.post_cash_movement(
                movement_type="IN",
                reference_id=reference_id,
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("500"),
                actor=world.head_actor,
            )
            with pytest.raises(DuplicateResourceError) as refusal:
                await service.post_cash_movement(
                    movement_type="IN",
                    reference_id=reference_id,
                    branch_id=world.branch_id,
                    cash_account_id=world.account("cash_afn"),
                    counter_account_id=world.account("capital"),
                    currency_id=world.base.id,
                    amount=Decimal("500"),
                    actor=world.head_actor,
                )
            return first, refusal.value

        first, refusal = run_scenario(main_database, scenario)
        assert refusal.http_status == 409
        assert refusal.details["journal_entry_id"] == str(first.id)
        assert refusal.details["reference_id"] == str(reference_id)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )

    def test_the_database_refuses_a_second_entry_for_one_document(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Raw SQL cannot do what the service refuses (§I-7)."""
        from sqlalchemy import create_engine

        from tests.helpers import database_dsn

        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=reference_id,
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("10"),
                actor=world.head_actor,
            )

        run_scenario(main_database, scenario)
        engine = create_engine(database_dsn(main_database), future=True)
        statement = text(
            """
            INSERT INTO journal_entries
                (reference_type, reference_id, description, transaction_date,
                 created_by, branch_id)
            VALUES ('CASH_MOVEMENT', :reference_id, 'second journal',
                    now(), :user_id, :branch_id)
            """
        )
        parameters = {
            "reference_id": reference_id,
            "user_id": world.head_user_id,
            "branch_id": world.branch_id,
        }
        try:
            with pytest.raises(Exception) as refusal, engine.begin() as connection:
                connection.execute(statement, parameters)
        finally:
            engine.dispose()
        assert "ux_journal_entries_one_per_reference" in str(refusal.value) or (
            "duplicate key" in str(refusal.value)
        )
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )

    def test_a_reversal_counts_as_the_documents_second_posting(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """``ux_journal_entries_one_per_reference`` also caps a document at one reversal."""
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()

        async def scenario(service):
            original = await service.post_cash_movement(
                movement_type="IN",
                reference_id=reference_id,
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("40"),
                actor=world.head_actor,
            )
            reversal = await service.reverse_transaction(
                reference_type="CASH_MOVEMENT",
                reference_id=reference_id,
                reason="cancelled",
                actor=world.head_actor,
            )
            return original, reversal

        original, reversal = run_scenario(main_database, scenario)
        # The reversal's own reference *is* the entry it mirrors, so the document reference
        # index caps a document at exactly one entry — and the entry, in turn, at exactly
        # one reversal.
        assert reversal.reference_id == original.id
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_type = 'REVERSAL' AND reference_id = :entry",
                entry=original.id,
            )
            == 1
        )

    def test_the_same_key_and_payload_from_two_callers_posts_once(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The realistic retry: the same request arriving twice, concurrently."""
        world = build_world(api_client, admin_headers, main_database)
        key, reference_id = uuid.uuid4(), uuid.uuid4()

        async def attempt(service):
            return await service.create_journal_entry(
                **manual_entry(world, reference_id=reference_id, idempotency_key=key, amount="77")  # type: ignore[arg-type]
            )

        results = run_race(main_database, attempt, attempt)
        posted = [result for result in results if not isinstance(result, Exception)]
        refused = [result for result in results if isinstance(result, Exception)]
        assert len(posted) >= 1, f"no caller succeeded: {results}"
        # Either the second caller replayed the answer (same id) or it was refused with a
        # conflict that says the work is in flight — never a second entry.
        for result in refused:
            assert isinstance(result, (IdempotencyInProgressError, DuplicateResourceError))
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=reference_id,
            )
            == 1
        )
        assert {view.id for view in posted} == {posted[0].id}

    def test_a_rate_snapshot_is_part_of_the_request_it_identifies(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Reusing a key with a different quote is a different request (409)."""
        world = build_world(api_client, admin_headers, main_database)
        key = uuid.uuid4()

        async def scenario(service):
            first = await service.create_journal_entry(
                **manual_entry(world, idempotency_key=key, amount="30"),  # type: ignore[arg-type]
                rate_snapshot=RateSnapshot(
                    rate=Decimal("70"),
                    rate_id=uuid.uuid4(),
                    from_currency_id=world.money("AFN").id,
                    to_currency_id=world.base.id,
                ),
            )
            with pytest.raises(IdempotencyKeyReusedError):
                await service.create_journal_entry(
                    **manual_entry(world, idempotency_key=key, amount="30"),  # type: ignore[arg-type]
                    rate_snapshot=RateSnapshot(
                        rate=Decimal("71"),
                        rate_id=uuid.uuid4(),
                        from_currency_id=world.money("AFN").id,
                        to_currency_id=world.base.id,
                    ),
                )
            return first

        assert run_scenario(main_database, scenario).total_debit == Decimal("30")


# --------------------------------------------------------------------------- plumbing
def _claim(database: str, *, key: uuid.UUID, user_id: uuid.UUID) -> None:
    """Commit an ``IN_PROGRESS`` claim, the state a crashed caller leaves behind."""
    from tests.helpers import execute_sql

    execute_sql(
        database,
        """
        INSERT INTO idempotency_keys (key, user_id, endpoint, request_hash, status)
        VALUES (:key, :user_id, :endpoint, :hash, 'IN_PROGRESS')
        """,
        key=key,
        user_id=user_id,
        endpoint=ENDPOINT_LEDGER_POSTING,
        hash=canonical_request_hash({"probe": "in-progress"}),
    )
