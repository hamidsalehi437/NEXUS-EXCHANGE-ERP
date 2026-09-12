"""Phase 6 — the movement book: receipts, payouts, corrections and their reversal.

Every standalone movement (``POST /cash/in``, ``/cash/out``, ``/cash/adjustment`` and the
reversal door) is one atomic act: a journal entry through ``AccountingService``, a physical
``cash_movements`` row bound to it, an audit row, and — for the money-moving doors — an
idempotency record. This suite proves that shape from the outside: what the HTTP door says,
what the tables hold, and what the drawer position and the ledger each independently report.

The rules it refuses to let slide:

* a payout the drawer cannot cover is refused **before** anything is written, and the refusal
  carries the shortfall (``details.shortfall``);
* receipts in a foreign currency are valued by the house's own quote, disposals at the
  drawer's carrying rate, and a client cannot supply either;
* an adjustment is the only way to state a correction, it always goes to ``5090``, and it
  needs ``cash.adjust``;
* retries are replays: the same ``Idempotency-Key`` with the same body answers with the
  recorded result and moves no money a second time; a changed body on the same key is refused;
  a re-sent ``client_event_id`` (the offline hook) answers with the movement it already holds;
* a reversal mirrors the original entry exactly, is refused a second time, cannot touch a
  movement another document owns, and is itself not reversible.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.audit_actions import AuditAction
from app.core.exceptions import CashCounterAccountRequiredError, InsufficientBalanceError
from tests.auth_helpers import bearer, login
from tests.cash_helpers import (
    SHORT_OVER_CODE,
    attach_session,
    audit_rows,
    branch_entries,
    get_movement,
    get_movements,
    idempotency_rows,
    ledger_quantity,
    movement_row,
    movement_rows,
    movements_for_reference,
    position,
    post_adjustment,
    post_in,
    post_open,
    post_out,
    post_reverse,
    publish,
    read_one,
    run_cash,
    service_in,
    service_open_session_id,
    service_out,
)
from tests.exchange_helpers import (
    ExchangeWorld,
    account_id_of,
    build_world,
    error_code,
    error_details,
    retire,
)

pytestmark = [pytest.mark.integration, pytest.mark.cash]

AFN = "AFN"
USD = "USD"
CAPITAL = "3000"
SALARIES = "5000"


@pytest.fixture
def cash_counter(
    exchange_world: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> ExchangeWorld:
    """A counter of its own: its own branch, its own device and session, nothing funded."""
    world = exchange_world
    attach_session(world, api_client, admin_headers)
    return world


def open_shift(world: ExchangeWorld, client: TestClient, **declared: str) -> uuid.UUID:
    """Open a shift with the given ``{code: amount}`` declarations and return its id."""
    response = post_open(
        client,
        world.headers,
        branch_id=world.branch_id,
        openings=[
            {"currency_id": str(world.money(code).id), "amount": amount}
            for code, amount in declared.items()
        ],
    )
    return uuid.UUID(response.json()["session_id"])


def as_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def drawer_code(world: ExchangeWorld, code: str) -> str:
    """The chart code of the branch's own drawer for a currency (what the entry posts to)."""
    row = read_one(
        world.database, "SELECT code FROM accounts WHERE id = :id", id=world.drawer(code)
    )
    return str(row["code"]).strip()


