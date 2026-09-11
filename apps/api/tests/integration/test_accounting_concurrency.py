"""Phase 4 — concurrent posting: races are resolved by the database, not by luck.

Two cashiers on two devices really do post at the same instant, and the retry of a dropped
connection really does arrive while the first attempt is still in flight. The guarantees
that must survive that are the same ones that must survive a single posting:

* one document, one journal entry — ``ux_journal_entries_one_per_reference`` decides;
* one reversal per entry — the row lock on the original plus
  ``ux_journal_entries_reversed_once`` decide;
* a drawer cannot deliver more currency than it holds — the account row lock plus the
  quantity guard in ``_carrying_rate`` decide (§6.3);
* the entry a loser never committed leaves nothing behind, and the ledger is still
  balanced.

Every scenario here runs on its own engine in one event loop, so the callers meet inside
PostgreSQL — in the unique indexes, the row locks and the deferred balance constraint —
and never inside Python. ``run_race`` returns exceptions rather than raising them, so a
loser cannot hide the winner's result.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import (
    AlreadyReversedError,
    DuplicateResourceError,
    InsufficientBalanceError,
    JournalUnbalancedError,
)
from app.core.money import money_sum
from tests.accounting_helpers import (
    World,
    build_world,
    count,
    ledger_rows,
    line,
    read,
    run_race,
    run_scenario,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.accounting]


def cash_in(world: World, *, reference_id: uuid.UUID, amount: str):
    """A valid two-leg receipt, varied by amount and reference."""

    async def scenario(service):
        return await service.post_cash_movement(
            movement_type="IN",
            reference_id=reference_id,
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_afn"),
            counter_account_id=world.account("capital"),
            currency_id=world.base.id,
            amount=Decimal(amount),
            actor=world.head_actor,
        )

    return scenario


def sell_usd(world: World, *, reference_id: uuid.UUID, amount: str):
    """A disposal of the branch's USD drawer at its carrying rate."""

    async def scenario(service):
        return await service.post_exchange(
            transaction_type="SELL",
            reference_id=reference_id,
            branch_id=world.branch_id,
            from_currency_id=world.money("USD").id,
            to_currency_id=world.base.id,
            from_amount=Decimal(amount),
            exchange_rate=Decimal("70"),
            from_cash_account_id=world.account("cash_usd"),
            to_cash_account_id=world.account("cash_afn"),
            fx_account_id=world.account("fx"),
            commission=Decimal("0"),
            actor=world.head_actor,
        )

    return scenario


def open_usd(world: World, *, reference_id: uuid.UUID, amount: str):
    async def scenario(service):
        return await service.post_cash_movement(
            movement_type="OPENING",
            reference_id=reference_id,
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_usd"),
            counter_account_id=world.account("capital"),
            currency_id=world.money("USD").id,
            amount=Decimal(amount),
            exchange_rate=Decimal("70"),
            actor=world.head_actor,
        )

    return scenario


def quantity_of(database: str, account_id: uuid.UUID) -> Decimal:
    """The units of currency the account holds, summed from the immutable ledger."""
    return Decimal(
        str(
            scalar(
                database,
                """
                SELECT COALESCE(SUM(foreign_amount) FILTER (WHERE debit > 0), 0)
                     - COALESCE(SUM(foreign_amount) FILTER (WHERE credit > 0), 0)
                  FROM journal_lines WHERE account_id = :account
                """,
                account=account_id,
            )
        )
    )


def ledger_is_balanced(database: str) -> bool:
    row = read(database, "SELECT SUM(debit) AS debit, SUM(credit) AS credit FROM journal_lines")[0]
    return row["debit"] == row["credit"]


