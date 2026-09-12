"""Phase 5 — recording a deal: document, journal, cash movements, number and audit, atomically.

Every test drives :class:`~app.services.exchange_service.ExchangeService` (the orchestration
layer) against the real migrated, seeded database and then reads **the database**: the
document row, the entry the ledger posted, the physical movements the drawers recorded, the
number the counter issued, the audit row that attributes the deal, and the idempotency row a
retry replays. A test that only inspected the service's return value would pass even if the
row were missing.

What the suite pins down here:

* the arithmetic of BUY and SELL, with and without commission, read line by line off the
  ledger (``ACCOUNTING_MODEL.md`` §6.2, §6.3);
* the document's own numbers: applied rate, gross, settlement, and the branch's own drawers;
* direction and catalogue refusals, *before* anything is priced or posted;
* the quote: which side of it a BUY and a SELL spend, the tolerance band, and the snapshot
  that travels into the audit trail;
* the branch's business date and the per-branch document number;
* the offline hooks: replay of a recorded event, and a conflict when the same event carries
  different content;
* idempotency: a replayed key answers with the recorded body, a reused key with different
  content is refused, and a *failed* attempt never becomes a stored answer;
* the rollback rule: a refusal leaves no document, no movement, no entry, no consumed number.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.audit_actions import AuditAction
from app.core.money import money_sum
from app.core.permissions import RoleName
from app.schemas.exchange import ExchangeDocument
from tests.accounting_helpers import create_account, unique_code
from tests.exchange_helpers import (
    REFERENCE_TYPE_EXCHANGE,
    ExchangeWorld,
    account_id_of,
    audit_rows_for_action,
    audit_rows_for_entity,
    build_world,
    count,
    create_customer,
    currency_id_of,
    document_counter,
    error_code,
    error_details,
    exchange_headers,
    number_suffix,
    period_of,
    post_exchange,
    retire,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.exchange]

AFN = "AFN"
USD = "USD"
EUR = "EUR"


# --------------------------------------------------------------------------- fixtures
def buy(world: ExchangeWorld, **fields: object):
    return world.create(transaction_type="BUY", **fields)


def sell(world: ExchangeWorld, **fields: object):
    return world.create(transaction_type="SELL", **fields)


def lines_of(world: ExchangeWorld, result) -> list[dict[str, object]]:
    _entry, lines = world.entry(uuid.UUID(str(result.payload["journal_entry_id"])))
    return lines


def line_for(lines: list[dict[str, object]], account_id: uuid.UUID) -> dict[str, object]:
    matches = [row for row in lines if row["account_id"] == account_id]
    assert len(matches) == 1, f"expected exactly one line for {account_id}: {lines}"
    return matches[0]


# ------------------------------------------------------------------- recording a deal
def test_a_buy_records_document_entry_movements_number_and_audit(
    quoted_counter: ExchangeWorld,
) -> None:
    world = quoted_counter
    result = buy(world, from_amount="1000", exchange_rate="70", commission="500")

    document = ExchangeDocument.model_validate(result.payload)
    assert result.status_code == 201
    assert document.status == "COMPLETED"
    assert document.transaction_type == "BUY"
    assert document.origin == "ONLINE"
    # The document's own numbers: quantity x rate = gross, gross - fee = settlement (§6.2).
    assert document.from_amount == "1000.0000000000"
    assert document.exchange_rate == "70.0000000000"
    assert document.gross_amount == "70000.0000000000"
    assert document.commission == "500.0000000000"
    assert document.to_amount == "69500.0000000000"
    assert document.journal_total_debit == document.journal_total_credit == "70000.0000000000"
    assert document.transaction_number.startswith("NX-")
    assert document.reversal_of_id is None

    # The stored row agrees with the payload, field by field.
    row = world.document(document.id)
    assert row["status"] == "COMPLETED"
    assert row["journal_entry_id"] == uuid.UUID(str(document.journal_entry_id))
    assert row["from_amount"] == Decimal("1000.0000000000")
    assert row["to_amount"] == Decimal("69500.0000000000")
    assert row["exchange_rate"] == Decimal("70.0000000000")
    assert row["commission"] == Decimal("500.0000000000")
    assert row["cashier_id"] == world.head_user_id
    assert row["branch_id"] == world.branch_id

    # The ledger's side: Dr Cash-USD 70,000 (1,000 USD at 70) / Cr Cash-AFN 69,500 /
    # Cr Commission 500 — read from the immutable lines.
    entry, lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    assert entry["reference_type"] == REFERENCE_TYPE_EXCHANGE
    assert entry["reference_id"] == document.id
    assert entry["branch_id"] == world.branch_id
    assert len(lines) == 3
    acquired = line_for(lines, world.drawer(USD))
    paid = line_for(lines, world.drawer(AFN))
    commission = line_for(lines, world.account("commission"))
    assert acquired["debit"] == Decimal("70000.0000000000")
    assert acquired["foreign_amount"] == Decimal("1000.0000000000")
    assert acquired["exchange_rate"] == Decimal("70.0000000000")
    assert paid["credit"] == Decimal("69500.0000000000")
    assert commission["credit"] == Decimal("500.0000000000")
    debit_total = money_sum(Decimal(str(row["debit"])) for row in lines)
    credit_total = money_sum(Decimal(str(row["credit"])) for row in lines)
    assert debit_total == credit_total == Decimal("70000")

    # The physical side: the customer handed over USD, the drawer paid out AFN, and both
    # movements belong to *this branch's own* drawers (the multi-branch rule, model §10).
    movements = world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=document.id)
    assert [(m["movement_type"], m["currency_code"]) for m in movements] == [
        ("IN", USD),
        ("OUT", AFN),
    ]
    assert [m["amount"] for m in movements] == [
        Decimal("1000.0000000000"),
        Decimal("69500.0000000000"),
    ]
    assert {m["account_id"] for m in movements} == {world.drawer(USD), world.drawer(AFN)}
    assert {m["journal_entry_id"] for m in movements} == {entry["id"]}

    # Positions: the physical view and the ledger agree, quantity for quantity.
    assert world.cash(USD) == Decimal("21000")
    assert world.cash(AFN) == Decimal("5000000") - Decimal("69500")
    assert world.ledger(USD) == world.cash(USD)
    assert world.ledger(AFN) == world.cash(AFN)

    # The audit row: who, what, where, at what rate, and which quote it came from.
    rows = world.audit(document.id)
    assert [row["action"] for row in rows] == [AuditAction.EXCHANGE_CREATED]
    audit = rows[0]["new_data"]
    assert audit["transaction_number"] == document.transaction_number
    assert audit["transaction_type"] == "BUY"
    assert audit["branch_id"] == str(world.branch_id)
    assert audit["from_currency"] == USD
    assert audit["to_currency"] == AFN
    assert audit["from_amount"] == "1000.0000000000"
    assert audit["gross_amount"] == "70000.0000000000"
    assert audit["commission"] == "500.0000000000"
    assert audit["to_amount"] == "69500.0000000000"
    assert audit["journal_entry_id"] == str(entry["id"])
    assert audit["rate_snapshot"]["rate"] == "70.0000000000"
    assert audit["rate_snapshot"]["source"] == "RESOLVED"
    assert audit["rate_snapshot"]["branch_id"] == str(world.branch_id)
    assert audit["client_event_id"] is None
    assert rows[0]["user_id"] == world.head_user_id
    assert rows[0]["entity_type"] == "exchange_transaction"


def test_a_sell_delivers_the_foreign_leg_and_recognizes_the_carrying_difference(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """A SELL spends ``sell_rate`` and values the delivered currency at what it cost us.

    The drawer's USD was bought at 70, so delivering 500 of it releases 35,000 of value;
    the customer pays 35,500 (500 x 71) of which 200 is commission, and the remaining 300
    is the realized result of the trade (``ACCOUNTING_MODEL.md`` §6.3, §9).
    """
    world = quoted_counter
    result = sell(world, from_amount="500", exchange_rate="71", commission="200")

    document = ExchangeDocument.model_validate(result.payload)
    assert document.transaction_type == "SELL"
    assert document.from_amount == "500.0000000000"
    assert document.exchange_rate == "71.0000000000"
    # A SELL collects the fee inside the receipt: the settlement *is* the gross (§6.3).
    assert document.gross_amount == "35500.0000000000"
    assert document.to_amount == "35500.0000000000"
    assert document.commission == "200.0000000000"

    _entry, lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    delivered = line_for(lines, world.drawer(USD))
    received = line_for(lines, world.drawer(AFN))
    commission = line_for(lines, world.account("commission"))
    fx = line_for(lines, world.account("fx"))
    assert delivered["credit"] == Decimal("35000.0000000000")  # 500 x carrying 70
    assert delivered["foreign_amount"] == Decimal("500.0000000000")
    assert delivered["exchange_rate"] == Decimal("70.0000000000")
    assert received["debit"] == Decimal("35500.0000000000")
    assert commission["credit"] == Decimal("200.0000000000")
    assert fx["credit"] == Decimal("300.0000000000")  # 35,500 - 35,000 - 200
    assert money_sum(Decimal(str(row["debit"])) for row in lines) == Decimal("35500")
    assert money_sum(Decimal(str(row["credit"])) for row in lines) == Decimal("35500")

    movements = world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=document.id)
    assert [(m["movement_type"], m["currency_code"]) for m in movements] == [
        ("IN", AFN),
        ("OUT", USD),
    ]
    assert world.cash(USD) == Decimal("19500")
    assert world.cash(AFN) == Decimal("5000000") + Decimal("35500")


def test_a_deal_without_commission_has_no_commission_line(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    result = buy(world, from_amount="1000", exchange_rate="70")
    document = ExchangeDocument.model_validate(result.payload)
    assert document.commission == "0.0000000000"
    assert document.to_amount == document.gross_amount == "70000.0000000000"

    _entry, lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    assert len(lines) == 2
    assert {row["account_id"] for row in lines} == {world.drawer(USD), world.drawer(AFN)}


def test_multicurrency_deal_between_two_foreign_currencies(
    funded_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """USD/EUR: the acquired currency enters at the cross rate, the paid one at its carrying.

    EUR has no functional quote of its own here, so the engine must value the *acquired*
    EUR through the deal rate (USD/EUR) applied to the functional quote of USD — the case
    where a naive implementation posts the deal rate as if it were an AFN rate.
    """
    world = funded_counter
    world.quote(
        api_client,
        admin_headers,
        from_code=EUR,
        to_code=AFN,
        buy_rate="75",
        sell_rate="75",
    )
    world.quote(
        api_client, admin_headers, from_code=USD, to_code=EUR, buy_rate="0.95", sell_rate="0.95"
    )
    world.quote(
        api_client, admin_headers, from_code=EUR, to_code=USD, buy_rate="1.05", sell_rate="1.05"
    )
    world.quote(
        api_client, admin_headers, from_code=USD, to_code=AFN, buy_rate="70", sell_rate="71"
    )
    result = buy(
        world,
        from_code=EUR,
        to_code=USD,
        from_amount="100",
        exchange_rate="1.05",
        commission="0",
    )
    document = ExchangeDocument.model_validate(result.payload)
    assert document.from_currency_code == EUR
    assert document.to_currency_code == USD
    assert document.to_amount == "105.0000000000"
    assert document.gross_amount == "105.0000000000"
    assert document.journal_total_debit == document.journal_total_credit == "7455.0000000000"

    # The acquisition rule (§6.2) converted through the *house's* USD/AFN side: paying USD
    # out is the side at which the house sells it, 71 — so 100 EUR enter at 1.05 x 71 =
    # 74.55 (7,455), while the 105 USD leave at the 70 they are carried at (7,350). The 105
    # difference is the realized result of the cross trade, and it is posted, not hidden.
    _entry, lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    acquired = line_for(lines, world.drawer(EUR))
    paid = line_for(lines, world.drawer(USD))
    result_line = line_for(lines, world.account("fx"))
    assert acquired["debit"] == Decimal("7455.0000000000")
    assert acquired["foreign_amount"] == Decimal("100.0000000000")
    assert acquired["exchange_rate"] == Decimal("74.5500000000")  # 1.05 x 71
    assert paid["credit"] == Decimal("7350.0000000000")
    assert paid["foreign_amount"] == Decimal("105.0000000000")
    assert paid["exchange_rate"] == Decimal("70.0000000000")  # the USD drawer's carrying rate
    assert result_line["credit"] == Decimal("105.0000000000")
    assert money_sum(Decimal(str(row["debit"])) for row in lines) == Decimal("7455")
    assert money_sum(Decimal(str(row["credit"])) for row in lines) == Decimal("7455")

    # Both drawers moved in the currency they hold, and the ledger agrees with the drawers.
    assert world.cash(EUR) == Decimal("20100")
    assert world.cash(USD) == Decimal("19895")
    assert world.ledger(EUR) == world.cash(EUR)
    assert world.ledger(USD) == world.cash(USD)


def test_each_branch_keeps_its_own_cash_and_its_own_number(
    quoted_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers,
    main_database: str,
) -> None:
    """Two branches trading the same pair do not share drawers and do not share counters."""
    world = quoted_counter
    from tests.exchange_helpers import build_world, retire

    world = quoted_counter
    other = build_world(api_client, admin_headers, main_database)
    try:
        other.fund_all()
        other.quote(api_client, admin_headers)
        first = buy(world, from_amount="100", exchange_rate="70")
        second = buy(other, from_amount="200", exchange_rate="70")
        again = buy(other, from_amount="300", exchange_rate="70")

        # Numbers come from the frozen per-period counter (Phase 1), so they are strictly
        # increasing and never repeated — across branches as well as within one.
        numbers = [item.payload["transaction_number"] for item in (first, second, again)]
        assert len(set(numbers)) == 3
        suffixes = [number_suffix(item) for item in numbers]
        assert suffixes == sorted(suffixes)
        assert suffixes[2] == suffixes[1] + 1

        # Branch A's trade moved A's drawers only; B's positions are exactly its funding.
        assert world.cash(USD) == Decimal("20100")  # 20,000 + 100
        assert other.cash(USD) == Decimal("20500")  # 20,000 + 200 + 300
        assert world.cash(AFN) == Decimal("5000000") - Decimal("7000")
        assert other.cash(AFN) == Decimal("5000000") - Decimal("35000")
        assert world.ledger(USD) == Decimal("20100")
        assert other.ledger(USD) == Decimal("20500")

        # A document belongs to one branch and is read through that branch's scope.
        document = ExchangeDocument.model_validate(first.payload)
        stored = world.document(document.id)
        assert stored["branch_id"] == world.branch_id
        assert (
            count(
                main_database,
                "exchange_transactions",
                where="branch_id = :branch",
                branch=world.branch_id,
            )
            == 1
        )
    finally:
        retire(api_client, admin_headers, other)


def test_the_document_number_uses_the_branchs_own_day(
    funded_counter: ExchangeWorld, api_client: TestClient, admin_headers, main_database: str
) -> None:
    """A counter in Honolulu is still trading yesterday when UTC has moved on.

    Document numbers roll over on the branch's business date (§18), so the number must carry
    the branch's local day — not the UTC day of the instant that happens to be on the clock.
    """
    now = dt.datetime.now(dt.UTC)
    west = build_world(
        api_client, admin_headers, main_database, timezone="Pacific/Honolulu", drawers=True
    )
    try:
        west.fund(("AFN", "1000000", "1"))
        west.quote(api_client, admin_headers)
        result = west.create(
            transaction_type="BUY", from_amount="10", exchange_rate="70", commission="0"
        )
        local_day = now.astimezone(dt.timezone(dt.timedelta(hours=-10))).date()
        assert local_day != now.date(), "the sandbox clock is not far enough from UTC to test this"
        assert result.payload["transaction_number"].startswith(
            f"NX-{local_day.strftime('%Y%m%d')}-"
        )
    finally:
        retire(api_client, admin_headers, west)


def test_the_receipt_of_a_quoted_deal_is_self_consistent(quoted_counter: ExchangeWorld) -> None:
    """The receipt's settlement list is derived from the stored movements (§20, PART 39)."""
    world = quoted_counter
    result = buy(world, from_amount="1000", exchange_rate="70", commission="500")
    receipt = world.receipt(result.transaction_id)
    assert receipt["transaction_number"] == result.payload["transaction_number"]
    assert receipt["business_date"] == result.payload["created_at"][:10]
    assert receipt["gross_amount"] == "70000.0000000000"
    assert receipt["commission"] == "500.0000000000"
    assert [(item["movement_type"], item["currency_code"]) for item in receipt["settlement"]] == [
        ("IN", USD),
        ("OUT", AFN),
    ]
    assert receipt["settlement"][1]["amount"] == "69500.0000000000"
    assert receipt["settlement"][0]["amount"] == receipt["from_amount"]


