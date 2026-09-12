"""Phase 4 — multi-currency accounting: functional conversion, FX result, carrying rate.

The ledger recognises money in the **functional currency** (Phase 0 fixes it to AFN) while
every account also remembers the *quantity* of the currency it holds: ``journal_lines``
stores ``debit``/``credit`` in functional units, ``currency_id`` + ``exchange_rate`` of the
line, and PostgreSQL generates ``foreign_amount = (debit + credit) / exchange_rate``. That
one generated column is what makes a posted entry reproducible for ever: the quantity and
the rate that produced it travel with the row, so no later quote can change what a past day
meant (ACCOUNTING_MODEL §3, §6).

The sign convention for the FX result is derived, not guessed (``4000 FX Gain / Loss`` is a
revenue account, so credit-normal): a gain is a **credit** to 4000, a loss is a **debit**.
Both directions are asserted against a hand-computed entry.

Exchange arithmetic follows §6.2/§6.3: what the business *acquires* enters at its
transaction price; what it *delivers* leaves at the rate that branch's drawer actually
carries, which is read from the immutable ledger (never from a cache and never from today's
quote). Any residual is the realized exchange result and is closed to 4000.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.exceptions import (
    InsufficientBalanceError,
    RateNotFoundError,
    ValidationError,
)
from tests.accounting_helpers import (
    World,
    build_world,
    count,
    fund_drawer,
    ledger_rows,
    line,
    publish_rate,
    read,
    run_scenario,
    scalar,
    unique_code,
)

pytestmark = [pytest.mark.integration, pytest.mark.accounting]

# The house's quotes in these tests: it buys USD at 70 and sells at 71 (AFN per USD).
USD_BUY, USD_SELL = "70", "71"
EUR_BUY, EUR_SELL = "75", "76"


def legs(view: object) -> list[tuple[str, str, Decimal, Decimal, Decimal, Decimal | None]]:
    """``(account, currency, debit, credit, rate, foreign_amount)`` per line, in order."""
    return [
        (
            entry.account_code,
            entry.currency_code or "",
            entry.debit,
            entry.credit,
            entry.exchange_rate,
            entry.foreign_amount,
        )
        for entry in view.lines  # type: ignore[attr-defined]
    ]


async def sell(service: object, world: World, *, amount: str, rate: str, reference_id: uuid.UUID):
    """One SELL of ``amount`` USD at ``rate`` against the branch's AFN drawer."""
    return await service.post_exchange(  # type: ignore[attr-defined]
        transaction_type="SELL",
        reference_id=reference_id,
        branch_id=world.branch_id,
        from_currency_id=world.money("USD").id,
        to_currency_id=world.base.id,
        from_amount=Decimal(amount),
        exchange_rate=Decimal(rate),
        from_cash_account_id=world.account("cash_usd"),
        to_cash_account_id=world.account("cash_afn"),
        fx_account_id=world.account("fx"),
        commission=Decimal("0"),
        actor=world.head_actor,
    )


def open_drawer(
    world: World,
    *,
    currency: str,
    amount: str,
    rate: str,
    counter: str = "capital",
    actor: object | None = None,
    movement_type: str = "OPENING",
) -> object:
    """Post an opening balance in ``currency`` against a functional counter leg."""

    async def scenario(service):
        return await service.post_cash_movement(
            movement_type=movement_type,
            reference_id=uuid.uuid4(),
            branch_id=world.branch_id,
            cash_account_id=world.account(f"cash_{currency.lower()}"),
            counter_account_id=world.account(counter),
            currency_id=world.money(currency).id,
            amount=Decimal(amount),
            exchange_rate=Decimal(rate),
            description=f"{movement_type} {amount} {currency}",
            actor=actor or world.head_actor,
        )

    return scenario


