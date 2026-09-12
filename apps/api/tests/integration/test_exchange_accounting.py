"""Phase 5 — the accounting properties of a posted exchange (§7, §8, §9, §10).

These tests do not check that a deal *was accepted*; the posting and lifecycle suites own
that. They check what the books say afterwards, and they check it as a property rather than
as a script: for every deal in a matrix the entry must be balanced, every drawer line must be
valued at exactly one rate, the physical movement must match the document, the drawer's two
independent readings must agree, and the result the ledger recognized must be exactly what the
branch gained on the deal.

The identity every test here either asserts or builds on is ``ACCOUNTING_MODEL.md`` §6's, and
it needs no engine internals to be read out of committed rows:

    result = (functional value the branch received) - (functional value the branch delivered)

The ledger's side of that identity is the net of the FX (``4000``) and commission (``4010``)
accounts. A deal is therefore *reproducible*: given the document row, its movements and its
entry, an auditor computes the same result the engine recognized, without re-pricing anything
(Phase 5 §9).

Nothing here mocks money: every posting goes through :class:`AccountingService` on real
PostgreSQL, and every assertion reads the committed rows back with SQL. The suites share the
database's accumulated history, so every check that could be polluted by what another suite
posted is written as a *delta* measured around this test's own deals, or scoped to the ids
this file created.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.money import money_sum, multiply_money
from app.services.accounting_service import EXCHANGE_TRANSACTION_TYPES, compute_exchange_amounts
from tests.accounting_helpers import financial_state, scalar
from tests.exchange_helpers import (
    BASE_CODE,
    REFERENCE_TYPE_EXCHANGE,
    REFERENCE_TYPE_REVERSAL,
    ExchangeWorld,
)

pytestmark = [pytest.mark.integration, pytest.mark.exchange]

# The chart's vocabulary as the accounting model fixes it (seed 003).
FX_ACCOUNT = "4000"  # FX Gain / Loss
COMMISSION_ACCOUNT = "4010"  # Commission Income - Exchange
INVENTORY_BAND = range(1000, 1100)

# The quotes these tests trade on and the band they stay inside (±50 bps, Phase 3).
USD_BUY, USD_SELL = "70", "71"
EUR_BUY, EUR_SELL = "75", "76"


# ------------------------------------------------------------------------- readers
def as_decimal(value: object) -> Decimal:
    return Decimal(str(value))


def lines_of(world: ExchangeWorld, entry_id: uuid.UUID) -> list[dict[str, object]]:
    return world.entry(entry_id)[1]


def inventory_lines(
    world: ExchangeWorld, lines: list[dict[str, object]]
) -> list[dict[str, object]]:
    """The drawer lines of an entry: the accounts that hold a position (§2).

    Identified by *this world's* drawer accounts rather than by their codes: a scenario's
    drawers carry generated codes, and the question an accounting test asks is whether the
    engine posted to the till it was told to use — not whether a code convention was followed.
    """
    drawers = {str(account) for account in world.drawers.values()}
    return [line for line in lines if str(line["account_id"]) in drawers]


def result_lines(lines: list[dict[str, object]]) -> list[dict[str, object]]:
    """The lines that carry the deal's result: FX gain/loss and commission income."""
    return [line for line in lines if str(line["account_code"]) in {FX_ACCOUNT, COMMISSION_ACCOUNT}]


def recognized(lines: list[dict[str, object]]) -> tuple[Decimal, Decimal]:
    """What the ledger recognized, as ``(fx_result, commission_income)``.

    A credit on a revenue account is income and a debit is a loss — the sign convention the
    Phase 4 rules suite pins — applied here to read a document's result out of its entry.
    """
    fx = Decimal(0)
    commission = Decimal(0)
    for line in result_lines(lines):
        net = as_decimal(line["credit"]) - as_decimal(line["debit"])
        if str(line["account_code"]) == FX_ACCOUNT:
            fx += net
        else:
            commission += net
    return fx, commission