def balance_of(world: ExchangeWorld, code: str) -> Decimal:
    """The ledger's balance of one chart account for this branch."""
    row = read_one(
        world.database,
        """
        SELECT COALESCE(SUM(l.debit - l.credit), 0) AS balance
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=world.branch_id,
        account_id=account_id_of(world.database, code),
    )
    return as_decimal(row["balance"])


def book(world: ExchangeWorld) -> dict[str, Any]:
    """Everything a refused movement must leave untouched, in one comparable reading."""
    return {
        "movements": len(movement_rows(world.database, branch_id=world.branch_id)),
        "entries": len(branch_entries(world.database, branch_id=world.branch_id)),
        "position_afn": world.cash(AFN),
        "ledger_afn": world.ledger(AFN),
        "audit": len(audit_rows(world.database, action=AuditAction.CASH_MOVEMENT_RECORDED)),
    }


# ------------------------------------------------------------------------------ receipts
def test_a_receipt_posts_the_entry_the_movement_and_the_audit_row(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """PART 20: entry, movement and audit row, one transaction, one reference."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="100000")
    capital = account_id_of(world.database, CAPITAL)

    response = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="20000",
        source_account_id=capital,
        session_id=session_id,
        idempotency_key=uuid.uuid4(),
    )
    body = response.json()
    movement_id = uuid.UUID(body["id"])

    assert response.status_code == 201
    assert body["movement_type"] == "IN"
    assert body["currency_code"] == AFN
    assert body["amount"] == "20000.0000000000"
    assert body["signed_amount"] == "20000.0000000000"
    assert body["adjustment_sign"] is None
    assert body["reference_type"] == "CASH_MOVEMENT"
    assert body["session_id"] == str(session_id)
    assert body["session_status"] == "OPEN"
    assert body["journal_entry_id"] is not None
    assert body["created_by_username"] == "admin"
    assert body["reversed_by_movement_id"] is None

    # The physical row: attributed to the shift, the device and the entry.
    stored = movement_row(world.database, movement_id)
    assert stored["branch_id"] == world.branch_id
    assert stored["account_id"] == world.drawer(AFN)
    assert as_decimal(stored["amount"]) == Decimal("20000")
    assert as_decimal(stored["signed_amount"]) == Decimal("20000")
    assert stored["cash_session_id"] == session_id
    assert stored["device_id"] == world.device_id
    assert stored["created_at"].tzinfo is not None
    assert (dt.datetime.now(dt.UTC) - stored["created_at"]).total_seconds() < 120

    # The entry: Dr Cash / Cr Owner Capital, one line each side, balanced.
    entry_id = uuid.UUID(body["journal_entry_id"])
    entries = {row["id"]: row for row in branch_entries(world.database, branch_id=world.branch_id)}
    assert entry_id in entries
    assert entries[entry_id]["reference_type"] == "CASH_MOVEMENT"
    lines = movements_for_reference(
        world.database, reference_type="CASH_MOVEMENT", reference_id=stored["reference_id"]
    )
    assert [row["id"] for row in lines] == [movement_id]
    assert ledger_quantity(
        world.database, branch_id=world.branch_id, account_id=world.drawer(AFN)
    ) == Decimal("120000")
    assert balance_of(world, CAPITAL) == Decimal("-20000")  # credited
    assert world.cash(AFN) == world.ledger(AFN) == Decimal("120000")

    # Audit: who moved what, in which shift, against which account and entry (§17).
    audit = audit_rows(
        world.database, entity_id=movement_id, action=AuditAction.CASH_MOVEMENT_RECORDED
    )
    assert len(audit) == 1
    assert audit[0]["entity_type"] == "cash_movement"
    assert audit[0]["user_id"] == world.head_user_id
    assert audit[0]["new_data"]["movement_type"] == "IN"
    assert audit[0]["new_data"]["amount"] == "20000.0000000000"
    assert audit[0]["new_data"]["session_id"] == str(session_id)
    assert audit[0]["new_data"]["counter_account_id"] == str(capital)
    assert audit[0]["new_data"]["journal_entry_id"] == str(entry_id)
    assert audit[0]["created_at"].tzinfo is not None


def test_a_payout_the_drawer_cannot_cover_is_refused_before_anything_is_written(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Availability is checked under the drawer's lock, and a refusal leaves no trace."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    salaries = account_id_of(world.database, SALARIES)
    before = book(world)

    refusal = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1500",
        target_account_id=salaries,
        session_id=session_id,
        expect=None,
    )
    assert refusal.status_code == 409
    assert error_code(refusal) == "INSUFFICIENT_BALANCE"
    assert error_details(refusal)["shortfall"] == "500.0000000000"
    assert error_details(refusal)["reason"] == "QUANTITY_EXCEEDED"
    assert error_details(refusal)["foreign_quantity"] == "1000.0000000000"
    assert error_details(refusal)["disposing_quantity"] == "1500.0000000000"
    assert book(world) == before
    assert balance_of(world, SALARIES) == Decimal("0")

    # The amount the drawer *can* cover goes through, and only that amount left.
    paid = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="900",
        target_account_id=salaries,
        session_id=session_id,
    )
    assert paid.status_code == 201
    assert world.cash(AFN) == Decimal("100")
    assert world.ledger(AFN) == Decimal("100")
    assert balance_of(world, SALARIES) == Decimal("900")


def test_a_payout_from_a_drawer_emptied_to_zero_reports_no_position_not_a_shortfall(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The two shapes of an ``INSUFFICIENT_BALANCE`` refusal are not interchangeable.

    A drawer that holds *nothing* has no shortfall to state: the operator is told
    ``NO_POSITION`` (with the zero the drawer actually carries), not a number that would
    read as "we are a little short". A drawer that holds *some* cash but not enough is told
    exactly how much is missing. Both shapes name the drawer and the quantity it was asked
    to deliver, and each refuses without writing anything.
    """
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    salaries = account_id_of(world.database, SALARIES)

    emptied = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1000",
        target_account_id=salaries,
        session_id=session_id,
    )
    assert emptied.status_code == 201
    assert world.cash(AFN) == Decimal("0")
    assert world.ledger(AFN) == Decimal("0")

    before = book(world)
    refusal = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        target_account_id=salaries,
        session_id=session_id,
        expect=None,
    )
    assert refusal.status_code == 409
    assert error_code(refusal) == "INSUFFICIENT_BALANCE"
    details = error_details(refusal)
    assert details["reason"] == "NO_POSITION"
    assert "shortfall" not in details
    assert details["foreign_quantity"] == "0.0000000000"
    assert details["disposing_quantity"] == "100.0000000000"
    assert book(world) == before
    assert balance_of(world, SALARIES) == Decimal("1000")


def test_a_foreign_receipt_is_valued_by_the_house_quote_and_a_disposal_by_the_carrying_rate(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§6.4: money enters at what the house pays for it and leaves at what it is carried at."""
    world = cash_counter
    usd = world.money(USD).id
    publish(world, api_client, world.headers, USD, buy="70", sell="71")
    session_id = open_shift(world, api_client, USD="1000")
    capital = account_id_of(world.database, CAPITAL)

    receipt = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=usd,
        amount="500",
        source_account_id=capital,
        session_id=session_id,
    )
    receipt_id = uuid.UUID(receipt.json()["journal_entry_id"])
    lines = _entry_lines(world, receipt_id)
    cash_line = next(row for row in lines if row["account_code"] == drawer_code(world, USD))
    assert as_decimal(cash_line["exchange_rate"]) == Decimal("70")  # the buying quote
    assert as_decimal(cash_line["debit"]) == Decimal("35000")  # 500 x 70
    assert as_decimal(cash_line["foreign_amount"]) == Decimal("500")

    # The carrying rate is what the position is actually held at: change the quote and the
    # next disposal still leaves at 70, because that is what the drawer's cash cost.
    publish(world, api_client, world.headers, USD, buy="80", sell="81")
    payment = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=usd,
        amount="100",
        target_account_id=capital,
        session_id=session_id,
    )
    payment_entry = uuid.UUID(payment.json()["journal_entry_id"])
    payment_lines = _entry_lines(world, payment_entry)
    out_line = next(row for row in payment_lines if row["account_code"] == drawer_code(world, USD))
    assert as_decimal(out_line["exchange_rate"]) == Decimal("70")
    assert as_decimal(out_line["credit"]) == Decimal("7000")
    assert world.cash(USD) == Decimal("1400")
    assert world.ledger(USD) == Decimal("1400")
    assert position(world.database, branch_id=world.branch_id, currency_code=USD) == Decimal("1400")


