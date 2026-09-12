"""Phase 5 — races on the real database (PART 48, §11, §14, §15, §18).

Every test here starts two or more callers that *really* run at the same time: each gets its
own engine, its own connection and its own transaction on one event loop, and they contend
inside PostgreSQL. Nothing is mocked and nothing is serialized by the test — if the engine's
locking, its guards or its unique indexes were wrong, these tests would produce two sales of
the same cash, two documents under one idempotency key, or two rows sharing a document number.

What the suite pins down:

* **no negative position is ever observable**: two sales of a drawer that only holds one of
  them leave exactly one winner, a zero (never negative) balance, a balanced ledger and a
  cash position identical to the ledger's own reading of the same fact;
* limited inventory on both sides of a deal — two BUYs whose AFN payouts cannot both fit;
* a BUY and a SELL of the same currency racing each other: both land and the net position is
  the arithmetic sum of what they moved;
* two customers, one currency: the customer is identity, not inventory;
* two branches: each races inside its own drawers, and neither touches the other's cash;
* **idempotency under concurrency**: one key raced by four callers records one deal, all four
  answers are the recorded bytes, and none of them is a second posting (§14);
* a repeated offline event converges on one document;
* **numbering under concurrency**: distinct, contiguous numbers per branch, with no commit
  twice and no number wasted on a loser (§18);
* **lifecycle under concurrency**: a cancel and a reverse race for the same document, and the
  money comes back exactly once, whichever wins;
* and the losers of a race leave no document, no movement and no entry behind (§13).
"""

from __future__ import annotations

import uuid
from decimal import Decimal

import pytest

from app.core.audit_actions import AuditAction
from app.core.exceptions import NexusError
from tests.exchange_helpers import (
    REFERENCE_TYPE_EXCHANGE,
    REFERENCE_TYPE_REVERSAL,
    ExchangeWorld,
    audit_rows_for_entity,
    build_world,
    count,
    create_customer,
    number_suffix,
    retire,
    scalar,
)

pytestmark = [pytest.mark.integration, pytest.mark.exchange]

AFN = "AFN"
USD = "USD"
EUR = "EUR"
START_AFN = Decimal("5000000")
START_USD = Decimal("20000")
START_EUR = Decimal("20000")


def _signature(items: list[dict[str, object]]) -> list[tuple[str, str, Decimal]]:
    """What a set of movements did: account, currency and the signed quantity, comparable."""
    return sorted(
        (
            str(item["account_id"]),
            str(item["currency_id"]),
            Decimal(str(item["signed_amount"])),
        )
        for item in items
    )


def _undo_of(items: list[dict[str, object]]) -> list[tuple[str, str, Decimal]]:
    """The signature of the movements that would undo the given ones, sign flipped."""
    return sorted(
        (
            str(item["account_id"]),
            str(item["currency_id"]),
            -Decimal(str(item["signed_amount"])),
        )
        for item in items
    )


def refusal_code(outcome: object) -> str | None:
    return str(outcome.code) if isinstance(outcome, NexusError) else None


def refusal_details(outcome: object) -> dict[str, object]:
    return dict(getattr(outcome, "details", None) or {})


def succeeded(outcomes: list[object]) -> list[object]:
    return [item for item in outcomes if not isinstance(item, BaseException)]


def refusals(outcomes: list[object]) -> list[BaseException]:
    return [item for item in outcomes if isinstance(item, BaseException)]


def assert_no_negative_position(world: ExchangeWorld) -> None:
    """The two independent readings of one fact, both non-negative (§11)."""
    for code in (AFN, USD, EUR):
        assert world.cash(code) >= 0, f"{code} cash went negative"
        assert world.ledger(code) >= 0, f"{code} ledger went negative"


def assert_cash_equals_ledger(world: ExchangeWorld) -> None:
    for code in (AFN, USD, EUR):
        assert world.cash(code) == world.ledger(code), code


