1. **Indexes.** `employees.emp_code` is unique because employee codes are the public identifier. `attendance_logs(emp_code, date)` is unique because there can be only one daily record per employee and it supports employee monthly lookups. The date/employee index supports date ordered attendance pages and month scans. The department/join-date index supports headcount filters. I considered a separate index on `shift_end`, but no endpoint filters by it, so it would add write cost without helping these queries.

2. **Punch-in race.** Both requests can pass the employee lookup and build the same attendance key. The unique compound index allows one insert; MongoDB rejects the other with `DuplicateKeyError`, which becomes 409. There is no check-then-insert correctness dependency.

3. **Ties.** The pipeline ranks with `$rank` after sorting by total minutes, then returns every row whose rank is at most `limit`. So if the cutoff rank is tied, all tied employees are included. The final ordering uses employee code as a stable tie breaker.

4. **Headcount.** Department summary starts from employees who joined by the month's last day and looks up their logs. Employees with no matching logs remain in the employee stream and contribute zero metrics.

5. **100x data.** I would benchmark with representative traffic and likely move trend computation to a daily materialized summary updated as attendance changes, while keeping raw records for audit and reconciliation.