class TestTheSameDocumentCannotBePostedTwiceConcurrently:
    def test_two_callers_posting_one_document_produce_one_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The unique index is the referee: one winner, one refused duplicate."""
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()

        results = run_race(
            main_database,
            cash_in(world, reference_id=reference_id, amount="100"),
            cash_in(world, reference_id=reference_id, amount="100"),
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == 1, f"expected exactly one winner, got {results}"
        assert len(losers) == 1
        assert isinstance(losers[0], DuplicateResourceError)
        assert losers[0].details["journal_entry_id"] == str(winners[0].id)
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
                "journal_lines",
                where="journal_entry_id = :entry",
                entry=winners[0].id,
            )
            == 2
        )
        assert ledger_is_balanced(main_database)

    def test_a_concurrent_duplicate_leaves_no_half_written_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A loser inside ``_post`` must roll back its lines with it."""
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        entries_before = count(main_database, "journal_entries")
        lines_before = count(main_database, "journal_lines")

        run_race(
            main_database,
            cash_in(world, reference_id=reference_id, amount="55"),
            cash_in(world, reference_id=reference_id, amount="55"),
            cash_in(world, reference_id=reference_id, amount="55"),
        )

        assert count(main_database, "journal_entries") == entries_before + 1
        assert count(main_database, "journal_lines") == lines_before + 2

    def test_many_postings_of_distinct_documents_all_land(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Concurrency must not turn into lost writes: every distinct document posts."""
        world = build_world(api_client, admin_headers, main_database)
        references = [uuid.uuid4() for _ in range(6)]

        results = run_race(
            main_database,
            *(cash_in(world, reference_id=reference, amount="10") for reference in references),
        )
        assert all(not isinstance(result, Exception) for result in results), results
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = ANY(:references)",
                references=references,
            )
            == 6
        )
        assert ledger_is_balanced(main_database)


class TestDisposalsCannotRacePastThePosition:
    def test_two_racing_disposals_cannot_oversell_the_drawer(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """1,000 USD held, two simultaneous sales of 700: exactly one may post."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_usd(world, reference_id=uuid.uuid4(), amount="1000"))

        results = run_race(
            main_database,
            sell_usd(world, reference_id=uuid.uuid4(), amount="700"),
            sell_usd(world, reference_id=uuid.uuid4(), amount="700"),
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == 1, f"expected one sale, got {results}"
        assert isinstance(losers[0], InsufficientBalanceError)
        assert losers[0].details["reason"] == "QUANTITY_EXCEEDED"
        # The position can never go below zero, and the loser wrote nothing.
        assert quantity_of(main_database, world.account("cash_usd")) == Decimal("300.0000000000")
        assert ledger_is_balanced(main_database)

    def test_racing_disposals_that_fit_are_both_posted(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The guard must refuse overselling, not concurrency: two 400s fit in 1,000."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_usd(world, reference_id=uuid.uuid4(), amount="1000"))

        results = run_race(
            main_database,
            sell_usd(world, reference_id=uuid.uuid4(), amount="400"),
            sell_usd(world, reference_id=uuid.uuid4(), amount="400"),
        )
        assert all(not isinstance(result, Exception) for result in results), results
        assert quantity_of(main_database, world.account("cash_usd")) == Decimal("200.0000000000")
        assert ledger_is_balanced(main_database)

    def test_a_race_against_a_receipt_is_still_priced_from_the_ledger(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A concurrent deposit must not be mistaken for a cheaper carrying rate."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_usd(world, reference_id=uuid.uuid4(), amount="1000"))

        results = run_race(
            main_database,
            sell_usd(world, reference_id=uuid.uuid4(), amount="500"),
            open_usd(world, reference_id=uuid.uuid4(), amount="1000"),
        )
        assert all(not isinstance(result, Exception) for result in results), results
        # 1,000 @ 70 then 1,000 @ 70 again, less the 500 sold: 1,500 units remain, and the
        # disposal that got through was priced at 70, not at a blended guess.
        assert quantity_of(main_database, world.account("cash_usd")) == Decimal("1500.0000000000")
        sold = read(
            main_database,
            """
            SELECT exchange_rate FROM journal_lines
             WHERE account_id = :account AND credit > 0
            """,
            account=world.account("cash_usd"),
        )
        assert [row["exchange_rate"] for row in sold] == [Decimal("70.0000000000")]


class TestReversalsRace:
    def test_two_simultaneous_reversals_produce_one_mirror(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        original = run_scenario(
            main_database, cash_in(world, reference_id=uuid.uuid4(), amount="120")
        )

        async def reverse(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id,
                reason="counter account was wrong",
                actor=world.head_actor,
            )

        results = run_race(main_database, reverse, reverse)
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == 1, f"expected exactly one reversal, got {results}"
        assert isinstance(losers[0], AlreadyReversedError)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reversal_of_id = :entry",
                entry=original.id,
            )
            == 1
        )
        assert ledger_is_balanced(main_database)
        # The account is back to zero: one correction, not two.
        assert Decimal(
            str(
                scalar(
                    main_database,
                    """
                    SELECT COALESCE(SUM(debit - credit), 0) FROM journal_lines
                     WHERE account_id = :account AND currency_id = :currency
                    """,
                    account=world.account("cash_afn"),
                    currency=world.base.id,
                )
            )
        ) == Decimal("0.0000000000")

    def test_a_reversal_racing_a_second_posting_of_the_same_document(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Posting and reversing the same document at once still yields one of each."""
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()
        original = run_scenario(
            main_database, cash_in(world, reference_id=reference_id, amount="30")
        )

        async def post_again(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=reference_id,
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("30"),
                actor=world.head_actor,
            )

        async def reverse(service):
            return await service.reverse_journal_entry(
                journal_entry_id=original.id, reason="duplicate", actor=world.head_actor
            )

        results = run_race(main_database, post_again, reverse)
        # The second posting of the document loses (409); the reversal succeeds. Either
        # way the document ends with one entry and one mirror.
        assert any(isinstance(result, DuplicateResourceError) for result in results), results
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
                where="reversal_of_id = :entry",
                entry=original.id,
            )
            == 1
        )
        assert ledger_is_balanced(main_database)


class TestTheLedgerSurvivesContention:
    def test_many_callers_on_one_account_leave_a_balanced_ledger(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Six postings to the same two accounts: all land, in any order, balanced."""
        world = build_world(api_client, admin_headers, main_database)
        references = [uuid.uuid4() for _ in range(6)]

        results = run_race(
            main_database,
            *(cash_in(world, reference_id=reference, amount="25") for reference in references),
        )
        assert all(not isinstance(result, Exception) for result in results), results
        assert scalar(
            main_database,
            """
                SELECT COALESCE(SUM(debit - credit), 0) FROM journal_lines
                 WHERE account_id = :account AND currency_id = :currency
                """,
            account=world.account("cash_afn"),
            currency=world.base.id,
        ) == Decimal("150.0000000000")
        assert ledger_is_balanced(main_database)

    def test_a_race_between_a_valid_and_an_invalid_entry_leaves_only_the_valid_one(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        valid_reference, invalid_reference = uuid.uuid4(), uuid.uuid4()

        async def invalid(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=invalid_reference,
                branch_id=world.branch_id,
                lines=[
                    line(world.account("cash_afn"), debit="100", currency_id=world.base.id),
                    line(world.account("capital"), credit="99", currency_id=world.base.id),
                ],
                actor=world.head_actor,
                description="unbalanced",
            )

        results = run_race(
            main_database,
            cash_in(world, reference_id=valid_reference, amount="100"),
            invalid,
        )
        winners = [result for result in results if not isinstance(result, Exception)]
        losers = [result for result in results if isinstance(result, Exception)]
        assert len(winners) == 1
        assert isinstance(losers[0], JournalUnbalancedError)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id",
                reference_id=invalid_reference,
            )
            == 0
        )
        assert ledger_is_balanced(main_database)

    def test_the_posted_lines_of_a_race_are_reproducible(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Whatever order the race resolved in, each winner's entry stands on its own."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        references = [uuid.uuid4() for _ in range(4)]

        results = run_race(
            main_database,
            *(cash_in(world, reference_id=reference, amount="40") for reference in references),
        )
        for result in results:
            assert not isinstance(result, Exception), result
            stored = ledger_rows(main_database, result.id)
            debit = money_sum(Decimal(row["debit"]) for row in stored)
            credit = money_sum(Decimal(row["credit"]) for row in stored)
            assert debit == credit == Decimal("40.0000000000")
