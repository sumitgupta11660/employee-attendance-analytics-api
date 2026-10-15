# Starter review

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always reports ready without checking MongoDB. | Stop MongoDB and call `/health`; it still returns 200. | Ping MongoDB and return 503 on failure. |
| 2 | `compute_late_minutes` | Replaces the punch date with a shift start on that same date; overnight handling is wrong. | Punch in after midnight for a 22:00–06:00 shift. | Compute the correct local shift boundary from attendance date. |
| 3 | `compute_late_minutes` | The punch instant is not normalized or truncated before computing. | Submit a timestamp with fractional seconds near the 10-minute grace boundary. | Convert to UTC-naive storage time truncated to seconds, then calculate in IST. |
| 4 | `compute_work_hours` | Python's built-in `round` uses ties-to-even and binary floats. | Construct a duration that rounds on a .005-hour boundary. | Use `Decimal` and `ROUND_HALF_UP`. |
| 5 | `compute_overtime` | Uses the attendance date for shift end even for overnight shifts. | Punch out the next morning after a night shift. | Place the end boundary on the next day for overnight schedules. |
| 6 | `EmployeeIn` | Missing format, length, date, and distinct-shift validation. | Send malformed employee codes, dates, or identical shift endpoints. | Add contract-level Pydantic validation. |
| 7 | `create_employee` | Duplicate check followed by insert races under concurrent requests. | Send simultaneous creates for one `emp_code`. | Enforce a unique database index and map duplicate-key errors to 409. |
| 8 | `create_employee` | Stores a naive local clock value and returns BSON datetime rather than epoch milliseconds. | Inspect `created_at` or run the app outside UTC. | Store UTC and serialize it as epoch milliseconds. |
| 9 | `list_employees` | `skip` starts at `page * page_size`, skipping page one; total ignores department. | Request page 1 or filter a department with fewer employees than total. | Use `(page-1)*page_size` and count the same filtered query. |
| 10 | `list_employees` | No page bounds or deterministic ordering. | Use page 0, a huge page size, or compare repeated results. | Validate pagination and sort by `emp_code`. |
| 11 | `punch_in` | Missing employee causes an unhandled `None` dereference. | Punch in with an unknown code. | Return 404 before reading shift fields. |
| 12 | `punch_in` | `fromtimestamp` and `now` use host-local time; falsey checks mishandle timestamps. | Run under another timezone or send invalid/zero input. | Strictly validate epoch milliseconds and use UTC. |
| 13 | `punch_in` | Attendance day ignores IST and overnight shift assignment. | Punch in near UTC midnight or after midnight on a night shift. | Calculate the attendance date using the employee's shift and IST. |
| 14 | `punch_in` | Check-then-insert permits duplicate records under a race; status and timestamps are not validated. | Parallel punch-ins or send `ABSENT` as punch status. | Unique compound index, duplicate-key 409, and strict request validation. |
| 15 | `list_attendance` | Reads every matching document into Python and sorts/slices in memory. | Query a large collection and inspect memory/latency. | Match, sort, count, skip, and limit in MongoDB with indexed fields. |
| 16 | `list_attendance` | Missing stable tie-break sort; returns Mongo `_id` as a synthetic `id`, contrary to the contract. | Multiple employees share a date or inspect response fields. | Sort by date descending then code ascending; omit internal identifiers. |
| 17 | Helpers and serializers | BSON datetimes in nested history and attendance responses are not converted to epoch milliseconds. | List a regularized record and inspect JSON serialization. | Normalize top-level and nested time values at the API boundary. |

**Reviewed and not defects:** MongoDB's default `_id` index is present by design; it does not replace the required natural-key indexes. Calendar dates and shift times are strings in the data model by design, so they should not be converted to timezone-bearing instants in storage.

| 18 | `compute_late_minutes` / `late_minutes` | Shift start is taken on the punch-in's own calendar date, so after-midnight punch-ins on overnight shifts are measured from the wrong day. | Overnight 22:00-06:00 employee punches in at 00:30; expected 150 late minutes, got 0. | Compute from the shift start of the attendance date (`day`). 

| 19 | `/admin/explain` | Four endpoints explained a stub pipeline instead of the real one, so the plan shown could differ from the plan run. | Compare the explain output with the endpoint's actual pipeline. | One builder function per endpoint, used by both the endpoint and explain. |