def _entry_lines(world: ExchangeWorld, entry_id: uuid.UUID) -> list[dict[str, Any]]:
    from tests.cash_helpers import entry_with_lines

    _entry, lines = entry_with_lines(world.database, entry_id)
    return lines


def test_an_adjustment_goes_to_5090_in_both_directions_and_needs_the_permission(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers: dict[str, str],
    make_user: object,
    provisioned_device: object,
) -> None:
    """A correction is explicit, reasoned, audited — and never a cashier's own call."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="10000")

    over = post_adjustment(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="250",
        adjustment_sign=1,
        reason="Drawer counted 250 over at handover",
        session_id=session_id,
    )
    assert over.status_code == 201
    over_body = over.json()
    assert over_body["movement_type"] == "ADJUSTMENT"
    assert over_body["adjustment_sign"] == 1
    assert over_body["signed_amount"] == "250.0000000000"
    over_lines = _entry_lines(world, uuid.UUID(over_body["journal_entry_id"]))
    assert {row["account_code"] for row in over_lines} == {
        drawer_code(world, AFN),
        SHORT_OVER_CODE,
    }
    assert balance_of(world, SHORT_OVER_CODE) == Decimal("-250")  # credited: a gain
    assert world.cash(AFN) == Decimal("10250")

    short = post_adjustment(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        adjustment_sign=-1,
        reason="Drawer counted 100 short at handover",
        session_id=session_id,
    )
    assert short.status_code == 201
    assert short.json()["signed_amount"] == "-100.0000000000"
    assert balance_of(world, SHORT_OVER_CODE) == Decimal("-150")

    audit = audit_rows(
        world.database,
        entity_id=uuid.UUID(short.json()["id"]),
        action=AuditAction.CASH_ADJUSTMENT_RECORDED,
    )
    assert len(audit) == 1
    assert audit[0]["new_data"]["reason"] == "Drawer counted 100 short at handover"
    assert audit[0]["new_data"]["adjustment_sign"] == -1

    # A cashier holds cash.create but not cash.adjust: the door is closed to them.
    cashier = make_user(roles=("CASHIER",))  # type: ignore[operator]
    device_uuid = provisioned_device(assigned_branch=str(world.branch_id))  # type: ignore[operator]
    tokens = login(
        api_client,
        str(cashier["username"]),
        str(cashier["password"]),
        device_uuid=uuid.UUID(device_uuid),
        branch_id=world.branch_id,
    ).json()
    headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))
    denied = post_adjustment(
        api_client,
        headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="10",
        adjustment_sign=1,
        reason="Not mine to decide",
        session_id=session_id,
        expect=None,
    )
    assert denied.status_code == 403
    assert error_code(denied) == "PERMISSION_DENIED"
    assert error_details(denied)["required_permission"] == "cash.adjust"
    assert world.cash(AFN) == Decimal("10150")  # unchanged by the refusal


def test_a_bad_amount_is_refused_with_a_reason_and_no_side_effect(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Zero, negative, sub-cent and unrepresentable amounts never reach the ledger."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    before = book(world)

    zero = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="0",
        source_account_id=capital,
        session_id=session_id,
        expect=None,
    )
    assert zero.status_code == 422
    assert error_details(zero)["fields"][0]["code"] == "not_positive"

    negative = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="-5",
        source_account_id=capital,
        session_id=session_id,
        expect=None,
    )
    assert negative.status_code == 422
    assert error_details(negative)["fields"][0]["code"] == "negative"

    precise = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1.00000000001",
        source_account_id=capital,
        session_id=session_id,
        expect=None,
    )
    assert precise.status_code == 422
    assert error_details(precise)["fields"][0]["code"] == "not_exact_scale"

    sub_cent = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="0.005",
        source_account_id=capital,
        session_id=session_id,
        expect=None,
    )
    assert sub_cent.status_code == 422
    assert error_details(sub_cent)["fields"][0]["code"] == "below_smallest_unit"

    # An amount ``NUMERIC(30,10)`` cannot hold is refused at the edge, before any service or
    # database work: the schema parses money with the ledger's own bound check.
    enormous = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1" + "0" * 25,
        source_account_id=capital,
        session_id=session_id,
        expect=None,
    )
    assert enormous.status_code == 422
    assert error_code(enormous) == "VALIDATION_ERROR"
    refused_money = error_details(enormous)["fields"][0]
    assert refused_money["field"] == "amount"
    assert "maximum representable amount" in refused_money["message"]

    # A JSON number is refused wherever it appears: a binary fraction has already lost the
    # value before any validator could look at it (PART 62).
    floating = api_client.post(
        "/api/v1/cash/in",
        headers={**world.headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "branch_id": str(world.branch_id),
            "currency_id": str(afn),
            "amount": 1000.5,
            "source_account_id": str(capital),
            "session_id": str(session_id),
        },
    )
    assert floating.status_code == 422
    assert error_code(floating) == "VALIDATION_ERROR"
    assert "float" in error_details(floating)["fields"][0]["message"]

    assert book(world) == before


def test_the_counter_account_must_exist_and_differ_from_the_drawer(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Cash cannot appear from nowhere: the counter side is mandatory and is not the till."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    before = book(world)

    missing = api_client.post(
        "/api/v1/cash/in",
        headers={**world.headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "branch_id": str(world.branch_id),
            "currency_id": str(afn),
            "amount": "100",
            "session_id": str(session_id),
        },
    )
    assert missing.status_code == 422
    assert error_code(missing) == "VALIDATION_ERROR"

    same = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        target_account_id=world.drawer(AFN),
        session_id=session_id,
        expect=None,
    )
    assert same.status_code == 422
    assert error_code(same) == "CASH_COUNTER_ACCOUNT_REQUIRED"
    assert error_details(same)["fields"][0]["code"] == "same_as_cash_account"

    unknown = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        target_account_id=uuid.uuid4(),
        session_id=session_id,
        expect=None,
    )
    assert unknown.status_code == 404
    assert error_code(unknown) == "RESOURCE_NOT_FOUND"
    assert book(world) == before


def test_an_inactive_currency_cannot_be_moved(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A retired currency takes no new cash; history stays readable."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)

    # Retire a currency that nothing else in the suite trades, then try to move it.
    pk = world.money("PKR")
    response = api_client.patch(
        f"/api/v1/currencies/{pk.id}", headers=dict(admin_headers), json={"is_active": False}
    )
    assert response.status_code == 200, response.text
    before = book(world)
    try:
        refusal = post_in(
            api_client,
            world.headers,
            branch_id=world.branch_id,
            currency_id=pk.id,
            amount="100",
            source_account_id=capital,
            session_id=session_id,
            expect=None,
        )
        assert refusal.status_code == 422
        assert error_code(refusal) == "CURRENCY_INACTIVE"
        assert book(world) == before
    finally:
        restored = api_client.patch(
            f"/api/v1/currencies/{pk.id}", headers=dict(admin_headers), json={"is_active": True}
        )
        assert restored.status_code == 200, restored.text
    assert afn == world.money(AFN).id


# -------------------------------------------------------------------------- idempotency
def test_a_retry_with_the_same_key_replays_and_a_changed_body_is_refused(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """PART 40: one key, one movement — and a key is bound to the body it claimed."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    key = uuid.uuid4()

    first = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="500",
        source_account_id=capital,
        session_id=session_id,
        idempotency_key=key,
    )
    assert first.status_code == 201
    assert "Idempotency-Replayed" not in first.headers

    replay = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="500",
        source_account_id=capital,
        session_id=session_id,
        idempotency_key=key,
    )
    assert replay.status_code == 201
    assert replay.json() == first.json()

    reused = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="501",  # a different request under the same key
        source_account_id=capital,
        session_id=session_id,
        idempotency_key=key,
        expect=None,
    )
    assert reused.status_code == 409
    assert error_code(reused) == "IDEMPOTENCY_KEY_REUSED"

    # The key is scoped to the *endpoint*: the same key on the payout door is a new act.
    other_door = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        target_account_id=capital,
        session_id=session_id,
        idempotency_key=key,
    )
    assert other_door.status_code == 201
    assert other_door.json()["movement_type"] == "OUT"

    rows = movement_rows(world.database, branch_id=world.branch_id, session_id=session_id)
    assert [(row["movement_type"], as_decimal(row["amount"])) for row in rows] == [
        ("OPENING", Decimal("1000")),
        ("IN", Decimal("500")),
        ("OUT", Decimal("100")),
    ]
    # The key is scoped to the endpoint: the payout wrote its own claim, and the receipt's
    # claim is untouched by it.
    claims = {row["endpoint"]: row for row in idempotency_rows(world.database, key)}
    assert set(claims) == {"cash:in", "cash:out"}
    assert claims["cash:out"]["status"] == "COMPLETED"
    assert claims["cash:out"]["response_body"]["id"] == other_door.json()["id"]
    assert claims["cash:in"]["response_body"]["id"] == first.json()["id"]
    assert claims["cash:in"]["response_body"] == first.json()
    assert world.cash(AFN) == Decimal("1400")


