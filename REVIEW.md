# REVIEW.md

The following defects were found in the original `app/main.py` and fixed in the submitted version.

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `health` | Always reports ready without checking MongoDB. | Stop MongoDB and call `GET /health`; the starter still returns 200. | Ping MongoDB and return 503 if it cannot be reached. |
| 2 | `create_employee` | Duplicate check and insert are separate, so concurrent creates can both pass the check. | Send two parallel creates with the same `emp_code`. | Enforce a unique index at startup and map duplicate-key errors to 409. |
| 3 | `create_employee` / `EmployeeIn` | Request validation is too loose; codes, email, date, shift formats and equal shift times are not checked. | Submit malformed employee codes, dates or shifts. | Added Pydantic constraints and date/shift validation. |
| 4 | `create_employee` | `created_at` is a naive local datetime and is returned as a datetime rather than epoch milliseconds. | Inspect the response and compare it with the API contract. | Store a UTC-aware whole-second instant and serialize it to epoch milliseconds. |
| 5 | `list_employees` | `skip = page * page_size` skips the first page-size records on page 1. | Request page 1 with page size 2; it starts at the third record. | Use `(page - 1) * page_size`. |
| 6 | `list_employees` | `total` ignores the department filter. | Filter by a department and compare `total` with the matching records. | Count with the same filter used for the result page. |
| 7 | `list_employees` | Results are not sorted by `emp_code`, and pagination limits are not validated. | Insert codes out of order or request page size above 100. | Add deterministic sorting and validated page/page-size parameters. |
| 8 | `compute_late_minutes` | Uses the punch-in date as the shift-start date, mishandles overnight shifts, and compares floored minutes to the grace period. | Test 09:40:01 for a 09:30 shift and early-morning punches for overnight shifts. | Calculate against the attendance day's shift start and compare elapsed seconds to 600 before flooring. |
| 9 | `compute_work_hours` | Python `round` uses ties-to-even rather than the required half-up rounding. | Test durations whose hour value is exactly halfway at two decimal places. | Use `Decimal` with `ROUND_HALF_UP`. |
| 10 | `compute_overtime` | Mixes an aware shift-end datetime with the starter's naive punch-out datetime, which can raise `TypeError`; it also mishandles overnight shifts and the 30-minute minimum. | Punch out after a normal or overnight shift and observe an exception or incorrect small overtime. | Use consistent UTC instants, advance overnight shift end to the next day, floor whole minutes and apply the 30-minute threshold. |
| 11 | `PunchInIn` / `punch_in` | Timestamp and status validation is loose; local-time conversion, truthiness checks and millisecond precision violate the contract. | Send a float, seconds-looking timestamp, invalid status, or a timestamp with milliseconds. | Validate strict integer epoch milliseconds and allowed presence statuses, convert to UTC, and truncate to whole seconds. |
| 12 | `punch_in` | Unknown employees cause a `KeyError` and a 500; the attendance date is based on the machine's local date, not IST shift rules. | Punch in for an unknown code or near midnight/overnight shift boundaries. | Return 404 for unknown employees and calculate the attendance date in IST, including overnight shifts. |
| 13 | `punch_in` | Check-then-insert permits duplicate attendance records under concurrent requests; the response also exposes an `id` not in the contract. | Send parallel punch-ins for one employee/date or inspect response fields. | Use a unique `(emp_code, date)` index, return 409 on duplicate key, and omit internal IDs. |
| 14 | `list_attendance` | Reads every matching document into Python and sorts/paginates in memory, which will not scale to 100,000 records. | Run it against a large collection and inspect memory/query work. | Apply sorting, skip and limit in MongoDB and count with the same filter. |
| 15 | `list_attendance` | Does not validate date range order or enforce pagination limits; sorting lacks `emp_code` as a tie-breaker. | Send `date_from > date_to`, an oversized page, or same-date records. | Validate inputs and sort by date descending then employee code ascending. |
| 16 | `list_attendance` | Returns MongoDB's internal ID as `id` and does not serialize timestamps or legacy defaults to the API contract. | Inspect a response containing BSON datetimes or a legacy record. | Remove IDs, convert instants to epoch milliseconds, and normalize missing `history`, `half_day`, and derived fields. |
| 17 | Application startup | No indexes are created, despite the uniqueness, list-query and analytics performance requirements. | Start against an empty database and inspect its indexes. | Create idempotent unique and query-supporting indexes at startup. |
| 18 | `app/main.py` | Punch-out, regularization, four analytics endpoints and explain diagnostics are absent. | Call any required path that is marked for implementation. | Implement all missing contract endpoints and their validation, concurrency controls and aggregation pipelines. |

## Reviewed and retained

- The use of FastAPI and PyMongo is consistent with the required stack.
- `emp_code` is the business identifier; MongoDB `_id` remains internal.
- Calendar dates and shift times remain strings, while instants are stored as BSON datetimes.
- The two-collection model (`employees`, `attendance_logs`) is retained.
- The `sample_seed.py` Extended JSON loading approach is suitable for the supplied sample data. The samples are only a small local test set, not proof of complete correctness.