class TestFunctionalCurrencyConversion:
    """Functional amounts are what balance; the currency and rate explain them."""

    def test_a_foreign_opening_carries_its_quantity_and_rate(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§6.1: ``Dr Cash USD 70,000 / Cr 6000 Opening Offset 70,000`` at 70 and 1."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        view = run_scenario(
            main_database, open_drawer(world, currency="USD", amount="1000", rate="70")
        )

        stored = ledger_rows(main_database, view.id)
        by_account = {row["account_code"]: row for row in stored}
        cash = by_account[world.codes["cash_usd"]]
        offset = by_account[world.codes["capital"]]
        assert cash["debit"] == Decimal("70000.0000000000")
        assert cash["exchange_rate"] == Decimal("70.0000000000")
        assert cash["foreign_amount"] == Decimal("1000.0000000000")
        assert cash["currency_code"] == "USD"
        # The counter leg is in the *functional* currency at 1: the entry balances in
        # functional units, and the quantity it "paid" is the functional amount itself.
        assert offset["currency_code"] == "AFN"
        assert offset["credit"] == Decimal("70000.0000000000")
        assert offset["exchange_rate"] == Decimal("1.0000000000")
        assert offset["foreign_amount"] == Decimal("70000.0000000000")
        assert view.total_debit == view.total_credit == Decimal("70000")

    def test_a_functional_currency_posting_is_at_rate_one(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("1234.56"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        for entry in view.lines:
            assert entry.exchange_rate == Decimal(1)
            assert entry.foreign_amount == entry.debit + entry.credit
        assert view.total_debit == Decimal("1234.56")

    def test_a_second_foreign_currency_uses_its_own_quote(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("EUR").id),
            to_currency_id=str(world.base.id),
            buy_rate=EUR_BUY,
            sell_rate=EUR_SELL,
        )

        view = run_scenario(
            main_database, open_drawer(world, currency="EUR", amount="200", rate="75")
        )
        stored = ledger_rows(main_database, view.id)
        cash = next(row for row in stored if row["account_code"] == world.codes["cash_eur"])
        assert cash["debit"] == Decimal("15000.0000000000")
        assert cash["foreign_amount"] == Decimal("200.0000000000")
        assert cash["currency_code"] == "EUR"

    def test_three_currencies_in_one_entry_balance_in_functional_units(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """One entry, three currencies: the proof is still Σdebit = Σcredit.

        The afghanis the entry pays out must exist: the till is opened with exactly the
        10,000 the entry delivers, which is the generic journal door's inventory guard
        (Gate Review hole B) doing its job - before that guard existed, this entry was
        posted against an empty drawer and the AFN position went to -10,000.
        """
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(
            main_database,
            lambda service: service.post_cash_movement(
                movement_type="OPENING",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("10000"),
                exchange_rate=Decimal("1"),
                actor=world.head_actor,
            ),
        )
        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("EUR").id),
            to_currency_id=str(world.base.id),
            buy_rate=EUR_BUY,
            sell_rate=EUR_SELL,
        )

        async def scenario(service):
            return await service.create_journal_entry(
                reference_type="MANUAL_ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                lines=[
                    # 100 USD at 70 = 7,000 AFN of value
                    line(
                        world.account("cash_usd"),
                        debit="7000",
                        currency_id=world.money("USD").id,
                        exchange_rate="70",
                    ),
                    # 40 EUR at 75 = 3,000 AFN of value
                    line(
                        world.account("cash_eur"),
                        debit="3000",
                        currency_id=world.money("EUR").id,
                        exchange_rate="75",
                    ),
                    # ... paid for with afghanis
                    line(
                        world.account("cash_afn"),
                        credit="10000",
                        currency_id=world.base.id,
                    ),
                ],
                actor=world.head_actor,
                description="three currencies, one balanced entry",
            )

        view = run_scenario(main_database, scenario)
        assert view.is_balanced is True
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["cash_usd"]]["foreign_amount"] == Decimal("100.0000000000")
        assert stored[world.codes["cash_usd"]]["currency_code"] == "USD"
        assert stored[world.codes["cash_eur"]]["foreign_amount"] == Decimal("40.0000000000")
        assert stored[world.codes["cash_eur"]]["currency_code"] == "EUR"
        assert stored[world.codes["cash_afn"]]["foreign_amount"] == Decimal("10000.0000000000")
        assert stored[world.codes["cash_afn"]]["currency_code"] == "AFN"
        assert view.total_debit == Decimal("10000")