# ------------------------------------------------------------------ limited inventory
def test_two_sales_racing_for_one_drawer_leave_exactly_one_winner(
    quoted_counter: ExchangeWorld,
) -> None:
    """Both callers want the whole drawer: the second must be refused, not overdrawn."""
    world = quoted_counter
    before = world.state()
    outcomes = world.race(
        world.create_scenario(
            transaction_type="SELL", from_amount="20000", exchange_rate="71", commission="0"
        ),
        world.create_scenario(
            transaction_type="SELL", from_amount="20000", exchange_rate="71", commission="0"
        ),
    )

    winners = succeeded(outcomes)
    losers = refusals(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert len(losers) == 1
    assert refusal_code(losers[0]) == "INSUFFICIENT_BALANCE"
    details = refusal_details(losers[0])
    assert details["reason"] in {"NO_POSITION", "QUANTITY_EXCEEDED"}
    assert details["account_id"] == str(world.drawer(USD))

    # The winner's arithmetic, read from the positions rather than from its own answer.
    assert world.cash(USD) == Decimal("0")
    assert world.cash(AFN) == START_AFN + Decimal("1420000")
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)
    assert world.state().balanced
    # One document, one entry, one movement pair — and nothing from the loser.
    after = world.state()
    assert (
        after.documents - before.documents,
        after.entries - before.entries,
        after.movements - before.movements,
    ) == (1, 1, 2)


def test_two_sales_that_each_fit_but_not_together_leave_the_remainder(
    quoted_counter: ExchangeWorld,
) -> None:
    """12,000 of a 20,000 drawer twice: one lands, 8,000 stays, nothing goes below zero."""
    world = quoted_counter
    outcomes = world.race(
        world.create_scenario(
            transaction_type="SELL", from_amount="12000", exchange_rate="71", commission="0"
        ),
        world.create_scenario(
            transaction_type="SELL", from_amount="12000", exchange_rate="71", commission="0"
        ),
    )

    assert len(succeeded(outcomes)) == 1
    assert refusal_code(refusals(outcomes)[0]) == "INSUFFICIENT_BALANCE"
    assert world.cash(USD) == Decimal("8000")
    assert world.cash(AFN) == START_AFN + Decimal("852000")
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)


def test_two_buys_racing_for_the_functional_drawer_leave_exactly_one_winner(
    quoted_counter: ExchangeWorld,
) -> None:
    """The payout side is limited too: two 2,800,000 AFN payouts cannot both come from 5M."""
    world = quoted_counter
    outcomes = world.race(
        world.create_scenario(
            transaction_type="BUY", from_amount="40000", exchange_rate="70", commission="0"
        ),
        world.create_scenario(
            transaction_type="BUY", from_amount="40000", exchange_rate="70", commission="0"
        ),
    )

    assert len(succeeded(outcomes)) == 1
    loser = refusals(outcomes)[0]
    assert refusal_code(loser) == "INSUFFICIENT_BALANCE"
    assert refusal_details(loser)["account_id"] == str(world.drawer(AFN))
    assert world.cash(AFN) == START_AFN - Decimal("2800000")
    assert world.cash(USD) == START_USD + Decimal("40000")
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)


def test_a_buy_and_a_sell_of_the_same_currency_both_land_and_net_out(
    quoted_counter: ExchangeWorld,
) -> None:
    """Opposite directions are not a race: they are two deals, and both are recorded."""
    world = quoted_counter
    before = world.state()
    outcomes = world.race(
        world.create_scenario(
            transaction_type="BUY", from_amount="1000", exchange_rate="70", commission="0"
        ),
        world.create_scenario(
            transaction_type="SELL", from_amount="1000", exchange_rate="71", commission="0"
        ),
    )

    assert len(refusals(outcomes)) == 0, [getattr(item, "code", item) for item in outcomes]
    assert len({item.transaction_id for item in outcomes}) == 2  # two documents, not one
    assert world.cash(USD) == START_USD  # +1,000 bought, -1,000 sold
    assert world.cash(AFN) == START_AFN + Decimal("1000")  # -70,000 paid, +71,000 received
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)
    after = world.state()
    assert (after.documents - before.documents, after.entries - before.entries) == (2, 2)
    assert after.movements - before.movements == 4
    assert after.balanced


def test_racing_deals_for_the_same_customer_are_both_recorded(
    quoted_counter: ExchangeWorld, api_client, admin_headers
) -> None:
    """A customer is identity, not inventory: two deals for them are simply two deals."""
    world = quoted_counter
    customer = create_customer(
        api_client, admin_headers, full_name="Racing Customer", branch_id=str(world.branch_id)
    )
    customer_id = uuid.UUID(str(customer["id"]))
    outcomes = world.race(
        world.create_scenario(
            transaction_type="BUY",
            from_amount="100",
            exchange_rate="70",
            commission="0",
            customer_id=customer_id,
        ),
        world.create_scenario(
            transaction_type="SELL",
            from_amount="100",
            exchange_rate="71",
            commission="0",
            customer_id=customer_id,
        ),
    )

    assert len(refusals(outcomes)) == 0
    for outcome in outcomes:
        assert outcome.payload["customer_id"] == str(customer_id)
        assert outcome.payload["customer_name"] == "Racing Customer"
    assert (
        count(
            world.database,
            "exchange_transactions",
            where="customer_id = :customer_id",
            customer_id=customer_id,
        )
        == 2
    )
    assert_cash_equals_ledger(world)


