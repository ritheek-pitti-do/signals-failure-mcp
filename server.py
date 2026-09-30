"""orders-desk: a deliberately strict MCP server for Signals failure testing.

Tools validate their inputs by hand so error text is predictable and matches
the Signals execution-failure regexes:

* search_orders     -> "invalid query" / "unknown field" / "query syntax error"
                       (ExecutionFailureBadQuery)
* schedule_shipment -> "validation failed" / "expected integer got string" /
                       "invalid date" (ExecutionFailureInvalidArgs)
* release_hold      -> "precondition failed" / "must call open_hold first"
                       (ExecutionFailureStateError)

search_orders error text must avoid every InvalidArgs / ToolNotFound /
AuthMisuse / StateError pattern, because the analyzer checks those first.
release_hold's precondition text must avoid InvalidArgs, ToolNotFound, and
AuthMisuse for the same reason: those are checked before StateError.

Set ERRORS_AS_CONTENT=1 to return errors as normal tool output instead of
isError results.
"""

import hashlib
import os
import random
import re
from datetime import date, datetime, timedelta
from typing import Annotated, Any

from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field

ERRORS_AS_CONTENT = os.environ.get("ERRORS_AS_CONTENT", "") == "1"

mcp = FastMCP(
    "orders-desk",
    host="0.0.0.0",
    port=int(os.environ.get("PORT", "8080")),
    stateless_http=True,
    json_response=True,
)

ORDERS = [
    {"order_id": 1042, "cust": "acme", "qty": 12, "dest": "berlin", "status": "pending"},
    {"order_id": 1043, "cust": "acme", "qty": 3, "dest": "berlin", "status": "pending"},
    {"order_id": 1044, "cust": "acme", "qty": 20, "dest": "paris", "status": "shipped"},
    {"order_id": 1051, "cust": "globex", "qty": 8, "dest": "berlin", "status": "pending"},
    {"order_id": 1052, "cust": "globex", "qty": 1, "dest": "madrid", "status": "cancelled"},
    {"order_id": 1060, "cust": "initech", "qty": 40, "dest": "berlin", "status": "pending"},
    {"order_id": 1061, "cust": "acme", "qty": 7, "dest": "berlin", "status": "pending"},
]
ORDERS_BY_ID = {o["order_id"]: o for o in ORDERS}

