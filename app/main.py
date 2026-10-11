"""Employee Attendance & Analytics API for the HROne engineering assignment."""
import json
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI, tz_aware=True, tzinfo=timezone.utc, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
PRESENCE_STATUSES = ("PRESENT", "WFH", "ON_DUTY")
ALL_STATUSES = (*PRESENCE_STATUSES, "ABSENT", "LEAVE")
MIN_EPOCH_MS = 100_000_000_000
MAX_EPOCH_MS = 4_102_444_800_000

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Shared time, validation, and serialization helpers
# --------------------------------------------------------------------------- #
def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def datetime_from_epoch_ms(value: int) -> datetime:
    if type(value) is not int or value < MIN_EPOCH_MS or value > MAX_EPOCH_MS:
        raise HTTPException(422, "timestamp must be an integer epoch-millisecond value in range")
    return datetime.fromtimestamp(value // 1000, tz=UTC)


def epoch_ms(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return int(value.timestamp()) * 1000


def parse_date(value: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    if parsed.isoformat() != value:
        raise HTTPException(422, "date must be YYYY-MM-DD")
    return parsed


def parse_month(value: str) -> tuple[date, date, date]:
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value or ""):
        raise HTTPException(422, "month must be YYYY-MM")
    year, month = map(int, value.split("-"))
    try:
        start = date(year, month, 1)
        end = date(year, month, monthrange(year, month)[1])
        next_start = end + timedelta(days=1)
    except (ValueError, OverflowError):
        raise HTTPException(422, "month is outside the supported calendar range")
    return start, end, next_start


def local_instant(day: str | date, hhmm: str) -> datetime:
    day_value = parse_date(day) if isinstance(day, str) else day
    hour, minute = map(int, hhmm.split(":"))
    return datetime.combine(day_value, time(hour, minute), tzinfo=IST).astimezone(UTC)


def attendance_day(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    local = punch_in.astimezone(IST)
    result_day = local.date()
    if shift_end <= shift_start and local.strftime("%H:%M") < shift_end:
        result_day -= timedelta(days=1)
    return result_day.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, attendance_date: str) -> int:
    start = local_instant(attendance_date, shift_start)
    elapsed_seconds = int((punch_in - start).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return max(0, elapsed_seconds // 60)


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)


def compute_overtime(punch_out: datetime, shift_end: str, shift_start: str, attendance_date: str) -> int:
    end_day = parse_date(attendance_date)
    if shift_end <= shift_start:
        end_day += timedelta(days=1)
    shift_end_at = local_instant(end_day, shift_end)
    elapsed_seconds = int((punch_out - shift_end_at).total_seconds())
    whole_minutes = elapsed_seconds // 60
    return whole_minutes if whole_minutes >= 30 else 0


def round_half_up(value, places: int = 2):
    if value is None:
        return None
    quantum = Decimal("1").scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def normalize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": epoch_ms(doc.get("created_at")),
    }


def normalize_history(history: list) -> list:
    result = []
    for entry in history or []:
        changes = {}
        for field_name, change in (entry.get("changes") or {}).items():
            before, after = change.get("from"), change.get("to")
            if field_name in ("punch_in", "punch_out"):
                before, after = epoch_ms(before), epoch_ms(after)
            changes[field_name] = {"from": before, "to": after}
        result.append({
            "at": epoch_ms(entry.get("at")),
            "by": entry.get("by", ""),
            "reason": entry.get("reason", ""),
            "changes": changes,
        })
    return result


def normalize_attendance(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": epoch_ms(doc.get("punch_in")),
        "punch_out": epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": int(doc.get("late_minutes", 0) or 0),
        "overtime_minutes": int(doc.get("overtime_minutes", 0) or 0),
        "half_day": bool(doc.get("half_day", False)),
        "history": normalize_history(doc.get("history", [])),
    }


def presence_day_expr(record_prefix: str = "$log") -> dict:
    return {
        "$cond": [
            {"$in": [f"{record_prefix}.status", list(PRESENCE_STATUSES)]},
            {"$cond": [
                {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": f"{record_prefix}.date"}}}, [2, 3, 4, 5, 6]]},
                {"$cond": [{"$ifNull": [f"{record_prefix}.half_day", False]}, 0.5, 1]},
                0,
            ]},
            0,
        ]
    }


def half_up_expr(expression, places: int = 2):
    factor = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [expression, factor]}, 0.5]}}, factor]}


