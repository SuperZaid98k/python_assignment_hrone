# Employee Attendance & Analytics API

FastAPI service backed by MongoDB. Install dependencies with `pip install -r requirements.txt`, set `MONGO_URI` and `MONGO_DB`, then run `uvicorn app.main:app --port 8000`. Startup creates the required indexes. MongoDB 6.0 or newer is required for the aggregation stages used here.

The implementation follows the supplied OpenAPI contract and stores all instants as UTC BSON dates; API timestamps use epoch milliseconds. Before submission, review `app/main.py` and make sure you can explain its business rules and aggregation pipelines.