def gained(world: ExchangeWorld, lines: list[dict[str, object]]) -> Decimal:
    """The branch's gain from the *physical* legs alone (§9's identity, left side)."""
    drawers = inventory_lines(world, lines)
    received = money_sum(as_decimal(line["debit"]) for line in drawers if as_decimal(line["debit"]))
    delivered = money_sum(
        as_decimal(line["credit"]) for line in drawers if as_decimal(line["credit"])
    )
    return received - delivered


def entry_of(world: ExchangeWorld, transaction_id: uuid.UUID) -> uuid.UUID:
    row = world.document(transaction_id)
    assert row["journal_entry_id"] is not None, "a completed deal always has an entry"
    return uuid.UUID(str(row["journal_entry_id"]))


def movement_signature(movement: dict[str, object]) -> tuple[str, str, Decimal]:
    return (
        str(movement["movement_type"]),
        str(movement["currency_code"]),
        as_decimal(movement["amount"]),
    )


def positions(
    world: ExchangeWorld, codes: tuple[str, ...] = (BASE_CODE, "USD")
) -> dict[str, tuple[Decimal, Decimal]]:
    """The two independent readings of every position: ``v_cash_position`` and the ledger."""
    return {code: (world.cash(code), world.ledger(code)) for code in codes}


def assert_balanced(world: ExchangeWorld, entry_id: uuid.UUID) -> None:
    """PART 49's first invariant, read from the immutable lines themselves.

    The totals are never stored: an entry's balance is a fact about its lines, and asserting
    it from the lines is the only assertion that could catch a wrong total a denormalized
    column would happily repeat.
    """
    entry, lines = world.entry(entry_id)
    debit = money_sum(as_decimal(line["debit"]) for line in lines)
    credit = money_sum(as_decimal(line["credit"]) for line in lines)
    assert debit == credit, f"entry {entry['reference_type']} does not balance: {debit} != {credit}"
    assert debit > 0