def ensure_indexes() -> None:
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="ix_employee_department_joined")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)], name="ix_employee_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)], name="ix_employee_joined_department")

    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="uq_attendance_employee_date")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_date_employee")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], name="ix_attendance_status_date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="ix_attendance_employee_punchin")


@app.on_event("startup")
def startup() -> None:
    ensure_indexes()


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, value):
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError("joined_on must be YYYY-MM-DD")
        return value

    @model_validator(mode="after")
    def different_shift_times(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

    @model_validator(mode="after")
    def supplied_timestamp_must_not_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    emp_code: str
    punched_at: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)

    @model_validator(mode="after")
    def supplied_timestamp_must_not_be_null(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at must be an epoch-millisecond integer")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    punch_out: Optional[StrictInt] = Field(default=None, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def supplied_times_must_be_values(self):
        for field_name in ("punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be an epoch-millisecond integer")
        return self


# --------------------------------------------------------------------------- #
# Shared aggregation pipeline builders
# --------------------------------------------------------------------------- #
def attendance_filter(emp_code=None, date_from=None, date_to=None, status=None):
    query = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to
    if status:
        query["status"] = status
    return query


def employee_monthly_pipeline(emp_code: str, month: str) -> list:
    month_start, month_end, next_start = parse_month(month)
    start_s, end_s, next_s = month_start.isoformat(), month_end.isoformat(), next_start.isoformat()
    start_expr = {"$cond": [{"$gt": ["$joined_on", end_s]}, end_s, {"$cond": [{"$gt": ["$joined_on", start_s]}, "$joined_on", start_s]}]}
    start_dt = {"$dateFromString": {"dateString": start_expr}}
    day_count = {"$dateDiff": {"startDate": start_dt, "endDate": {"$dateFromString": {"dateString": next_s}}, "unit": "day"}}
    weekdays = {"$cond": [
        {"$gt": ["$joined_on", end_s]}, 0,
        {"$size": {"$filter": {
            "input": {"$map": {"input": {"$range": [0, {"$max": [0, day_count]}]}, "as": "offset", "in": {"$dayOfWeek": {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}}}}},
            "as": "dow", "cond": {"$and": [{"$gte": ["$$dow", 2]}, {"$lte": ["$$dow", 6]}]},
        }}}
    ]}
    return [
        {"$match": {"emp_code": emp_code}},
        {"$set": {"_month_start": start_s, "_month_end": end_s, "_next_start": next_s, "_working_days": weekdays}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", next_s]},
            ]}}},
        ], "as": "logs"}},
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {"$group": {
            "_id": "$emp_code", "month": {"$first": month}, "working_days": {"$first": "$_working_days"},
            "present_days": {"$sum": presence_day_expr("$logs")},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$logs.status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$logs.late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$logs.late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$logs.overtime_minutes", 0]}},
        }},
        {"$project": {
            "_id": 0, "emp_code": "$_id", "month": 1, "working_days": 1,
            "present_days": {"$round": ["$present_days", 2]}, "leave_days": 1, "late_count": 1,
            "total_late_minutes": 1, "total_overtime_minutes": 1,
            "attendance_pct": {"$cond": [
                {"$eq": ["$working_days", 0]}, None,
                half_up_expr({"$multiply": [{"$divide": ["$present_days", "$working_days"]}, 100]}, 4),
            ]},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str] = None) -> list:
    month_start, month_end, next_start = parse_month(month)
    end_s, start_s, next_s = month_end.isoformat(), month_start.isoformat(), next_start.isoformat()
    match = {"joined_on": {"$lte": end_s}}
    if department is not None:
        match["department"] = department
    presence_logs = {"$filter": {"input": "$logs", "as": "log", "cond": {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}}}
    work_logs = {"$filter": {"input": "$logs", "as": "log", "cond": {"$and": [
        {"$in": ["$$log.status", list(PRESENCE_STATUSES)]}, {"$ne": ["$$log.work_hours", None]},
    ]}}}
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", start_s]}, {"$lt": ["$date", next_s]},
            ]}}},
        ], "as": "logs"}},
        {"$set": {
            "_present_days": {"$sum": {"$map": {"input": presence_logs, "as": "log", "in": presence_day_expr("$$log")}}},
            "_late_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$gt": [{"$ifNull": ["$$log.late_minutes", 0]}, 0]}}}},
            "_total_late": {"$sum": {"$map": {"input": "$logs", "as": "log", "in": {"$ifNull": ["$$log.late_minutes", 0]}}}},
            "_leave_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "LEAVE"]}}}},
            "_on_duty_count": {"$size": {"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.status", "ON_DUTY"]}}}},
            "_work_sum": {"$sum": {"$map": {"input": work_logs, "as": "log", "in": "$$log.work_hours"}}},
            "_work_count": {"$size": work_logs},
        }},
        {"$group": {
            "_id": "$department", "headcount": {"$sum": 1}, "present_days": {"$sum": "$_present_days"},
            "late_count": {"$sum": "$_late_count"}, "total_late_minutes": {"$sum": "$_total_late"},
            "leave_count": {"$sum": "$_leave_count"}, "on_duty_count": {"$sum": "$_on_duty_count"},
            "work_sum": {"$sum": "$_work_sum"}, "work_count": {"$sum": "$_work_count"},
        }},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": {"$round": ["$present_days", 2]},
            "avg_work_hours": {"$cond": [{"$eq": ["$work_count", 0]}, None, half_up_expr({"$divide": ["$work_sum", "$work_count"]}, 2)]},
            "late_count": 1, "total_late_minutes": 1, "leave_count": 1, "on_duty_count": 1,
        }},
        {"$sort": {"department": 1}},
    ]


def leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> list:
    month_start, month_end, next_start = parse_month(month)
    start_s, next_s = month_start.isoformat(), next_start.isoformat()
    pipeline = [
        {"$match": {"date": {"$gte": start_s, "$lt": next_s}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline.extend([
        {"$set": {"emp_code": "$_id", "name": "$employee.name", "department": "$employee.department"}},
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": 1, "name": 1, "department": 1, "total_late_minutes": 1, "late_count": 1}},
    ])
    return pipeline


def department_trend_pipeline(department: str, from_date: str, to_date: str) -> list:
    start_day, end_day = parse_date(from_date), parse_date(to_date)
    if end_day < start_day:
        raise HTTPException(422, "to must be on or after from")
    if (end_day - start_day).days + 1 > 92:
        raise HTTPException(422, "date range cannot exceed 92 days")
    try:
        next_day = end_day + timedelta(days=1)
    except OverflowError:
        raise HTTPException(422, "to is outside the supported calendar range")
    start_dt = datetime.combine(start_day, time.min, tzinfo=UTC)
    end_exclusive = datetime.combine(next_day, time.min, tzinfo=UTC)
    day_count = (end_day - start_day).days + 1
    day_date_expr = {"$dateAdd": {"startDate": start_dt, "unit": "day", "amount": "$$offset"}}
    day_string_expr = {"$dateToString": {"format": "%Y-%m-%d", "date": day_date_expr}}
    return [
        {"$match": {"department": department}},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": [
            {"$match": {"$expr": {"$and": [
                {"$eq": ["$emp_code", "$$code"]}, {"$gte": ["$date", from_date]}, {"$lte": ["$date", to_date]},
            ]}}},
        ], "as": "logs"}},
        {"$set": {"days": {"$map": {"input": {"$range": [0, day_count]}, "as": "offset", "in": {
            "day": day_date_expr,
            "date": day_string_expr,
            "active": {"$lte": ["$joined_on", day_string_expr]},
            "record": {"$arrayElemAt": [{"$filter": {"input": "$logs", "as": "log", "cond": {"$eq": ["$$log.date", day_string_expr]}}}, 0]},
        }}}}},
        {"$unwind": "$days"},
        {"$group": {
            "_id": "$days.day",
            "headcount": {"$sum": {"$cond": ["$days.active", 1, 0]}},
            "present_count": {"$sum": {"$cond": [
                {"$in": ["$days.record.status", list(PRESENCE_STATUSES)]},
                {"$cond": [{"$ifNull": ["$days.record.half_day", False]}, 0.5, 1]}, 0,
            ]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$days.record.late_minutes", 0]}, 0]}, 1, 0]}},
        }},
        {"$project": {"_id": 0, "day": "$_id", "headcount": 1, "present_count": 1, "late_count": 1}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_exclusive]}}},
        {"$set": {
            "headcount": {"$ifNull": ["$headcount", 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$in": [{"$dayOfWeek": "$day"}, [2, 3, 4, 5, 6]]},
        }},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": ["$present_count", "$headcount"]}, 4), None,
        ]}}},
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {"_moving_avg": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}}}},
        {"$set": {"moving_avg_7d": half_up_expr("$_moving_avg", 4)}},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}},
            "is_working_day": 1, "headcount": 1, "present_count": {"$round": ["$present_count", 2]},
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


