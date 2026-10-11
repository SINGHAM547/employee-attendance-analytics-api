# DECISIONS.md

1. **Indexes.** I create a unique index on employee code and a unique compound index on attendance employee/date. These prevent duplicate identities and daily records during concurrent requests. Department/join-date, date/employee, status/date, and employee/punch-in indexes support filters, sorting, monthly lookups, and punch-out selection. I avoid unnecessary indexes because each one slows writes and uses storage.

2. **Punch-in race.** Both requests may try to insert the same employee/date. MongoDB's unique compound index allows only one insert. That request returns 201; the duplicate-key error from the other becomes 409. A preliminary lookup is not the guarantee.

3. **Ties.** MongoDB competition ranking uses total late minutes before the final employee-code sort. If the cutoff rank is tied, every employee with rank at or below the limit is returned.

4. **Headcount.** The department summary starts from employees who joined by month end, then looks up attendance. Employees with no logs remain in the aggregation and count toward headcount.

5. **100x data.** I would benchmark indexes with production-like data and monitor query latency, then consider pre-aggregated daily summaries if analytics became too slow.
