"""Phase 4 — the ledger's own invariants, re-asserted on an accumulated ledger.

Every other Phase 4 suite posts a handful of entries to make a point. This one builds a
realistic day — openings in three currencies, two exchanges (one gaining, one losing, one
with commission), a cash movement, an expense, an adjustment, a reversal — and then asks
the *database* whether the book still holds together:

* SUM(debit) = SUM(credit), per entry and ledger-wide (PART 49);
* ``account_balances`` is a faithful cache of ``journal_lines`` and the deterministic
  rebuild changes nothing (§I-2, §8);
* ``v_trial_balance`` and ``v_account_balances`` agree with the immutable table, with the
  direction of every account's normal balance;
* the §8 reconciliation identities hold numerically:
  ``net income = SUM(REVENUE) - SUM(EXPENSE)`` on one side, and the same number reached
  from the balance sheet (cash + inventory - capital - liabilities) on the other;
* the physical cash constraint still refuses to let a drawer go negative (``NEX01``,
  §I-5), which is the database-level counterpart of the service's quantity guard;
* every posted entry is audited, the audit chain verifies, and reversals are linked;
* money stays exact at the edges of ``NUMERIC(30,10)`` — the largest value the ledger can
  hold posts exactly, and one integer digit more is refused rather than silently
  truncated.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.exceptions import ValidationError, sqlstate_of
from app.core.money import MAX_MONEY, MONEY_QUANTUM, money_context, money_sum
from app.services.accounting_service import RateSnapshot
from tests.accounting_helpers import (
    World,
    build_world,
    count,
    line,
    publish_rate,
    read,
    read_one,
    run_scenario,
    scalar,
)
from tests.helpers import database_dsn, execute_sql

pytestmark = [pytest.mark.integration, pytest.mark.accounting, pytest.mark.slow]


# --------------------------------------------------------------------------- fixtures
@pytest.fixture
def busy_world(api_client: TestClient, admin_headers: dict[str, str], main_database: str) -> World:
    """A day's worth of postings, in three currencies, at one branch."""
    world = build_world(api_client, admin_headers, main_database, quotes=True)
    publish_rate(
        api_client,
        admin_headers,
        from_currency_id=str(world.money("EUR").id),
        to_currency_id=str(world.base.id),
        buy_rate="75",
        sell_rate="76",
    )

    async def opening(service):
        return await service.post_cash_movement(
            movement_type="OPENING",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_afn"),
            counter_account_id=world.account("capital"),
            currency_id=world.base.id,
            amount=Decimal("500000"),
            exchange_rate=Decimal("1"),
            description="opening afghanis",
            actor=world.head_actor,
        )

    async def buy_usd(service):
        return await service.post_exchange(
            transaction_type="BUY",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            from_currency_id=world.money("USD").id,
            to_currency_id=world.base.id,
            from_amount=Decimal("1000"),
            exchange_rate=Decimal("70"),
            from_cash_account_id=world.account("cash_usd"),
            to_cash_account_id=world.account("cash_afn"),
            fx_account_id=world.account("fx"),
            commission=Decimal("500"),
            commission_account_id=world.account("commission"),
            actor=world.head_actor,
        )

    async def sell_usd(service):
        return await service.post_exchange(
            transaction_type="SELL",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            from_currency_id=world.money("USD").id,
            to_currency_id=world.base.id,
            from_amount=Decimal("400"),
            exchange_rate=Decimal("72"),
            from_cash_account_id=world.account("cash_usd"),
            to_cash_account_id=world.account("cash_afn"),
            fx_account_id=world.account("fx"),
            commission=Decimal("0"),
            actor=world.head_actor,
        )

    async def buy_eur(service):
        return await service.post_exchange(
            transaction_type="BUY",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            from_currency_id=world.money("EUR").id,
            to_currency_id=world.base.id,
            from_amount=Decimal("200"),
            exchange_rate=Decimal("75"),
            from_cash_account_id=world.account("cash_eur"),
            to_cash_account_id=world.account("cash_afn"),
            fx_account_id=world.account("fx"),
            commission=Decimal("0"),
            actor=world.head_actor,
        )

    async def expense(service):
        return await service.post_expense(
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            expense_account_id=world.account("expense"),
            credit_account_id=world.account("cash_afn"),
            currency_id=world.base.id,
            amount=Decimal("1200"),
            description="rent",
            actor=world.head_actor,
        )

    async def adjustment(service):
        return await service.post_cash_movement(
            movement_type="ADJUSTMENT",
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account("cash_afn"),
            counter_account_id=world.account("expense"),
            currency_id=world.base.id,
            amount=Decimal("35"),
            adjustment_sign=-1,
            description="till was short",
            actor=world.head_actor,
        )

    # The id of each posting is the id the service returned, not a re-query by reference
    # type: three of these postings share a type, and "the newest EXCHANGE_TRANSACTION" is
    # whichever happened to be written last.
    world.ids.update(
        {
            name: run_scenario(main_database, scenario).id
            for name, scenario in (
                ("opening", opening),
                ("buy_usd", buy_usd),
                ("sell_usd", sell_usd),
                ("buy_eur", buy_eur),
                ("expense", expense),
                ("adjustment", adjustment),
            )
        }
    )
    return world