def test_a_money_moving_door_requires_an_idempotency_key(
    cash_counter: ExchangeWorld, api_client: TestClient
) -> None:
    """A retry must be able to name itself: without a key the door answers 400."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    response = api_client.post(
        "/api/v1/cash/in",
        headers=dict(world.headers),  # deliberately no Idempotency-Key
        json={
            "branch_id": str(world.branch_id),
            "currency_id": str(afn),
            "amount": "10",
            "source_account_id": str(account_id_of(world.database, CAPITAL)),
            "session_id": str(session_id),
        },
    )
    assert response.status_code == 400
    assert error_code(response) == "IDEMPOTENCY_KEY_REQUIRED"
    assert response.json()["error"]["details"]["header"] == "Idempotency-Key"


def test_a_re_sent_client_event_is_the_same_movement(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The offline hook (PART 34): a replayed event id answers with the recorded movement."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    event_id = uuid.uuid4()

    first = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="300",
        source_account_id=capital,
        session_id=session_id,
        client_event_id=event_id,
    )
    again = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="300",
        source_account_id=capital,
        session_id=session_id,
        client_event_id=event_id,
    )
    assert again.status_code == 201
    assert again.json()["id"] == first.json()["id"]
    assert world.cash(AFN) == Decimal("1300")

    conflicting = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="999",  # the same event cannot be a different amount
        source_account_id=capital,
        session_id=session_id,
        client_event_id=event_id,
        expect=None,
    )
    assert conflicting.status_code == 409
    assert error_code(conflicting) == "CONFLICT"
    assert error_details(conflicting)["reason"] == "DUPLICATE_RESOURCE"
    assert error_details(conflicting)["movement_id"] == first.json()["id"]
    assert world.cash(AFN) == Decimal("1300")


def test_the_accounting_date_is_server_bounded_and_created_at_is_the_servers(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A document cannot be dated beyond what the server's clock can vouch for (§11).

    The MVP has no period lock (``ACCOUNTING_MODEL.md`` §11), so a *late* document is
    postable for the day it belongs to — a documented limitation, not a licence to invent
    dates. What the server does enforce is that the accounting date it accepts is inside its
    own tolerance, and that both rows carry a ``created_at`` written by the database whatever
    date the operator supplied: the two facts stay distinguishable to an auditor.
    """
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    now = dt.datetime.now(dt.UTC)

    posted = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        source_account_id=capital,
        session_id=session_id,
        transaction_date=(now - dt.timedelta(hours=6)).isoformat(),
    )
    assert posted.status_code == 201
    movement = movement_row(world.database, uuid.UUID(posted.json()["id"]))
    entry = read_one(
        world.database,
        "SELECT transaction_date, created_at FROM journal_entries WHERE id = :id",
        id=posted.json()["journal_entry_id"],
    )
    # The operator's date is honoured for the accounting date (a late document is legitimate)…
    assert entry["transaction_date"] < now - dt.timedelta(hours=5)
    # …while both rows were written by this server, seconds ago: no client can forge that.
    assert (dt.datetime.now(dt.UTC) - movement["created_at"]).total_seconds() < 120
    assert (dt.datetime.now(dt.UTC) - entry["created_at"]).total_seconds() < 120

    # A moment the server cannot vouch for is refused, and nothing is written.
    before = book(world)
    future = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        source_account_id=capital,
        session_id=session_id,
        transaction_date=(now + dt.timedelta(days=1)).isoformat(),
        expect=None,
    )
    assert future.status_code == 422
    assert error_code(future) == "VALIDATION_ERROR"
    assert error_details(future)["fields"][0]["code"] == "future_date"
    assert "latest_allowed" in error_details(future)
    assert book(world) == before

    # A date without a timezone is refused too: the ledger stores UTC or nothing.
    naive = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        source_account_id=capital,
        session_id=session_id,
        transaction_date=now.replace(tzinfo=None).isoformat(),
        expect=None,
    )
    assert naive.status_code == 422
    assert error_code(naive) == "VALIDATION_ERROR"
    assert error_details(naive)["fields"][0]["code"] == "value_error"
    assert "timezone" in error_details(naive)["fields"][0]["message"]
    assert book(world) == before