# --------------------------------------------------------------------------- tests
def test_every_deal_of_the_matrix_posts_one_balanced_entry_and_matching_movements(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The posting identity of §10, asserted per deal across directions and commissions."""
    world = quoted_counter
    world.quote(api_client, admin_headers, from_code="EUR", buy_rate=EUR_BUY, sell_rate=EUR_SELL)
    before = world.state()
    before_positions = positions(world, (BASE_CODE, "USD", "EUR"))
    deals = [
        ("BUY", "USD", "1000", USD_BUY, "500"),
        ("SELL", "USD", "500", USD_SELL, "200"),
        ("BUY", "USD", "250", USD_BUY, "0"),
        ("BUY", "EUR", "100", EUR_BUY, "100"),
        ("SELL", "EUR", "50", EUR_SELL, "0"),
    ]
    for transaction_type, code, amount, rate, commission in deals:
        result = world.create(
            transaction_type=transaction_type,
            from_code=code,
            from_amount=amount,
            exchange_rate=rate,
            commission=commission,
        )
        document = world.document(result.transaction_id)
        entry_id = entry_of(world, result.transaction_id)
        entry, lines = world.entry(entry_id)

        # One entry, bound to this document, in this branch, for this deal.
        assert str(entry["reference_type"]) == REFERENCE_TYPE_EXCHANGE
        assert uuid.UUID(str(entry["reference_id"])) == result.transaction_id
        assert uuid.UUID(str(entry["branch_id"])) == world.branch_id
        assert_balanced(world, entry_id)

        # Every drawer line is valued exactly once, at the rate stored on the line itself:
        # this is what "never mix rates inside one transaction" (§6) looks like in the books.
        for line in inventory_lines(world, lines):
            expected = multiply_money(
                as_decimal(line["foreign_amount"]), as_decimal(line["exchange_rate"])
            )
            moved = as_decimal(line["debit"]) or as_decimal(line["credit"])
            assert moved == expected, f"{line['account_code']} was valued inconsistently"

        # The physical side: one movement in, one out, in the document's own quantities.
        movements = world.movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=result.transaction_id
        )
        assert len(movements) == 2
        expected_signatures = sorted(
            [
                ("IN" if transaction_type == "BUY" else "OUT", code, Decimal(amount)),
                (
                    "OUT" if transaction_type == "BUY" else "IN",
                    BASE_CODE,
                    as_decimal(document["to_amount"]),
                ),
            ]
        )
        assert sorted(movement_signature(item) for item in movements) == expected_signatures

    # The drawer and the ledger moved by the same amount — two readings of one fact.
    after = world.state()
    after_positions = positions(world, (BASE_CODE, "USD", "EUR"))
    assert after.balanced
    assert (after.documents - before.documents, after.entries - before.entries) == (5, 5)
    assert after.movements - before.movements == 10
    for code in before_positions:
        cash_before, ledger_before = before_positions[code]
        cash_after, ledger_after = after_positions[code]
        assert cash_after - cash_before == ledger_after - ledger_before


def test_the_result_of_a_deal_is_what_the_branch_gained_on_it(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§9: a fee-only deal, a deal with margin and fee, and a deal with neither."""
    world = quoted_counter

    # A BUY keeps the fee out of the payout: the gain is the commission, exactly.
    buy = world.create(
        transaction_type="BUY",
        from_code="USD",
        from_amount="1000",
        exchange_rate=USD_BUY,
        commission="500",
    )
    buy_lines = lines_of(world, entry_of(world, buy.transaction_id))
    assert recognized(buy_lines) == (Decimal("0"), Decimal("500"))
    assert gained(world, buy_lines) == Decimal("500")

    # A SELL at 71 against a carrying rate of 70 recognizes both parts of the gain.
    sell = world.create(
        transaction_type="SELL",
        from_code="USD",
        from_amount="500",
        exchange_rate=USD_SELL,
        commission="200",
    )
    sell_lines = lines_of(world, entry_of(world, sell.transaction_id))
    assert recognized(sell_lines) == (Decimal("300"), Decimal("200"))
    assert gained(world, sell_lines) == Decimal("500")

    # Sold at what it was carried at: nothing is realized but the commission, and with no
    # commission there is no result at all — the ledger invents no gain to balance itself.
    world.quote(api_client, admin_headers, from_code="EUR", buy_rate=EUR_BUY, sell_rate=EUR_BUY)
    flat = world.create(
        transaction_type="SELL",
        from_code="EUR",
        from_amount="100",
        exchange_rate=EUR_BUY,
        commission="0",
    )
    flat_lines = lines_of(world, entry_of(world, flat.transaction_id))
    assert recognized(flat_lines) == (Decimal("0"), Decimal("0"))
    assert gained(world, flat_lines) == Decimal("0")
    assert result_lines(flat_lines) == []


def test_the_ledgers_result_equals_the_branchs_gain_for_every_deal_in_the_sweep(
    quoted_counter: ExchangeWorld,
) -> None:
    """The two sides of §9's identity, per deal and in total, over a deterministic sweep.

    Every combination the brief names is exercised: both directions, fees at zero and above, a
    rate above the carrying rate and one equal to it, and the drawing down of a foreign drawer.
    """
    world = quoted_counter
    sweep = [
        ("BUY", "USD", "120", "70", "4"),
        ("BUY", "USD", "300", "70", "0"),
        ("BUY", "USD", "75", "69.9", "1.50"),
        ("SELL", "USD", "200", "71", "10"),
        ("SELL", "USD", "40", "71.2", "0"),
        ("SELL", "USD", "100", "70.7", "2.50"),
    ]
    totals = {"result": Decimal(0), "commission": Decimal(0), "gain": Decimal(0)}
    for transaction_type, code, amount, rate, commission in sweep:
        result = world.create(
            transaction_type=transaction_type,
            from_code=code,
            from_amount=amount,
            exchange_rate=rate,
            commission=commission,
        )
        lines = lines_of(world, entry_of(world, result.transaction_id))
        fx, fee = recognized(lines)
        gain = gained(world, lines)
        assert fx + fee == gain, f"{transaction_type} {amount}@{rate} recognized {fx}+{fee}"
        # The fee the customer was charged is commission income, never principal.
        assert fee == Decimal(commission)
        # The arithmetic the document published is the arithmetic that was posted.
        computation = compute_exchange_amounts(
            transaction_type=transaction_type,
            from_amount=Decimal(amount),
            exchange_rate=Decimal(rate),
            commission=Decimal(commission),
            from_decimal_places=2,
            to_decimal_places=2,
        )
        assert as_decimal(world.document(result.transaction_id)["to_amount"]) == (
            computation.settlement_amount
        )
        totals["result"] += fx + fee
        totals["commission"] += fee
        totals["gain"] += gain

    assert totals["result"] == totals["gain"]
    assert totals["commission"] == money_sum(Decimal(entry[4]) for entry in sweep)
    assert totals["gain"] != Decimal(0)  # a sweep that recognized nothing proves nothing
    assert world.state().balanced


def test_the_engine_posts_only_to_drawers_and_the_modeled_result_accounts(
    quoted_counter: ExchangeWorld,
) -> None:
    """§2/§10: no parallel ledger and no invented account — the engine uses the chart."""
    world = quoted_counter
    created = [
        world.create(
            transaction_type="BUY",
            from_code="USD",
            from_amount="100",
            exchange_rate=USD_BUY,
            commission="10",
        ),
        world.create(
            transaction_type="SELL",
            from_code="USD",
            from_amount="50",
            exchange_rate=USD_SELL,
            commission="5",
        ),
    ]
    allowed = {str(account) for account in world.drawers.values()}
    for result in created:
        _, lines = world.entry(entry_of(world, result.transaction_id))
        for line in lines:
            code = str(line["account_code"])
            if code in {FX_ACCOUNT, COMMISSION_ACCOUNT}:
                assert line["account_type"] == "REVENUE"
                continue
            assert str(line["account_id"]) in allowed, f"unexpected posting account {code}"
            assert line["account_type"] == "ASSET"

    # One entry per document and one document per entry: the link is total in both directions.
    entry_ids = [entry_of(world, result.transaction_id) for result in created]
    document_ids = [result.transaction_id for result in created]
    assert len(set(entry_ids)) == len(entry_ids)
    report = world.list(limit=200)
    documents = [view for view in report[0] if view.id in set(document_ids)]
    assert len(documents) == 2
    assert all(view.journal_entry_id is not None for view in documents)
    for entry_id, document_id in zip(entry_ids, document_ids, strict=True):
        entry, _ = world.entry(entry_id)
        assert uuid.UUID(str(entry["reference_id"])) == document_id


def test_a_cancellation_leaves_the_positions_exactly_as_they_were(
    quoted_counter: ExchangeWorld,
) -> None:
    """§13/§22: an undone deal moves the money back — the ledger keeps no net effect."""
    world = quoted_counter
    accounts = [*world.drawers.values(), world.account("fx"), world.account("commission")]
    before_state = world.state()
    before_positions = positions(world, (BASE_CODE, "USD"))
    before_financial = financial_state(world.database, accounts)

    result = world.create(
        transaction_type="BUY",
        from_code="USD",
        from_amount="200",
        exchange_rate=USD_BUY,
        commission="30",
    )
    assert positions(world, (BASE_CODE, "USD")) != before_positions
    world.cancel(result.transaction_id, reason="customer changed their mind")

    after_state = world.state()
    assert positions(world, (BASE_CODE, "USD")) == before_positions
    after_financial = financial_state(world.database, accounts)
    assert after_financial.quantities == before_financial.quantities
    assert after_financial.functional == before_financial.functional
    assert after_financial.balanced
    assert (
        after_state.documents - before_state.documents,
        after_state.entries - before_state.entries,
    ) == (1, 2)
    assert after_state.movements - before_state.movements == 4

    # The document records its own undo, and the ledger's mirror entry is bound to the entry
    # it reverses rather than to the document (the frozen model's linkage).
    document = world.document(result.transaction_id)
    assert str(document["status"]) == "CANCELLED"
    reversal_id = uuid.UUID(str(document["reversal_journal_entry_id"]))
    entry, _ = world.entry(reversal_id)
    assert str(entry["reference_type"]) == REFERENCE_TYPE_REVERSAL
    assert uuid.UUID(str(entry["reference_id"])) == entry_of(world, result.transaction_id)
    assert_balanced(world, reversal_id)
    # The deal and its undo recognize nothing between them: income is not a place to park.
    assert recognized(
        lines_of(world, entry_of(world, result.transaction_id)) + lines_of(world, reversal_id)
    ) == (Decimal("0"), Decimal("0"))


def test_a_reversal_returns_the_money_and_leaves_both_documents_visible(
    quoted_counter: ExchangeWorld,
) -> None:
    """§13: the original stays, the mirror documents the return, the positions come back."""
    world = quoted_counter
    accounts = [*world.drawers.values(), world.account("fx"), world.account("commission")]
    before_positions = positions(world, (BASE_CODE, "USD"))
    before_financial = financial_state(world.database, accounts)

    result = world.create(
        transaction_type="SELL",
        from_code="USD",
        from_amount="300",
        exchange_rate=USD_SELL,
        commission="40",
    )
    outcome = world.reverse(result.transaction_id, reason="returned funds")
    # The undo answers with the document that was undone, now carrying its linkage: a client
    # that asked for the undo of X never has to guess which id came back.
    assert outcome.transaction_id == result.transaction_id
    mirror = uuid.UUID(
        str(world.view(result.transaction_id).to_payload()["reversal_transaction_id"])
    )
    assert mirror != result.transaction_id
    # The frozen schema keeps the link on the mirror row, not on the original.
    assert uuid.UUID(str(world.document(mirror)["reversal_of_id"])) == result.transaction_id

    assert positions(world, (BASE_CODE, "USD")) == before_positions
    after_financial = financial_state(world.database, accounts)
    assert after_financial.quantities == before_financial.quantities
    assert after_financial.functional == before_financial.functional

    # Both documents have their own balanced entry, and neither recognizes income alone.
    original_entry = entry_of(world, result.transaction_id)
    mirror_entry = entry_of(world, mirror)
    assert_balanced(world, original_entry)
    assert_balanced(world, mirror_entry)
    assert recognized(lines_of(world, original_entry) + lines_of(world, mirror_entry)) == (
        Decimal("0"),
        Decimal("0"),
    )
    # The mirror's movements are the deal's, reversed: the same money went back the other way.
    original_moves = [
        movement_signature(item)
        for item in world.movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=result.transaction_id
        )
    ]
    mirror_moves = [
        movement_signature(item)
        for item in world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=mirror)
    ]
    assert mirror_moves, "the mirror document moved money"
    for movement_type, code, amount in original_moves:
        opposite = "OUT" if movement_type == "IN" else "IN"
        assert (opposite, code, amount) in mirror_moves