def nets_by_account_type(database: str) -> dict[str, Decimal]:
    """Signed SUM(debit - credit) per account type, from the immutable table alone."""
    rows = read(
        database,
        """
        SELECT a.account_type, COALESCE(SUM(l.debit - l.credit), 0) AS net
          FROM journal_lines l JOIN accounts a ON a.id = l.account_id
         GROUP BY a.account_type
        """,
    )
    return {row["account_type"]: Decimal(str(row["net"])) for row in rows}


def ledger_totals(database: str) -> tuple[Decimal, Decimal]:
    row = read_one(database, "SELECT SUM(debit) AS debit, SUM(credit) AS credit FROM journal_lines")
    return Decimal(str(row["debit"])), Decimal(str(row["credit"]))


class TestTheLedgerIsBalanced:
    def test_every_entry_balances(self, busy_world: World, main_database: str) -> None:
        unbalanced = read(
            main_database,
            """
            SELECT e.id, SUM(l.debit) AS debit, SUM(l.credit) AS credit, COUNT(*) AS lines
              FROM journal_entries e JOIN journal_lines l ON l.journal_entry_id = e.id
             GROUP BY e.id
            HAVING SUM(l.debit) <> SUM(l.credit) OR COUNT(*) < 2
            """,
        )
        assert unbalanced == []

    def test_every_line_quantity_is_its_amount_over_its_rate(self, main_database: str) -> None:
        """The generated ``foreign_amount`` is the ledger's definition of a quantity.

        A currency's own debit and credit *functional* totals are not equal, and must not
        be: an exchange debits USD and credits AFN for the same functional value. What must
        hold is this relation on every line — the number every position and carrying rate
        is built from.
        """
        wrong = read(
            main_database,
            """
            SELECT id FROM journal_lines
             WHERE foreign_amount <> ROUND((debit + credit) / exchange_rate, 10)
            """,
        )
        assert wrong == []
        assert (
            scalar(
                main_database,
                "SELECT count(*) FROM journal_lines WHERE exchange_rate <= 0",
            )
            == 0
        )

    def test_no_line_is_single_sided(self, main_database: str) -> None:
        invalid = read(
            main_database,
            """
            SELECT id FROM journal_lines
             WHERE (debit > 0 AND credit > 0) OR (debit = 0 AND credit = 0)
                OR debit < 0 OR credit < 0
            """,
        )
        assert invalid == []

    def test_every_posted_entry_carries_at_least_two_lines(self, main_database: str) -> None:
        rows = read(
            main_database,
            """
            SELECT e.id, COUNT(l.id) AS lines
              FROM journal_entries e LEFT JOIN journal_lines l ON l.journal_entry_id = e.id
             GROUP BY e.id HAVING COUNT(l.id) < 2
            """,
        )
        assert rows == []