class TestExchangePosting:
    """§6.2 (acquisition) and §6.3 (disposal), asserted line by line."""

    def test_a_buy_values_what_is_acquired_at_its_transaction_price(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """BUY 1,000 USD at 70 with a 500 AFN commission, per §6.2."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            # The payout leg is real money: the branch buys 1,000 USD with 69,500 AFN, so the
            # drawer has to hold the afghanis the guard reads (§11).
            await fund_drawer(
                service, world, account_key="cash_afn", currency_code="AFN", amount="70000"
            )
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

        view = run_scenario(main_database, scenario)
        stored = ledger_rows(main_database, view.id)
        by_account = {row["account_code"]: row for row in stored}

        acquired = by_account[world.codes["cash_usd"]]
        assert acquired["debit"] == Decimal("70000.0000000000")  # 1,000 at 70
        assert acquired["exchange_rate"] == Decimal("70.0000000000")
        assert acquired["foreign_amount"] == Decimal("1000.0000000000")

        paid = by_account[world.codes["cash_afn"]]
        assert paid["credit"] == Decimal("69500.0000000000")  # 70,000 less the 500 commission

        commission = by_account[world.codes["commission"]]
        assert commission["credit"] == Decimal("500.0000000000")
        # No FX line: the legs already balance (§6.2's documented case).
        assert world.codes["fx"] not in by_account
        assert view.total_debit == Decimal("70000")
        assert view.total_credit == Decimal("70000")

    def test_a_sale_at_the_carrying_rate_realizes_only_the_commission(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Sold at what it was carried at: the commission is the whole result."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
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

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["cash_afn"]]["debit"] == Decimal("70000.0000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("70000.0000000000")
        assert stored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("70.0000000000")
        assert stored[world.codes["commission"]]["credit"] == Decimal("500.0000000000")
        # Debits (70,000) are 500 short of credits (70,000 + 500): the commission has to
        # come from somewhere, and that difference is the realized result — a *loss*.
        assert stored[world.codes["fx"]]["debit"] == Decimal("500.0000000000")
        assert view.is_balanced is True

    def test_a_sale_above_the_carrying_rate_realizes_a_gain(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§16, gain direction: the delivered currency was carried at 70 and sold at 72."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("1000"),
                exchange_rate=Decimal("72"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["cash_afn"]]["debit"] == Decimal("72000.0000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("70000.0000000000")
        assert stored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("70.0000000000")
        # 72,000 received less 70,000 delivered = +2,000: a *credit* to the revenue account.
        assert stored[world.codes["fx"]]["credit"] == Decimal("2000.0000000000")
        assert stored[world.codes["fx"]]["debit"] == Decimal("0E-10")
        assert view.is_balanced is True

        # And it lands on the right side of the reported balance.

        async def balance(service):
            return await service.get_account_balance(
                account_id=world.account("fx"), actor=world.head_actor
            )

        reported = run_scenario(main_database, balance)
        assert reported.rows[0].balance == Decimal("2000.0000000000")

    def test_a_sale_below_the_carrying_rate_realizes_a_loss(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§16, loss direction: carried at 70, sold at 68."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("1000"),
                exchange_rate=Decimal("68"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["cash_afn"]]["debit"] == Decimal("68000.0000000000")
        assert stored[world.codes["fx"]]["debit"] == Decimal("2000.0000000000")
        assert stored[world.codes["fx"]]["credit"] == Decimal("0E-10")

        async def balance(service):
            return await service.get_account_balance(
                account_id=world.account("fx"), actor=world.head_actor
            )

        # The account is a revenue (credit-normal) account, so a loss reports as negative.
        reported = run_scenario(main_database, balance)
        assert reported.rows[0].balance == Decimal("-2000.0000000000")

    def test_the_carrying_rate_is_the_weighted_average_of_what_was_actually_bought(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """1,000 USD at 70 and 1,000 USD at 74 are carried at 72, whatever the quote says."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))
        run_scenario(
            main_database,
            open_drawer(world, currency="USD", amount="1000", rate="74", counter="payable"),
        )

        async def sell(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("500"),
                exchange_rate=Decimal("76"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, sell)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        disposed = stored[world.codes["cash_usd"]]
        # 500 of the 2,000 units held: (70,000 + 74,000) / 2,000 = 72 a unit.
        assert disposed["exchange_rate"] == Decimal("72.0000000000")
        assert disposed["credit"] == Decimal("36000.0000000000")
        assert disposed["foreign_amount"] == Decimal("500.0000000000")
        # Received 500 at 76 = 38,000 against 36,000 of carrying value: a +2,000 gain.
        assert stored[world.codes["fx"]]["credit"] == Decimal("2000.0000000000")

    def test_the_position_counts_what_the_deliveries_actually_removed(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A credit line *removes* units: 1,000 held less 700 delivered leaves 300.

        Regression: a quantity that summed both sides of every line reported 1,700 here,
        which priced the next disposal at a fraction of its carrying rate and let the
        branch deliver money it did not have.
        """
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))
        run_scenario(
            main_database,
            lambda service: sell(
                service, world, amount="700", rate="70", reference_id=uuid.uuid4()
            ),
        )

        async def scenario(service):
            with pytest.raises(InsufficientBalanceError) as refusal:
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("400"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["reason"] == "QUANTITY_EXCEEDED"
        assert refusal.details["foreign_quantity"] == "300.0000000000"
        assert refusal.details["shortfall"] == "100.0000000000"

    def test_the_next_disposal_is_priced_from_what_is_left(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Two lots at 70 and 74, sold twice: the average is recomputed each time."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))
        run_scenario(
            main_database,
            open_drawer(world, currency="USD", amount="1000", rate="74", counter="payable"),
        )
        first = run_scenario(
            main_database,
            lambda service: sell(
                service, world, amount="500", rate="76", reference_id=uuid.uuid4()
            ),
        )
        second = run_scenario(
            main_database,
            lambda service: sell(
                service, world, amount="500", rate="76", reference_id=uuid.uuid4()
            ),
        )

        for view in (first, second):
            stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
            # (70,000 + 74,000) / 2,000 = 72 before the first sale; afterwards the
            # remaining 1,500 units still carry 108,000 / 1,500 = 72.
            assert stored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("72.0000000000")
            assert stored[world.codes["cash_usd"]]["foreign_amount"] == Decimal("500.0000000000")
            assert stored[world.codes["fx"]]["credit"] == Decimal("2000.0000000000")

        # 2,000 held less two deliveries of 500 leaves exactly 1,000.
        remaining = read(
            main_database,
            """
            SELECT COALESCE(SUM(foreign_amount) FILTER (WHERE debit > 0), 0)
                 - COALESCE(SUM(foreign_amount) FILTER (WHERE credit > 0), 0) AS quantity
              FROM journal_lines WHERE account_id = :account
            """,
            account=world.account("cash_usd"),
        )[0]["quantity"]
        assert Decimal(str(remaining)) == Decimal("1000.0000000000")

    def test_a_disposal_larger_than_the_position_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """A branch cannot sell currency it does not hold (the physical constraint)."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="100", rate="70"))
        before = count(main_database, "journal_entries")

        async def scenario(service):
            with pytest.raises(InsufficientBalanceError) as refusal:
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("500"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["reason"] == "QUANTITY_EXCEEDED"
        assert refusal.details["foreign_quantity"] == "100.0000000000"
        assert refusal.details["disposing_quantity"] == "500.0000000000"
        assert refusal.details["shortfall"] == "400.0000000000"
        # Nothing was written: the refusal happens before the entry is built.
        assert count(main_database, "journal_entries") == before

    def test_a_sale_from_an_empty_drawer_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            with pytest.raises(InsufficientBalanceError):
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("1"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )

        run_scenario(main_database, scenario)

    def test_a_commission_that_swallows_the_payout_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.post_exchange(
                    transaction_type="BUY",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("100"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=world.account("fx"),
                    commission=Decimal("7000"),
                    commission_account_id=world.account("commission"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"][0]["code"] in {"exceeds_gross", "zero_result"}

    def test_a_zero_amount_or_rate_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            outcomes = []
            for amount, rate in ((Decimal("0"), Decimal("70")), (Decimal("1"), Decimal("0"))):
                with pytest.raises(ValidationError) as refusal:
                    await service.post_exchange(
                        transaction_type="BUY",
                        reference_id=uuid.uuid4(),
                        branch_id=world.branch_id,
                        from_currency_id=world.money("USD").id,
                        to_currency_id=world.base.id,
                        from_amount=amount,
                        exchange_rate=rate,
                        from_cash_account_id=world.account("cash_usd"),
                        to_cash_account_id=world.account("cash_afn"),
                        fx_account_id=world.account("fx"),
                        commission=Decimal("0"),
                        actor=world.head_actor,
                    )
                outcomes.append(refusal.value.details["fields"][0]["code"])
            return outcomes

        assert run_scenario(main_database, scenario) == ["not_positive", "not_positive"]

    def test_an_amount_below_the_currencys_smallest_unit_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """USD has two decimals: 1.001 dollars cannot be paid over a counter."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.post_exchange(
                    transaction_type="BUY",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("1.001"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"][0]["code"] == "below_smallest_unit"
        assert refusal.details["decimal_places"] == 2

    def test_a_result_that_needs_the_fx_account_without_one_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """No silent rounding: an unconfigured FX account refuses the posting."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    from_amount=Decimal("1000"),
                    exchange_rate=Decimal("72"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=world.account("cash_afn"),
                    fx_account_id=None,
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["fields"] == [{"field": "fx_account_id", "code": "required"}]
        assert refusal.details["difference"] == "2000.0000000000"


class TestRateProvenanceAndHistory:
    """A posted entry is reproducible: no later quote may change what it meant."""

    def test_a_quote_published_later_does_not_change_a_posted_entry(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        opening = run_scenario(
            main_database, open_drawer(world, currency="USD", amount="1000", rate="70")
        )
        before = ledger_rows(main_database, opening.id)

        # The market moves: a new quote at 90 is published (books are append-only, so this
        # is the *only* way a rate can change).
        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("USD").id),
            to_currency_id=str(world.base.id),
            buy_rate="90",
            sell_rate="91",
        )

        after = ledger_rows(main_database, opening.id)
        assert before == after
        cash = next(row for row in after if row["account_code"] == world.codes["cash_usd"])
        assert cash["exchange_rate"] == Decimal("70.0000000000")
        assert cash["foreign_amount"] == Decimal("1000.0000000000")

    def test_the_carrying_rate_comes_from_the_ledger_not_from_todays_quote(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """The drawer was filled at 70; today's quote is 90 and must not revalue it."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))
        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("USD").id),
            to_currency_id=str(world.base.id),
            buy_rate="90",
            sell_rate="91",
        )

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("1000"),
                exchange_rate=Decimal("90"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        # The disposal still leaves at 70 (its historical cost), so the result is real.
        assert stored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("70.0000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("70000.0000000000")
        assert stored[world.codes["fx"]]["credit"] == Decimal("20000.0000000000")

    def test_a_back_dated_posting_uses_the_quote_in_force_then(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Phase 0's ``resolve_exchange_rate`` picks by instant, so history stays history."""
        world = build_world(api_client, admin_headers, main_database)
        old_quote = read(
            main_database,
            """
            SELECT buy_rate FROM exchange_rates
             WHERE from_currency_id = :currency
             ORDER BY effective_at DESC LIMIT 1
            """,
            currency=world.money("USD").id,
        )
        assert old_quote  # the scaffold published one

        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("USD").id),
            to_currency_id=str(world.base.id),
            buy_rate="80",
            sell_rate="81",
            effective_at="2030-01-01T00:00:00+00:00",
        )

        # A sale dated *before* the future quote must not use it: it prices off the ledger
        # position (70) and receives at the caller's rate.
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            return await service.post_exchange(
                transaction_type="SELL",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("100"),
                exchange_rate=Decimal("70"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["cash_usd"]]["exchange_rate"] == Decimal("70.0000000000")

    def test_a_currency_without_a_quote_cannot_be_valued(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Receiving a foreign currency needs the house's quote for it: refused without one.

        The **direction** matters here. This test used to hand the service
        ``from_currency_id=base`` and passed because ``RATE_NOT_FOUND`` fired first - a
        missing quote for a deal that could not exist in the first place. Gate Review hole A
        gave the direction its own refusal, and the test now reaches the boundary it meant to
        test: a legitimate SELL of USD, funded to cover the delivery, whose *received*
        currency has no quote in force.
        """
        from tests.accounting_helpers import create_account, create_currency
        from tests.masterdata_helpers import unique_currency_code

        world = build_world(api_client, admin_headers, main_database)
        run_scenario(
            main_database,
            lambda service: service.post_cash_movement(
                movement_type="OPENING",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("capital"),
                currency_id=world.money("USD").id,
                amount=Decimal("1000"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            ),
        )
        exotic = create_currency(api_client, admin_headers, code=unique_currency_code())
        exotic_id = uuid.UUID(exotic["id"])
        exotic_cash = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=unique_code(),
                name="Cash in an unquoted currency",
                account_type="ASSET",
                currency_id=exotic["id"],
            )["id"]
        )

        async def scenario(service):
            with pytest.raises(RateNotFoundError) as refusal:
                await service.post_exchange(
                    transaction_type="SELL",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    from_currency_id=world.money("USD").id,
                    to_currency_id=exotic_id,
                    from_amount=Decimal("100"),
                    exchange_rate=Decimal("70"),
                    from_cash_account_id=world.account("cash_usd"),
                    to_cash_account_id=exotic_cash,
                    fx_account_id=world.account("fx"),
                    commission=Decimal("0"),
                    actor=world.head_actor,
                )
            return refusal.value

        refusal = run_scenario(main_database, scenario)
        assert refusal.details["currency_code"] == exotic["code"]

    def test_a_branch_quote_overrides_the_global_one_for_postings_at_that_branch(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Phase 0's precedence rule, exercised through the *posting* path."""
        from tests.accounting_helpers import (
            create_branch,
            deactivate_branch,
            load_currencies,
            scaffold_chart,
        )

        world = build_world(api_client, admin_headers, main_database, quotes=True)
        publish_rate(
            api_client,
            admin_headers,
            from_currency_id=str(world.money("USD").id),
            to_currency_id=str(world.base.id),
            buy_rate="73",
            sell_rate="74",
        )
        branch = create_branch(api_client, admin_headers)
        try:
            currencies = load_currencies(main_database, ["AFN", "USD"])
            accounts, _ = scaffold_chart(
                api_client, admin_headers, branch_id=uuid.UUID(branch["id"]), currencies=currencies
            )
            publish_rate(
                api_client,
                admin_headers,
                from_currency_id=str(world.money("USD").id),
                to_currency_id=str(world.base.id),
                buy_rate="77",
                sell_rate="78",
                branch_id=branch["id"],
            )

            async def scenario(service):
                return await service.post_cash_movement(
                    movement_type="OPENING",
                    reference_id=uuid.uuid4(),
                    branch_id=uuid.UUID(branch["id"]),
                    cash_account_id=accounts["cash_usd"],
                    counter_account_id=accounts["capital"],
                    currency_id=currencies["USD"].id,
                    amount=Decimal("100"),
                    # The caller's rate is what the counter applied; the *scene* the posting
                    # is valued in comes from the branch quote resolution.
                    exchange_rate=Decimal("77"),
                    actor=world.head_actor,
                )

            view = run_scenario(main_database, scenario)
            stored = ledger_rows(main_database, view.id)
            cash = next(row for row in stored if row["currency_code"] == "USD")
            assert cash["exchange_rate"] == Decimal("77.0000000000")
            assert cash["debit"] == Decimal("7700.0000000000")
            assert {row["currency_code"] for row in stored} == {"USD", "AFN"}
        finally:
            deactivate_branch(api_client, admin_headers, branch["id"])

    def test_the_audit_row_records_the_quote_the_posting_used(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Provenance: the auditor sees which quote a counter applied, not just the number."""
        from app.services.accounting_service import RateSnapshot

        world = build_world(api_client, admin_headers, main_database, quotes=True)
        quote = read(
            main_database,
            """
            SELECT id, buy_rate, sell_rate, branch_id, effective_at, source
              FROM exchange_rates
             WHERE from_currency_id = :currency
             ORDER BY effective_at DESC LIMIT 1
            """,
            currency=world.money("USD").id,
        )[0]

        async def scenario(service):
            await fund_drawer(
                service, world, account_key="cash_afn", currency_code="AFN", amount="7000"
            )
            return await service.post_exchange(
                transaction_type="BUY",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                from_currency_id=world.money("USD").id,
                to_currency_id=world.base.id,
                from_amount=Decimal("100"),
                exchange_rate=Decimal("70"),
                from_cash_account_id=world.account("cash_usd"),
                to_cash_account_id=world.account("cash_afn"),
                fx_account_id=world.account("fx"),
                commission=Decimal("0"),
                rate_snapshot=RateSnapshot(
                    rate=Decimal("70"),
                    rate_id=quote["id"],
                    from_currency_id=world.money("USD").id,
                    to_currency_id=world.base.id,
                    effective_at=quote["effective_at"],
                    source=str(quote["source"]),
                ),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        audited = read(
            main_database,
            """
            SELECT new_data FROM audit_logs
             WHERE entity_id = :entry AND action = 'JOURNAL_POSTED'
            """,
            entry=view.id,
        )[0]["new_data"]
        snapshot = audited["rate_snapshot"]
        assert snapshot["exchange_rate_id"] == str(quote["id"])
        assert snapshot["rate"] == "70.0000000000"
        assert snapshot["source"] == "MANUAL"
        # The ledger line itself keeps the *number*, which is what makes the entry
        # reproducible even if the quote row were ever archived.
        assert ledger_rows(main_database, view.id)[0]["exchange_rate"] in {
            Decimal("70.0000000000"),
            Decimal("1.0000000000"),
        }


class TestCashAndExpenseInForeignCurrency:
    def test_a_foreign_drawer_returns_to_zero_when_the_same_amount_leaves(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """250 USD in, 250 USD out: the position is exactly where it started."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            into = await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("capital"),
                currency_id=world.money("USD").id,
                amount=Decimal("250"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )
            out_of = await service.post_cash_movement(
                movement_type="OUT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("capital"),
                currency_id=world.money("USD").id,
                amount=Decimal("250"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )
            balance = await service.get_account_balance(
                account_id=world.account("cash_usd"), actor=world.head_actor
            )
            return into, out_of, balance

        into, out_of, balance = run_scenario(main_database, scenario)
        assert into.is_balanced and out_of.is_balanced
        assert into.total_debit == out_of.total_debit == Decimal("17500")
        assert balance.rows[0].balance == Decimal("0.0000000000")
        # And the ledger's currency quantity agrees with the physical movement identity.
        position = read(
            main_database,
            """
            SELECT SUM(foreign_amount) FILTER (WHERE debit > 0)
                 - SUM(foreign_amount) FILTER (WHERE credit > 0) AS quantity
              FROM journal_lines WHERE account_id = :account
            """,
            account=world.account("cash_usd"),
        )[0]["quantity"]
        assert Decimal(str(position)) == Decimal("0.0000000000")

    def test_a_counter_account_in_the_functional_currency_is_valued_at_one(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§6.1: the offset leg of a foreign opening is functional, at rate 1."""
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="OPENING",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("cash_afn"),  # an AFN-bound account
                currency_id=world.money("USD").id,
                amount=Decimal("100"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        by_currency = {entry.currency_code: entry for entry in view.lines}
        assert by_currency["USD"].exchange_rate == Decimal("70")
        assert by_currency["AFN"].exchange_rate == Decimal("1")
        assert by_currency["AFN"].credit == Decimal("7000")
        assert view.is_balanced is True

    def test_a_currency_bound_counter_account_mirrors_the_movement_exactly(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """Two accounts of the same currency: identical quantity, identical rate, no FX."""
        from tests.accounting_helpers import create_account, unique_code

        world = build_world(api_client, admin_headers, main_database, quotes=True)
        second_drawer = uuid.UUID(
            create_account(
                api_client,
                admin_headers,
                code=unique_code(),
                name="Cash USD (second drawer)",
                account_type="ASSET",
                currency_id=str(world.money("USD").id),
                branch_id=str(world.branch_id),
            )["id"]
        )

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=second_drawer,
                currency_id=world.money("USD").id,
                amount=Decimal("400"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = ledger_rows(main_database, view.id)
        assert {row["currency_code"] for row in stored} == {"USD"}
        assert {row["exchange_rate"] for row in stored} == {Decimal("70.0000000000")}
        assert {row["foreign_amount"] for row in stored} == {Decimal("400.0000000000")}
        assert view.total_debit == view.total_credit == Decimal("28000")

    def test_a_currency_less_counter_account_records_functional_value(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """§6.1: the offset of a foreign movement is functional, at rate 1.

        ``3000``/``6000`` hold no currency of their own, so their leg states the functional
        amount — not a quantity of dollars the house never paid out.
        """
        world = build_world(api_client, admin_headers, main_database, quotes=True)

        async def scenario(service):
            return await service.post_cash_movement(
                movement_type="IN",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_usd"),
                counter_account_id=world.account("capital"),
                currency_id=world.money("USD").id,
                amount=Decimal("250"),
                exchange_rate=Decimal("70"),
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        capital = stored[world.codes["capital"]]
        assert capital["currency_code"] == "AFN"
        assert capital["exchange_rate"] == Decimal("1.0000000000")
        assert capital["credit"] == Decimal("17500.0000000000")
        assert capital["foreign_amount"] == Decimal("17500.0000000000")
        assert view.total_debit == view.total_credit == Decimal("17500")

    def test_an_expense_in_a_foreign_currency_converts_once(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database, quotes=True)
        run_scenario(main_database, open_drawer(world, currency="USD", amount="1000", rate="70"))

        async def scenario(service):
            return await service.post_expense(
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                expense_account_id=world.account("expense"),
                credit_account_id=world.account("cash_usd"),
                currency_id=world.money("USD").id,
                amount=Decimal("120.50"),
                exchange_rate=Decimal("70"),
                description="Fuel in dollars",
                actor=world.head_actor,
            )

        view = run_scenario(main_database, scenario)
        assert view.total_debit == Decimal("8435")
        stored = {row["account_code"]: row for row in ledger_rows(main_database, view.id)}
        assert stored[world.codes["expense"]]["debit"] == Decimal("8435.0000000000")
        assert stored[world.codes["expense"]]["foreign_amount"] == Decimal("120.5000000000")
        assert stored[world.codes["cash_usd"]]["credit"] == Decimal("8435.0000000000")

    def test_an_adjustment_moves_a_drawer_in_either_direction(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        """``ADJUSTMENT`` is the auditable way to state a count difference (§6.4)."""
        world = build_world(api_client, admin_headers, main_database)

        async def scenario(service):
            up = await service.post_cash_movement(
                movement_type="ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("expense"),  # 5090 Cash Short/Over shape
                currency_id=world.base.id,
                amount=Decimal("25"),
                adjustment_sign=1,
                description="count was higher",
                actor=world.head_actor,
            )
            down = await service.post_cash_movement(
                movement_type="ADJUSTMENT",
                reference_id=uuid.uuid4(),
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("expense"),
                currency_id=world.base.id,
                amount=Decimal("10"),
                adjustment_sign=-1,
                description="count was lower",
                actor=world.head_actor,
            )
            return up, down

        up, down = run_scenario(main_database, scenario)
        upward = {row["account_code"]: row for row in ledger_rows(main_database, up.id)}
        downward = {row["account_code"]: row for row in ledger_rows(main_database, down.id)}
        assert upward[world.codes["cash_afn"]]["debit"] == Decimal("25.0000000000")
        assert upward[world.codes["cash_afn"]]["credit"] == Decimal("0E-10")
        assert upward[world.codes["expense"]]["credit"] == Decimal("25.0000000000")
        assert downward[world.codes["cash_afn"]]["credit"] == Decimal("10.0000000000")
        assert downward[world.codes["expense"]]["debit"] == Decimal("10.0000000000")

    def test_an_adjustment_without_a_sign_is_refused(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)

        async def scenario(service):
            with pytest.raises(ValidationError) as refusal:
                await service.post_cash_movement(
                    movement_type="ADJUSTMENT",
                    reference_id=uuid.uuid4(),
                    branch_id=world.branch_id,
                    cash_account_id=world.account("cash_afn"),
                    counter_account_id=world.account("expense"),
                    currency_id=world.base.id,
                    amount=Decimal("10"),
                    actor=world.head_actor,
                )
            return refusal.value

        assert run_scenario(main_database, scenario).details["fields"][0]["code"] == "required"

    def test_a_cash_movement_records_its_entry_and_its_drawer_balance(
        self, api_client: TestClient, admin_headers: dict[str, str], main_database: str
    ) -> None:
        world = build_world(api_client, admin_headers, main_database)
        reference_id = uuid.uuid4()

        async def scenario(service):
            view = await service.post_cash_movement(
                movement_type="IN",
                reference_id=reference_id,
                branch_id=world.branch_id,
                cash_account_id=world.account("cash_afn"),
                counter_account_id=world.account("capital"),
                currency_id=world.base.id,
                amount=Decimal("900"),
                description="till top-up",
                actor=world.head_actor,
            )
            balance = await service.get_account_balance(
                account_id=world.account("cash_afn"), actor=world.head_actor
            )
            return view, balance

        view, balance = run_scenario(main_database, scenario)
        assert (
            count(
                main_database,
                "journal_entries",
                where="reference_id = :reference_id AND reference_type = 'CASH_MOVEMENT'",
                reference_id=reference_id,
            )
            == 1
        )
        assert balance.rows[0].balance == Decimal("900.0000000000")
        assert view.line_count == 2
        assert (
            scalar(
                main_database,
                "SELECT count(*) FROM journal_lines WHERE journal_entry_id = :entry",
                entry=view.id,
            )
            == 2
        )
