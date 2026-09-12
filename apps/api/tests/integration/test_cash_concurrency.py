"""Phase 6 — cash races on the real database (PART 48, §9.4, §11, §14, §18).

Every test here starts two or more callers that *really* run at the same time: each gets its
own engine, its own connection and its own transaction on one event loop, and they contend
inside PostgreSQL — the shift row, the drawer's inventory account, the movement's own row and
the unique indexes decide the outcome. Nothing is mocked and nothing is serialised by the
test: the only thing that can stop two payouts from overdrawing one till is the engine.

What the suite pins down:

* **a position is never observably negative**: two payouts that cannot both fit leave one
  winner, one refusal carrying the shortfall, and a drawer equal to the ledger's own reading
  of the same fact;
* a receipt and a payout racing on one drawer net out to the arithmetic sum of what moved;
* **idempotency under concurrency**: one key raced by four callers records one movement and
  answers all four with the recorded bytes (§14);
* **a shift closes once**: two closes race, one changes the status, the loser writes nothing
  and leaves the position alone;
* **a movement reverses once**: the winner's mirror entry and compensating movement restore
  the drawer exactly, and the loser is refused rather than posting a second mirror;
* two drawers move in parallel without touching each other's currency or position;
* opposite directions between one drawer and one account do not deadlock (the engine locks
  ``(account_id, currency_id)`` in order) and both land;
* a storm of mixed directions never leaves the till out of step with the books;
* two opens on one drawer leave exactly one open shift and one opening movement.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.core.audit_actions import AuditAction
from app.core.exceptions import NexusError
from tests.cash_helpers import (
    audit_rows,
    ledger_quantity,
    movement_rows,
    position,
    publish,
    read,
    run_cash,
    run_cash_race,
    service_close,
    service_in,
    service_open_session_id,
    service_out,
    service_reverse,
    session_row,
)
from tests.exchange_helpers import ExchangeWorld, account_id_of

pytestmark = [pytest.mark.integration, pytest.mark.cash]

AFN = "AFN"
USD = "USD"
SALARIES = "5000"

# What an unfunded drawer is declared at when a shift opens on it: the opening entry posts
# it, so this is the whole position the races contend for.
OPENING = "1000"


# --------------------------------------------------------------------------- assertions
def refusal_code(outcome: object) -> str | None:
    return str(outcome.code) if isinstance(outcome, NexusError) else None


def refusal_details(outcome: object) -> dict[str, Any]:
    return dict(getattr(outcome, "details", None) or {})


def succeeded(outcomes: list[object]) -> list[Any]:
    return [item for item in outcomes if not isinstance(item, BaseException)]


def refusals(outcomes: list[object]) -> list[BaseException]:
    return [item for item in outcomes if isinstance(item, BaseException)]


def assert_no_negative_position(world: ExchangeWorld, *codes: str) -> None:
    """The two independent readings of one fact, both non-negative (§11)."""
    for code in codes:
        assert world.cash(code) >= 0, f"{code} physical position went negative"
        assert world.ledger(code) >= 0, f"{code} ledger position went negative"


def assert_drawer_agrees_with_the_books(world: ExchangeWorld, *codes: str) -> None:
    """The till and the ledger must be the same number, not two numbers that rhyme."""
    for code in codes:
        assert world.cash(code) == world.ledger(code), (
            f"{code}: movements say {world.cash(code)}, ledger says {world.ledger(code)}"
        )
        assert ledger_quantity(
            world.database, branch_id=world.branch_id, account_id=world.drawer(code)
        ) == world.ledger(code), code


def open_shift(world: ExchangeWorld, **declared: str) -> uuid.UUID:
    """Open a shift before a race, so the race is about the movement, not the opening."""

    async def scenario(cash: Any) -> uuid.UUID:
        return await service_open_session_id(cash, world, openings=dict(declared))

    return run_cash(world.database, scenario)


def open_carried_shift(world: ExchangeWorld) -> uuid.UUID:
    """Open a shift on a drawer the books already fund (no opening movement, no declaration)."""

    async def scenario(cash: Any) -> uuid.UUID:
        return await service_open_session_id(cash, world, openings={})

    return run_cash(world.database, scenario)


def sessions_of(world: ExchangeWorld) -> list[dict[str, Any]]:
    return read(
        world.database,
        "SELECT id, status, closed_at FROM cash_sessions WHERE branch_id = :branch_id"
        " ORDER BY opened_at, id",
        branch_id=world.branch_id,
    )


def entry_count(world: ExchangeWorld) -> int:
    return int(
        read(
            world.database,
            "SELECT COUNT(*) AS total FROM journal_entries WHERE branch_id = :branch_id",
            branch_id=world.branch_id,
        )[0]["total"]
    )


def move_in(world: ExchangeWorld, session_id: uuid.UUID, amount: str, **kwargs: Any) -> Any:
    async def scenario(cash: Any) -> Any:
        return await service_in(
            cash, world, code=AFN, amount=amount, session_id=session_id, **kwargs
        )

    return scenario


def move_out(world: ExchangeWorld, session_id: uuid.UUID, amount: str, **kwargs: Any) -> Any:
    async def scenario(cash: Any) -> Any:
        return await service_out(
            cash, world, code=AFN, amount=amount, session_id=session_id, **kwargs
        )

    return scenario


# ------------------------------------------------------------------- limited position
def test_two_payouts_racing_for_one_drawer_leave_exactly_one_winner(
    exchange_world: ExchangeWorld,
) -> None:
    """Both callers want most of the till: the second is refused with the shortfall, after
    the first has committed — which is only true if the check happens under the lock."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    before = entry_count(world)
    salaries = account_id_of(world.database, SALARIES)

    outcomes = run_cash_race(
        world.database,
        move_out(world, session_id, "600", counter_account_id=salaries),
        move_out(world, session_id, "600", counter_account_id=salaries),
    )

    winners = succeeded(outcomes)
    losers = refusals(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert len(losers) == 1
    assert refusal_code(losers[0]) == "INSUFFICIENT_BALANCE"
    details = refusal_details(losers[0])
    assert details["shortfall"] == "200.0000000000"
    assert details["reason"] == "QUANTITY_EXCEEDED"

    # One payout left the till; the loser left no movement and no entry.
    assert world.cash(AFN) == Decimal("400")
    assert entry_count(world) == before + 1
    # The two payouts raced: their commit order is the database's business, so the book is
    # read as what it recorded, not as the order a clock happened to produce.
    rows = movement_rows(world.database, branch_id=world.branch_id)
    assert sorted(row["movement_type"] for row in rows) == ["OPENING", "OUT"]
    assert_no_negative_position(world, AFN)
    assert_drawer_agrees_with_the_books(world, AFN)
    assert position(world.database, branch_id=world.branch_id, currency_code=AFN) == Decimal("400")


def test_a_receipt_and_a_payout_racing_on_one_drawer_net_out_exactly(
    exchange_world: ExchangeWorld,
) -> None:
    """Opposite directions are not a race: both land and the till holds the arithmetic sum."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    salaries = account_id_of(world.database, SALARIES)

    outcomes = run_cash_race(
        world.database,
        move_in(world, session_id, "500"),
        move_out(world, session_id, "300", counter_account_id=salaries),
    )

    assert refusals(outcomes) == [], [getattr(item, "code", item) for item in outcomes]
    assert world.cash(AFN) == Decimal("1200")
    assert world.ledger(AFN) == Decimal("1200")
    rows = movement_rows(world.database, branch_id=world.branch_id)
    assert sorted((row["movement_type"], Decimal(str(row["signed_amount"]))) for row in rows) == [
        ("IN", Decimal("500")),
        ("OPENING", Decimal("1000")),
        ("OUT", Decimal("-300")),
    ]
    assert_no_negative_position(world, AFN)
    assert_drawer_agrees_with_the_books(world, AFN)


def test_a_storm_of_mixed_directions_never_leaves_the_till_out_of_step(
    exchange_world: ExchangeWorld,
) -> None:
    """Six callers, one drawer: whatever the order, the two readings of the position agree."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    salaries = account_id_of(world.database, SALARIES)

    outcomes = run_cash_race(
        world.database,
        move_in(world, session_id, "200"),
        move_in(world, session_id, "300"),
        move_in(world, session_id, "700"),
        move_out(world, session_id, "1200", counter_account_id=salaries),
        move_out(world, session_id, "900", counter_account_id=salaries),
        move_out(world, session_id, "100", counter_account_id=salaries),
    )

    # Whatever the database decided, a refusal may only ever be "not enough cash" — and it has
    # to say which of the two shapes it is. A payout that reaches a till another payout has just
    # emptied finds no position at all (``NO_POSITION``: there is no shortfall to state), while
    # one that finds some cash and not enough of it is a ``QUANTITY_EXCEEDED`` carrying the
    # shortfall. Which one arrives depends on the interleaving, so the invariant is the contract's
    # shape (exactly one of the two, with its own evidence) rather than a particular winner.
    for outcome in refusals(outcomes):
        assert refusal_code(outcome) == "INSUFFICIENT_BALANCE", outcome
        details = refusal_details(outcome)
        assert Decimal(str(details["disposing_quantity"])) > 0, details
        if details["reason"] == "NO_POSITION":
            assert "shortfall" not in details, details
            assert Decimal(str(details["foreign_quantity"])) == 0, details
        else:
            assert details["reason"] == "QUANTITY_EXCEEDED", details
            assert Decimal(str(details["foreign_quantity"])) > 0, details
            assert Decimal(str(details["shortfall"])) > 0, details
    assert len(succeeded(outcomes)) >= 1

    rows = movement_rows(world.database, branch_id=world.branch_id)
    book = sum((Decimal(str(row["signed_amount"])) for row in rows), Decimal(0))
    assert book == world.cash(AFN)
    assert_no_negative_position(world, AFN)
    assert_drawer_agrees_with_the_books(world, AFN)


# ------------------------------------------------------------------------- idempotency
def test_duplicate_receipts_under_one_key_post_once(exchange_world: ExchangeWorld) -> None:
    """Four callers, one key, one payload: one movement, and four identical answers (§14)."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    key = uuid.uuid4()

    outcomes = run_cash_race(
        world.database,
        *[move_in(world, session_id, "200", key=key) for _ in range(4)],
    )

    assert refusals(outcomes) == [], [getattr(item, "code", item) for item in outcomes]
    payloads = [dict(item.payload) for item in outcomes]
    assert len({payload["id"] for payload in payloads}) == 1
    assert all(payload == payloads[0] for payload in payloads)

    rows = movement_rows(world.database, branch_id=world.branch_id)
    assert len(rows) == 2  # the opening and the receipt, not four receipts
    assert world.cash(AFN) == Decimal("1200")
    assert_drawer_agrees_with_the_books(world, AFN)
    claims = read(world.database, "SELECT * FROM idempotency_keys WHERE key = :key", key=key)
    assert len(claims) == 1
    assert claims[0]["status"] == "COMPLETED"
    assert claims[0]["response_body"]["id"] == payloads[0]["id"]


# ---------------------------------------------------------------------------- lifecycle
def test_two_closes_of_one_shift_close_it_once(exchange_world: ExchangeWorld) -> None:
    """Two closes race: one changes the status, the loser posts nothing at all."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    run_cash(
        world.database,
        move_in(world, session_id, "250"),
    )
    before_entries = entry_count(world)

    async def close_once(cash: Any) -> Any:
        return await service_close(cash, world, session_id=session_id, counted={AFN: "1250"})

    outcomes = run_cash_race(world.database, close_once, close_once)

    winners = succeeded(outcomes)
    losers = refusals(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert len(losers) == 1
    assert refusal_code(losers[0]) == "CASH_SESSION_NOT_OPEN"
    assert refusal_details(losers[0])["status"] == "CLOSED"
    assert str(refusal_details(losers[0])["closed_at"]).endswith("Z")

    stored = session_row(world.database, session_id)
    assert stored["status"] == "CLOSED"
    assert stored["closed_at"] is not None
    assert len(sessions_of(world)) == 1  # one shift, closed once
    assert entry_count(world) == before_entries  # no variance: nothing was posted twice
    assert world.cash(AFN) == Decimal("1250")
    assert_drawer_agrees_with_the_books(world, AFN)
    closes = audit_rows(
        world.database, entity_id=session_id, action=AuditAction.CASH_SESSION_CLOSED
    )
    assert len(closes) == 1
    assert closes[0]["new_data"]["status"] == "CLOSED"


def test_two_reversals_of_one_movement_compensate_once(
    exchange_world: ExchangeWorld,
) -> None:
    """The mirror is posted once: the loser is refused, and the drawer comes back exactly."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    salaries = account_id_of(world.database, SALARIES)
    paid = run_cash(
        world.database,
        move_out(world, session_id, "400", counter_account_id=salaries),
    )
    movement_id = uuid.UUID(str(paid.movement_id))
    before_entries = entry_count(world)

    async def reverse_once(cash: Any) -> Any:
        return await service_reverse(cash, world, movement_id=movement_id, reason="Recorded twice")

    outcomes = run_cash_race(world.database, reverse_once, reverse_once)

    winners = succeeded(outcomes)
    losers = refusals(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert len(losers) == 1
    assert refusal_code(losers[0]) == "ALREADY_REVERSED"

    rows = movement_rows(world.database, branch_id=world.branch_id)
    assert [row["movement_type"] for row in rows] == ["OPENING", "OUT", "IN"]
    assert [row["reference_type"] for row in rows] == [
        "OPENING_BALANCE",
        "CASH_MOVEMENT",
        "REVERSAL",
    ]
    # One mirror entry, and the drawer is exactly where it started.
    assert entry_count(world) == before_entries + 1
    assert world.cash(AFN) == Decimal("1000")
    assert_no_negative_position(world, AFN)
    assert_drawer_agrees_with_the_books(world, AFN)
    assert position(world.database, branch_id=world.branch_id, currency_code=AFN) == Decimal("1000")


def test_two_opens_of_one_drawer_leave_one_shift(exchange_world: ExchangeWorld) -> None:
    """The drawer has one shift: the second open is refused and posts no opening entry."""
    world = exchange_world

    async def open_once(cash: Any) -> Any:
        return await service_open_session_id(cash, world, openings={AFN: OPENING})

    outcomes = run_cash_race(world.database, open_once, open_once)

    winners = succeeded(outcomes)
    losers = refusals(outcomes)
    assert len(winners) == 1, [getattr(item, "code", item) for item in outcomes]
    assert len(losers) == 1
    assert refusal_code(losers[0]) == "CASH_SESSION_ALREADY_OPEN"

    stored = sessions_of(world)
    assert len(stored) == 1
    assert stored[0]["status"] == "OPEN"
    rows = movement_rows(world.database, branch_id=world.branch_id)
    assert [row["movement_type"] for row in rows] == ["OPENING"]
    assert world.cash(AFN) == Decimal("1000")
    assert_drawer_agrees_with_the_books(world, AFN)


# ------------------------------------------------------------- independent drawers
def test_two_drawers_move_in_parallel_without_touching_each_other(
    exchange_world: ExchangeWorld, api_client: TestClient, admin_headers: dict[str, str]
) -> None:
    """One receipt in AFN and one in USD: both land, each in its own position."""
    world = exchange_world
    world.fund((AFN, "1000", "1"), (USD, "1000", "70"))
    # A receipt in a foreign currency is valued by the house's own quote (§6.4): the counter
    # publishes USD/AFN before it can take dollars over the counter.
    publish(world, api_client, admin_headers, USD, buy="70", sell="71")
    session_id = open_carried_shift(world)

    async def receive_usd(cash: Any) -> Any:
        return await service_in(cash, world, code=USD, amount="100", session_id=session_id)

    outcomes = run_cash_race(
        world.database,
        move_in(world, session_id, "500"),
        receive_usd,
    )

    assert refusals(outcomes) == [], [getattr(item, "code", item) for item in outcomes]
    assert world.cash(AFN) == Decimal("1500")
    assert world.cash(USD) == Decimal("1100")
    # A currency is never summed with another: each drawer reconciles on its own.
    assert_drawer_agrees_with_the_books(world, AFN, USD)
    assert_no_negative_position(world, AFN, USD)
    # The movement book adds up per currency: every currency is its own book, and the two
    # never meet in an aggregate.
    totals: dict[str, Decimal] = {}
    for row in movement_rows(world.database, branch_id=world.branch_id):
        code = str(row["currency_code"])
        totals[code] = totals.get(code, Decimal(0)) + Decimal(str(row["signed_amount"]))
    assert totals == {"AFN": Decimal("1500"), "USD": Decimal("1100")}


def test_opposite_directions_between_one_drawer_and_one_account_do_not_deadlock(
    exchange_world: ExchangeWorld,
) -> None:
    """The engine locks ``(account_id, currency_id)`` in order, so a two-way race still runs:
    both callers touch the drawer and the same counter account, and neither waits for ever."""
    world = exchange_world
    session_id = open_shift(world, AFN=OPENING)
    salaries = account_id_of(world.database, SALARIES)

    outcomes = run_cash_race(
        world.database,
        move_in(world, session_id, "500", counter_account_id=salaries),
        move_out(world, session_id, "400", counter_account_id=salaries),
    )

    assert refusals(outcomes) == [], [
        (getattr(item, "code", None), str(item)[:120]) for item in outcomes
    ]
    assert world.cash(AFN) == Decimal("1100")
    # 1,000 declared + 500 received - 400 paid, and the expense account agrees.
    balance = read(
        world.database,
        """
        SELECT COALESCE(SUM(l.debit - l.credit), 0) AS balance
          FROM journal_lines l
          JOIN journal_entries e ON e.id = l.journal_entry_id
         WHERE e.branch_id = :branch_id AND l.account_id = :account_id
        """,
        branch_id=world.branch_id,
        account_id=salaries,
    )[0]["balance"]
    assert Decimal(str(balance)) == Decimal("-100")
    assert_drawer_agrees_with_the_books(world, AFN)