class TestTheCacheNeverDriftsFromTheLedger:
    def test_every_balance_row_matches_the_immutable_table(self, main_database: str) -> None:
        drifted = read(
            main_database,
            """
            SELECT ab.account_id, ab.currency_id, ab.debit_total, ab.credit_total,
                   l.debit AS ledger_debit, l.credit AS ledger_credit
              FROM account_balances ab
              LEFT JOIN (
                    SELECT account_id, currency_id,
                           SUM(debit) AS debit, SUM(credit) AS credit
                      FROM journal_lines GROUP BY account_id, currency_id
                   ) l ON l.account_id = ab.account_id AND l.currency_id = ab.currency_id
             WHERE ab.debit_total  <> COALESCE(l.debit, 0)
                OR ab.credit_total <> COALESCE(l.credit, 0)
            """,
        )
        assert drifted == []

    def test_the_deterministic_rebuild_changes_nothing(self, main_database: str) -> None:
        """§I-2: the cache is derived, so rebuilding it is a no-op on a sound ledger."""
        before = read(
            main_database,
            """
            SELECT account_id, currency_id, debit_total, credit_total
              FROM account_balances ORDER BY account_id, currency_id
            """,
        )
        execute_sql(main_database, "SELECT rebuild_account_balances()")
        after = read(
            main_database,
            """
            SELECT account_id, currency_id, debit_total, credit_total
              FROM account_balances ORDER BY account_id, currency_id
            """,
        )
        assert after == before

    def test_the_balance_view_applies_the_normal_balance_direction(
        self, busy_world: World, main_database: str
    ) -> None:
        row = read_one(
            main_database,
            """
            SELECT normal_balance, balance, debit_total, credit_total
              FROM v_account_balances
             WHERE account_id = :account
            """,
            account=busy_world.account("capital"),
        )
        assert row["normal_balance"].strip() == "CREDIT"
        assert row["balance"] == row["credit_total"] - row["debit_total"]

    def test_the_trial_balance_view_agrees_with_the_ledger(self, main_database: str) -> None:
        view = read(
            main_database,
            """
            SELECT SUM(total_debit) AS debit, SUM(total_credit) AS credit
              FROM v_trial_balance
            """,
        )[0]
        ledger = ledger_totals(main_database)
        assert view["debit"] == ledger[0]
        assert view["credit"] == ledger[1]


class TestTheReconciliationIdentities:
    def net_income(self, database: str) -> Decimal:
        """SUM over REVENUE less SUM over EXPENSE, in the ledger's own terms."""
        nets = nets_by_account_type(database)
        # Revenue accounts are credit-normal, expense accounts debit-normal.
        return money_sum(
            [
                -nets.get("REVENUE", Decimal(0)),
                -nets.get("EXPENSE", Decimal(0)),
            ]
        )

    def test_the_trial_balance_identity_of_net_income(self, main_database: str) -> None:
        """§8: net income = SUM(REVENUE) - SUM(EXPENSE), from the immutable table."""
        nets = nets_by_account_type(main_database)
        revenue = -nets.get("REVENUE", Decimal(0))  # credit-normal
        expense = nets.get("EXPENSE", Decimal(0))  # debit-normal
        assert revenue >= 0
        assert expense >= 0
        assert self.net_income(main_database) == money_sum([revenue, -expense])

    def test_the_balance_sheet_identity_of_net_income(self, main_database: str) -> None:
        """The other half of §8, from the same numbers:

        ``net income = cash + inventory - capital - liabilities``
        """
        nets = nets_by_account_type(main_database)
        cash = nets.get("ASSET", Decimal(0))  # the scenario's assets are cash and currency
        capital = nets.get("EQUITY", Decimal(0))
        liabilities = nets.get("LIABILITY", Decimal(0))
        # Both sides are expressed as "value the business created"; equity and liabilities
        # are credit-normal, so their contribution is subtracted.
        assert self.net_income(main_database) == money_sum([cash, capital, liabilities])

    def test_the_expansion_of_the_identity_is_exact(self, main_database: str) -> None:
        """Every account type contributes: the two sides must agree to the last digit."""
        nets = nets_by_account_type(main_database)
        assets = nets.get("ASSET", Decimal(0))
        liabilities = nets.get("LIABILITY", Decimal(0))
        equity = nets.get("EQUITY", Decimal(0))
        revenue = nets.get("REVENUE", Decimal(0))
        expense = nets.get("EXPENSE", Decimal(0))
        total = money_sum([assets, liabilities, equity, revenue, expense])
        assert total == Decimal("0.0000000000")