# ------------------------------------------------------------------------------- reads
def test_the_movement_book_filters_and_paginates(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """The book is branch-scoped, newest first, and its filters mean what they say."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    for amount in ("100", "200"):
        post_in(
            api_client,
            world.headers,
            branch_id=world.branch_id,
            currency_id=afn,
            amount=amount,
            source_account_id=capital,
            session_id=session_id,
        )
    post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="50",
        target_account_id=capital,
        session_id=session_id,
    )

    everything = get_movements(api_client, world.headers, branch_id=world.branch_id).json()
    assert everything["total"] == 4
    assert [row["movement_type"] for row in everything["items"]] == [
        "OUT",
        "IN",
        "IN",
        "OPENING",
    ]

    receipts = get_movements(
        api_client, world.headers, branch_id=world.branch_id, movement_type="IN"
    ).json()
    assert receipts["total"] == 2
    assert {row["movement_type"] for row in receipts["items"]} == {"IN"}

    by_currency = get_movements(
        api_client, world.headers, branch_id=world.branch_id, currency_id=afn
    ).json()
    assert by_currency["total"] == 4

    by_session = get_movements(
        api_client, world.headers, branch_id=world.branch_id, session_id=session_id
    ).json()
    assert by_session["total"] == 4
    assert {row["session_id"] for row in by_session["items"]} == {str(session_id)}

    # Movements written in one transaction share a timestamp to the microsecond, so
    # "newest first" is only meaningful as a *stable* order: a page must be the window of
    # the full list, never a re-sorted sample of it.
    page = get_movements(
        api_client, world.headers, branch_id=world.branch_id, limit=2, offset=1
    ).json()
    assert page["total"] == 4
    assert page["limit"] == 2
    assert page["offset"] == 1
    assert page["items"] == everything["items"][1:3]
    assert page["items"] != everything["items"][:2]

    # An unknown movement type is a bad request, not an empty page.
    unknown = get_movements(
        api_client, world.headers, branch_id=world.branch_id, movement_type="TELEPORT", expect=None
    )
    assert unknown.status_code == 422
    assert error_code(unknown) == "VALIDATION_ERROR"

    one = get_movement(api_client, world.headers, uuid.UUID(everything["items"][0]["id"])).json()
    assert one["id"] == everything["items"][0]["id"]
    assert one["signed_amount"] == "-50.0000000000"


# ---------------------------------------------------------------------------- reversal
def test_reversing_an_out_puts_the_money_back_exactly_once(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """§6.7: a mirror entry and one compensating movement; the original is untouched."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    cashier_cost = account_id_of(world.database, SALARIES)

    paid = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="400",
        target_account_id=cashier_cost,
        session_id=session_id,
    )
    original_id = uuid.UUID(paid.json()["id"])
    original_entry = uuid.UUID(paid.json()["journal_entry_id"])
    original_row = movement_row(world.database, original_id)

    reversal = post_reverse(
        api_client,
        world.headers,
        movement_id=original_id,
        reason="Paid the wrong supplier",
        idempotency_key=uuid.uuid4(),
        with_key=True,
    )
    body = reversal.json()
    assert reversal.status_code == 200
    assert body["movement_type"] == "IN"
    assert body["amount"] == "400.0000000000"
    assert body["signed_amount"] == "400.0000000000"
    assert body["reference_type"] == "REVERSAL"
    # The compensating movement points at the movement it undid (the evidence chain), while
    # the original keeps pointing at its own document.
    assert body["reference_id"] == str(original_id)
    assert original_row["reference_type"] == "CASH_MOVEMENT"
    assert body["session_id"] == str(session_id)
    assert body["id"] != str(original_id)

    # The original row is byte-for-byte what it was: history is not rewritten.
    assert movement_row(world.database, original_id) == original_row

    # The reversal entry mirrors the original's lines exactly.
    original_lines = _entry_lines(world, original_entry)
    reversal_lines = _entry_lines(world, uuid.UUID(body["journal_entry_id"]))
    assert len(reversal_lines) == len(original_lines)
    mirrored = {row["account_id"]: row for row in reversal_lines}
    for line in original_lines:
        counterpart = mirrored[line["account_id"]]
        assert as_decimal(counterpart["debit"]) == as_decimal(line["credit"])
        assert as_decimal(counterpart["credit"]) == as_decimal(line["debit"])
        assert as_decimal(counterpart["foreign_amount"]) == as_decimal(line["foreign_amount"])
    reversal_entry = read_one(
        world.database,
        "SELECT reference_type, reference_id, reversal_of_id FROM journal_entries WHERE id = :id",
        id=body["journal_entry_id"],
    )
    assert reversal_entry["reference_type"] == "REVERSAL"
    assert reversal_entry["reversal_of_id"] == original_entry

    # The physical movement carries the REVERSAL reference to the movement it undid.
    reversal_movement = movement_row(world.database, uuid.UUID(body["id"]))
    assert reversal_movement["reference_type"] == "REVERSAL"
    assert reversal_movement["reference_id"] == original_id
    assert reversal_movement["journal_entry_id"] == uuid.UUID(body["journal_entry_id"])

    # Money: the drawer is back where it started, and the books agree with it.
    assert world.cash(AFN) == Decimal("1000")
    assert world.ledger(AFN) == Decimal("1000")
    assert balance_of(world, SALARIES) == Decimal("0")

    # The detail read reports the reversal linkage on the original.
    detail = get_movement(api_client, world.headers, original_id).json()
    assert detail["reversed_by_movement_id"] == body["id"]

    # Audit: the reason travels with the reversal, attributed to the actor.
    audit = audit_rows(
        world.database, entity_id=original_id, action=AuditAction.CASH_MOVEMENT_REVERSED
    )
    assert len(audit) == 1
    assert audit[0]["new_data"]["reason"] == "Paid the wrong supplier"
    assert audit[0]["new_data"]["reversal_movement_id"] == body["id"]
    assert audit[0]["new_data"]["journal_entry_id"] == body["journal_entry_id"]
    assert audit[0]["user_id"] == world.head_user_id