def explain_aggregate(collection: str, pipeline: list, hint: str) -> dict:
    raw = db.command("explain", {
        "aggregate": collection, "pipeline": pipeline, "cursor": {}, "hint": hint,
    }, verbosity="executionStats")
    return json.loads(json_util.dumps(raw))


def attendance_list_hint(emp_code=None, status=None) -> str:
    if status:
        return "ix_attendance_status_date_employee"
    if emp_code:
        return "uq_attendance_employee_date"
    return "ix_attendance_date_employee"


def explain_find(query: dict, sort: dict, skip: int, limit: int, hint: str) -> dict:
    raw = db.command("explain", {
        "find": "attendance_logs", "filter": query, "sort": sort, "skip": skip, "limit": limit, "hint": hint,
    }, verbosity="executionStats")
    return json.loads(json_util.dumps(raw))


# --------------------------------------------------------------------------- #
# System and employee endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "MongoDB is unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return normalize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    total = db.employees.count_documents(query)
    items = list(db.employees.find(query, {"_id": 0}).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size))
    return {"items": [normalize_employee(item) for item in items], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance endpoints
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(punched_at, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(punched_at, emp["shift_start"], day),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this attendance date")
    return normalize_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    punched_at = datetime_from_epoch_ms(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punched_at}},
        sort=[("punch_in", DESCENDING), ("date", DESCENDING)],
    )
    if record is None:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "attendance record is already punched out")
    if punched_at <= record["punch_in"] or punched_at - record["punch_in"] > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    work_hours = compute_work_hours(record["punch_in"], punched_at)
    overtime = compute_overtime(punched_at, emp["shift_end"], emp["shift_start"], record["date"])
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None, "punch_in": record["punch_in"], "status": record["status"]},
        {"$set": {"punch_out": punched_at, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": work_hours < 4.50}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "attendance record was punched out by another request")
    return normalize_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    query = attendance_filter(emp_code, date_from.isoformat() if date_from else None, date_to.isoformat() if date_to else None, status)
    total = db.attendance_logs.count_documents(query)
    docs = list(db.attendance_logs.find(query, hint=attendance_list_hint(emp_code, status)).sort([("date", DESCENDING), ("emp_code", ASCENDING)]).skip((page - 1) * page_size).limit(page_size))
    return {"items": [normalize_attendance(doc) for doc in docs], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: date, body: RegularizeIn):
    day = date.isoformat()
    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if old is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    status = body.status if "status" in supplied else old["status"]
    if status in ("ABSENT", "LEAVE") and ("punch_in" in supplied or "punch_out" in supplied):
        raise HTTPException(422, "ABSENT and LEAVE records cannot be supplied with punch times")

    old_in = old.get("punch_in")
    old_out = old.get("punch_out")
    new_in = datetime_from_epoch_ms(body.punch_in) if "punch_in" in supplied else old_in
    new_out = datetime_from_epoch_ms(body.punch_out) if "punch_out" in supplied else old_out
    if status in ("ABSENT", "LEAVE"):
        new_in, new_out = None, None
    else:
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        if attendance_day(new_in, emp["shift_start"], emp["shift_end"]) != day:
            raise HTTPException(422, "punch_in must belong to the record attendance date")
        if new_out is not None and (new_out <= new_in or new_out - new_in > timedelta(hours=24)):
            raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    if status in ("ABSENT", "LEAVE"):
        late, work, overtime, half_day = 0, None, 0, False
    else:
        late = compute_late_minutes(new_in, emp["shift_start"], day)
        if new_out is None:
            work, overtime, half_day = None, 0, False
        else:
            work = compute_work_hours(new_in, new_out)
            overtime = compute_overtime(new_out, emp["shift_end"], emp["shift_start"], day)
            half_day = work < 4.50

    before_values = {
        "status": old.get("status"), "punch_in": old_in, "punch_out": old_out,
        "work_hours": old.get("work_hours"), "late_minutes": int(old.get("late_minutes", 0) or 0),
        "overtime_minutes": int(old.get("overtime_minutes", 0) or 0), "half_day": bool(old.get("half_day", False)),
    }
    after_values = {
        "status": status, "punch_in": new_in, "punch_out": new_out,
        "work_hours": work, "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    changes = {}
    for key in before_values:
        before, after = before_values[key], after_values[key]
        if before != after:
            changes[key] = {"from": before, "to": after}
    if not changes:
        raise HTTPException(422, "request does not change the record")

    at = now_utc()
    history_entry = {"at": at, "by": body.regularized_by, "reason": body.reason, "changes": changes}
    update_filter = {"_id": old["_id"], "status": old.get("status"), "punch_in": old_in, "punch_out": old_out}
    if "history" in old:
        update_filter["history"] = old["history"]
    else:
        update_filter["history"] = {"$exists": False}
    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {"$set": {
            "status": status, "punch_in": new_in, "punch_out": new_out, "work_hours": work,
            "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
        }, "$push": {"history": history_entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record changed during regularization; retry with the latest version")
    return normalize_attendance(updated)


# --------------------------------------------------------------------------- #
# Analytics endpoints - all calculations are performed by MongoDB pipelines
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")):
    parse_month(month)
    pipeline = employee_monthly_pipeline(emp_code, month)
    result = list(db.employees.aggregate(pipeline, hint="uq_employee_code"))
    if not result:
        raise HTTPException(404, "employee not found")
    return result[0]


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    parse_month(month)
    hint = "ix_employee_department_joined" if department is not None else "ix_employee_joined_department"
    return {"month": month, "items": list(db.employees.aggregate(department_summary_pipeline(month, department), hint=hint))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    parse_month(month)
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department), hint="ix_attendance_date_employee"))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: date = Query(alias="from"), to_date: date = Query(alias="to")):
    start, end = from_date.isoformat(), to_date.isoformat()
    pipeline = department_trend_pipeline(department, start, end)
    if db.employees.find_one({"department": department}, {"_id": 1}) is None:
        raise HTTPException(404, "department not found")
    items = list(db.employees.aggregate(pipeline, hint="ix_employee_department_joined"))
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must be on or before date_to")
        query = attendance_filter(emp_code, date_from.isoformat() if date_from else None, date_to.isoformat() if date_to else None, status)
        explain = explain_find(query, {"date": -1, "emp_code": 1}, (page - 1) * page_size, page_size, attendance_list_hint(emp_code, status))
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required for employee_monthly")
        parse_month(month)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", employee_monthly_pipeline(emp_code, month), "uq_employee_code")}

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required for department_summary")
        parse_month(month)
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", department_summary_pipeline(month, department), "ix_employee_department_joined" if department is not None else "ix_employee_joined_department")}

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required for late_leaderboard")
        parse_month(month)
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department), "ix_attendance_date_employee")}

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required for department_trend")
        pipeline = department_trend_pipeline(department, from_date.isoformat(), to_date.isoformat())
        return {"endpoint": endpoint, "collection": "employees", "explain": explain_aggregate("employees", pipeline, "ix_employee_department_joined")}

    raise HTTPException(422, "unsupported endpoint")