def test_two_branches_racing_never_touch_each_others_cash(
    quoted_counter: ExchangeWorld, api_client, admin_headers, main_database: str
) -> None:
    """Branch isolation is not a lock: two counters trade at once and stay separate (§11)."""
    first = quoted_counter
    second = build_world(api_client, admin_headers, main_database)
    try:
        second.fund_all()
        second.quote(api_client, admin_headers)
        outcomes = second.race(
            first.create_scenario(
                transaction_type="SELL", from_amount="5000", exchange_rate="71", commission="0"
            ),
            second.create_scenario(
                transaction_type="SELL", from_amount="5000", exchange_rate="71", commission="0"
            ),
        )
        assert len(refusals(outcomes)) == 0, [getattr(item, "code", item) for item in outcomes]

        for world in (first, second):
            assert world.cash(USD) == START_USD - Decimal("5000")
            assert world.cash(AFN) == START_AFN + Decimal("355000")
            assert_no_negative_position(world)
            assert_cash_equals_ledger(world)
            assert (
                count(
                    world.database,
                    "exchange_transactions",
                    where="branch_id = :branch",
                    branch=world.branch_id,
                )
                == 1
            )
        # One series per day, issued by the frozen atomic counter: two branches trading at the
        # same instant still receive two different numbers, and both carry the branch's day.
        numbers = {
            world.view(outcome.transaction_id).transaction_number
            for world, outcome in zip((first, second), outcomes, strict=True)
        }
        assert len(numbers) == 2
        assert all(number.startswith("NX-") for number in numbers)
    finally:
        retire(api_client, admin_headers, second)


# ------------------------------------------------------------------------ idempotency
def test_one_key_raced_by_four_callers_records_one_deal(quoted_counter: ExchangeWorld) -> None:
    """§14: the same key at the same time produces one posting and four identical answers."""
    world = quoted_counter
    before = world.state()
    key = uuid.uuid4()
    outcomes = world.race(
        *(
            world.create_scenario(
                transaction_type="BUY",
                from_amount="1000",
                exchange_rate="70",
                commission="500",
                idempotency_key=key,
            )
            for _ in range(4)
        )
    )

    assert len(refusals(outcomes)) == 0, [getattr(item, "code", item) for item in outcomes]
    first_payload = outcomes[0].payload
    # Byte-identical: every caller receives the recorded answer, not a re-rendered one. The
    # message names the field that differs, because a replay that drifts by one field is
    # exactly the bug this assertion exists to catch.
    for item in outcomes:
        differing = {
            key: (first_payload.get(key), item.payload.get(key))
            for key in set(first_payload) | set(item.payload)
            if first_payload.get(key) != item.payload.get(key)
        }
        assert differing == {}, {"replayed": item.replayed, "fields": differing}
    assert len({item.transaction_id for item in outcomes}) == 1
    assert sum(1 for item in outcomes if item.replayed) == 3
    after = world.state()
    assert (
        after.documents - before.documents,
        after.entries - before.entries,
        after.movements - before.movements,
    ) == (1, 1, 2)
    assert world.cash(AFN) == START_AFN - Decimal("69500")
    assert world.cash(USD) == START_USD + Decimal("1000")
    assert world.state().balanced
    # The key holds the answer that was actually posted, and it is the only row for it.
    stored = world.idempotency(key)
    assert stored["status"] == "COMPLETED"
    assert stored["endpoint"] == "exchange:create"
    assert count(world.database, "idempotency_keys", where="key = :key", key=key) == 1


