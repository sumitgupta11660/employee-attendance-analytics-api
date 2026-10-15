"""Employee Attendance & Analytics API (FastAPI + PyMongo).

Time conventions
* MongoDB stores instants as BSON dates. The client is not tz-aware, so PyMongo hands back naive UTC datetimes and
  every datetime in this file is naive UTC. Epoch-millisecond conversion happens only in `epoch_ms` / `dt_from_ms`.
* Shift times, attendance dates and months are IST calendar strings and stay strings.
* Analytics read the STORED derived values (work_hours, late_minutes, ...) and never recompute them from punches.
"""
import json
import logging
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # real environment variables win: load_dotenv() does not override them by default
log = logging.getLogger("attendance")
client = MongoClient(os.getenv("MONGO_URI") or os.getenv("MONGODB_URI") or "mongodb://localhost:27017",
                     serverSelectionTimeoutMS=8000)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1)
ONE_MS = timedelta(milliseconds=1)
PRESENCE = ("PRESENT", "WFH", "ON_DUTY")
STATUSES = PRESENCE + ("ABSENT", "LEAVE")
ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]
DERIVED_DEFAULTS = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
_indexes_ready = False


# --------------------------------------------------------------------------- #
# Startup: indexes (idempotent) and app object
# --------------------------------------------------------------------------- #
def ensure_indexes():
    global _indexes_ready
    emp, logs = db.employees, db.attendance_logs
    # employees
    emp.create_index([("emp_code", ASCENDING)], unique=True, name="uq_employee_code")  # identity + race-safe create
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    emp.create_index([("joined_on", ASCENDING)], name="employee_joined")  # headcount without a department filter
    # attendance_logs
    logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True,
                      name="uq_attendance_employee_date")  # one record per day, punch-in race, PATCH, monthly
    logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="attendance_date_employee")  # list + month scans
    logs.create_index([("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
                      name="attendance_status_date_employee")  # list filtered by status
    logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="attendance_employee_punch_in")  # punch-out
    _indexes_ready = True


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError:
        log.warning("could not create indexes at startup; /health will retry", exc_info=True)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def utc_naive(dt: datetime) -> datetime:
    return dt if dt.tzinfo is None else dt.astimezone(UTC).replace(tzinfo=None)


def epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else (utc_naive(dt) - EPOCH) // ONE_MS  # integer maths, no float drift


def dt_from_ms(value: int) -> datetime:
    return EPOCH + timedelta(seconds=value // 1000)  # truncates to whole seconds (R1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0, tzinfo=None)


def round_half_up(value, places: int = 2) -> float:
    return float(Decimal(str(value)).quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


def to_decimal(value) -> Decimal:
    return value.to_decimal() if isinstance(value, Decimal128) else Decimal(str(value))


def shift_boundary(day: str, clock: str, next_day: bool = False) -> datetime:
    """The IST wall-clock `clock` on attendance date `day` (or the day after), as naive UTC."""
    d = date.fromisoformat(day) + timedelta(days=1 if next_day else 0)
    h, m = map(int, clock.split(":"))
    return datetime.combine(d, time(h, m), tzinfo=IST).astimezone(UTC).replace(tzinfo=None)


def late_minutes(punch_in: datetime, shift_start: str, day: str) -> int:
    """R2. Measured from the shift start of the ATTENDANCE date, so a 00:30 punch-in on a 22:00 shift is late."""
    seconds = int((utc_naive(punch_in) - shift_boundary(day, shift_start)).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def overtime_minutes(punch_out: datetime, shift_start: str, shift_end: str, day: str) -> int:
    """R3. The end of an overnight shift falls on the next calendar day."""
    end = shift_boundary(day, shift_end, next_day=shift_end <= shift_start)
    minutes = int((utc_naive(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, 2 decimals, half-up."""
    seconds = int((utc_naive(punch_out) - utc_naive(punch_in)).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


def attendance_day(ts: datetime, emp: dict) -> str:
    """R1: IST date of the punch-in; overnight shifts: a punch-in before shift_end belongs to the previous day."""
    local = ts.replace(tzinfo=UTC).astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.strftime("%H:%M") < emp["shift_end"]:
        day -= timedelta(days=1)
    return day.isoformat()


def month_bounds(month: str):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month):
        raise HTTPException(422, "month must be YYYY-MM")
    try:
        first = date.fromisoformat(month + "-01")
        following = date(first.year + (first.month == 12), first.month % 12 + 1, 1)
    except ValueError:
        raise HTTPException(422, "month out of range")
    return first, following - timedelta(days=1)


def working_days(start: date, end: date) -> int:
    """R7: Mon-Fri between two dates, inclusive (pure date arithmetic, at most 31 iterations)."""
    count, d = 0, start
    while d <= end:
        count += d.weekday() < 5
        d += timedelta(days=1)
    return count


def validate_date(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise HTTPException(422, "date must be YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, "date must be a real calendar date")
    return value


def public_doc(doc: dict) -> dict:
    """Mongo document -> API shape: no _id, instants as epoch ms (also inside history), defaults for legacy gaps."""
    doc = dict(doc)
    doc.pop("_id", None)
    for key in ("punch_in", "punch_out", "created_at"):
        if key in doc:
            doc[key] = epoch_ms(doc[key])
    doc.setdefault("half_day", False)
    doc.setdefault("history", [])
    for entry in doc["history"]:
        entry["at"] = epoch_ms(entry.get("at"))
        for change in entry.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(change.get(side), datetime):
                    change[side] = epoch_ms(change[side])
    return doc


# --------------------------------------------------------------------------- #
# Request models
# --------------------------------------------------------------------------- #
def valid_epoch(v):
    if type(v) is not int or not 100_000_000_000 <= v <= 4_102_444_800_000:
        raise ValueError("timestamp must be an integer in epoch milliseconds")
    return v


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
    def valid_joined(cls, v):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            raise ValueError("joined_on must be YYYY-MM-DD")
        date.fromisoformat(v)  # ValueError for e.g. 2026-02-30
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"

    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls, v):
        return None if v is None else valid_epoch(v)

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v not in PRESENCE:
            raise ValueError("status must be PRESENT, WFH, or ON_DUTY")
        return v

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator("punched_at")
    @classmethod
    def ts_valid(cls, v):
        return None if v is None else valid_epoch(v)

    @model_validator(mode="after")
    def reject_null_timestamp(self):
        if "punched_at" in self.model_fields_set and self.punched_at is None:
            raise ValueError("punched_at cannot be null")
        return self


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)  # derived fields in the body are ignored
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @field_validator("punch_in", "punch_out")
    @classmethod
    def times_valid(cls, v):
        return None if v is None else valid_epoch(v)

    @field_validator("status")
    @classmethod
    def status_valid(cls, v):
        if v is not None and v not in STATUSES:
            raise ValueError("invalid status")
        return v

    @model_validator(mode="after")
    def reject_null_times(self):
        for name in ("punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError("punch times cannot be null")
        return self


# --------------------------------------------------------------------------- #
# Query / pipeline builders. The endpoints AND /admin/explain both call these, so the plan you explain is the plan you run.
# --------------------------------------------------------------------------- #
def attendance_filter(emp_code, date_from, date_to, status) -> dict:
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = validate_date(date_from)
        if date_to:
            q["date"]["$lte"] = validate_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(422, "date_from must not exceed date_to")
    if status:
        if status not in STATUSES:
            raise HTTPException(422, "invalid status")
        q["status"] = status
    return q


def attendance_find_command(q: dict, page: int, page_size: int) -> dict:
    return {"find": "attendance_logs", "filter": q, "sort": dict(ATTENDANCE_SORT), "projection": {"_id": 0},
            "skip": (page - 1) * page_size, "limit": page_size}


def month_log_match(month: str, emp_code: Optional[str] = None) -> dict:
    start, end = month_bounds(month)
    q = {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}
    if emp_code:
        q["emp_code"] = emp_code
    return q


# Reusable aggregation expressions (R6 presence, R7 Mon-Fri, stored derived values only).
IS_PRESENT = {"$in": ["$status", list(PRESENCE)]}
IS_WEEKDAY = {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]}
IS_LATE = {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
PRESENT_WEIGHT = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}, 0]}


def month_stats() -> dict:
    """$group accumulators shared by the employee summary and the per-employee part of the department summary."""
    return {
        "present_days": {"$sum": PRESENT_WEIGHT},
        "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
        "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        "late_count": {"$sum": {"$cond": [IS_LATE, 1, 0]}},
        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
        "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        # Decimal128 keeps the sum exact; the mean is divided in Python Decimal so ties round half-up correctly.
        "work_sum": {"$sum": {"$cond": [HAS_WORK_HOURS, {"$toDecimal": "$work_hours"}, 0]}},
        "work_count": {"$sum": {"$cond": [HAS_WORK_HOURS, 1, 0]}},
    }


def monthly_pipeline(month: str, emp_code: str) -> list:
    return [{"$match": month_log_match(month, emp_code)}, {"$group": {"_id": None, **month_stats()}}]


def summary_plan(month: str, department: Optional[str]):
    """Start from `employees` so people with zero logs still count (R9); join each one's month of logs by index."""
    start, end = month_bounds(month)
    employee_match = {"joined_on": {"$lte": end.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    pipeline = [
        {"$match": employee_match},
        {"$lookup": {"from": "attendance_logs", "localField": "emp_code", "foreignField": "emp_code",
                     "pipeline": [{"$match": {"date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
                                  {"$group": {"_id": None, **month_stats()}}],
                     "as": "stats"}},
        {"$set": {"stats": {"$ifNull": [{"$arrayElemAt": ["$stats", 0]}, {}]}}},
        {"$group": {"_id": "$department",
                    "headcount": {"$sum": 1},
                    "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
                    "work_sum": {"$sum": {"$ifNull": ["$stats.work_sum", 0]}},
                    "work_count": {"$sum": {"$ifNull": ["$stats.work_count", 0]}},
                    "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                    "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                    "leave_count": {"$sum": {"$ifNull": ["$stats.leave_days", 0]}},
                    "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}}}},
        {"$sort": {"_id": 1}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1, "present_days": 1, "work_sum": 1,
                      "work_count": 1, "late_count": 1, "total_late_minutes": 1, "leave_count": 1,
                      "on_duty_count": 1}},
    ]
    return pipeline, ("department_joined" if department is not None else "employee_joined")


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    match = month_log_match(month)
    match["late_minutes"] = {"$gt": 0}  # only late records can contribute to a total > 0
    pipeline = [
        {"$match": match},
        {"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"},  # logs of unknown employees are dropped
    ]
    if department is not None:  # rank inside the department only, so filter BEFORE the window stage
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total": -1}, "output": {"rank": {"$rank": {}}}}},  # 1, 2, 2, 4
        {"$match": {"rank": {"$lte": limit}}},  # limit applies AFTER ranking
        {"$sort": {"total": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": "$total", "late_count": 1}},
    ]
    return pipeline


def trend_pipeline(department: str, from_date: str, to: str) -> list:
    first, last = date.fromisoformat(validate_date(from_date)), date.fromisoformat(validate_date(to))
    n_days = (last - first).days + 1
    if n_days < 1 or n_days > 92:
        raise HTTPException(422, "`to` must not precede `from`, and the range is at most 92 days")
    people = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not people:
        raise HTTPException(404, "department not found")
    calendar = {"$map": {"input": {"$range": [0, n_days]}, "as": "i", "in": {"$dateToString": {
        "format": "%Y-%m-%d",
        "date": {"$dateAdd": {"startDate": {"$dateFromString": {"dateString": from_date}},
                              "unit": "day", "amount": "$$i"}}}}}}
    joined = sorted(p["joined_on"] for p in people)
    return [
        # 1. only this department's logs in range, via the (date, emp_code) index
        {"$match": {"date": {"$gte": from_date, "$lte": to}, "emp_code": {"$in": [p["emp_code"] for p in people]}}},
        # 2. one row per day that has records; present2 = 2 x present days (half day = 1) so everything stays an integer
        {"$group": {"_id": "$date",
                    "present2": {"$sum": {"$cond": [IS_PRESENT, {"$cond": [{"$eq": ["$half_day", True]}, 1, 2]}, 0]}},
                    "late": {"$sum": {"$cond": [IS_LATE, 1, 0]}}}},
        # 3. $facet always emits exactly one document, even when no log matched
        {"$facet": {"daily": [{"$sort": {"_id": 1}}]}},
        # 4. gap filling: one row per calendar day, weekends and empty days included
        {"$project": {"_id": 0, "daily": 1, "days": calendar}},
        {"$unwind": "$days"},
        {"$set": {"stat": {"$arrayElemAt": [{"$filter": {"input": "$daily", "cond": {"$eq": ["$$this._id", "$days"]}}}, 0]},
                  "is_working_day": {"$lt": [{"$isoDayOfWeek": {"$dateFromString": {"dateString": "$days"}}}, 6]},
                  "headcount": {"$size": {"$filter": {"input": {"$literal": joined}, "as": "j",
                                                      "cond": {"$lte": ["$$j", "$days"]}}}}}},
        {"$set": {"present2": {"$ifNull": ["$stat.present2", 0]}, "late_count": {"$ifNull": ["$stat.late", 0]}}},
        # 5. rate in 1/10000 as an exact integer, half-up: floor(present*10000/headcount + 1/2)
        {"$set": {"rate_int": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [{"$add": [{"$multiply": ["$present2", 10000]}, "$headcount"]},
                                    {"$multiply": [2, "$headcount"]}]}},
            None]}}},
        # 6. 7-row window inside the range; integer sums keep the average exact
        {"$setWindowFields": {"sortBy": {"days": 1}, "output": {
            "win_sum": {"$sum": "$rate_int", "window": {"documents": [-6, 0]}},
            "win_n": {"$sum": {"$cond": [{"$isNumber": "$rate_int"}, 1, 0]}, "window": {"documents": [-6, 0]}}}}},
        {"$sort": {"days": 1}},
        {"$project": {"_id": 0, "date": "$days", "is_working_day": 1, "headcount": 1, "present2": 1,
                      "late_count": 1, "rate_int": 1, "win_sum": 1, "win_n": 1}},
    ]


# --------------------------------------------------------------------------- #
# Health + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        if not _indexes_ready:
            ensure_indexes()
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # the unique index is the guarantee; there is no check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    return {**doc, "created_at": epoch_ms(doc["created_at"])}


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = Query(1, ge=1),
                   page_size: int = Query(20, ge=1, le=100)):
    q = {"department": department} if department is not None else {}
    items = list(db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING)
                 .skip((page - 1) * page_size).limit(page_size))
    for item in items:
        item["created_at"] = epoch_ms(item.get("created_at"))
    return {"items": items, "total": db.employees.count_documents(q), "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    ts = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    day = attendance_day(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status, "punch_in": ts, "punch_out": None,
           "work_hours": None, "late_minutes": late_minutes(ts, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date): exactly one concurrent winner
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return public_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    out = dt_from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    record = db.attendance_logs.find_one({"emp_code": body.emp_code, "punch_in": {"$lte": out}},
                                         sort=[("punch_in", DESCENDING)])
    if not record:
        raise HTTPException(404, "no punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")
    started = utc_naive(record["punch_in"])
    if out <= started or out - started > timedelta(hours=24):
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")
    hours = work_hours(started, out)
    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},  # atomic: only one concurrent punch-out can match
        {"$set": {"punch_out": out, "work_hours": hours, "half_day": hours < 4.5,
                  "overtime_minutes": overtime_minutes(out, emp["shift_start"], emp["shift_end"], record["date"])}},
        return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "already punched out")
    return public_doc(updated)


@app.get("/attendance")
def list_attendance(emp_code: Optional[str] = None, date_from: Optional[str] = None, date_to: Optional[str] = None,
                    status: Optional[str] = None, page: int = Query(1, ge=1),
                    page_size: int = Query(20, ge=1, le=100)):
    q = attendance_filter(emp_code, date_from, date_to, status)
    cursor = (db.attendance_logs.find(q, {"_id": 0}).sort(ATTENDANCE_SORT)
              .skip((page - 1) * page_size).limit(page_size))
    total = db.attendance_logs.count_documents(q) if q else db.attendance_logs.estimated_document_count()
    return {"items": [public_doc(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    # NB: the path parameter `date` shadows the imported datetime.date class inside this function only.
    day = validate_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    old = db.attendance_logs.find_one({"emp_code": emp_code, "date": day})
    if not emp or not old:
        raise HTTPException(404, "employee or attendance record not found")

    status = body.status if body.status is not None else old["status"]
    absent = status in ("ABSENT", "LEAVE")
    if absent and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
    punch_in_at = None if absent else (dt_from_ms(body.punch_in) if body.punch_in is not None else old.get("punch_in"))
    punch_out_at = None if absent else (dt_from_ms(body.punch_out) if body.punch_out is not None else old.get("punch_out"))
    if not absent and punch_in_at is None:
        raise HTTPException(422, "presence status requires punch_in")
    if punch_in_at is not None and attendance_day(punch_in_at, emp) != day:
        raise HTTPException(422, "punch_in must belong to the record's attendance date")
    if punch_out_at is not None and (punch_out_at <= punch_in_at or punch_out_at - punch_in_at > timedelta(hours=24)):
        raise HTTPException(422, "punch_out must be after punch_in and within 24 hours")

    hours = work_hours(punch_in_at, punch_out_at) if punch_in_at and punch_out_at else None
    final = {"status": status, "punch_in": punch_in_at, "punch_out": punch_out_at, "work_hours": hours,
             "late_minutes": late_minutes(punch_in_at, emp["shift_start"], day) if punch_in_at else 0,
             "overtime_minutes": (overtime_minutes(punch_out_at, emp["shift_start"], emp["shift_end"], day)
                                  if punch_out_at else 0),
             "half_day": bool(hours is not None and hours < 4.5)}
    changes = {}
    for key, value in final.items():
        before = old.get(key, DERIVED_DEFAULTS.get(key))
        if before != value:
            changes[key] = {"from": before, "to": value}
    if not changes:
        raise HTTPException(422, "request makes no changes")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}
    # Optimistic concurrency: apply only if nobody changed the fields we derived from or appended to history meanwhile.
    guard = {"_id": old["_id"], "status": old["status"], "punch_in": old.get("punch_in"),
             "punch_out": old.get("punch_out"),
             "history": {"$size": len(old["history"])} if "history" in old else {"$exists": False}}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": final, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER)
    if not updated:
        raise HTTPException(409, "record changed concurrently; retry")
    return public_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    start, end = month_bounds(month)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rows = list(db.attendance_logs.aggregate(monthly_pipeline(month, emp_code)))
    data = rows[0] if rows else {}
    first_day = max(start, date.fromisoformat(emp["joined_on"]))
    wd = working_days(first_day, end) if first_day <= end else 0
    present = data.get("present_days", 0)
    return {"emp_code": emp_code, "month": month, "working_days": wd, "present_days": round_half_up(present),
            "leave_days": data.get("leave_days", 0), "late_count": data.get("late_count", 0),
            "total_late_minutes": data.get("total_late_minutes", 0),
            "total_overtime_minutes": data.get("total_overtime_minutes", 0),
            "attendance_pct": round_half_up(Decimal(str(present)) / Decimal(wd) * 100) if wd else None}


@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    pipeline, hint = summary_plan(month, department)
    items = []
    for row in db.employees.aggregate(pipeline, hint=hint):
        count, total = row.pop("work_count"), to_decimal(row.pop("work_sum"))
        row["avg_work_hours"] = round_half_up(total / Decimal(count)) if count else None
        row["present_days"] = round_half_up(row["present_days"])
        items.append(row)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = Query(10, ge=1, le=50), department: Optional[str] = None):
    return {"month": month, "items": list(db.attendance_logs.aggregate(leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_date: str = Query(..., alias="from"), to: str = Query(...)):
    items = []
    for row in db.attendance_logs.aggregate(trend_pipeline(department, from_date, to)):
        rate_int, window_n = row["rate_int"], int(row["win_n"])
        items.append({
            "date": row["date"], "is_working_day": row["is_working_day"], "headcount": row["headcount"],
            "present_count": row["present2"] / 2, "late_count": row["late_count"],
            "attendance_rate": None if rate_int is None else float(Decimal(int(rate_int)) / 10000),
            "moving_avg_7d": (round_half_up(Decimal(int(row["win_sum"])) / (Decimal(window_n) * 10000), 4)
                              if window_n else None)})
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def aggregate_command(collection: str, pipeline: list, hint: Optional[str] = None) -> dict:
    command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}
    if hint:
        command["hint"] = hint
    return command


def require(**params):
    missing = [name for name, value in params.items() if value in (None, "")]
    if missing:
        raise HTTPException(422, f"missing required parameter(s): {', '.join(missing)}")


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: str, emp_code: Optional[str] = None, month: Optional[str] = None,
            department: Optional[str] = None, limit: int = Query(10, ge=1, le=50),
            date_from: Optional[str] = None, date_to: Optional[str] = None, status: Optional[str] = None,
            page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100),
            from_date: Optional[str] = Query(None, alias="from"), to: Optional[str] = None):
    if endpoint == "attendance_list":
        collection = "attendance_logs"
        command = attendance_find_command(attendance_filter(emp_code, date_from, date_to, status), page, page_size)
    elif endpoint == "employee_monthly":
        require(emp_code=emp_code, month=month)
        collection, command = "attendance_logs", aggregate_command("attendance_logs", monthly_pipeline(month, emp_code))
    elif endpoint == "department_summary":
        require(month=month)
        pipeline, hint = summary_plan(month, department)
        collection, command = "employees", aggregate_command("employees", pipeline, hint)
    elif endpoint == "late_leaderboard":
        require(month=month)
        collection = "attendance_logs"
        command = aggregate_command(collection, leaderboard_pipeline(month, limit, department))
    elif endpoint == "department_trend":
        require(department=department, **{"from": from_date}, to=to)
        collection = "attendance_logs"
        command = aggregate_command(collection, trend_pipeline(department, from_date, to))
    else:
        raise HTTPException(422, "unknown endpoint")
    raw = db.command("explain", command, verbosity="executionStats")
    # Explain output contains BSON types (Int64, Timestamp, ...); round-trip through Extended JSON for FastAPI.
    return {"endpoint": endpoint, "collection": collection, "explain": json.loads(json_util.dumps(raw))}