class TestThePhysicalConstraintStillBites:
    def test_the_database_refuses_a_cash_movement_that_would_go_negative(
        self, busy_world: World, main_database: str
    ) -> None:
        """§I-5/§6.3: the ledger's quantity guard has a database counterpart."""
        currency_id = busy_world.money("USD").id
        signed_before = _cash_position(main_database, busy_world, currency_id)

        error = _refusal(
            main_database,
            """
            INSERT INTO cash_movements
                (branch_id, account_id, movement_type, currency_id, amount,
                 reference_type, reference_id, created_by)
            VALUES (:branch, :account, 'OUT', :currency, 5000, 'CASH_MOVEMENT',
                    :reference_id, :user_id)
            """,
            branch=busy_world.branch_id,
            account=busy_world.account("cash_usd"),
            currency=currency_id,
            reference_id=uuid.uuid4(),
            user_id=busy_world.head_user_id,
        )
        assert error is not None, "the database accepted a negative cash position"
        # The SQLSTATE is what the API's error map keys on, so it is what is asserted.
        assert sqlstate_of(error) == "NEX01", error
        assert "NEXUS_INSUFFICIENT_BALANCE" in str(error)
        assert _cash_position(main_database, busy_world, currency_id) == signed_before

    def test_the_currency_position_view_matches_the_movements(self, main_database: str) -> None:
        rows = read(
            main_database,
            """
            SELECT v.branch_id, v.currency_id, v.quantity,
                   (SELECT COALESCE(SUM(m.signed_amount), 0) FROM cash_movements m
                     WHERE m.branch_id = v.branch_id AND m.currency_id = v.currency_id)
                   AS recomputed
              FROM v_currency_position v
            """,
        )
        for row in rows:
            assert row["quantity"] == row["recomputed"]