def test_a_movement_cannot_be_reversed_twice_or_undone_again(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A reversal is final: the second attempt is refused, and the mirror is not reversible."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    paid = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="100",
        target_account_id=account_id_of(world.database, SALARIES),
        session_id=session_id,
    )
    original_id = uuid.UUID(paid.json()["id"])
    first = post_reverse(api_client, world.headers, movement_id=original_id, reason="Wrong account")
    mirror_id = uuid.UUID(first.json()["id"])

    before = book(world)
    again = post_reverse(
        api_client,
        world.headers,
        movement_id=original_id,
        reason="Wrong account again",
        expect=None,
    )
    assert again.status_code == 409
    assert error_code(again) == "ALREADY_REVERSED"
    assert error_details(again)["reversal_movement_id"] == str(mirror_id)

    undo_mirror = post_reverse(
        api_client,
        world.headers,
        movement_id=mirror_id,
        reason="Undo the reversal",
        expect=None,
    )
    assert undo_mirror.status_code == 409
    assert error_code(undo_mirror) == "CASH_MOVEMENT_NOT_REVERSIBLE"
    assert error_details(undo_mirror)["reference_type"] == "REVERSAL"
    assert book(world) == before
    assert world.cash(AFN) == Decimal("1000")


def test_a_documents_own_movement_is_not_reversed_here(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """A shift opening and an exchange leg belong to their document: correct *it* instead."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    opening = movement_rows(
        world.database, branch_id=world.branch_id, session_id=session_id, movement_type="OPENING"
    )[0]

    refusal = post_reverse(
        api_client,
        world.headers,
        movement_id=uuid.UUID(str(opening["id"])),
        reason="Wrong count",
        expect=None,
    )
    assert refusal.status_code == 409
    assert error_code(refusal) == "CASH_MOVEMENT_NOT_REVERSIBLE"
    assert error_details(refusal)["reference_type"] == "OPENING_BALANCE"
    assert afn == world.money(AFN).id


def test_a_reversal_that_the_drawer_cannot_afford_is_refused_whole(
    cash_counter: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """Undoing a receipt takes money out: if it is no longer there, nothing moves."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    capital = account_id_of(world.database, CAPITAL)
    received = post_in(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1000",
        source_account_id=capital,
        session_id=session_id,
    )
    post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="1900",
        target_account_id=capital,
        session_id=session_id,
    )
    assert world.cash(AFN) == Decimal("100")

    before = book(world)
    entry = read_one(
        world.database,
        "SELECT reference_type, reference_id, reversal_of_id FROM journal_entries WHERE id = :id",
        id=received.json()["journal_entry_id"],
    )
    refusal = post_reverse(
        api_client,
        world.headers,
        movement_id=uuid.UUID(received.json()["id"]),
        reason="The customer took the cash back",
        expect=None,
    )
    assert refusal.status_code == 409
    assert error_code(refusal) == "INSUFFICIENT_BALANCE"
    assert error_details(refusal)["shortfall"] == "900.0000000000"
    assert book(world) == before
    # Nothing of the reversal was written: no mirror entry, no compensating movement.
    assert (
        read_one(
            world.database,
            "SELECT COUNT(*) AS total FROM journal_entries WHERE reversal_of_id = :id",
            id=uuid.UUID(received.json()["journal_entry_id"]),
        )["total"]
        == 0
    )
    assert entry["reference_type"] == "CASH_MOVEMENT"