def test_the_drawer_the_ledger_and_the_movements_tell_one_story(
    quoted_counter: ExchangeWorld,
) -> None:
    """The closing property: physical cash, ledger position and movements never disagree."""
    world = quoted_counter
    before_positions = positions(world, (BASE_CODE, "USD"))
    deals = [
        ("BUY", "USD", "400", USD_BUY, "20"),
        ("SELL", "USD", "150", USD_SELL, "15"),
        ("BUY", "USD", "60", USD_BUY, "0"),
        ("SELL", "USD", "90", USD_SELL, "0"),
    ]
    created = [
        world.create(
            transaction_type=transaction_type,
            from_code=code,
            from_amount=amount,
            exchange_rate=rate,
            commission=commission,
        )
        for transaction_type, code, amount, rate, commission in deals
    ]
    world.cancel(created[0].transaction_id, reason="wrong customer")
    world.cancel(created[1].transaction_id, reason="wrong amount")
    world.reverse(created[2].transaction_id, reason="customer returned")

    # The undos net out, so the branch's position moved by exactly the fourth deal's effect.
    standing = world.document(created[3].transaction_id)
    assert str(standing["status"]) == "COMPLETED"
    expected = {
        BASE_CODE: as_decimal(standing["to_amount"]),
        "USD": -as_decimal(standing["from_amount"]),
    }
    after_positions = positions(world, (BASE_CODE, "USD"))
    for code in expected:
        cash_before, ledger_before = before_positions[code]
        cash_after, ledger_after = after_positions[code]
        assert cash_after - cash_before == expected[code]
        assert ledger_after - ledger_before == expected[code]

    # Every movement this branch wrote for those four deals is the document's own quantity:
    # a document's movements never sum to zero (they are two currencies), so the property is
    # stated per currency — one leg in, one leg out, each of them the amount on the paper.
    for result, (transaction_type, code, amount, _rate, _commission) in zip(
        created, deals, strict=True
    ):
        movements = world.movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=result.transaction_id
        )
        by_currency = {
            str(item["currency_code"]): (
                str(item["movement_type"]),
                as_decimal(item["amount"]),
            )
            for item in movements
        }
        expected_delivered = as_decimal(world.document(result.transaction_id)["to_amount"])
        if transaction_type == "BUY":
            assert by_currency[code] == ("IN", Decimal(amount))
            assert by_currency[BASE_CODE] == ("OUT", expected_delivered)
        else:
            assert by_currency[code] == ("OUT", Decimal(amount))
            assert by_currency[BASE_CODE] == ("IN", expected_delivered)
    # ...and the books are balanced with every entry of the branch still present.
    totals_row = world.state()
    assert totals_row.balanced