def test_one_offline_event_raced_by_two_callers_produces_one_document(
    quoted_counter: ExchangeWorld,
) -> None:
    """§19/PART 34: a duplicate event converges on the document it already produced."""
    world = quoted_counter
    before = world.state()
    event_id = uuid.uuid4()
    outcomes = world.race(
        world.create_scenario(
            transaction_type="BUY",
            from_amount="1000",
            exchange_rate="70",
            commission="0",
            client_event_id=event_id,
        ),
        world.create_scenario(
            transaction_type="BUY",
            from_amount="1000",
            exchange_rate="70",
            commission="0",
            client_event_id=event_id,
        ),
    )

    assert len(succeeded(outcomes)) >= 1
    for loser in refusals(outcomes):
        # A racing duplicate loses the unique index on ``client_event_id`` instead of
        # replaying (the winner has not committed yet, so there is nothing to replay *from*);
        # the refusal is clean, and the client's very next attempt replays the document.
        assert refusal_code(loser) in {"DUPLICATE_RESOURCE", "CONFLICT"}, loser
    assert (
        count(
            world.database,
            "exchange_transactions",
            where="client_event_id = :event_id",
            event_id=event_id,
        )
        == 1
    )
    assert world.cash(USD) == START_USD + Decimal("1000")
    assert world.cash(AFN) == START_AFN - Decimal("70000")
    assert world.state().balanced
    after = world.state()
    assert (after.documents - before.documents, after.entries - before.entries) == (1, 1)


def test_a_replayed_deal_after_a_race_never_posts_twice(quoted_counter: ExchangeWorld) -> None:
    """The retry a client actually performs: send the same key again, receive the same deal."""
    world = quoted_counter
    before = world.state()
    key = uuid.uuid4()
    outcomes = world.race(
        world.create_scenario(
            transaction_type="BUY",
            from_amount="1000",
            exchange_rate="70",
            commission="0",
            idempotency_key=key,
        ),
        world.create_scenario(
            transaction_type="BUY",
            from_amount="1000",
            exchange_rate="70",
            commission="0",
            idempotency_key=key,
        ),
    )
    assert len(refusals(outcomes)) == 0
    raced = world.state()
    assert (
        raced.documents - before.documents,
        raced.entries - before.entries,
        raced.movements - before.movements,
    ) == (1, 1, 2)

    replay = world.create(
        transaction_type="BUY",
        from_amount="1000",
        exchange_rate="70",
        commission="0",
        idempotency_key=key,
    )
    assert replay.replayed is True
    assert replay.transaction_id == outcomes[0].transaction_id
    assert world.state().documents == raced.documents


# -------------------------------------------------------------------------- numbering
def test_racing_deals_in_one_branch_get_distinct_contiguous_numbers(
    quoted_counter: ExchangeWorld,
) -> None:
    """§18: the counter is atomic, gap-tolerant, and never committed twice."""
    world = quoted_counter
    outcomes = world.race(
        *(
            world.create_scenario(
                transaction_type="BUY", from_amount="10", exchange_rate="70", commission="0"
            )
            for _ in range(5)
        )
    )

    assert len(refusals(outcomes)) == 0, [getattr(item, "code", item) for item in outcomes]
    numbers = sorted(str(item.transaction_number) for item in outcomes)
    suffixes = [number_suffix(number) for number in numbers]
    assert len(set(numbers)) == 5
    assert suffixes == list(range(suffixes[0], suffixes[0] + 5))  # contiguous, no duplicates
    assert all(number.startswith(numbers[0][:11]) for number in numbers)  # one business day
    # The branch's counter is at least the highest number it issued in this period.
    counter = scalar(
        world.database,
        "SELECT current_value FROM sequences WHERE name = :name",
        name=f"exchange_transaction:{numbers[0].split('-')[1]}",
    )
    assert int(counter) >= suffixes[-1]


def test_two_branches_number_their_own_deals_in_parallel(
    quoted_counter: ExchangeWorld, api_client, admin_headers, main_database: str
) -> None:
    """Per-branch counters: neither branch's issuance can collide with the other's (§18)."""
    first = quoted_counter
    second = build_world(api_client, admin_headers, main_database)
    try:
        second.fund_all()
        second.quote(api_client, admin_headers)
        outcomes = second.race(
            first.create_scenario(
                transaction_type="BUY", from_amount="10", exchange_rate="70", commission="0"
            ),
            *(
                second.create_scenario(
                    transaction_type="BUY", from_amount="10", exchange_rate="70", commission="0"
                )
                for _ in range(2)
            ),
        )
        assert len(refusals(outcomes)) == 0
        suffixes = sorted(number_suffix(str(item.transaction_number)) for item in outcomes)
        # One series per business day: three deals in two branches, three distinct numbers,
        # issued without a gap even though three transactions raced for the same counter.
        assert len(set(suffixes)) == 3
        assert suffixes == list(range(suffixes[0], suffixes[0] + 3))
        # And each document belongs to the branch that recorded it.
        for world, outcome in zip((first, *([second] * 2)), outcomes, strict=True):
            assert world.view(outcome.transaction_id).branch_id == world.branch_id
    finally:
        retire(api_client, admin_headers, second)