def test_a_cashier_cannot_reverse_a_movement(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers: dict[str, str],
    make_user: object,
    provisioned_device: object,
) -> None:
    """Reversal is a correction: it needs ``cash.adjust``, which a cashier does not hold."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    paid = post_out(
        api_client,
        world.headers,
        branch_id=world.branch_id,
        currency_id=afn,
        amount="50",
        target_account_id=account_id_of(world.database, SALARIES),
        session_id=session_id,
    )
    origin = movement_row(world.database, uuid.UUID(paid.json()["id"]))["reference_id"]

    cashier = make_user(roles=("CASHIER",))  # type: ignore[operator]
    device_uuid = provisioned_device(assigned_branch=str(world.branch_id))  # type: ignore[operator]
    tokens = login(
        api_client,
        str(cashier["username"]),
        str(cashier["password"]),
        device_uuid=uuid.UUID(device_uuid),
        branch_id=world.branch_id,
    ).json()
    headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

    denied = post_reverse(
        api_client,
        headers,
        movement_id=uuid.UUID(paid.json()["id"]),
        reason="Not mine to reverse",
        expect=None,
    )
    assert denied.status_code == 403
    assert error_code(denied) == "PERMISSION_DENIED"
    assert error_details(denied)["required_permission"] == "cash.adjust"
    assert (
        movements_for_reference(world.database, reference_type="REVERSAL", reference_id=origin)
        == []
    )


# ------------------------------------------------------- service-level contracts
def test_the_service_refuses_a_movement_without_a_counter_account(
    cash_counter: ExchangeWorld,
) -> None:
    """``CASH_COUNTER_ACCOUNT_REQUIRED`` is the service's own contract, not just the schema's.

    The HTTP schema makes the field mandatory, so the code is only reachable from the service
    API — which is exactly where a later phase would call it from. It is tested here so the
    rule cannot be dropped when someone "tidies" the schema.
    """

    async def scenario(cash: Any) -> Any:
        world = cash_counter
        session_id = await _open(cash, world)
        return await cash.record_in(
            actor=world.actor(),
            request=_movement_request(world, session_id=session_id, counter_account_id=None),
            idempotency_key=uuid.uuid4(),
        )

    with pytest.raises(CashCounterAccountRequiredError) as caught:
        run_cash(cash_counter.database, scenario)
    assert str(caught.value.code) == "CASH_COUNTER_ACCOUNT_REQUIRED"
    assert caught.value.http_status == 422
    assert caught.value.details["fields"][0]["code"] == "required"


def _movement_request(
    world: ExchangeWorld,
    *,
    session_id: uuid.UUID,
    counter_account_id: Any,
    amount: str = "10",
    movement_type: str = "IN",
):
    """The service's own request object, built here so the guard can be reached directly."""
    from app.services.cash_service import CashMovementRequest

    return CashMovementRequest(
        branch_id=world.branch_id,
        currency_id=world.money(AFN).id,
        amount=Decimal(amount),
        session_id=session_id,
        device_id=world.device_id,
        counter_account_id=counter_account_id,
        description=f"A {movement_type} that must never be posted",
    )