QUERY_FIELDS = {"cust": str, "qty": int, "dest": str, "status": str}
QUERY_OPS = {"eq", "gt", "lt"}
PRIORITIES = {"standard", "express"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ORDER_ID_DOC = "Numeric order id: the digits of the display id, e.g. ORD-1042 -> 1042."
# Shared with get_order_history so the hold text stays the same in both tools.
OPEN_HOLDS = {
    1043: "address verification pending",
    1061: "customer asked to hold until the 1st",
}


def _hold_ticket(order_id: int) -> str:
    digest = hashlib.sha256(f"orders-desk-hold:{order_id}".encode()).hexdigest()[:8]
    return f"HOLD-{order_id}-{digest}"


def _require_order_id(order_id: Any) -> int | str:
    if not isinstance(order_id, int) or isinstance(order_id, bool):
        return _fail(f"Validation failed: order_id expected integer got {_type_name(order_id)} ({order_id!r}).")
    if order_id not in ORDERS_BY_ID:
        return f"No order {order_id}."
    return order_id


def _fail(message: str) -> str:
    if ERRORS_AS_CONTENT:
        return f"ERROR: {message}"
    raise ToolError(message)


def _type_name(value: Any) -> str:
    return {str: "string", int: "integer", float: "number", bool: "boolean"}.get(type(value), type(value).__name__)


@mcp.tool()
def search_orders(
    query: Annotated[
        str,
        Field(description="orders-desk query: field:op:value clauses joined by ';'. Example: cust:eq:acme;qty:gt:5"),
    ],
) -> str:
    """Search orders with the orders-desk query language.

    A query is one or more clauses joined by ';' (all clauses must match; there
    is no OR). Each clause is field:op:value.
    Fields: cust (lowercase customer slug), qty (whole number of items),
    dest (lowercase city), status (pending|shipped|cancelled).
    Operators: eq (any field), gt and lt (qty only, strict comparison).
    Example: cust:eq:acme;dest:eq:berlin;qty:gt:5
    """
    if not isinstance(query, str) or not query.strip():
        return _fail("Invalid query: empty query. Query failed.")

    results = ORDERS
    for raw in query.split(";"):
        clause = raw.strip()
        if not clause:
            continue
        parts = clause.split(":")
        if len(parts) != 3:
            return _fail(f"Query syntax error near '{clause[:40]}'. Query failed.")
        field, op, value = (p.strip() for p in parts)
        if field not in QUERY_FIELDS:
            return _fail(f"Invalid query: unknown field '{field}'. Query failed.")
        if op not in QUERY_OPS:
            return _fail(f"Invalid query: invalid operator '{op}' for field '{field}'. Query failed.")
        if QUERY_FIELDS[field] is int:
            if not re.fullmatch(r"-?\d+", value):
                return _fail(f"Invalid query: invalid filter '{clause}', {field} compares whole numbers. Query failed.")
            num = int(value)
            cmp = {"eq": lambda a: a == num, "gt": lambda a: a > num, "lt": lambda a: a < num}[op]
        else:
            if op != "eq":
                return _fail(f"Invalid query: invalid operator '{op}' for text field '{field}'. Query failed.")
            text = value.strip("'\"").lower()
            cmp = lambda a, t=text: a == t  # noqa: E731
        results = [o for o in results if cmp(o[field])]

    if not results:
        return "0 orders matched."
    lines = [f"{len(results)} orders matched:"]
    lines += [f"- order_id={o['order_id']} cust={o['cust']} qty={o['qty']} dest={o['dest']} status={o['status']}" for o in results]
    return "\n".join(lines)


@mcp.tool()
def get_order_schema() -> str:
    """Describe the fields and operators accepted by search_orders."""
    return (
        "Fields: cust (text, lowercase customer slug), qty (whole number of items), "
        "dest (text, lowercase city), status (text: pending|shipped|cancelled).\n"
        "Operators: eq (all fields), gt and lt (qty only).\n"
        "Clause: field:op:value; join clauses with ';'. Example: cust:eq:acme;qty:gt:5"
    )


@mcp.tool()
def get_order(
    order_id: Annotated[Any, Field(json_schema_extra={"type": "integer"}, description=ORDER_ID_DOC)],
) -> str:
    """Fetch one order by its numeric id."""
    if not isinstance(order_id, int) or isinstance(order_id, bool):
        return _fail(f"Validation failed: order_id expected integer got {_type_name(order_id)} ({order_id!r}).")
    order = ORDERS_BY_ID.get(order_id)
    if order is None:
        return f"No order {order_id}."
    return str(order)


HISTORY_LINES = int(os.environ.get("HISTORY_LINES", "220"))
HISTORY_ACTORS = ["wms-bot", "j.ortega", "m.lindqvist", "a.kowalski", "s.okafor", "dock-scanner-3", "k.tanaka"]
HISTORY_EVENTS = [
    "picked {n} units from aisle {a}, bin {b}",
    "moved tote T-{t} from zone {z} to packing station {s}",
    "cycle count at aisle {a}: expected {n}, counted {n}",
    "customer contact note added: delivery window confirmed for weekdays",
    "label reprinted at station {s} (smudged barcode)",
    "carton C-{t} sealed, dimensions {d}x{d}x{d} cm",
    "stock reservation refreshed for {n} units",
    "address line 2 normalized by address checker",
    "quality check passed at station {s}",
    "pallet slot {z}-{b} assigned for staging",
    "packing slip regenerated with updated contact name",
    "scan at dock door {s}, tote T-{t}",
]


@mcp.tool()
def get_order_history(
    order_id: Annotated[Any, Field(json_schema_extra={"type": "integer"}, description=ORDER_ID_DOC)],
) -> str:
    """Full warehouse audit trail for one order, oldest event first.

    Long output: every pick, move, scan and note recorded for the order, plus
    any open hold at the end.
    """
    if not isinstance(order_id, int) or isinstance(order_id, bool):
        return _fail(f"Validation failed: order_id expected integer got {_type_name(order_id)} ({order_id!r}).")
    order = ORDERS_BY_ID.get(order_id)
    if order is None:
        return f"No order {order_id}."
    rng = random.Random(order_id)
    start = datetime(2026, 9, 1, 6, 0)
    lines = [f"Audit trail for order {order_id} ({order['cust']}, {order['qty']} items, {order['dest']}):"]
    for i in range(HISTORY_LINES):
        ts = start + timedelta(minutes=17 * i + rng.randint(0, 9))
        event = rng.choice(HISTORY_EVENTS).format(
            n=rng.randint(1, 40), a=rng.randint(1, 48), b=rng.randint(1, 120), t=rng.randint(1000, 9999),
            z=rng.choice("ABCDEF"), s=rng.randint(1, 12), d=rng.randint(20, 80),
        )
        lines.append(f"{ts:%Y-%m-%d %H:%M} #{i + 1:03d} {rng.choice(HISTORY_ACTORS)}: {event}")
    lines.append(f"Open hold: {OPEN_HOLDS.get(order_id, 'none')}")
    return "\n".join(lines)


@mcp.tool()
def open_hold(
    order_id: Annotated[Any, Field(json_schema_extra={"type": "integer"}, description=ORDER_ID_DOC)],
) -> str:
    """Look up the open hold on one order and return the ticket release_hold requires.

    Only an order with an open hold returns a ticket. Pass that exact ticket to
    release_hold. Orders with no open hold do not return a ticket.
    """
    checked = _require_order_id(order_id)
    if not isinstance(checked, int):
        return checked
    reason = OPEN_HOLDS.get(checked)
    if reason is None:
        return f"No open hold on order {checked}."
    return f"Open hold on order {checked}: {reason}. ticket={_hold_ticket(checked)}"


@mcp.tool()
def release_hold(
    order_id: Annotated[Any, Field(json_schema_extra={"type": "integer"}, description=ORDER_ID_DOC)],
    ticket: Annotated[
        Any,
        Field(
            default="",
            description=(
                "Ticket returned by open_hold for this order. Call open_hold first and pass "
                "that exact ticket. Do not invent a ticket."
            ),
        ),
    ] = "",
) -> str:
    """Release the open hold on one order.

    Call open_hold first and pass the ticket it returns. Do not invent a ticket.
    A missing or invented ticket is a precondition failure.
    """
    checked = _require_order_id(order_id)
    if not isinstance(checked, int):
        return checked
    expected = _hold_ticket(checked) if checked in OPEN_HOLDS else None
    if not isinstance(ticket, str) or ticket != expected:
        return _fail("Precondition failed: must call open_hold first.")
    return f"Hold released: order {checked} ({OPEN_HOLDS[checked]})."


@mcp.tool()
def schedule_shipment(
    order_id: Annotated[Any, Field(json_schema_extra={"type": "integer"}, description=ORDER_ID_DOC)],
    ship_date: Annotated[
        Any,
        Field(
            json_schema_extra={"type": "string", "format": "date", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
            description="Calendar date in YYYY-MM-DD, from today up to 60 days out. Resolve relative dates first.",
        ),
    ],
    weight_kg: Annotated[
        Any,
        Field(
            json_schema_extra={"type": "number", "exclusiveMinimum": 0, "maximum": 30},
            description="Package weight in kilograms (convert pounds first). Must be > 0 and <= 30.",
        ),
    ],
    priority: Annotated[
        Any,
        Field(
            json_schema_extra={"type": "string", "enum": sorted(PRIORITIES)},
            description="standard or express. Use express for urgent/rush requests.",
        ),
    ] = "standard",
) -> str:
    """Schedule a shipment for an existing pending order.

    One shipment per call, max 30 kg. Heavier orders must be split across
    several calls for the same order_id.
    """
    if not isinstance(order_id, int) or isinstance(order_id, bool):
        return _fail(f"Validation failed: order_id expected integer got {_type_name(order_id)} ({order_id!r}).")
    if order_id not in ORDERS_BY_ID:
        return _fail(f"Validation failed: order_id {order_id} out of range, no such order.")
    if not isinstance(ship_date, str) or not DATE_RE.match(ship_date):
        return _fail(f"Validation failed: invalid date {ship_date!r} for ship_date, use YYYY-MM-DD.")
    try:
        parsed = date.fromisoformat(ship_date)
    except ValueError:
        return _fail(f"Validation failed: invalid date {ship_date!r} for ship_date.")
    today = date.today()
    if parsed < today or parsed > today + timedelta(days=60):
        return _fail(f"Validation failed: ship_date {ship_date} out of range (today to 60 days out).")
    if isinstance(weight_kg, bool) or not isinstance(weight_kg, (int, float)):
        return _fail(f"Validation failed: weight_kg expected number got {_type_name(weight_kg)} ({weight_kg!r}).")
    if not 0 < weight_kg <= 30:
        return _fail(f"Validation failed: weight_kg {weight_kg} out of range (0-30 kg).")
    if priority not in PRIORITIES:
        return _fail(f"Validation failed: invalid value {priority!r} for priority, allowed: standard, express.")

    order = ORDERS_BY_ID[order_id]
    if order["status"] != "pending":
        return f"Order {order_id} is {order['status']}; nothing scheduled."
    return f"Shipment scheduled: order {order_id} ships {ship_date} ({priority}, {weight_kg} kg) to {order['dest']}."


app = mcp.streamable_http_app()

if __name__ == "__main__":
    mcp.run(transport="streamable-http")
