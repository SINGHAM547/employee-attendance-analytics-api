# Employee Attendance & Analytics API

FastAPI and MongoDB implementation for the HROne Software Engineer assignment.

## Requirements

- Python 3.11+
- MongoDB 6.0+ (MongoDB Atlas free tier or a local MongoDB instance)

## Run locally

From this directory, create and activate a virtual environment, then install dependencies:

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in your shell or in a local `.env` file. If not set, the app connects to `mongodb://localhost:27017` and uses `attendance_db`. Environment variables take priority over `.env` values. The application creates its indexes at startup.

Load the supplied sample documents (this clears the two sample collections first):

```bash
python sample_seed.py
```

Start the API:

```bash
uvicorn app.main:app --port 8000
```

Open `http://127.0.0.1:8000/docs` to try the endpoints. `GET /health` returns 200 only when MongoDB responds to a ping.

## Notes

- All application code is in `app/main.py` as requested.
- API instants are epoch milliseconds; stored MongoDB datetimes are UTC.
- Do not commit `.env`, credentials, virtual environments, or database dumps.
- Verify the app against a MongoDB 6.0+ instance before submission; local syntax checks alone do not verify MongoDB aggregation behavior.
