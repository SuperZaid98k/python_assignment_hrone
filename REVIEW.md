# Review of the starter

| # | Where | What is wrong | How you'd notice it | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | It uses the punch-in's timezone as if it were the shift timezone and discards the date relationship for overnight work. | Punch in at 00:30 on a 22:00–06:00 shift. | Convert instants to IST and associate early morning punches with the preceding shift date. |
| 2 | `compute_late_minutes` | The original logic otherwise treats the 10 minute grace boundary incorrectly when seconds are present. | Compare 09:40:00 and 09:40:01 for a 09:30 shift. | Truncate instants to seconds and apply a strict greater-than-600-second test. |
| 3 | `compute_work_hours` | Python float rounding is banker's rounding, not required half-up rounding. | A duration whose hundredths tie, such as 4h 29m 24s. | Use `Decimal` with `ROUND_HALF_UP`. |
| 4 | `compute_overtime` | It computes overtime without the required 30-minute minimum and uses a potentially mismatched date for overnight shifts. | A 29-minute overtime or an overnight shift. | Build the shift-end instant on the correct IST calendar day and apply the 30-minute threshold. |
| 5 | Mongo client setup | Default naive datetimes and local `fromtimestamp` make timezone behavior host-dependent. | Run the same timestamp on UTC and non-UTC hosts. | Use timezone-aware UTC PyMongo datetimes and explicit epoch conversion. |
| 6 | `/health` | It always returned ok, even when MongoDB was down. | Stop MongoDB and request health. | Issue a ping and return 503 on failure. |
| 7 | Employee create | Duplicate check then insert races; creation instant was naive and response omitted its required epoch-millisecond conversion. | Send concurrent duplicate creates. | Unique database index catches races; convert server timestamp in response. |
| 8 | Employee list | Skip was `page * page_size`; total ignored department; results were unsorted. | Request page 1 or a filtered department. | Use `(page-1)*page_size`, filtered count, and ascending `emp_code` sort. |
| 9 | Punch-in | Unknown employees caused a 500; timestamp used local time; status was not restricted; duplicate check was racy. | Unknown code, seconds timestamp, invalid status, or concurrent punches. | Validate request, return 404, derive IST shift date, and rely on a unique natural-key index. |
| 10 | Attendance list | It loaded all matched rows into Python, sorted/sliced there, omitted filters from the total, used an incorrect page boundary, and leaked Mongo `_id`. | Large dataset, page 1, or filtered count. | Count and paginate in MongoDB with stable sort; serialize without `_id`. |
| 11 | Attendance list | It did not validate date strings or reject reversed date bounds. | Malformed date or `date_from > date_to`. | Parse strict calendar dates and return 422 for invalid ranges. |
| 12 | Punch-out (missing) | There was no implementation for closing the latest eligible open shift or resolving races. | Call the contract endpoint, including across midnight. | Added conditional update and correct 404/409/422 behavior. |
| 13 | Regularization (missing) | There was no correction validation, derived-field recalculation, or append-only audit trail. | Correct a record and inspect history/derived values. | Added natural-key update, validation, changed-field diff, and conditional history append. |
| 14 | Analytics (missing) | Required summaries and trends were absent. | Call any analytics route. | Added Mongo aggregation pipelines, including lookup-based department headcount and densified trend days. |
| 15 | Indexes/explain (missing) | No startup indexes or explain endpoint existed. | Inspect indexes or request an explain plan. | Added idempotent indexes and the explain route. |

I reviewed the original `compute_work_hours` duration formula: subtracting two instants is the right basis for elapsed hours. The defect was its rounding method, not the subtraction itself. The sample seed script's use of Extended JSON was also appropriate.
