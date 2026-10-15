# Employee Attendance & Analytics API

FastAPI + MongoDB service for punch-in/out with business rules R1-R10, manual corrections with an audit trail,
and analytics built on MongoDB aggregation pipelines. All code is in `app/main.py`.

**Status:** every endpoint in `openapi.yaml` is implemented. Nothing is intentionally missing.

## Run

Requires Python 3.11+ and MongoDB 6.0+ (tested against MongoDB Atlas).

```bash
python -m venv .venv
.venv\Scripts\activate          # Windows;  macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (never committed) or set real environment variables, which take precedence:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

```bash
uvicorn app.main:app --port 8000
```

Indexes are created idempotently at startup. Interactive docs: `http://localhost:8000/docs`.
`GET /health` returns 200 only when MongoDB answers a ping, otherwise 503.

## Design in brief

- Instants are stored as BSON dates (naive UTC) and exposed as epoch milliseconds; conversion happens only at the
  API boundary. Dates, months and shift times stay IST strings. Instants are truncated to whole seconds first (R1).
- Concurrency relies on the database, not on read-then-write: unique indexes for employee creation and punch-in,
  an atomic `punch_out: null` filter for punch-out, and a guarded update for PATCH (the loser gets 409).
- `GET /admin/explain/{endpoint}` explains the same query or pipeline the endpoint runs; both use one builder.
- Analytics read the stored derived values. Rates and the moving average are computed with exact integer or
  `Decimal` arithmetic so ties round half-up.

## Assumptions

- Trend range limit: at most 92 calendar days, counted inclusively (`from` and `to` both included).
- `moving_avg_7d` averages the reported 4-decimal `attendance_rate` values in the window.
- The trend calendar is generated inside MongoDB with `$range` / `$dateAdd` instead of `$densify`.
- An explicit `null` for `punched_at`, `punch_in` or `punch_out` is rejected with 422.
- A punch-in on a date that already has any record (including a seeded ABSENT or LEAVE) returns 409.
- PATCH keeps the stored status unless one is sent: punch times sent for a record that stays ABSENT or LEAVE are 422.
- Punch-out error order: 404 (unknown employee or no punch-in), then 409 (already out), then 422 (time rules).
- `leave_days` / `leave_count` count every LEAVE record, weekends included; `present_days` counts Mon-Fri only.
- Leaderboard ignores logs whose employee does not exist.