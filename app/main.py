"""Employee attendance API. Instants are UTC in MongoDB and epoch ms over HTTP."""
import os
import re
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"), tz_aware=True)
db = client[os.getenv("MONGO_DB", "attendance_db")]
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}
app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def indexes():
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="employee_code_uq")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)], name="department_joined")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True, name="employee_date_uq")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)], name="date_employee")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING), ("status", ASCENDING)], name="employee_month")

def round_num(n, places=2):
    return float(Decimal(str(n)).quantize(Decimal("1") if places == 0 else Decimal("1." + "0" * places), rounding=ROUND_HALF_UP))

def parse_date(s: str) -> date:
    try:
        d = date.fromisoformat(s)
        if d.isoformat() != s: raise ValueError()
        return d
    except (ValueError, TypeError): raise HTTPException(422, "date must be YYYY-MM-DD")

def validate_ms(v):
    if v is None: return None
    if type(v) is not int or not 100000000000 <= v <= 4102444800000:
        raise ValueError("timestamp must be epoch milliseconds in supported range")
    return datetime.fromtimestamp(v / 1000, UTC).replace(microsecond=0)

def dt_ms(dt):
    if dt is None: return None
    if dt.tzinfo is None: dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)

def clock(s): return time.fromisoformat(s)
def local(dt): return dt.astimezone(IST)
def attendance_date(punch, emp):
    t = local(punch)
    d = t.date()
    if clock(emp["shift_end"]) <= clock(emp["shift_start"]) and t.time().replace(tzinfo=None) < clock(emp["shift_end"]):
        d -= timedelta(days=1)
    return d.isoformat()

def late_minutes(punch, emp):
    t = local(punch)
    start = datetime.combine(t.date(), clock(emp["shift_start"]), IST)
    if clock(emp["shift_end"]) <= clock(emp["shift_start"]) and t.time().replace(tzinfo=None) < clock(emp["shift_end"]): start -= timedelta(days=1)
    mins = int((t - start).total_seconds() // 60)
    return mins if (t - start).total_seconds() > 600 else 0

def derived(pin, pout, emp):
    if pin is None: return {"late_minutes": 0, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    late = late_minutes(pin, emp)
    if pout is None: return {"late_minutes": late, "work_hours": None, "overtime_minutes": 0, "half_day": False}
    hours = round_num((pout-pin).total_seconds()/3600)
    lt = local(pin); endday = lt.date()
    if clock(emp["shift_end"]) <= clock(emp["shift_start"]): endday += timedelta(days=1)
    end = datetime.combine(endday, clock(emp["shift_end"]), IST)
    over = int((local(pout)-end).total_seconds() // 60)
    return {"late_minutes": late, "work_hours": hours, "overtime_minutes": over if over >= 30 else 0, "half_day": hours < 4.5}

def public_record(doc):
    out = {k:v for k,v in doc.items() if k != "_id"}
    for k in ("punch_in", "punch_out"): out[k] = dt_ms(out.get(k))
    out.setdefault("late_minutes", 0); out.setdefault("overtime_minutes", 0); out.setdefault("half_day", False); out.setdefault("history", [])
    for h in out["history"]:
        h["at"] = dt_ms(h["at"])
        for c in h.get("changes", {}).values():
            for side in ("from", "to"):
                if isinstance(c.get(side), datetime): c[side] = dt_ms(c[side])
    return out

def month_bounds(month):
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month): raise HTTPException(422, "month must be YYYY-MM")
    y,m=map(int,month.split("-")); return f"{month}-01", f"{month}-{monthrange(y,m)[1]:02d}"

class StrictModel(BaseModel):
    model_config = ConfigDict(extra="ignore")
class EmployeeIn(StrictModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$"); name: str=Field(min_length=1,max_length=100); email: str=Field(max_length=120,pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"); department: str=Field(min_length=1,max_length=50)
    shift_start: str="09:30"; shift_end: str="18:30"; joined_on: str
    @field_validator("shift_start","shift_end")
    @classmethod
    def shift(cls,v):
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d",v): raise ValueError("expected HH:MM")
        return v
    @field_validator("joined_on")
    @classmethod
    def joined(cls,v): parse_date(v); return v
    @field_validator("shift_end")
    @classmethod
    def distinct(cls,v,info):
        if info.data.get("shift_start") == v: raise ValueError("shift times must differ")
        return v
class PunchInIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt]=None
    status: Literal["PRESENT","WFH","ON_DUTY"]="PRESENT"
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls,v): return validate_ms(v)
class PunchOutIn(StrictModel):
    emp_code: str
    punched_at: Optional[StrictInt]=None
    @field_validator("punched_at")
    @classmethod
    def timestamp(cls,v): return validate_ms(v)