async def _open(cash: Any, world: ExchangeWorld) -> uuid.UUID:
    """A shift declared at 1,000 AFN on an unfunded drawer: the only cash in it."""
    return await service_open_session_id(cash, world, openings={AFN: "1000"})


def test_the_service_refuses_a_payout_beyond_the_position(
    cash_counter: ExchangeWorld,
) -> None:
    """The same guard at the service door, with the shortfall in the error's details."""
    world = cash_counter

    async def scenario(cash: Any) -> Any:
        session_id = await _open(cash, world)
        return await cash.record_out(
            actor=world.actor(),
            request=_movement_request(
                world,
                session_id=session_id,
                counter_account_id=world.account("opening"),
                amount="1000000",
                movement_type="OUT",
            ),
            idempotency_key=uuid.uuid4(),
        )

    with pytest.raises(InsufficientBalanceError) as caught:
        run_cash(world.database, scenario)
    assert str(caught.value.code) == "INSUFFICIENT_BALANCE"
    assert caught.value.details["shortfall"] == "999000.0000000000"
    assert caught.value.details["reason"] == "QUANTITY_EXCEEDED"
    assert world.cash(AFN) == Decimal("1000")  # nothing moved


def test_the_service_records_a_receipt_and_its_expected_amount(
    exchange_world: ExchangeWorld,
) -> None:
    """A service-level receipt reconciles exactly like the HTTP one (the closed set of doors)."""
    world = exchange_world

    async def scenario(cash: Any) -> Any:
        session_id = await _open(cash, world)
        await service_in(cash, world, code=AFN, amount="250", session_id=session_id)
        await service_out(cash, world, code=AFN, amount="100", session_id=session_id)
        return await service_in(cash, world, code=AFN, amount="10", session_id=session_id)

    result = run_cash(world.database, scenario)
    assert result.status_code == 201
    assert world.cash(AFN) == Decimal("1160")
    assert world.ledger(AFN) == Decimal("1160")
    assert position(world.database, branch_id=world.branch_id, currency_code=AFN) == Decimal("1160")


def test_a_confined_operator_cannot_move_another_branchs_cash(
    cash_counter: ExchangeWorld,
    api_client: TestClient,
    admin_headers: dict[str, str],
    main_database: str,
    make_user: object,
    provisioned_device: object,
) -> None:
    """Branch scope is enforced before anything else: another branch's till is out of reach."""
    world = cash_counter
    afn = world.money(AFN).id
    session_id = open_shift(world, api_client, AFN="1000")
    before = book(world)

    other = build_world(api_client, admin_headers, main_database, currencies=(AFN,))
    try:
        teller = make_user(roles=("CASHIER",))  # type: ignore[operator]
        device_uuid = provisioned_device(assigned_branch=str(other.branch_id))  # type: ignore[operator]
        tokens = login(
            api_client,
            str(teller["username"]),
            str(teller["password"]),
            device_uuid=uuid.UUID(device_uuid),
            branch_id=other.branch_id,
        ).json()
        confined = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))

        refusal = post_in(
            api_client,
            confined,
            branch_id=world.branch_id,
            currency_id=afn,
            amount="100",
            source_account_id=account_id_of(world.database, CAPITAL),
            session_id=session_id,
            expect=None,
        )
        assert refusal.status_code == 403
        assert error_code(refusal) == "FORBIDDEN_SCOPE"
        assert book(world) == before
        assert world.cash(AFN) == Decimal("1000")
    finally:
        retire(api_client, admin_headers, other)