def test_a_later_quote_cannot_re_price_a_posted_deal(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§6: the document, its entry and its receipt are facts; a quote is an observation."""
    world = quoted_counter
    result = world.create(
        transaction_type="BUY",
        from_code="USD",
        from_amount="300",
        exchange_rate=USD_BUY,
        commission="15",
    )
    entry_id = entry_of(world, result.transaction_id)
    before_row = world.document(result.transaction_id)
    before_receipt = world.receipt(result.transaction_id)
    before_lines = lines_of(world, entry_id)
    before_payload = world.view(result.transaction_id).to_payload()

    # The market moves: Phase 3 books are append-only, so a new quote is the only way a rate
    # can change, and it must not reach backwards into what was already posted.
    world.quote(api_client, admin_headers, buy_rate="88", sell_rate="89")

    after_row = world.document(result.transaction_id)
    assert as_decimal(after_row["exchange_rate"]) == as_decimal(before_row["exchange_rate"])
    assert as_decimal(after_row["to_amount"]) == as_decimal(before_row["to_amount"])
    assert world.receipt(result.transaction_id) == before_receipt
    assert lines_of(world, entry_id) == before_lines
    assert world.view(result.transaction_id).to_payload() == before_payload
    # The posted document still carries the vocabulary and the amount it was posted with.
    assert str(after_row["transaction_type"]) in EXCHANGE_TRANSACTION_TYPES
    assert as_decimal(after_row["from_amount"]) == Decimal("300")


def test_no_posted_line_ever_carries_a_negative_quantity(quoted_counter: ExchangeWorld) -> None:
    """§11: a position can be spent down, never below zero — the check an auditor can run."""
    world = quoted_counter
    for _ in range(3):
        world.create(
            transaction_type="SELL",
            from_code="USD",
            from_amount="100",
            exchange_rate=USD_SELL,
            commission="0",
        )
    assert world.cash("USD") >= 0
    assert world.cash(BASE_CODE) >= 0
    assert world.ledger("USD") == world.cash("USD")
    negatives = int(
        scalar(
            world.database,
            """
            SELECT count(*) FROM journal_lines l
              JOIN journal_entries e ON e.id = l.journal_entry_id
             WHERE e.branch_id = :branch AND (l.debit < 0 OR l.credit < 0)
            """,
            branch=world.branch_id,
        )
        or 0
    )
    assert negatives == 0