class RegularizeIn(StrictModel):
    status: Optional[Literal["PRESENT","WFH","ON_DUTY","ABSENT","LEAVE"]]=None
    punch_in: Optional[StrictInt]=None; punch_out: Optional[StrictInt]=None
    reason: str=Field(min_length=5,max_length=200); regularized_by: str=Field(min_length=1,max_length=50)
    @field_validator("punch_in","punch_out")
    @classmethod
    def timestamp(cls,v): return validate_ms(v)

@app.get("/health")
def health():
    try: db.command("ping"); return {"status":"ok"}
    except Exception: raise HTTPException(503,"database unavailable")

@app.post("/employees",status_code=201)
def create_employee(body: EmployeeIn):
    doc=body.model_dump(); doc["created_at"]=datetime.now(UTC).replace(microsecond=0)
    try: db.employees.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"emp_code already exists")
    doc["created_at"]=dt_ms(doc["created_at"]); return doc

@app.get("/employees")
def list_employees(department:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={"department":department} if department is not None else {}
    total=db.employees.count_documents(q); cur=db.employees.find(q,{"_id":0}).sort("emp_code",ASCENDING).skip((page-1)*page_size).limit(page_size)
    items=list(cur)
    for e in items: e["created_at"]=dt_ms(e["created_at"])
    return {"items":items,"total":total,"page":page,"page_size":page_size}

@app.post("/attendance/punch-in",status_code=201)
def punch_in(body:PunchInIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=body.punched_at or datetime.now(UTC); ts=ts.replace(microsecond=0) if isinstance(ts,datetime) else ts
    d=attendance_date(ts,emp)
    doc={"emp_code":body.emp_code,"date":d,"status":body.status,"punch_in":ts,"punch_out":None,"work_hours":None,"late_minutes":late_minutes(ts,emp),"overtime_minutes":0,"half_day":False,"history":[]}
    try: db.attendance_logs.insert_one(doc)
    except DuplicateKeyError: raise HTTPException(409,"already punched in for this date")
    return public_record(doc)

@app.post("/attendance/punch-out")
def punch_out(body:PunchOutIn):
    emp=db.employees.find_one({"emp_code":body.emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    ts=body.punched_at or datetime.now(UTC); ts=ts.replace(microsecond=0) if isinstance(ts,datetime) else ts
    rec=db.attendance_logs.find_one({"emp_code":body.emp_code,"punch_in":{"$ne":None,"$lte":ts}},sort=[("punch_in",DESCENDING)])
    if not rec: raise HTTPException(404,"no punch-in found")
    if rec.get("punch_out") is not None: raise HTTPException(409,"already punched out")
    if ts<=rec["punch_in"] or ts-rec["punch_in"]>timedelta(hours=24): raise HTTPException(422,"punch-out must be after punch-in and within 24 hours")
    der=derived(rec["punch_in"],ts,emp)
    updated=db.attendance_logs.find_one_and_update({"_id":rec["_id"],"punch_out":None},{"$set":{"punch_out":ts,**der}},return_document=ReturnDocument.AFTER)
    if updated is None: raise HTTPException(409,"already punched out")
    return public_record(updated)

@app.get("/attendance")
def list_attendance(emp_code:Optional[str]=None,date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100)):
    q={}
    if emp_code: q["emp_code"]=emp_code
    if date_from or date_to:
        q["date"]={}
        if date_from: parse_date(date_from); q["date"]["$gte"]=date_from
        if date_to: parse_date(date_to); q["date"]["$lte"]=date_to
        if date_from and date_to and date_from>date_to: raise HTTPException(422,"date_from must be <= date_to")
    if status: q["status"]=status
    total=db.attendance_logs.count_documents(q); docs=db.attendance_logs.find(q).sort([("date",DESCENDING),("emp_code",ASCENDING)]).skip((page-1)*page_size).limit(page_size)
    return {"items":[public_record(d) for d in docs],"total":total,"page":page,"page_size":page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code:str,date:str,body:RegularizeIn):
    parse_date(date); emp=db.employees.find_one({"emp_code":emp_code})
    old=db.attendance_logs.find_one({"emp_code":emp_code,"date":date})
    if not emp or not old: raise HTTPException(404,"employee or attendance record not found")
    changes_in=body.model_dump(exclude={"reason","regularized_by"},exclude_unset=True)
    status=changes_in.get("status",old["status"])
    if status in {"ABSENT","LEAVE"}:
        if "punch_in" in changes_in or "punch_out" in changes_in: raise HTTPException(422,"absence/leave cannot have punch times")
        pin=pout=None
    else:
        pin=changes_in.get("punch_in",old.get("punch_in")); pout=changes_in.get("punch_out",old.get("punch_out"))
        if pin is None: raise HTTPException(422,"presence status requires punch_in")
        if attendance_date(pin,emp)!=date: raise HTTPException(422,"punch_in must belong to attendance date")
        if pout is not None and (pout<=pin or pout-pin>timedelta(hours=24)): raise HTTPException(422,"invalid punch_out")
    der=derived(pin,pout,emp) if status in PRESENCE else derived(None,None,emp)
    values={"status":status,"punch_in":pin,"punch_out":pout,**der}
    changed={}
    for k,v in values.items():
        before=old.get(k)
        if before!=v: changed[k]={"from":before,"to":v}
    if not changed: raise HTTPException(422,"request makes no changes")
    at=datetime.now(UTC).replace(microsecond=0)
    entry={"at":at,"by":body.regularized_by,"reason":body.reason,"changes":changed}
    # Conditional update prevents concurrent corrections from overwriting the same old state/history.
    q={"_id":old["_id"],"history":old.get("history",[])}
    update=db.attendance_logs.find_one_and_update(q,{"$set":values,"$push":{"history":entry}},return_document=ReturnDocument.AFTER)
    if update is None: raise HTTPException(409,"record changed concurrently; retry")
    return public_record(update)

def month_match(month):
    lo,hi=month_bounds(month); return {"date":{"$gte":lo,"$lte":hi}}
def present_expr(): return {"$in":["$status",list(PRESENCE)]}
def weekday_expr(date_expr): return {"$lt":[{"$isoDayOfWeek":{"$dateFromString":{"dateString":date_expr,"timezone":"Asia/Kolkata"}}},6]}

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code:str,month:str):
    emp=db.employees.find_one({"emp_code":emp_code})
    if not emp: raise HTTPException(404,"employee not found")
    lo,hi=month_bounds(month); start=max(parse_date(lo),parse_date(emp["joined_on"])); end=parse_date(hi)
    working=sum(1 for i in range(max(0,(end-start).days+1)) if (start+timedelta(days=i)).weekday()<5)
    pipe=[{"$match":{"emp_code":emp_code,**month_match(month)}},{"$group":{"_id":None,"present":{"$sum":{"$cond":[{"$and":[present_expr(),weekday_expr("$date")]},{"$cond":["$half_day",0.5,1]},0]}},"leave":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"late_total":{"$sum":{"$ifNull":["$late_minutes",0]}},"over":{"$sum":{"$ifNull":["$overtime_minutes",0]}}}}]
    x=list(db.attendance_logs.aggregate(pipe)); g=x[0] if x else {}
    present=round_num(g.get("present",0)); pct=round_num(present/working*100,2) if working else None
    return {"emp_code":emp_code,"month":month,"working_days":working,"present_days":present,"leave_days":g.get("leave",0),"late_count":g.get("late",0),"total_late_minutes":g.get("late_total",0),"total_overtime_minutes":g.get("over",0),"attendance_pct":pct}

@app.get("/analytics/departments/summary")
def department_summary(month:str,department:Optional[str]=None):
    lo,hi=month_bounds(month); match={"joined_on":{"$lte":hi}}
    if department is not None: match["department"]=department
    pipe=[{"$match":match},{"$lookup":{"from":"attendance_logs","let":{"code":"$emp_code"},"pipeline":[{"$match":{"$expr":{"$and":[{"$eq":["$emp_code","$$code"]},{"$gte":["$date",lo]},{"$lte":["$date",hi]}]}}},{"$group":{"_id":None,"present":{"$sum":{"$cond":[{"$and":[present_expr(),weekday_expr("$date")]},{"$cond":["$half_day",0.5,1]},0]}},"hours":{"$avg":{"$cond":[{"$and":[present_expr(),{"$ne":["$work_hours",None]}]},"$work_hours",None]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}},"late_total":{"$sum":{"$ifNull":["$late_minutes",0]}},"leave":{"$sum":{"$cond":[{"$eq":["$status","LEAVE"]},1,0]}},"duty":{"$sum":{"$cond":[{"$eq":["$status","ON_DUTY"]},1,0]}}}}],"as":"stats"}},{"$addFields":{"s":{"$ifNull":[{"$arrayElemAt":["$stats",0]},{"present":0,"hours":None,"late":0,"late_total":0,"leave":0,"duty":0}]}}},{"$group":{"_id":"$department","headcount":{"$sum":1},"present_days":{"$sum":"$s.present"},"hours":{"$push":"$s.hours"},"late_count":{"$sum":"$s.late"},"total_late_minutes":{"$sum":"$s.late_total"},"leave_count":{"$sum":"$s.leave"},"on_duty_count":{"$sum":"$s.duty"}}},{"$project":{"_id":0,"department":"$_id","headcount":1,"present_days":{"$round":["$present_days",2]},"avg_work_hours":{"$cond":[{"$eq":[{"$size":{"$filter":{"input":"$hours","as":"h","cond":{"$ne":["$$h",None]}}}},0]},None,{"$round":[{"$avg":{"$filter":{"input":"$hours","as":"h","cond":{"$ne":["$$h",None]}}}},2]}]},"late_count":1,"total_late_minutes":1,"leave_count":1,"on_duty_count":1}},{"$sort":{"department":1}}]
    return {"month":month,"items":list(db.employees.aggregate(pipe))}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month:str,limit:int=Query(10,ge=1,le=50),department:Optional[str]=None):
    lo,hi=month_bounds(month)
    pipe=[{"$match":{**month_match(month),"late_minutes":{"$gt":0}}},{"$group":{"_id":"$emp_code","total":{"$sum":"$late_minutes"},"count":{"$sum":1}}},{"$lookup":{"from":"employees","localField":"_id","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"}]
    if department is not None: pipe.append({"$match":{"emp.department":department}})
    pipe += [{"$sort":{"total":-1,"_id":1}},{"$setWindowFields":{"sortBy":{"total":-1},"output":{"rank":{"$rank":{}}}}},{"$match":{"rank":{"$lte":limit}}},{"$project":{"_id":0,"rank":1,"emp_code":"$_id","name":"$emp.name","department":"$emp.department","total_late_minutes":"$total","late_count":"$count"}},{"$sort":{"total_late_minutes":-1,"emp_code":1}}]
    return {"month":month,"items":list(db.attendance_logs.aggregate(pipe))}

@app.get("/analytics/departments/{department}/trend")
def department_trend(department:str,from_:str=Query(...,alias="from"),to:str=Query(...)):
    a,b=parse_date(from_),parse_date(to)
    if b<a or (b-a).days+1>92: raise HTTPException(422,"invalid trend range")
    if not db.employees.find_one({"department":department}): raise HTTPException(404,"department not found")
    lo,hi=a.isoformat(),b.isoformat()
    # Start from a MongoDB date seed so a range with zero attendance still has every day.
    pipe=[{"$documents":[{"date":datetime.combine(a,time.min,IST)}]},{"$densify":{"field":"date","range":{"step":1,"unit":"day","bounds":[datetime.combine(a,time.min,IST),datetime.combine(b,time.min,IST)]}}},{"$lookup":{"from":"attendance_logs","let":{"d":{"$dateToString":{"date":"$date","format":"%Y-%m-%d","timezone":"Asia/Kolkata"}}},"pipeline":[{"$match":{"$expr":{"$eq":["$date","$$d"]}}},{"$lookup":{"from":"employees","localField":"emp_code","foreignField":"emp_code","as":"emp"}},{"$unwind":"$emp"},{"$match":{"emp.department":department}},{"$group":{"_id":None,"present":{"$sum":{"$cond":[present_expr(),{"$cond":["$half_day",0.5,1]},0]}},"late":{"$sum":{"$cond":[{"$gt":[{"$ifNull":["$late_minutes",0]},0]},1,0]}}}}],"as":"daily"}},{"$lookup":{"from":"employees","let":{"d":"$date"},"pipeline":[{"$match":{"department":department}},{"$match":{"$expr":{"$lte":["$joined_on",{"$dateToString":{"date":"$$d","format":"%Y-%m-%d","timezone":"Asia/Kolkata"}}]}}},{"$count":"n"}],"as":"hc"}},{"$set":{"date":{"$dateToString":{"date":"$date","format":"%Y-%m-%d","timezone":"Asia/Kolkata"}},"headcount":{"$ifNull":[{"$arrayElemAt":["$hc.n",0]},0]},"present":{"$ifNull":[{"$arrayElemAt":["$daily.present",0]},0]},"late":{"$ifNull":[{"$arrayElemAt":["$daily.late",0]},0]}}},{"$set":{"is_working_day":{"$lt":[{"$isoDayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]},"attendance_rate":{"$cond":[{"$and":[{"$lt":[{"$isoDayOfWeek":{"$dateFromString":{"dateString":"$date"}}},6]},{"$gt":["$headcount",0]}]},{"$round":[{"$divide":["$present","$headcount"]},4]},None]}}},{"$setWindowFields":{"sortBy":{"date":1},"output":{"moving_avg_7d":{"$avg":"$attendance_rate","window":{"documents":[-6,0]}}}}},{"$project":{"_id":0,"date":1,"is_working_day":1,"headcount":1,"present_count":"$present","late_count":"$late","attendance_rate":1,"moving_avg_7d":{"$round":["$moving_avg_7d",4]}}},{"$sort":{"date":1}}]
    return {"department":department,"items":list(db.aggregate(pipe))}

@app.get("/admin/explain/{endpoint}")
def explain(endpoint:str,emp_code:Optional[str]=None,month:Optional[str]=None,department:Optional[str]=None,limit:int=Query(10,ge=1,le=50),date_from:Optional[str]=None,date_to:Optional[str]=None,status:Optional[str]=None,page:int=Query(1,ge=1),page_size:int=Query(20,ge=1,le=100),from_:Optional[str]=Query(None,alias="from"),to:Optional[str]=None):
    if endpoint=="attendance_list":
        q={}
        if emp_code:q["emp_code"]=emp_code
        if status:q["status"]=status
        if date_from or date_to:
            q["date"]={}
            if date_from:q["date"]["$gte"]=date_from
            if date_to:q["date"]["$lte"]=date_to
        exp=db.command("explain",{"find":"attendance_logs","filter":q,"sort":{"date":-1,"emp_code":1},"skip":(page-1)*page_size,"limit":page_size},verbosity="executionStats"); coll="attendance_logs"
    elif endpoint=="employee_monthly":
        if not emp_code or not month: raise HTTPException(422,"emp_code and month required")
        month_bounds(month); exp=db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{"emp_code":emp_code,**month_match(month)}}],"cursor":{}},verbosity="executionStats"); coll="attendance_logs"
    elif endpoint=="department_summary":
        if not month: raise HTTPException(422,"month required")
        lo,hi=month_bounds(month); q={"joined_on":{"$lte":hi}}
        if department:q["department"]=department
        exp=db.command("explain",{"aggregate":"employees","pipeline":[{"$match":q}],"cursor":{}},verbosity="executionStats"); coll="employees"
    elif endpoint=="late_leaderboard":
        if not month: raise HTTPException(422,"month required")
        month_bounds(month); exp=db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{**month_match(month),"late_minutes":{"$gt":0}}}],"cursor":{}},verbosity="executionStats"); coll="attendance_logs"
    elif endpoint=="department_trend":
        if not department or not from_ or not to: raise HTTPException(422,"department, from and to required")
        exp=db.command("explain",{"aggregate":"attendance_logs","pipeline":[{"$match":{"date":{"$gte":from_,"$lte":to}}}],"cursor":{}},verbosity="executionStats"); coll="attendance_logs"
    else: raise HTTPException(422,"unknown endpoint")
    return {"endpoint":endpoint,"collection":coll,"explain":exp}