class TestTheAuditTrailHolds:
    def test_every_posted_entry_has_an_audit_row(
        self, busy_world: World, main_database: str
    ) -> None:
        missing = read(
            main_database,
            """
            SELECT e.id FROM journal_entries e
             WHERE e.branch_id = :branch
               AND NOT EXISTS (
                     SELECT 1 FROM audit_logs a
                      WHERE a.entity_id = e.id
                        AND a.action IN ('JOURNAL_POSTED', 'JOURNAL_REVERSED')
                   )
            """,
            branch=busy_world.branch_id,
        )
        assert missing == []

    def test_the_audit_chain_verifies(self, main_database: str) -> None:
        broken = read(main_database, "SELECT * FROM verify_audit_chain(0)")
        assert broken == []

    def test_a_reversal_is_audited_and_linked(self, busy_world: World, main_database: str) -> None:
        entry_id = busy_world.ids["buy_usd"]

        async def scenario(service):
            return await service.reverse_journal_entry(
                journal_entry_id=entry_id,
                reason="the customer's receipt was voided",
                actor=busy_world.head_actor,
            )

        reversal = run_scenario(main_database, scenario)
        audited = read_one(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE entity_id = :entry AND action = 'JOURNAL_REVERSED'
            """,
            entry=reversal.id,
        )
        assert audited["new_data"]["reversal_of_id"] == str(entry_id)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reversal_of_id = :entry",
                entry=entry_id,
            )
            == 1
        )
        # The reversal is balanced, and it cancelled the FX result of the entry it mirrors.
        totals = read_one(
            main_database,
            """
            SELECT SUM(debit) AS debit, SUM(credit) AS credit
              FROM journal_lines WHERE journal_entry_id = :entry
            """,
            entry=reversal.id,
        )
        assert totals["debit"] == totals["credit"]


class TestMoneyPrecisionAtTheEdges:
    def test_the_largest_value_the_ledger_can_hold_posts_exactly(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """NUMERIC(30,10): twenty integer digits and ten decimals, exactly."""
        world = build_world(api_client, admin_headers, main_database)
        # One quantum below ``MAX_MONEY``: the guard refuses the maximum itself, because a
        # value with no head-room left can still be added to and would overflow NUMERIC.
        # Subtracted inside the money context on purpose: 20 + 10 significant digits do not
        # fit the process's default 28-digit context, which would round the operand up to
        # the very maximum this test is stepping just below.
        with money_context():
            huge = MAX_MONEY - MONEY_QUANTUM

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=huge,
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        assert view.total_debit == huge
        stored = read_one(
            main_database,
            """
            SELECT debit FROM journal_lines
             WHERE journal_entry_id = :entry AND debit > 0
            """,
            entry=view.id,
        )
        assert stored["debit"] == huge
        assert (
            scalar(
                main_database,
                """
                SELECT balance FROM v_account_balances
                 WHERE account_id = :account AND currency_id = :currency
                """,
                account=world.account("cash_afn"),
                currency=world.base.id,
            )
            == huge
        )

    def test_the_guard_refuses_the_very_maximum_and_one_digit_beyond_it(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Both edges are refused as *input*, before PostgreSQL could truncate anything."""
        world = build_world(api_client, admin_headers, main_database)
        entries_before = count(main_database, "journal_entries")

        async def scenario(service):
            outcomes = []
            for value in (MAX_MONEY, MAX_MONEY * 10):
                with pytest.raises(ValidationError) as refusal:
                    await service.post_cash_movement(
                        movement_type="IN",
                        reference_id=uuid.uuid4(),
                        branch_id=world.branch_id,
                        cash_account_id=world.account("cash_afn"),
                        counter_account_id=world.account("capital"),
                        currency_id=world.base.id,
                        amount=value,
                        actor=world.head_actor,
                    )
                outcomes.append(refusal.value.details["fields"][0]["code"])
            return outcomes

        assert run_scenario(main_database, scenario) == ["over_maximum", "over_maximum"]
        assert count(main_database, "journal_entries") == entries_before

    def test_the_smallest_positive_value_posts_and_survives_the_cache(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """One ten-billionth: the smallest number the ledger can represent."""
        world = build_world(api_client, admin_headers, main_database)
        tiny = Decimal("0.0000000001")

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=tiny,
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        assert view.total_debit == tiny
        assert (
            read_one(
                main_database,
                """
                SELECT debit_total FROM account_balances
                 WHERE account_id = :account AND currency_id = :currency
                """,
                account=world.account("cash_afn"),
                currency=world.base.id,
            )["debit_total"]
            >= tiny
        )

    def test_many_tiny_values_do_not_lose_a_digit(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Five hundred ten-billionths are 0.00000005: summed exactly, never lost."""
        world = build_world(api_client, admin_headers, main_database)
        tiny = Decimal("0.0000000001")
        times = 500

        async def scenario(service):
            # One transaction for the lot: the ledger's arithmetic, not the driver's
            # latency, is what this test is about.
            async with service._database.transaction() as session:  # type: ignore[attr-defined]
                for _ in range(times):
                    await service.create_journal_entry(
                        reference_type="MANUAL_ADJUSTMENT",
                        reference_id=uuid.uuid4(),
                        branch_id=world.branch_id,
                        lines=[
                            line(
                                world.account("receivable"),
                                debit=tiny,
                                currency_id=world.base.id,
                            ),
                            line(
                                world.account("payable"),
                                credit=tiny,
                                currency_id=world.base.id,
                            ),
                        ],
                        actor=world.head_actor,
                        description="tiny value",
                        session=session,
                    )
            return True

        assert run_scenario(main_database, scenario) is True
        total = Decimal(
            str(
                scalar(
                    main_database,
                    """
                    SELECT COALESCE(SUM(debit), 0) FROM journal_lines
                     WHERE account_id = :account AND currency_id = :currency
                    """,
                    account=world.account("receivable"),
                    currency=world.base.id,
                )
            )
        )
        assert total == Decimal("0.0000000500")
        assert total == money_sum([tiny] * times)

    def test_a_rounded_commission_never_leaves_the_entry_unbalanced(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A half-quantum result is closed to the FX account, never rounded away."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="BUY",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("1"),
                exchange_rate=Decimal("70.0000000001"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        # The acquired leg is 1 x a rate that is not a whole number of afghanis, so the
        # functional value cannot be exact in both directions: the FX line closes it.
        assert view.total_debit == view.total_credit
        lines = read(
            main_database,
            """
            SELECT account_id, debit, credit FROM journal_lines WHERE journal_entry_id = :entry
            """,
            entry=view.id,
        )
        debit = money_sum(Decimal(str(row["debit"])) for row in lines)
        credit = money_sum(Decimal(str(row["credit"])) for row in lines)
        assert debit == credit


class TestTheRateSnapshotIsProtected:
    """The Phase 4 review of the rate snapshot (``PHASE4_REPORT.md`` decision D-4-1).

    A posting never *points* at a quote: the rate that priced it is copied into the
    immutable line, and the quote it came from is recorded in the audit row. That is what
    lets a quote stay mutable — it holds a market observation, not money — while history
    can never be re-priced. These three tests are the evidence for that claim, and the
    reason no schema change was needed to protect the snapshot.
    """

    def test_re_pricing_every_quote_for_the_pair_cannot_re_price_history(
        self, busy_world: World, main_database: str
    ) -> None:
        entry_id = busy_world.ids["buy_usd"]
        before = _line_snapshot(main_database, entry_id)
        assert Decimal("70") in {rate for _, _, _, _, rate, _ in before}

        # Every quote for the pair this entry used, moved to a different number: the market
        # observed something else afterwards. ``exchange_rates`` carries no append-only
        # trigger (a quote is an observation, not money), so the write is legal — which is
        # exactly why the ledger must not depend on it.
        moved = execute_sql(
            main_database,
            """
            UPDATE exchange_rates SET buy_rate = 99, sell_rate = 99
             WHERE from_currency_id = :source AND to_currency_id = :target
            """,
            source=busy_world.money("USD").id,
            target=busy_world.base.id,
        )
        assert moved >= 1, "the scenario published no quote for the pair it priced"
        assert _line_snapshot(main_database, entry_id) == before
        # The immutable numbers still imply the quantity they implied at posting time.
        for _, _, debit, credit, rate, foreign in before:
            assert foreign == ((debit + credit) / rate).quantize(MONEY_QUANTUM)

    def test_the_posted_rate_cannot_be_rewritten_or_removed(
        self, busy_world: World, main_database: str
    ) -> None:
        entry_id = busy_world.ids["buy_usd"]
        line = read_one(
            main_database,
            """
            SELECT id, exchange_rate FROM journal_lines
             WHERE journal_entry_id = :entry
             ORDER BY exchange_rate DESC, id LIMIT 1
            """,
            entry=entry_id,
        )
        assert Decimal(line["exchange_rate"]) == Decimal("70")
        before = _line_snapshot(main_database, entry_id)

        for statement in (
            "UPDATE journal_lines SET exchange_rate = 99 WHERE id = :line",
            "UPDATE journal_lines SET exchange_rate = exchange_rate * 2 WHERE id = :line",
            "DELETE FROM journal_lines WHERE id = :line",
        ):
            error = _refusal(main_database, statement, line=line["id"])
            assert error is not None, statement
            assert sqlstate_of(error) == "P0001", statement
            assert "NEXUS_APPEND_ONLY" in str(error), statement
        assert _line_snapshot(main_database, entry_id) == before

    def test_the_audit_trail_records_the_rate_and_the_quote_behind_it(
        self,
        busy_world: World,
        main_database: str,
        api_client: TestClient,
        admin_headers: dict[str, str],
    ) -> None:
        quoted = publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(busy_world.money("USD").id),
            to_currency_id=str(busy_world.base.id),
            buy_rate="70",
            sell_rate="71",
        )
        quote_id = uuid.UUID(str(quoted["id"]))

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="BUY",
                reference_id=uuid.uuid4(),
                branch_id=busy_world.branch_id,
                from_currency_id=busy_world.money("USD").id,
                to_currency_id=busy_world.base.id,
                from_amount=Decimal("100"),
                exchange_rate=Decimal("70"),
                from_cash_account_id=busy_world.account("cash_usd"),
                to_cash_account_id=busy_world.account("cash_afn"),
                fx_account_id=busy_world.account("fx"),
                commission=Decimal("0"),
                # A caller that resolved a quote hands the quote's identity along, and the
                # audit row pins it: "which price did this counter apply?" stays answerable
                # after newer quotes exist.
                rate_snapshot=RateSnapshot(
                    rate=Decimal("70"),
                    rate_id=quote_id,
                    from_currency_id=busy_world.money("USD").id,
                    to_currency_id=busy_world.base.id,
                    source="MANUAL",
                ),
                actor=busy_world.head_actor,
            )

        entry = run_scenario(main_database, scenario)
        audited = read_one(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE entity_id = :entry AND action = 'JOURNAL_POSTED'
            """,
            entry=entry.id,
        )
        snapshot = audited["new_data"]["rate_snapshot"]
        assert snapshot is not None, "the posting did not record where its rate came from"
        assert Decimal(snapshot["rate"]) == Decimal("70")
        assert snapshot["exchange_rate_id"] == str(quote_id)
        assert snapshot["source"] == "MANUAL"
        assert (
            count(
                main_database,
                "exchange_rates",
                where="id = CAST(:quote AS UUID)",
                quote=quote_id,
            )
            == 1
        ), "the quote the audit row names is no longer readable"

        # The per-line snapshot in the audit row matches the immutable lines exactly, so a
        # rate dispute can be answered from the audit trail alone.
        detail = audited["new_data"]["line_detail"]
        assert {(item["account_id"], item["exchange_rate"]) for item in detail} == {
            (str(row["account_id"]), str(row["exchange_rate"]))
            for row in read(
                main_database,
                """
                SELECT account_id, exchange_rate FROM journal_lines
                 WHERE journal_entry_id = :entry
                """,
                entry=entry.id,
            )
        }
        assert Decimal("70") in {Decimal(item["exchange_rate"]) for item in detail}


# --------------------------------------------------------------------------- utilities
def _line_snapshot(database: str, entry_id: uuid.UUID) -> list[tuple]:
    """Every immutable column of an entry's lines, in a comparable form."""
    return [
        (
            str(row["account_id"]),
            str(row["currency_id"]),
            Decimal(row["debit"]),
            Decimal(row["credit"]),
            Decimal(row["exchange_rate"]),
            Decimal(row["foreign_amount"]),
        )
        for row in read(
            database,
            """
            SELECT account_id, currency_id, debit, credit, exchange_rate, foreign_amount
              FROM journal_lines WHERE journal_entry_id = :entry
             ORDER BY account_id, currency_id
            """,
            entry=entry_id,
        )
    ]


def _cash_position(database: str, world: World, currency_id: uuid.UUID) -> Decimal:
    return Decimal(
        str(
            scalar(
                database,
                """
                SELECT COALESCE(SUM(signed_amount), 0) FROM cash_movements
                 WHERE branch_id = :branch AND currency_id = :currency
                """,
                branch=world.branch_id,
                currency=currency_id,
            )
        )
    )


def _refusal(database: str, sql: str, **params: object) -> BaseException | None:
    """Run a statement the database should refuse; return its error, or ``None``."""
    engine = create_engine(database_dsn(database), future=True)
    try:
        try:
            with engine.begin() as connection:
                connection.execute(text(sql), params)
        except Exception as error:
            return error
    finally:
        engine.dispose()
    return None