# -------------------------------------------------------------------------- lifecycle
def test_a_cancel_and_a_reverse_racing_give_one_undo(quoted_counter: ExchangeWorld) -> None:
    """Two undos of one document: one wins, one is refused, and the money comes back once."""
    world = quoted_counter
    created = world.create(
        transaction_type="BUY", from_amount="1000", exchange_rate="70", commission="0"
    )
    document_id = created.transaction_id
    outcomes = world.race(
        world.cancel_scenario(document_id, reason="racing cancel"),
        world.reverse_scenario(document_id, reason="racing reverse"),
    )

    winners = succeeded(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert refusal_code(refusals(outcomes)[0]) in {
        "INVALID_STATUS_TRANSITION",
        "ALREADY_REVERSED",
        "REVERSAL_NOT_UNDOABLE",
    }
    row = world.document(document_id)
    assert row["status"] in {"CANCELLED", "REVERSED"}
    assert world.cash(USD) == START_USD
    assert world.cash(AFN) == START_AFN
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)
    # Exactly one undo exists for the document, whichever one won the race.
    assert (
        count(
            world.database,
            "journal_entries",
            where="reference_type = 'REVERSAL' AND branch_id = :branch",
            branch=world.branch_id,
        )
        == 1
    )
    # ...and the physical undo is one pair of movements, in whichever of the two shapes the
    # model allows: a *cancellation* reverses the deal in place, so its reversing movements are
    # recorded against the document itself, while a *reversal* is a mirror document that owns
    # its own pair for the swapped direction. The race decides which one wins; what must hold
    # either way is that the branch holds **one** undo — exactly the opposite of the deal on the
    # same accounts and currencies — and never the other shape as well, which would have moved
    # the money twice.
    posted = world.movements(reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=document_id)
    assert len(posted) == 2
    mirror_id = scalar(
        world.database,
        "SELECT id FROM exchange_transactions WHERE reversal_of_id = :document_id",
        document_id=document_id,
    )
    if mirror_id is None:
        assert row["status"] == "CANCELLED"
        undo = world.movements(reference_type=REFERENCE_TYPE_REVERSAL, reference_id=document_id)
    else:
        assert row["status"] == "REVERSED"
        assert (
            count(
                world.database,
                "cash_movements",
                where="reference_type = :kind AND reference_id = :document",
                kind=REFERENCE_TYPE_REVERSAL,
                document=document_id,
            )
            == 0
        )
        undo = world.movements(
            reference_type=REFERENCE_TYPE_EXCHANGE, reference_id=uuid.UUID(str(mirror_id))
        )
    assert len(undo) == 2
    assert _undo_of(undo) == _signature(posted)
    assert world.state().balanced


def test_the_losers_of_a_race_leave_no_document_no_movement_and_no_entry(
    quoted_counter: ExchangeWorld,
) -> None:
    """A refused race must not leave a fragment behind (§13: no partial state after failure)."""
    world = quoted_counter
    before = world.state()
    outcomes = world.race(
        world.create_scenario(
            transaction_type="SELL", from_amount="20000", exchange_rate="71", commission="0"
        ),
        world.create_scenario(
            transaction_type="SELL", from_amount="20000", exchange_rate="71", commission="0"
        ),
        world.create_scenario(
            transaction_type="SELL", from_amount="30000", exchange_rate="71", commission="0"
        ),
    )
    winners = succeeded(outcomes)
    assert len(winners) == 1
    assert len(refusals(outcomes)) == 2

    after = world.state()
    assert (after.documents, after.entries, after.movements) == (
        before.documents + 1,
        before.entries + 1,
        before.movements + 2,
    )
    winner_entry = uuid.UUID(str(winners[0].payload["journal_entry_id"]))
    assert after.lines == before.lines + len(world.entry(winner_entry)[1])
    assert after.balanced
    assert_no_negative_position(world)
    assert_cash_equals_ledger(world)
    # One EXCHANGE_CREATED audit row for the winner, and none for a deal that never happened.
    rows = audit_rows_for_entity(world.database, uuid.UUID(str(winners[0].transaction_id)))
    assert [row["action"] for row in rows] == [AuditAction.EXCHANGE_CREATED]
