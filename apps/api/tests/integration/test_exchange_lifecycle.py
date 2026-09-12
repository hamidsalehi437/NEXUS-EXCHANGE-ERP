"""Phase 5 — the lifecycle: cancel, reverse, receipt, listing, and the rules around them.

A deal is never edited and never deleted (PART 22). It is *undone*, and the way it is undone
is the whole subject of this suite:

* ``cancel`` posts the ledger's mirror entry and writes the mirror movement pair under a
  ``REVERSAL`` reference: the document goes to ``CANCELLED``, the money goes back, and there is
  deliberately *no* replacement document — the deal simply did not happen.
* ``reverse`` additionally creates a **mirror document** (the frozen ``NEX04`` trigger requires
  the reversing row to swap the currencies and carry each amount to the other side), with its
  own number, its own movement pair and the ledger's reversal entry as its posting, while the
  original moves to ``REVERSED`` with ``reversed_at``/``reversed_by`` and the entry that undid
  it.
* the state machine is closed: a cancelled document cannot be undone, a reversed document can
  only be answered with ``ALREADY_REVERSED``, and a reversal document cannot be undone at all —
  that would put the money back while the original stayed marked as undone.
* the tables themselves refuse the rest: a posted document cannot be edited (``NEX06``, the
  money columns are frozen) and cannot be deleted (``P0001``, history is append-only).

Reads are part of the lifecycle too: the receipt is deterministic (the same document renders
the same payload, from committed rows, including the rate that was applied), the list is
branch-scoped and filterable, and a document outside the caller's scope is refused rather than
answered — whether another branch traded at all is itself information.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app.core.audit_actions import AuditAction
from app.core.exceptions import NexusError, sqlstate_of
from app.core.permissions import Permission, RoleName
from app.schemas.exchange import ExchangeDocument, ExchangeReceipt
from tests.accounting_helpers import create_account, create_currency, unique_code
from tests.auth_helpers import bearer, login
from tests.exchange_helpers import (
    REFERENCE_TYPE_EXCHANGE,
    REFERENCE_TYPE_REVERSAL,
    ExchangeWorld,
    audit_rows_for_action,
    build_world,
    count,
    create_customer,
    db_refusal,
    error_code,
    number_suffix,
    post_exchange,
    retire,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.exchange]

AFN = "AFN"
USD = "USD"


def buy(world: ExchangeWorld, **fields: object):
    return world.create(transaction_type="BUY", **fields)


def refusal_code(outcome: object) -> str | None:
    assert isinstance(outcome, NexusError), outcome
    return str(outcome.code)


def refusal_details(outcome: object) -> dict[str, object]:
    assert isinstance(outcome, NexusError), outcome
    return dict(outcome.details or {})


def _fresh_currency_code() -> str:
    """A three-letter currency code that cannot collide with the currencies in the chart."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    token = uuid.uuid4().int
    return "".join(alphabet[(token >> (5 * index)) % 26] for index in range(3))


def as_decimal(value: object) -> Decimal:
    """Compare money by value: the database renders zero as ``0E-10``, not ``0``."""
    return Decimal(str(value))


def mirror_id_of(world: ExchangeWorld, original: uuid.UUID) -> uuid.UUID:
    """The id of the one reversing document of a reversed deal (``NEX04``: at most one)."""
    value = scalar(
        world.database,
        "SELECT id FROM exchange_transactions WHERE reversal_of_id = :id",
        id=original,
    )
    assert value is not None, f"no mirror document for {original}"
    return uuid.UUID(str(value))


