# orders-desk MCP

A deliberately strict MCP server (streamable HTTP at `/mcp`) used to reproduce the
Signals `ExecutionFailureInvalidArgs`, `ExecutionFailureBadQuery`, and
`ExecutionFailureStateError` signals.

| Tool | Behaviour |
|---|---|
| `search_orders(query)` | Accepts only `field:op:value` clauses joined by `;`. SQL or guessed field names return `Invalid query: unknown field ...` / `Query syntax error ...` (BadQuery) |
| `get_order_schema()` | Lists the queryable fields and operators |
| `get_order(order_id)` | Integer id only |
| `schedule_shipment(order_id, ship_date, weight_kg, priority)` | Integer id, `YYYY-MM-DD` date, kg in 0-30, `standard`/`express`. Anything else returns `Validation failed: ...` (InvalidArgs) |
| `open_hold(order_id)` | Returns a ticket only when the order has an open hold (1043, 1061). No hold means no ticket |
| `release_hold(order_id, ticket)` | Accepts only the ticket `open_hold` returned. A missing or invented ticket returns `Precondition failed: must call open_hold first.` (StateError) |

Errors are returned as `isError` tool results. Set `ERRORS_AS_CONTENT=1` to return
them as normal text instead.

```bash
pip install -r requirements.txt
PORT=8080 python server.py
```

Deployed to App Platform with [`.do/app.yaml`](.do/app.yaml).