# --------------------------------------------------------------- refusing bad documents
def test_direction_refusals_happen_before_anything_is_priced_or_posts(
    quoted_counter: ExchangeWorld,
) -> None:
    """Same currency, and the functional currency as the delivered leg (model §6.2/§6.3)."""
    world = quoted_counter
    before = world.state()
    same = world.try_create(
        transaction_type="BUY",
        from_code=USD,
        to_code=USD,
        from_amount="10",
        exchange_rate="70",
    )
    functional = world.try_create(
        transaction_type="SELL",
        from_code=AFN,
        to_code=USD,
        from_amount="7000",
        exchange_rate="70",
    )
    for outcome, reason in (
        (same, "SAME_CURRENCY"),
        (functional, "FUNCTIONAL_CURRENCY_NOT_DELIVERABLE"),
    ):
        assert error_code_of(outcome) == "EXCHANGE_DIRECTION_INVALID"
        assert error_details_of(outcome)["reason"] == reason
    assert world.state() == before


def test_an_unknown_pair_is_not_priced_from_the_inverse_quote(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """A missing pair is ``RATE_NOT_FOUND``, never the inverse of another quote (§6)."""
    world = quoted_counter
    world.quote(
        api_client, admin_headers, from_code=EUR, to_code=USD, buy_rate="1.05", sell_rate="1.05"
    )
    before = world.state()
    outcome = world.try_create(
        transaction_type="BUY", from_code=USD, to_code=EUR, from_amount="100", exchange_rate="1.05"
    )
    assert error_code_of(outcome) == "RATE_NOT_FOUND"
    assert world.state() == before


def test_a_rate_outside_the_tolerance_band_is_refused(quoted_counter: ExchangeWorld) -> None:
    """The band is 50 bps of the published side: 70 +/- 0.35 for a BUY at 70."""
    world = quoted_counter
    before = world.state()
    inside = world.try_create(
        transaction_type="BUY", from_amount="100", exchange_rate="70.34", commission="0"
    )
    outside = world.try_create(
        transaction_type="BUY", from_amount="100", exchange_rate="70.36", commission="0"
    )
    assert not isinstance(inside, BaseException), inside
    assert error_code_of(outside) == "RATE_OUT_OF_TOLERANCE"
    details = error_details_of(outside)
    assert details["published_rate"] == "70.0000000000"
    assert details["tolerance_bps"] == 50
    # The refused call left nothing behind; the accepted one priced at its own rate.
    assert world.document(inside.transaction_id)["exchange_rate"] == Decimal("70.3400000000")
    assert count(world.database, "exchange_transactions") >= 1
    assert before.documents + 1 == world.state().documents


def test_a_sell_spends_the_sell_side_of_the_quote(quoted_counter: ExchangeWorld) -> None:
    """``sell_rate`` is what a SELL is priced from; the buy side must not be reachable."""
    world = quoted_counter
    before = world.state()
    outcome = world.try_create(
        transaction_type="SELL", from_amount="10", exchange_rate="70", commission="0"
    )
    assert error_code_of(outcome) == "RATE_OUT_OF_TOLERANCE"
    assert error_details_of(outcome)["published_side"] == "sell_rate"
    assert error_details_of(outcome)["published_rate"] == "71.0000000000"
    accepted = sell(world, from_amount="10", exchange_rate="71", commission="0")
    assert world.document(accepted.transaction_id)["exchange_rate"] == Decimal("71.0000000000")
    assert world.state().documents == before.documents + 1


def test_an_unknown_currency_customer_or_device_is_not_found(
    quoted_counter: ExchangeWorld, main_database: str
) -> None:
    world = quoted_counter
    before = world.state()
    request = world.request(from_amount="10")
    unknown = world.try_create(
        transaction_type="BUY",
        from_amount="10",
        customer_id=uuid.uuid4(),
    )
    assert error_code_of(unknown) == "RESOURCE_NOT_FOUND"
    assert error_details_of(unknown)["fields"] == [{"field": "customer_id", "code": "not_found"}]
    assert world.state() == before
    assert request.from_amount == Decimal("10")


def test_inactive_currency_and_customer_are_refused_before_posting(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """A deactivated catalogue entry cannot be traded, and a parked customer cannot deal."""
    world = quoted_counter
    customer = create_customer(
        api_client, admin_headers, full_name="Inactive Trader", branch_id=str(world.branch_id)
    )
    patch = api_client.patch(
        f"/api/v1/customers/{customer['id']}",
        headers=dict(admin_headers),
        json={"is_active": False},
    )
    assert patch.status_code == 200, patch.text
    before = world.state()
    parked = world.try_create(
        transaction_type="BUY",
        from_amount="10",
        customer_id=uuid.UUID(str(customer["id"])),
    )
    assert error_code_of(parked) == "CUSTOMER_INACTIVE"
    assert world.state() == before


def test_a_client_amount_disagreeing_beyond_one_minor_unit_is_refused(
    quoted_counter: ExchangeWorld,
) -> None:
    """The server computes the settlement; the client's expectation is cross-checked (§7)."""
    world = quoted_counter
    before = world.state()
    exact = buy(world, from_amount="1000", exchange_rate="70", commission="500", to_amount="69500")
    assert exact.payload["to_amount"] == "69500.0000000000"

    # A device that rounds the last decimal differently is tolerated by one minor unit...
    rounded_by_the_device = world.try_create(
        transaction_type="BUY",
        from_amount="1000",
        exchange_rate="70",
        commission="500",
        to_amount="69500.01",
    )
    assert not isinstance(rounded_by_the_device, BaseException), rounded_by_the_device
    # ... anything larger means the device and the server disagree about the settlement.
    disagreed = world.try_create(
        transaction_type="BUY",
        from_amount="1000",
        exchange_rate="70",
        commission="500",
        to_amount="69500.02",
    )
    assert error_code_of(disagreed) == "AMOUNT_MISMATCH"
    details = error_details_of(disagreed)
    assert details["computed_to_amount"] == "69500.0000000000"
    assert details["stated_to_amount"] == "69500.0200000000"
    assert details["allowed_difference"] == "0.0100000000"
    assert world.state().documents == before.documents + 2


def test_commission_cannot_meet_or_exceed_the_gross(quoted_counter: ExchangeWorld) -> None:
    """A fee the size of the deal is not a fee; it is refused, not silently negative (§8)."""
    world = quoted_counter
    before = world.state()
    for fee in ("70000", "70000.0000000001"):
        outcome = world.try_create(
            transaction_type="BUY", from_amount="1000", exchange_rate="70", commission=fee
        )
        assert error_code_of(outcome) == "VALIDATION_ERROR"
        assert error_details_of(outcome)["fields"][0]["code"] == "exceeds_gross"
    negative = world.try_create(
        transaction_type="BUY", from_amount="1000", exchange_rate="70", commission="-1"
    )
    assert error_code_of(negative) == "VALIDATION_ERROR"
    assert world.state() == before


def test_a_deal_the_drawer_cannot_cover_is_refused_and_rolls_back(
    funded_counter: ExchangeWorld, api_client: TestClient, admin_headers, main_database: str
) -> None:
    """Insufficient position: refused in the ledger, and the half-written document is gone.

    The document row is inserted *before* the entry is posted, so this is the case that proves
    the transaction is one unit: no ``PENDING`` row, no movement, no consumed number.
    """
    world = funded_counter
    world.quote(api_client, admin_headers)
    opening = sell(world, from_amount="10", exchange_rate="71", commission="0")
    period = period_of(opening.payload["transaction_number"])
    before = world.state()
    counter_before = document_counter(main_database, period=period)

    outcome = world.try_create(
        transaction_type="SELL", from_amount="100000", exchange_rate="71", commission="0"
    )
    assert error_code_of(outcome) == "INSUFFICIENT_BALANCE"
    assert error_details_of(outcome)["reason"] == "QUANTITY_EXCEEDED"
    assert world.state() == before
    # The half-written document is gone *and* the number it had taken is back on the
    # counter: the increment happens inside the posting transaction (PART 49).
    assert document_counter(main_database, period=period) == counter_before

    accepted = sell(world, from_amount="1", exchange_rate="71", commission="0")
    assert number_suffix(accepted.payload["transaction_number"]) == counter_before + 1


def test_an_ambiguous_drawer_is_a_configuration_defect(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """Two accounts can hold the same currency at one branch: the engine refuses to guess."""
    world = quoted_counter
    create_account(
        api_client,
        admin_headers,
        code=unique_code(),
        name=f"Second AFN drawer {unique_code()}",
        account_type="ASSET",
        currency_id=str(world.money(AFN).id),
        branch_id=str(world.branch_id),
    )
    before = world.state()
    outcome = world.try_create(
        transaction_type="BUY", from_amount="10.5", exchange_rate="70", commission="0"
    )
    assert error_code_of(outcome) == "DATA_INTEGRITY_ERROR"
    assert error_details_of(outcome)["reason"] == "AMBIGUOUS_CASH_ACCOUNT"
    assert error_details_of(outcome)["branch_id"] == str(world.branch_id)
    assert world.state() == before


def test_a_currency_without_a_drawer_cannot_be_traded(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers, main_database: str
) -> None:
    """No cash account can hold the currency: a named refusal, not a silent no-op.

    The chart seeds one cash account per currency, so the case is built the way production
    would: a currency added *after* the chart was seeded has no drawer to trade out of, and
    the refusal says exactly that instead of posting to an account that does not exist.
    """
    world = quoted_counter
    created = api_client.post(
        "/api/v1/currencies",
        headers=dict(admin_headers),
        json={
            "code": "CNY",
            "name": "Chinese Yuan",
            "symbol": "\u00a5",
            "decimal_places": 2,
            "display_order": 90,
        },
    )
    assert created.status_code == 201, created.text
    cny_id = currency_id_of(main_database, "CNY")
    try:
        world.quote(
            api_client,
            admin_headers,
            from_code="CNY",
            to_code=AFN,
            buy_rate="10",
            sell_rate="10",
        )
        before = world.state()
        outcome = world.try_create(
            transaction_type="BUY",
            from_code="CNY",
            to_code=AFN,
            from_amount="100",
            exchange_rate="10",
            commission="0",
        )
        assert error_code_of(outcome) == "VALIDATION_ERROR"
        details = error_details_of(outcome)
        assert details["fields"] == [{"field": "currency_id", "code": "no_cash_account"}]
        assert details["currency_id"] == str(cny_id)
        assert details["branch_id"] == str(world.branch_id)
        assert world.state() == before
    finally:
        api_client.patch(
            f"/api/v1/currencies/{cny_id}",
            headers=dict(admin_headers),
            json={"is_active": False},
        )


def test_a_branch_without_a_till_of_its_own_trades_out_of_the_group_chart(
    api_client: TestClient, admin_headers: dict[str, str], main_database: str
) -> None:
    """§10 / §5: the drawer resolution order, second step, end to end.

    A branch may trade out of the *group* chart's cash accounts (the seeded ``1000``/``1001``
    band) when it has no till of its own — that is how a new counter starts before it is given
    its own drawers. The step has to be provable, so this branch is created with no asset
    accounts of its own at all and the deal is expected to land on the seeded band accounts,
    branch-scoped in the movements (``cash_movements.branch_id`` is this branch, so its
    position is its own even though the account is shared).
    """
    world = build_world(api_client, admin_headers, main_database, seeded_drawers=True)
    try:
        # The group drawers are the branch's drawers here, and they are funded for *this*
        # branch: a cash movement carries the branch, so the position is branch-scoped.
        world.fund(("AFN", "200000", "1"), ("USD", "1000", "70"))
        # What resolution found *is* the seeded band account, not a branch-bound copy of it.
        assert world.drawer("USD") == account_id_of(main_database, "1001")
        assert world.drawer(AFN) == account_id_of(main_database, "1000")
        world.quote(api_client, admin_headers)

        before = world.cash("USD")
        created = world.create(
            transaction_type="BUY",
            from_code="USD",
            from_amount="100",
            exchange_rate="70",
            commission="10",
        )
        movements = world.movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=created.transaction_id
        )
        assert {str(item["account_code"]) for item in movements} == {"1000", "1001"}
        assert {str(item["branch_id"]) for item in movements} == {str(world.branch_id)}
        assert world.cash("USD") == before + Decimal("100")
        assert world.cash(AFN) == Decimal("200000") - Decimal("6990")
        assert world.ledger("USD") == world.cash("USD")
        assert world.ledger(AFN) == world.cash(AFN)
    finally:
        retire(api_client, admin_headers, world)


# ------------------------------------------------------------------------ idempotency
def test_the_same_key_replays_the_recorded_document(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    key = uuid.uuid4()
    first = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        idempotency_key=key,
    )
    before = world.state()
    replay_result = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        idempotency_key=key,
    )
    assert replay_result.payload == first.payload
    assert world.state() == before  # no second document, no second entry
    row = world.idempotency(key)
    assert row["status"] == "COMPLETED"
    assert row["endpoint"] == "exchange:create"
    assert row["response_status"] == 201
    assert row["resource_id"] == uuid.UUID(str(first.payload["id"]))
    assert row["response_body"]["transaction_number"] == first.payload["transaction_number"]


def test_the_same_key_with_a_different_deal_is_a_conflict(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    key = uuid.uuid4()
    world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        idempotency_key=key,
    )
    before = world.state()
    outcome = world.try_create(
        transaction_type="BUY",
        from_amount="200",
        exchange_rate="70",
        commission="0",
        idempotency_key=key,
    )
    assert error_code_of(outcome) == "IDEMPOTENCY_KEY_REUSED"
    assert world.state() == before


def test_a_failed_attempt_does_not_become_a_stored_answer(
    api_client: TestClient, admin_headers: dict[str, str], main_database: str
) -> None:
    """Retry-after-failure: the key is free again, and the retry succeeds normally.

    The pair is PKR/AFN on purpose. A quote is an observation of the market that lives in an
    append-only book any branch may fall back on, so a test that needs "this pair has no rate"
    has to name a pair no other scenario ever prices — otherwise a group-wide quote published
    by another suite would quietly price the deal and the test would be asserting the state of
    the database rather than the behaviour of the engine.
    """
    world = build_world(
        api_client, admin_headers, main_database, currencies=(AFN, "PKR"), drawers=True
    )
    try:
        world.fund(("AFN", "1000", "1"))
        key = uuid.uuid4()
        before = world.state()
        failed = world.try_create(
            transaction_type="BUY",
            from_code="PKR",
            from_amount="100",
            exchange_rate="0.5",
            commission="0",
            idempotency_key=key,
        )
        assert error_code_of(failed) == "RATE_NOT_FOUND"
        assert world.state().idempotency_rows == before.idempotency_rows

        world.quote(api_client, admin_headers, from_code="PKR", buy_rate="0.5", sell_rate="0.55")
        retry = world.create(
            transaction_type="BUY",
            from_code="PKR",
            from_amount="100",
            exchange_rate="0.5",
            commission="0",
            idempotency_key=key,
        )
        assert retry.status_code == 201
        assert world.idempotency(key)["response_status"] == 201
    finally:
        retire(api_client, admin_headers, world)


def test_a_recorded_offline_event_is_replayed_by_its_event_id(
    quoted_counter: ExchangeWorld,
) -> None:
    """PART 34/§19: the same event returns its document even under a fresh key."""
    world = quoted_counter
    event = uuid.uuid4()
    first = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    assert first.payload["origin"] == "OFFLINE"
    assert first.payload["client_event_id"] == str(event)
    before = world.state()
    replay_result = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    assert replay_result.replayed is True
    assert replay_result.payload == first.payload
    assert world.state().book == before.book


def test_an_offline_event_with_different_content_is_a_recorded_conflict(
    quoted_counter: ExchangeWorld,
) -> None:
    """Last-Write-Wins is forbidden: the conflict is refused and audited (§19, PART 34)."""
    world = quoted_counter
    event = uuid.uuid4()
    first = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    before = world.state()
    outcome = world.try_create(
        transaction_type="BUY",
        from_amount="250",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    assert error_code_of(outcome) == "CONFLICT"
    assert error_details_of(outcome)["fields"][0]["code"] == "event_conflict"
    assert (
        error_details_of(outcome)["existing_transaction_number"]
        == first.payload["transaction_number"]
    )
    after = world.state()
    assert after.documents == before.documents
    # The refusal itself is auditable: the conflict survives the failed request's rollback.
    conflicts = audit_rows_for_entity(world.database, first.transaction_id)
    assert [row["action"] for row in conflicts] == [
        AuditAction.EXCHANGE_CREATED,
        AuditAction.EXCHANGE_EVENT_CONFLICT,
    ]
    assert conflicts[1]["new_data"]["conflict"] == "CLIENT_EVENT_CONFLICT"
    assert (
        conflicts[1]["new_data"]["existing_transaction_number"]
        == first.payload["transaction_number"]
    )


def test_a_replayed_offline_event_survives_catalogue_drift(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """A re-synced event is answered from what is stored, before the catalogue is re-read."""
    world = quoted_counter
    event = uuid.uuid4()
    first = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    # The quote is superseded and the currency deactivated *after* the deal: a replayed event
    # must not become impossible because the world moved on.
    world.quote(api_client, admin_headers, buy_rate="80", sell_rate="81")
    deactivate = api_client.patch(
        f"/api/v1/currencies/{world.money(USD).id}",
        headers=dict(admin_headers),
        json={"is_active": False},
    )
    assert deactivate.status_code == 200, deactivate.text
    replay_result = world.create(
        transaction_type="BUY",
        from_amount="100",
        exchange_rate="70",
        commission="0",
        client_event_id=event,
    )
    assert replay_result.replayed is True
    assert replay_result.payload == first.payload
    api_client.patch(
        f"/api/v1/currencies/{world.money(USD).id}",
        headers=dict(admin_headers),
        json={"is_active": True},
    )


# ------------------------------------------------------------- HTTP shape and payload
def test_the_endpoint_requires_an_idempotency_key(
    http_counter: ExchangeWorld, api_client: TestClient
) -> None:
    """PART 40: a money-moving POST without the header is refused before it reaches the book."""
    world = http_counter
    before = world.state()
    response = api_client.post(
        "/api/v1/exchange",
        headers=dict(world.headers),
        json={
            "transaction_type": "BUY",
            "branch_id": str(world.branch_id),
            "from_currency_id": str(world.money(USD).id),
            "from_amount": "100",
            "to_currency_id": str(world.money(AFN).id),
            "exchange_rate": "70",
        },
    )
    assert response.status_code == 400, response.text
    assert error_code(response) == "IDEMPOTENCY_KEY_REQUIRED"
    assert error_details(response)["header"] == "Idempotency-Key"

    malformed = api_client.post(
        "/api/v1/exchange",
        headers={**dict(world.headers), "Idempotency-Key": "not-a-uuid"},
        json={
            "transaction_type": "BUY",
            "branch_id": str(world.branch_id),
            "from_currency_id": str(world.money(USD).id),
            "from_amount": "100",
            "to_currency_id": str(world.money(AFN).id),
            "exchange_rate": "70",
        },
    )
    assert malformed.status_code == 422, malformed.text
    assert error_code(malformed) == "VALIDATION_ERROR"
    assert error_details(malformed)["reason"] == "NOT_A_UUID"
    assert world.state() == before


def test_the_http_contract_of_a_create(http_counter: ExchangeWorld, api_client: TestClient) -> None:
    """The endpoint's body, status and headers, over the wire (API_CONTRACT §9.3)."""
    world = http_counter
    key = uuid.uuid4()
    response = post_exchange(
        api_client,
        world.headers,
        transaction_type="BUY",
        branch_id=world.branch_id,
        from_currency_id=world.money(USD).id,
        from_amount="100",
        to_currency_id=world.money(AFN).id,
        exchange_rate="70",
        commission="50",
        idempotency_key=key,
    )
    body = response.json()
    assert response.status_code == 201
    assert body["to_amount"] == "6950.0000000000"
    assert body["receipt"]["url"] == f"/api/v1/exchange/{body['id']}/receipt"
    assert [item["movement_type"] for item in body["cash_movements"]] == ["IN", "OUT"]
    # The signed-in device is recorded on the document and in the audit trail.
    assert body["device_id"] == str(world.device_id)
    assert world.document(uuid.UUID(body["id"]))["device_id"] == world.device_id
    assert world.audit(uuid.UUID(body["id"]))[0]["new_data"]["device_id"] == str(world.device_id)

    # The replay answers the recorded body and the recorded status, not a re-render.
    replay = post_exchange(
        api_client,
        world.headers,
        transaction_type="BUY",
        branch_id=world.branch_id,
        from_currency_id=world.money(USD).id,
        from_amount="100",
        to_currency_id=world.money(AFN).id,
        exchange_rate="70",
        commission="50",
        idempotency_key=key,
    )
    assert replay.headers.get("idempotency-replayed") in {"true", "True", None}
    assert replay.json() == body


def test_a_float_amount_is_refused_by_the_schema(
    http_counter: ExchangeWorld, api_client: TestClient
) -> None:
    """Money crosses the wire as a decimal string: a float cannot be exact (PART 62)."""
    world = http_counter
    before = world.state()
    response = api_client.post(
        "/api/v1/exchange",
        headers=exchange_headers(world.headers),
        json={
            "transaction_type": "BUY",
            "branch_id": str(world.branch_id),
            "from_currency_id": str(world.money(USD).id),
            "from_amount": 1000.5,
            "to_currency_id": str(world.money(AFN).id),
            "exchange_rate": "70",
        },
    )
    assert response.status_code == 422, response.text
    assert error_code(response) == "VALIDATION_ERROR"
    assert world.state() == before


def test_a_device_of_another_branch_cannot_be_used(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """``device_id`` is the counter's identity: validated and scoped before anything else.

    A device belongs to one branch, so a deal rung up on another branch's device is refused —
    silently attributing the document to a device that was never at this counter is exactly
    what §16 forbids.
    """
    world = quoted_counter
    before = world.state()
    foreign_device = uuid.UUID(str(admin_headers["X-Device-Id"]))
    outcome = world.try_create(
        transaction_type="BUY",
        from_amount="10",
        exchange_rate="70",
        commission="0",
        device_id=foreign_device,
    )
    assert error_code_of(outcome) == "FORBIDDEN_SCOPE"
    assert error_details_of(outcome)["fields"][0]["code"] == "branch_mismatch"
    assert error_details_of(outcome)["device_branch_id"] != str(world.branch_id)
    assert world.state() == before

    unknown = world.try_create(
        transaction_type="BUY",
        from_amount="10",
        exchange_rate="70",
        commission="0",
        device_id=uuid.uuid4(),
    )
    assert error_code_of(unknown) == "RESOURCE_NOT_FOUND"
    assert error_details_of(unknown)["fields"][0]["code"] == "not_found"
    assert world.state() == before


def test_a_cross_branch_create_is_refused_and_audited(
    quoted_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers,
    main_database: str,
) -> None:
    """Branch isolation on the write path, with the denial written to the audit chain."""
    world = quoted_counter
    other = build_world(api_client, admin_headers, main_database)
    try:
        other.fund_all()
        other.quote(api_client, admin_headers)
        # A manager scoped to *its own* branch, dealing on another branch's counter.
        actor = other.actor(roles=(RoleName.MANAGER,))
        before = world.state()
        audit_baseline = int(scalar(main_database, "SELECT COALESCE(MAX(seq), 0) FROM audit_logs"))
        outcome = other.try_create(
            actor=actor,
            transaction_type="BUY",
            from_amount="10",
            exchange_rate="70",
            commission="0",
            branch_id=world.branch_id,
        )
        assert error_code_of(outcome) == "FORBIDDEN_SCOPE"
        after = world.state()
        assert (after.documents, after.movements, after.entries) == (
            before.documents,
            before.movements,
            before.entries,
        )
        denials = [
            row
            for row in audit_rows_for_action(main_database, str(AuditAction.LEDGER_POSTING_DENIED))
            if row["seq"] > audit_baseline
        ]
        assert len(denials) == 1
        assert denials[0]["new_data"]["reason"] == "ANOTHER_BRANCH"
        assert denials[0]["new_data"]["target_branch_id"] == str(world.branch_id)
        assert denials[0]["user_id"] == actor.user_id
    finally:
        retire(api_client, admin_headers, other)


def test_the_number_is_unique_and_committed_once_under_the_same_period(
    quoted_counter: ExchangeWorld,
) -> None:
    """Sequential deals in one branch take consecutive numbers; nothing is reused."""
    world = quoted_counter
    numbers = [
        world.create(
            transaction_type="BUY", from_amount="1", exchange_rate="70", commission="0"
        ).payload["transaction_number"]
        for _ in range(3)
    ]
    assert len(set(numbers)) == 3
    suffixes = [number_suffix(number) for number in numbers]
    assert suffixes == [suffixes[0], suffixes[0] + 1, suffixes[0] + 2]
    period = numbers[0].split("-")[1]
    assert all(number.startswith(f"NX-{period}-") for number in numbers)
    assert period == dt.datetime.now(dt.UTC).strftime("%Y%m%d") or period == (
        dt.datetime.now(dt.timezone(dt.timedelta(hours=4, minutes=30))).strftime("%Y%m%d")
    )


# ---------------------------------------------------------------------------- utilities
def error_code_of(outcome: object) -> str | None:
    """The domain error code of a refused call (``None`` when it did not refuse)."""
    from app.core.exceptions import NexusError

    if isinstance(outcome, NexusError):
        return str(outcome.code)
    if isinstance(outcome, BaseException):
        raise AssertionError(f"expected a domain refusal, got {outcome!r}")
    return None


def error_details_of(outcome: object) -> dict[str, object]:
    from app.core.exceptions import NexusError

    assert isinstance(outcome, NexusError), outcome
    return dict(outcome.details or {})