# ------------------------------------------------------------------------- cancellation
def test_a_cancel_reverses_the_entry_and_the_drawer(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    created = buy(world, from_amount="1000", exchange_rate="70", commission="500")
    document = ExchangeDocument.model_validate(created.payload)

    result = world.cancel(created.transaction_id, reason="customer handed over the wrong notes")
    cancelled = ExchangeDocument.model_validate(result.payload)

    assert result.status_code == 200
    assert cancelled.id == document.id
    assert cancelled.status == "CANCELLED"
    assert cancelled.reversal_journal_entry_id is not None
    assert cancelled.reversal_journal_entry_id != cancelled.journal_entry_id
    assert cancelled.reversal_reason == "customer handed over the wrong notes"
    # A cancellation leaves no trace *inside* the document's own frozen columns: the reason
    # lives on the reversal entry (its description) and in the audit row, and the deal was
    # never "reversed" — nothing was handed back as a document.
    row = world.document(document.id)
    assert row["reversed_at"] is None
    assert row["reversed_by"] is None
    assert row["reversal_of_id"] is None
    assert row["reversal_journal_entry_id"] == uuid.UUID(str(cancelled.reversal_journal_entry_id))
    # A cancellation creates **no** replacement document: only the ledger and the money moved.
    assert row["reversal_of_id"] is None
    assert (
        count(
            world.database,
            "exchange_transactions",
            where="reversal_of_id = :id",
            id=document.id,
        )
        == 0
    )

    # The ledger posted the mirror of the original entry: same accounts, every side swapped.
    original_entry, original_lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    reversal_entry, reversal_lines = world.entry(
        uuid.UUID(str(cancelled.reversal_journal_entry_id))
    )
    assert reversal_entry["reference_type"] == REFERENCE_TYPE_REVERSAL
    assert reversal_entry["reference_id"] == original_entry["id"]
    assert reversal_entry["reversal_of_id"] == original_entry["id"]
    assert reversal_entry["branch_id"] == world.branch_id
    assert reversal_entry["description"] == "customer handed over the wrong notes"
    assert len(reversal_lines) == len(original_lines)
    mirrored = {row["account_id"]: row for row in reversal_lines}
    for line in original_lines:
        counterpart = mirrored[line["account_id"]]
        assert as_decimal(counterpart["debit"]) == as_decimal(line["credit"])
        assert as_decimal(counterpart["credit"]) == as_decimal(line["debit"])
        assert as_decimal(counterpart["foreign_amount"]) == as_decimal(line["foreign_amount"])

    # The drawers moved back: the AFN we paid out came in, the USD we took in went out.
    movements = world.movements(reference_type=REFERENCE_TYPE_REVERSAL, reference_id=document.id)
    assert [(m["movement_type"], m["currency_code"]) for m in movements] == [
        ("IN", AFN),
        ("OUT", USD),
    ]
    assert [as_decimal(m["amount"]) for m in movements] == [
        Decimal("69500"),
        Decimal("1000"),
    ]
    assert {m["journal_entry_id"] for m in movements} == {reversal_entry["id"]}
    assert {m["branch_id"] for m in movements} == {world.branch_id}
    assert world.cash(AFN) == Decimal("5000000")
    assert world.cash(USD) == Decimal("20000")
    assert world.ledger(AFN) == world.cash(AFN)
    assert world.ledger(USD) == world.cash(USD)

    # Audit: creation then cancellation, with the reversal entry and the reason (§17).
    rows = world.audit(document.id)
    assert [row["action"] for row in rows] == [
        AuditAction.EXCHANGE_CREATED,
        AuditAction.EXCHANGE_CANCELLED,
    ]
    assert rows[1]["new_data"]["reason"] == "customer handed over the wrong notes"
    assert rows[1]["new_data"]["reversal_journal_entry_id"] == str(
        cancelled.reversal_journal_entry_id
    )
    assert rows[1]["new_data"]["journal_total_debit"] == "70000.0000000000"
    assert rows[1]["user_id"] == world.head_user_id
    assert rows[1]["entity_type"] == "exchange_transaction"


def test_a_cancelled_document_is_never_deleted(quoted_counter: ExchangeWorld) -> None:
    """PART 22/49: cancelled ≠ deleted. The row, its entry and its movements all remain."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    world.cancel(created.transaction_id, reason="mis-keyed amount")

    row = world.document(created.transaction_id)
    assert row["status"] == "CANCELLED"
    assert row["journal_entry_id"] is not None
    assert world.entry(uuid.UUID(str(row["journal_entry_id"])))[0]["id"] is not None
    # The deal's own pair *and* the reversal's pair, under one document id.
    assert (
        count(
            world.database,
            "cash_movements",
            where="reference_id = :id",
            id=created.transaction_id,
        )
        == 4
    )


def test_a_cancel_is_idempotent_under_its_key(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    key = uuid.uuid4()
    first = world.cancel(created.transaction_id, reason="recount", idempotency_key=key)
    entries = count(world.database, "journal_entries")
    replay = world.cancel(created.transaction_id, reason="recount", idempotency_key=key)

    assert replay.payload == first.payload
    assert replay.status_code == 200
    assert count(world.database, "journal_entries") == entries
    assert world.idempotency(key)["endpoint"] == "exchange:cancel"
    # One reversal entry for the document, not two — and the second call posted nothing.
    assert (
        count(
            world.database,
            "journal_entries",
            where="branch_id = :branch AND reference_type = 'REVERSAL'",
            branch=world.branch_id,
        )
        == 1
    )


def test_a_document_can_only_be_undone_once(quoted_counter: ExchangeWorld) -> None:
    """The closed state machine: COMPLETED → CANCELLED/REVERSED, and nothing after that."""
    world = quoted_counter
    cancelled = buy(world, from_amount="100", exchange_rate="70", commission="0")
    reversed_one = buy(world, from_amount="100", exchange_rate="70", commission="0")
    world.cancel(cancelled.transaction_id, reason="first undo")
    world.reverse(reversed_one.transaction_id, reason="second undo")

    cases = (
        ("cancel a cancelled document", world.try_cancel(cancelled.transaction_id), "cancel"),
        ("reverse a cancelled document", world.try_reverse(cancelled.transaction_id), "reverse"),
        ("cancel a reversed document", world.try_cancel(reversed_one.transaction_id), "cancel"),
    )
    for label, outcome, operation in cases:
        assert refusal_code(outcome) == "INVALID_STATUS_TRANSITION", label
        details = refusal_details(outcome)
        assert details["allowed_statuses"] == ["COMPLETED"]
        assert details["operation"] == operation
        assert details["status"] in {"CANCELLED", "REVERSED"}
    second_reverse = world.try_reverse(reversed_one.transaction_id)
    assert refusal_code(second_reverse) == "ALREADY_REVERSED"
    assert refusal_details(second_reverse)["status"] == "REVERSED"

    # Nothing was posted twice: exactly two REVERSAL entries, one per undone document.
    assert (
        count(
            world.database,
            "journal_entries",
            where="reference_type = 'REVERSAL' AND branch_id = :branch",
            branch=world.branch_id,
        )
        == 2
    )


def test_a_reversal_document_cannot_itself_be_undone(quoted_counter: ExchangeWorld) -> None:
    """Undoing the undo would restore the money while the original stayed ``REVERSED``."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    world.reverse(created.transaction_id, reason="customer changed their mind")
    mirror_id = mirror_id_of(world, created.transaction_id)

    before = world.state()
    for operation, outcome in (
        ("cancel", world.try_cancel(mirror_id, reason="undo the undo")),
        ("reverse", world.try_reverse(mirror_id, reason="undo the undo")),
    ):
        assert refusal_code(outcome) == "REVERSAL_NOT_UNDOABLE", operation
        details = refusal_details(outcome)
        assert details["reversal_of_id"] == str(created.transaction_id)
        assert details["operation"] == operation
    # The original stays REVERSED and the money stays where the reversal put it.
    assert world.document(created.transaction_id)["status"] == "REVERSED"
    assert world.state().book == before.book
    assert world.cash(AFN) == Decimal("5000000")
    assert world.cash(USD) == Decimal("20000")


def test_a_posted_document_cannot_be_edited_or_deleted(quoted_counter: ExchangeWorld) -> None:
    """Two frozen guards, two different SQLSTATEs (§13, PART 62)."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")

    # The money columns are frozen: a later edit is refused by ``NEX06``.
    for statement in (
        "UPDATE exchange_transactions SET from_amount = 999 WHERE id = :id",
        "UPDATE exchange_transactions SET exchange_rate = 999 WHERE id = :id",
        "UPDATE exchange_transactions SET commission = 1 WHERE id = :id",
        "UPDATE exchange_transactions SET transaction_number = 'X' WHERE id = :id",
    ):
        error = db_refusal(world.database, statement, id=created.transaction_id)
        assert sqlstate_of(error) == "NEX06", statement
    # A deal is history: it cannot be removed at all (the append-only guard).
    delete_error = db_refusal(
        world.database,
        "DELETE FROM exchange_transactions WHERE id = :id",
        id=created.transaction_id,
    )
    assert sqlstate_of(delete_error) == "P0001"

    row = world.document(created.transaction_id)
    assert row["from_amount"] == Decimal("100.0000000000")
    assert row["exchange_rate"] == Decimal("70.0000000000")
    assert row["transaction_number"] == created.payload["transaction_number"]


# ---------------------------------------------------------------------------- reversal
def test_a_reverse_creates_the_mirror_document_the_trigger_requires(
    quoted_counter: ExchangeWorld,
) -> None:
    """``NEX04``: same branch and type, currencies swapped, each amount carried across."""
    world = quoted_counter
    created = buy(world, from_amount="1000", exchange_rate="70", commission="500")
    document = ExchangeDocument.model_validate(created.payload)
    result = world.reverse(created.transaction_id, reason="bank rejected the deposit")
    original = ExchangeDocument.model_validate(result.payload)

    assert result.status_code == 200
    assert original.id == document.id
    assert original.status == "REVERSED"
    assert original.reversed_at is not None
    assert original.reversed_by == world.head_user_id
    assert original.reversal_reason == "bank rejected the deposit"
    assert original.reversal_transaction_id is not None
    assert original.reversal_transaction_id != original.id
    assert original.reversal_journal_entry_id != original.journal_entry_id
    # The mirror is a document of its own: its own number, issued right after the original's.
    assert number_suffix(str(original.reversal_transaction_number)) == (
        number_suffix(document.transaction_number) + 1
    )

    mirror = ExchangeDocument.model_validate(
        world.view(uuid.UUID(str(original.reversal_transaction_id))).to_payload()
    )
    assert mirror.id != document.id
    assert mirror.transaction_number == original.reversal_transaction_number
    assert mirror.status == "COMPLETED"
    assert mirror.reversal_of_id == document.id
    assert mirror.reversal_reason == "bank rejected the deposit"
    assert mirror.transaction_type == document.transaction_type
    assert mirror.branch_id == document.branch_id
    assert mirror.from_currency_code == document.to_currency_code
    assert mirror.to_currency_code == document.from_currency_code
    assert mirror.from_amount == document.to_amount  # 69,500 AFN
    assert mirror.to_amount == document.from_amount  # 1,000 USD
    assert mirror.from_amount == "69500.0000000000"
    # The mirror charges nothing: a reversal is not a new deal (§8: no hidden fees).
    assert mirror.commission == "0.0000000000"
    # Its rate is the deal's own effective rate (from ÷ to), quantized once.
    assert mirror.exchange_rate == "0.0143884892"
    # Its posting is the ledger's reversal entry of the original — not a second exchange.
    assert mirror.journal_entry_id == original.reversal_journal_entry_id
    assert mirror.journal_total_debit == mirror.journal_total_credit == "70000.0000000000"
    assert [(m.movement_type, m.currency_code) for m in mirror.cash_movements] == [
        ("IN", AFN),
        ("OUT", USD),
    ]
    assert [as_decimal(m.amount) for m in mirror.cash_movements] == [
        Decimal("69500"),
        Decimal("1000"),
    ]


def test_a_reverse_moves_both_drawers_back_and_squares_the_ledger(
    quoted_counter: ExchangeWorld,
) -> None:
    world = quoted_counter
    created = world.create(
        transaction_type="SELL", from_amount="500", exchange_rate="71", commission="200"
    )
    document = ExchangeDocument.model_validate(created.payload)
    result = world.reverse(created.transaction_id, reason="customer brought the dollars back")
    reversed_document = ExchangeDocument.model_validate(result.payload)
    mirror_id = mirror_id_of(world, document.id)

    movements = world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=mirror_id)
    assert [(m["movement_type"], m["currency_code"]) for m in movements] == [
        ("IN", USD),
        ("OUT", AFN),
    ]
    assert [as_decimal(m["amount"]) for m in movements] == [
        Decimal("500"),
        Decimal("35500"),
    ]
    assert {m["account_id"] for m in movements} == {world.drawer(USD), world.drawer(AFN)}

    # The mirror's entry is the ledger's own reversal of the original: same lines, swapped.
    original_entry, original_lines = world.entry(uuid.UUID(str(document.journal_entry_id)))
    mirror_entry, mirror_lines = world.entry(
        uuid.UUID(str(reversed_document.reversal_journal_entry_id))
    )
    assert mirror_entry["reference_type"] == REFERENCE_TYPE_REVERSAL
    assert mirror_entry["reversal_of_id"] == original_entry["id"]
    assert mirror_entry["reference_id"] == original_entry["id"]
    swapped = {row["account_id"]: row for row in mirror_lines}
    assert len(swapped) == len(original_lines)
    for line in original_lines:
        counterpart = swapped[line["account_id"]]
        assert as_decimal(counterpart["debit"]) == as_decimal(line["credit"])
        assert as_decimal(counterpart["credit"]) == as_decimal(line["debit"])

    # Everything is back where it started — cash and ledger, quantity for quantity.
    assert world.cash(USD) == Decimal("20000")
    assert world.cash(AFN) == Decimal("5000000")
    assert world.ledger(USD) == Decimal("20000")
    assert world.ledger(AFN) == Decimal("5000000")
    assert world.state().balanced


def test_a_reverse_is_audited_on_both_documents(quoted_counter: ExchangeWorld) -> None:
    """Two documents, two histories: the reversal is recorded on the mirror as well."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    world.reverse(created.transaction_id, reason="duplicate entry on the terminal")
    mirror_id = mirror_id_of(world, created.transaction_id)

    original_rows = world.audit(created.transaction_id)
    assert [row["action"] for row in original_rows] == [
        AuditAction.EXCHANGE_CREATED,
        AuditAction.EXCHANGE_REVERSED,
    ]
    reversal = original_rows[1]["new_data"]
    assert reversal["reason"] == "duplicate entry on the terminal"
    assert reversal["reversal_transaction_id"] == str(mirror_id)
    assert reversal["reversal_transaction_number"] is not None
    assert reversal["reversal_journal_entry_id"] == reversal["reversal_journal_entry_id"]
    assert reversal["journal_total_debit"] == "7000.0000000000"
    assert reversal["reversed_at"] is not None
    assert original_rows[1]["user_id"] == world.head_user_id

    mirror_rows = world.audit(mirror_id)
    assert [row["action"] for row in mirror_rows] == [AuditAction.EXCHANGE_CREATED]
    created_row = mirror_rows[0]["new_data"]
    assert created_row["role"] == "REVERSAL_DOCUMENT"
    assert created_row["reversal_of_id"] == str(created.transaction_id)
    assert created_row["reverses_transaction_number"] == created.payload["transaction_number"]
    assert created_row["reversal_reason"] == "duplicate entry on the terminal"
    assert created_row["commission"] == "0.0000000000"
    assert created_row["journal_entry_id"] == reversal["reversal_journal_entry_id"]
    assert created_row["journal_total_debit"] == created_row["journal_total_credit"]


def test_a_reverse_is_idempotent_under_its_key(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    key = uuid.uuid4()
    first = world.reverse(created.transaction_id, reason="reversal retry", idempotency_key=key)
    documents = count(world.database, "exchange_transactions")
    replay = world.reverse(created.transaction_id, reason="reversal retry", idempotency_key=key)
    assert replay.payload == first.payload
    assert count(world.database, "exchange_transactions") == documents
    assert world.idempotency(key)["endpoint"] == "exchange:reverse"


# ------------------------------------------------------------------- receipt and reads
def test_the_receipt_is_deterministic_and_reflects_the_lifecycle(
    quoted_counter: ExchangeWorld,
) -> None:
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="50")
    first = world.receipt(created.transaction_id)
    second = world.receipt(created.transaction_id)
    assert first == second  # byte-identical: nothing is re-derived from a moving clock

    receipt = ExchangeReceipt.model_validate(first)
    assert receipt.receipt_version == "phase5-1"
    assert receipt.status == "COMPLETED"
    assert receipt.document_id == created.transaction_id
    assert receipt.transaction_number == created.payload["transaction_number"]
    assert receipt.gross_amount == "7000.0000000000"
    assert receipt.commission == "50.0000000000"
    assert receipt.to_amount == "6950.0000000000"
    assert receipt.reversal_of_id is None
    assert [(line.movement_type, line.currency_code) for line in receipt.settlement] == [
        ("IN", USD),
        ("OUT", AFN),
    ]
    assert receipt.reversal_settlement == []
    assert receipt.branch_code == world.branch_code

    world.cancel(created.transaction_id, reason="customer walked away")
    after = world.receipt(created.transaction_id)
    assert after["status"] == "CANCELLED"
    assert after["issued_at"] == first["issued_at"]  # the document's own timestamp, not now()
    # The receipt of the *deal* still shows the deal: the money that was handed over is a fact.
    assert after["settlement"] == first["settlement"]
    assert after["exchange_rate"] == first["exchange_rate"]
    # …and the money the cancellation moved back is reported separately, never blended in.
    assert [
        (line["movement_type"], line["currency_code"]) for line in after["reversal_settlement"]
    ] == [("IN", AFN), ("OUT", USD)]
    assert [as_decimal(line["amount"]) for line in after["reversal_settlement"]] == [
        Decimal("6950"),
        Decimal("100"),
    ]


def test_the_receipt_of_a_reversal_names_what_it_reverses(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    world.reverse(created.transaction_id, reason="returned")
    mirror_id = mirror_id_of(world, created.transaction_id)

    receipt = ExchangeReceipt.model_validate(world.receipt(mirror_id))
    assert receipt.reversal_of_id == created.transaction_id
    assert receipt.status == "COMPLETED"
    assert receipt.commission == "0.0000000000"
    original = ExchangeReceipt.model_validate(world.receipt(created.transaction_id))
    assert original.status == "REVERSED"
    assert original.reversal_transaction_number == receipt.transaction_number


def test_the_receipt_endpoint_serves_json_and_refuses_an_unbuilt_format(
    http_counter: ExchangeWorld, api_client: TestClient
) -> None:
    world = http_counter
    created = buy(world, from_amount="10", exchange_rate="70", commission="0")
    ok = api_client.get(
        f"/api/v1/exchange/{created.transaction_id}/receipt",
        headers=dict(world.headers),
        params={"format": "json"},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["receipt_version"] == "phase5-1"
    # ``format=pdf`` belongs to the reporting phase; until then it is refused honestly.
    refused = api_client.get(
        f"/api/v1/exchange/{created.transaction_id}/receipt",
        headers=dict(world.headers),
        params={"format": "pdf"},
    )
    assert refused.status_code == 422, refused.text
    assert error_code(refused) == "VALIDATION_ERROR"


def test_the_list_is_branch_scoped_filterable_and_newest_first(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers, main_database: str
) -> None:
    world = quoted_counter
    customer = create_customer(
        api_client, admin_headers, full_name="Listed Customer", branch_id=str(world.branch_id)
    )
    first = buy(world, from_amount="100", exchange_rate="70", commission="0")
    second = world.create(
        transaction_type="SELL",
        from_amount="50",
        exchange_rate="71",
        commission="0",
        customer_id=uuid.UUID(str(customer["id"])),
    )
    world.cancel(second.transaction_id, reason="wrong customer")

    items, total = world.list()
    assert total == 2
    assert [item.id for item in items] == [second.transaction_id, first.transaction_id]
    items, total = world.list(status="CANCELLED")
    assert (total, [item.id for item in items]) == (1, [second.transaction_id])
    items, total = world.list(transaction_type="BUY")
    assert (total, [item.id for item in items]) == (1, [first.transaction_id])
    items, total = world.list(customer_id=uuid.UUID(str(customer["id"])))
    assert (total, [item.id for item in items]) == (1, [second.transaction_id])
    items, total = world.list(number_query=str(first.payload["transaction_number"]))
    assert (total, [item.id for item in items]) == (1, [first.transaction_id])
    items, total = world.list(limit=1)
    assert total == 2 and [item.id for item in items] == [second.transaction_id]

    # A manager scoped to *another* branch sees neither the rows nor the document. The scope
    # refusal is checked with a branch-scoped actor: the group-wide administrator may read any
    # branch by design (PART 41), so asking as the administrator would prove nothing.
    other = build_world(api_client, admin_headers, main_database)
    try:
        manager = other.actor(roles=(RoleName.MANAGER,))
        own_items, own_total = other.list(actor=manager)
        assert (own_total, own_items) == (0, [])
        with pytest.raises(NexusError) as refusal:
            other.list(actor=manager, branch_id=world.branch_id)
        assert refusal.value.code == "FORBIDDEN_SCOPE"
        # A point read answers "does not exist" (the Phase 4 read rule): telling a manager that
        # another branch's document exists is itself a disclosure.
        view_outcome = other.try_view(first.transaction_id, actor=manager)
        assert refusal_code(view_outcome) == "RESOURCE_NOT_FOUND"
        assert refusal_details(view_outcome)["resource"] == "exchange_transaction"
    finally:
        retire(api_client, admin_headers, other)


def test_the_http_endpoints_cover_the_whole_lifecycle(
    http_counter: ExchangeWorld, api_client: TestClient
) -> None:
    """The six contract endpoints, exercised over the wire in order (API_CONTRACT §9.3)."""
    world = http_counter
    created = post_exchange(
        api_client,
        world.headers,
        transaction_type="BUY",
        branch_id=world.branch_id,
        from_currency_id=world.money(USD).id,
        from_amount="10",
        to_currency_id=world.money(AFN).id,
        exchange_rate="70",
        commission="0",
    )
    body = created.json()
    document_id = body["id"]

    listed = api_client.get("/api/v1/exchange", headers=dict(world.headers))
    assert listed.status_code == 200, listed.text
    assert listed.json()["total"] >= 1
    assert any(item["id"] == document_id for item in listed.json()["items"])

    read = api_client.get(f"/api/v1/exchange/{document_id}", headers=dict(world.headers))
    assert read.status_code == 200
    assert read.json()["transaction_number"] == body["transaction_number"]
    assert read.json()["device_id"] == str(world.device_id)

    receipt = api_client.get(f"/api/v1/exchange/{document_id}/receipt", headers=dict(world.headers))
    assert receipt.status_code == 200, receipt.text
    assert receipt.json()["document_id"] == document_id

    cancelled = api_client.post(
        f"/api/v1/exchange/{document_id}/cancel",
        headers={**dict(world.headers), "Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "http cancel"},
    )
    assert cancelled.status_code == 200, cancelled.text
    assert cancelled.json()["status"] == "CANCELLED"
    assert cancelled.json()["reversal_reason"] == "http cancel"

    # A cancelled document cannot be reversed, and the endpoint says so in the envelope.
    reverse_conflict = api_client.post(
        f"/api/v1/exchange/{document_id}/reverse",
        headers={**dict(world.headers), "Idempotency-Key": str(uuid.uuid4())},
        json={"reason": "too late"},
    )
    assert reverse_conflict.status_code == 409, reverse_conflict.text
    assert error_code(reverse_conflict) == "INVALID_STATUS_TRANSITION"

    missing_key = api_client.post(
        f"/api/v1/exchange/{document_id}/cancel",
        headers=dict(world.headers),
        json={"reason": "no key"},
    )
    assert missing_key.status_code == 400
    assert error_code(missing_key) == "IDEMPOTENCY_KEY_REQUIRED"


# ------------------------------------------------------------------------------------ RBAC
def test_a_role_that_may_not_reverse_is_refused_and_the_refusal_is_audited(
    quoted_counter: ExchangeWorld, main_database: str
) -> None:
    """§16/§17: the permission check is on the service as well as the route, and it is traced."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    actor = world.actor(roles=(RoleName.ACCOUNTANT,))
    before = world.state()
    baseline = int(scalar(main_database, "SELECT COALESCE(MAX(seq), 0) FROM audit_logs"))

    outcome = world.try_reverse(created.transaction_id, reason="not my job", actor=actor)
    assert refusal_code(outcome) == "PERMISSION_DENIED"
    details = refusal_details(outcome)
    assert details["required_permission"] == str(Permission.EXCHANGE_REVERSE)
    assert details["transaction_id"] == str(created.transaction_id)
    after = world.state()
    # Money did not move: the documents, the entries, the movements and the positions are
    # identical. The one row that *is* new is the audit trail of the refusal itself (§17).
    assert (after.documents, after.movements, after.entries, after.lines) == (
        before.documents,
        before.movements,
        before.entries,
        before.lines,
    )
    assert (after.ledger_quantities, after.cash_quantities) == (
        before.ledger_quantities,
        before.cash_quantities,
    )
    assert after.audit_rows == before.audit_rows + 1
    assert world.document(created.transaction_id)["status"] == "COMPLETED"

    denials = [
        row
        for row in audit_rows_for_action(main_database, str(AuditAction.EXCHANGE_ACCESS_DENIED))
        if row["seq"] > baseline
    ]
    assert len(denials) == 1
    assert denials[0]["user_id"] == actor.user_id
    assert denials[0]["entity_id"] is None
    assert denials[0]["new_data"]["required_permission"] == str(Permission.EXCHANGE_REVERSE)
    assert denials[0]["new_data"]["reason"] == "PERMISSION_DENIED"


ROLE_MATRIX: tuple[tuple[str, int, int, int], ...] = (
    # (role, expected status for create, for cancel, for reverse)
    (RoleName.MANAGER, 201, 200, 200),
    (RoleName.ACCOUNTANT, 201, 200, 403),
    (RoleName.CASHIER, 201, 403, 403),
    (RoleName.AUDITOR, 403, 403, 403),
)


@pytest.mark.parametrize(("role", "create_status", "cancel_status", "reverse_status"), ROLE_MATRIX)
def test_the_role_matrix_on_the_lifecycle_endpoints(
    http_counter: ExchangeWorld,
    api_client: TestClient,
    make_user: object,
    provisioned_device: object,
    role: str,
    create_status: int,
    cancel_status: int,
    reverse_status: int,
) -> None:
    """Every seeded role against every money-moving endpoint (PART 41, PART 48)."""
    world = http_counter
    user = make_user(roles=(role,))  # type: ignore[operator]
    # The installation is provisioned by an administrator: only some roles may register one.
    # It is provisioned for *this* counter's branch, because a device belongs to one branch.
    device_uuid = provisioned_device(assigned_branch=str(world.branch_id))  # type: ignore[operator]
    tokens = login(
        api_client,
        str(user["username"]),
        str(user["password"]),
        device_uuid=device_uuid,
        branch_id=world.branch_id,
    ).json()
    headers = bearer(str(tokens["access_token"]), str(tokens["device"]["id"]))
    assert tokens["user"]["roles"] == [role]

    created = api_client.post(
        "/api/v1/exchange",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={
            "transaction_type": "BUY",
            "branch_id": str(world.branch_id),
            "from_currency_id": str(world.money(USD).id),
            "from_amount": "10",
            "to_currency_id": str(world.money(AFN).id),
            "exchange_rate": "70",
            "commission": "0",
        },
    )
    assert created.status_code == create_status, created.text
    if create_status != 201:
        assert error_code(created) == "PERMISSION_DENIED"

    # The administrator records a document for the lifecycle calls, so a role's refusal is
    # about its permission and never about a missing document.
    recorded = buy(world, from_amount="10", exchange_rate="70", commission="0")
    cancelled = api_client.post(
        f"/api/v1/exchange/{recorded.transaction_id}/cancel",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"reason": f"{role} cancel"},
    )
    assert cancelled.status_code == cancel_status, cancelled.text
    if cancelled.status_code == 403:
        assert error_code(cancelled) == "PERMISSION_DENIED"

    other = buy(world, from_amount="10", exchange_rate="70", commission="0")
    reversed_response = api_client.post(
        f"/api/v1/exchange/{other.transaction_id}/reverse",
        headers={**headers, "Idempotency-Key": str(uuid.uuid4())},
        json={"reason": f"{role} reverse"},
    )
    assert reversed_response.status_code == reverse_status, reversed_response.text
    if reverse_status == 403:
        assert error_code(reversed_response) == "PERMISSION_DENIED"
    # Whatever a role was allowed to do, the books still balance.
    assert world.state().balanced


def test_an_accountant_may_cancel_but_the_cancellation_is_attributed_to_them(
    quoted_counter: ExchangeWorld, main_database: str
) -> None:
    """A role that may undo must own the undo: the audit row names the actor, not the maker."""
    from tests.helpers import create_user

    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    other_user = create_user(
        main_database,
        username=f"undoer-{uuid.uuid4().hex[:10]}",
        password="An0ther!Passw0rd",
        roles=(RoleName.ACCOUNTANT,),
    )
    actor = world.actor(roles=(RoleName.ACCOUNTANT,), user_id=uuid.UUID(other_user))
    world.cancel(created.transaction_id, reason="day-end correction", actor=actor)

    rows = world.audit(created.transaction_id)
    assert rows[0]["user_id"] == world.head_user_id  # the maker
    assert rows[1]["action"] == AuditAction.EXCHANGE_CANCELLED
    assert rows[1]["user_id"] == actor.user_id  # the undoer
    assert rows[1]["user_id"] != rows[0]["user_id"]


def test_an_unauthenticated_caller_reaches_nothing(api_client: TestClient) -> None:
    key = str(uuid.uuid4())
    for method, path in (
        ("get", "/api/v1/exchange"),
        ("get", f"/api/v1/exchange/{uuid.uuid4()}"),
        ("get", f"/api/v1/exchange/{uuid.uuid4()}/receipt"),
    ):
        response = getattr(api_client, method)(path)
        assert response.status_code == 401, (method, path, response.text)
        assert error_code(response) in {"TOKEN_INVALID", "TOKEN_EXPIRED", "UNAUTHENTICATED"}
    for path in (
        "/api/v1/exchange",
        f"/api/v1/exchange/{uuid.uuid4()}/cancel",
        f"/api/v1/exchange/{uuid.uuid4()}/reverse",
    ):
        response = api_client.post(
            path, json={"reason": "anonymous"}, headers={"Idempotency-Key": key}
        )
        assert response.status_code == 401, (path, response.text)
        assert error_code(response) in {"TOKEN_INVALID", "TOKEN_EXPIRED", "UNAUTHENTICATED"}


def test_an_unknown_document_is_not_found_rather_than_invented(
    quoted_counter: ExchangeWorld,
) -> None:
    world = quoted_counter
    missing = uuid.uuid4()
    for outcome, operation in (
        (world.try_view(missing), "view"),
        (world.try_cancel(missing), "cancel"),
        (world.try_reverse(missing), "reverse"),
    ):
        assert refusal_code(outcome) == "RESOURCE_NOT_FOUND", operation
        assert refusal_details(outcome)["resource"] == "exchange_transaction"


# ----------------------------------------------------------------------------- times/dates
def test_a_backdated_deal_is_dated_by_its_own_day(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """PART 5/§7: the document, its entry and its number all carry one accounting date."""
    world = quoted_counter
    yesterday = dt.datetime.now(dt.UTC) - dt.timedelta(days=1)
    # A quote is only in force from the moment it is published, so a backdated deal needs a
    # quote that was already in force then — the engine never prices history from today's rate.
    world.quote(api_client, admin_headers, effective_at=yesterday)

    backdated = buy(
        world,
        from_amount="10",
        exchange_rate="70",
        commission="0",
        transaction_date=yesterday,
    )
    entry, _lines = world.entry(uuid.UUID(str(backdated.payload["journal_entry_id"])))
    view = world.view(backdated.transaction_id)
    assert entry["transaction_date"].date() == yesterday.date()
    # The number is issued in the *deal's* business day, not in the day the row was written.
    assert number_suffix(view.transaction_number) >= 1
    assert view.transaction_number.startswith(f"NX-{yesterday.date().strftime('%Y%m%d')}-")
    # The view reports the day the counter was working when it recorded the document; the
    # deal's own day is on the entry and in the number, which is where history reads it.
    assert view.business_date >= yesterday.date()
    stored = world.document(backdated.transaction_id)
    assert stored["created_at"].date() >= yesterday.date()


def test_a_future_deal_is_refused_before_anything_is_priced(quoted_counter: ExchangeWorld) -> None:
    world = quoted_counter
    before = world.state()
    outcome = world.try_create(
        transaction_type="BUY",
        from_amount="10",
        exchange_rate="70",
        commission="0",
        transaction_date=dt.datetime.now(dt.UTC) + dt.timedelta(hours=5),
    )
    assert refusal_code(outcome) == "VALIDATION_ERROR"
    details = refusal_details(outcome)
    assert details["fields"] == [{"field": "transaction_date", "code": "future_date"}]
    assert details["tolerated_skew_minutes"] == 120
    assert world.state() == before


def test_a_zero_rate_or_negative_amount_never_reaches_the_book(
    quoted_counter: ExchangeWorld,
) -> None:
    """Malformed numbers are answered as malformed numbers — not as a rate diagnosis (§5/§8)."""
    world = quoted_counter
    before = world.state()
    cases: tuple[tuple[dict[str, str], str, str], ...] = (
        ({"from_amount": "0"}, "from_amount", "not_positive"),
        ({"from_amount": "-100"}, "from_amount", "not_positive"),
        ({"exchange_rate": "0"}, "exchange_rate", "not_positive"),
        ({"exchange_rate": "-70"}, "exchange_rate", "not_positive"),
        ({"commission": "-5"}, "commission", "negative"),
        ({"commission": "70000"}, "commission", "exceeds_gross"),
    )
    for fields, field, code in cases:
        deal: dict[str, object] = {
            "from_amount": "1000",
            "exchange_rate": "70",
            "commission": "0",
            **fields,
        }
        outcome = world.try_create(transaction_type="BUY", **deal)
        assert refusal_code(outcome) == "VALIDATION_ERROR", fields
        assert refusal_details(outcome)["fields"][0] == {"field": field, "code": code}, fields
    assert world.state() == before
    assert world.cash(AFN) == Decimal("5000000")


def test_a_currency_deactivated_after_posting_keeps_its_documents_readable(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """Reads do not re-validate the catalogue: a posted document stays readable forever.

    The currency is created for this test and never reactivated: deactivating one of the
    shared currencies would take every later scenario's drawer with it, and a test that
    measures one thing must not edit the world for the next one.
    """
    world = quoted_counter
    code = _fresh_currency_code()
    currency = create_currency(api_client, admin_headers, code=code, decimal_places=2)
    create_account(
        api_client,
        admin_headers,
        code=unique_code(),
        name=f"Cash {code} {unique_code()}",
        account_type="ASSET",
        currency_id=str(currency["id"]),
        branch_id=str(world.branch_id),
    )
    world.quote(api_client, admin_headers, from_code=code, to_code=AFN)
    created = buy(world, from_code=code, to_code=AFN, from_amount="1000", exchange_rate="70")
    deactivated = api_client.patch(
        f"/api/v1/currencies/{currency['id']}",
        headers=dict(admin_headers),
        json={"is_active": False},
    )
    assert deactivated.status_code == 200, deactivated.text

    view = world.view(created.transaction_id)
    assert view.status == "COMPLETED"
    assert view.transaction_number == created.payload["transaction_number"]
    assert view.to_payload()["from_currency_code"] == code
    receipt = world.receipt(created.transaction_id)
    assert receipt["document_id"] == str(created.transaction_id)
    assert receipt["from_currency_code"] == code
    assert receipt["status"] == "COMPLETED"


def test_a_document_of_a_deactivated_branch_is_still_readable(
    quoted_counter: ExchangeWorld, api_client: TestClient, admin_headers
) -> None:
    """A branch is deactivated, never deleted: its documents remain part of the record."""
    world = quoted_counter
    created = buy(world, from_amount="10", exchange_rate="70", commission="0")
    api_client.patch(
        f"/api/v1/branches/{world.branch_id}",
        headers=dict(admin_headers),
        json={"is_active": False},
    )
    view = world.view(created.transaction_id)
    assert view.status == "COMPLETED"
    assert view.branch_id == world.branch_id
    assert world.document(created.transaction_id)["branch_id"] == world.branch_id


def test_the_lifecycle_never_writes_a_second_journal_entry_for_one_document(
    quoted_counter: ExchangeWorld,
) -> None:
    """One document, at most one original entry and one reversal entry (§10)."""
    world = quoted_counter
    created = buy(world, from_amount="100", exchange_rate="70", commission="0")
    entry_id = uuid.UUID(str(created.payload["journal_entry_id"]))
    world.cancel(created.transaction_id, reason="undo")
    world.try_cancel(created.transaction_id, reason="undo again")
    world.try_reverse(created.transaction_id, reason="and again")

    row = world.document(created.transaction_id)
    assert row["status"] == "CANCELLED"
    reversal_entry = uuid.UUID(str(row["reversal_journal_entry_id"]))
    assert reversal_entry != entry_id
    assert count(
        world.database,
        "journal_lines",
        where="journal_entry_id IN (:one, :two)",
        one=entry_id,
        two=reversal_entry,
    ) == 2 * len(world.entry(entry_id)[1])
    assert (
        count(
            world.database,
            "exchange_transactions",
            where="branch_id = :branch",
            branch=world.branch_id,
        )
        == 1
    )
    assert world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=row["id"])
