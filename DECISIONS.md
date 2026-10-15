# DECISIONS

**1. Indexes.** `employees`: unique `emp_code` (identity, race-safe create); `(department, joined_on)` for department filters and headcount; `joined_on` for headcount without a department. `attendance_logs`: unique `(emp_code, date)` (one record per day, punch-in race, PATCH); `(date desc, emp_code)` for the list sort and month scans; `(status, date desc, emp_code)` for status filters; `(emp_code, punch_in desc)` to find the record to punch out. Rejected: a standalone `status` index (five values, not selective) and `(emp_code, date, status)` (redundant).

**2. Punch-in race.** Both requests compute the same date and both insert. The unique `(emp_code, date)` index lets one win; the other raises `DuplicateKeyError`, returned as 409. No check-then-insert.

**3. Ties.** `$rank` gives standard competition ranking (1, 2, 2, 4). I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even beyond `limit`. Rows sort by total late minutes, then `emp_code`.

**4. Headcount.** The summary starts from `employees` (`joined_on` on or before month end) and looks up each person's logs, so employees with zero logs still count; later joiners do not.

**5. At 100x data.** I would keep a pre-aggregated daily summary per department, updated on punch and correction, and use keyset pagination instead of skip/limit.