"""
Event Resource Reservation - BACKEND ONLY (Kushwant)
Run local:  pip install -r requirements.txt  ->  python booking_server.py
Deploy:     gunicorn booking_server:app     (Render / PythonAnywhere)

Env vars (all optional):
  GROQ_API_KEY  -> turns on /chat and /speech-to-text (rest works without it)
  ADMIN_KEY     -> password for approve/reject (header X-Admin-Key). Default: admin123
  DB_PATH       -> where bookings.db lives
  FLASK_DEBUG=1 -> debug mode (local only)

API CONTRACT (Harshil ke liye). Time format: "YYYY-MM-DDTHH:MM"
  GET  /                         health + list of endpoints
  GET  /resources                all resources with capacity, location, description, rules
  GET  /resources/<id>           one resource (+ what it requires)
  GET  /bookings?from=&to=&status=pending,approved   (default: pending,approved)
  GET  /availability?date=YYYY-MM-DD[&resource_id=]  busy + free windows per resource
  POST /bookings/check   {resource_ids,start,end}              -> ok, conflicts, suggestions
  POST /bookings         {event_name,requester,resource_ids,start,end} -> 201 (status=pending) | 409 clash
  POST /bookings/<id>/cancel                                   -> status=cancelled
  POST /bookings/<id>/status  {status:"approved"|"rejected"}   -> ADMIN (X-Admin-Key header)
  GET  /bookings/<id>/flow       steps + current step for a flowchart
  GET  /process-flow             steps of the whole booking process (flowchart)
  GET  /faq[?q=...]              booking rules / FAQs
  POST /chat   {message, history:[{query,answer}]}  -> AI assistant (never books by itself;
                                  returns a `draft` the frontend confirms via POST /bookings)
  POST /speech-to-text  (multipart, field "audio")             -> {text}
  GET  /demo-requests            8 ready-made overlapping requests
"""
import os, json, difflib, sqlite3
from datetime import datetime, timedelta, date
from flask import Flask, request, jsonify, g, send_from_directory

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))
except ImportError:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
# Vercel pe sirf /tmp me file likh sakte hain (wahan data temporary hota hai)
DB = os.environ.get("DB_PATH") or ("/tmp/bookings.db" if os.environ.get("VERCEL") else os.path.join(HERE, "bookings.db"))
SCHEMA_VERSION = 2
FMT = "%Y-%m-%dT%H:%M"
DAY_START_HOUR, DAY_END_HOUR = 8, 20
STEP_MIN = 30
HORIZON_DAYS = 14
ACTIVE = ("pending", "approved")          # these block a slot
ADMIN_KEY = os.environ.get("ADMIN_KEY", "admin123")
MODEL_NAME = "openai/gpt-oss-120b"

app = Flask(__name__)


# ---------- database ----------
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_):
    d = g.pop("db", None)
    if d: d.close()

def init_db():
    con = sqlite3.connect(DB)
    con.executescript("""
    DROP TABLE IF EXISTS booking_resources;
    DROP TABLE IF EXISTS bookings;
    DROP TABLE IF EXISTS resources;
    CREATE TABLE resources(id INTEGER PRIMARY KEY, name TEXT, requires_id INTEGER,
                           capacity INTEGER, location TEXT, description TEXT, rules TEXT);
    CREATE TABLE bookings(id INTEGER PRIMARY KEY, event_name TEXT, requester TEXT,
                          start TEXT, end TEXT, status TEXT DEFAULT 'pending');
    CREATE TABLE booking_resources(booking_id INTEGER, resource_id INTEGER);
    """)
    # id, name, requires_id, capacity, location, description, rules   (edit these to your campus)
    con.executemany("INSERT INTO resources VALUES(?,?,?,?,?,?,?)", [
        (1, "Generator", None, None, "Power yard behind Main Stage",
         "Backup power unit for outdoor events.", "Added automatically whenever Main Stage is booked."),
        (2, "Main Stage", 1, 1500, "Open Ground, North Side",
         "Outdoor stage with backdrop and lighting rig.", "Needs the Generator. Setup 30 min before event."),
        (3, "Sound System", None, 2000, "Equipment Store, Block C",
         "PA speakers, mixer and 6 wireless mics.", "Booked with an operator. No outdoor use in rain."),
        (4, "Auditorium", None, 500, "Academic Block A, Ground Floor",
         "Air-conditioned hall with fixed seating and stage.", "No food or drinks inside."),
        (5, "Projector", None, None, "AV Room, Block B",
         "Portable full-HD projector with screen.", "Return in the same condition."),
        (6, "Seminar Hall", None, 120, "Academic Block B, First Floor",
         "Seminar room with podium and whiteboard.", "Keep the room tidy after use."),
    ])
    con.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    con.commit(); con.close()

def base_day():
    return date(2026, 10, 10)             # demo date kept fixed on purpose

def at(day_offset, hour, minute=0):
    d = datetime.combine(base_day() + timedelta(days=day_offset), datetime.min.time())
    return (d + timedelta(hours=hour, minutes=minute)).strftime(FMT)

def seed_bookings():
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    existing = [
        ("Tech Fest Opening", "Tech Club", at(0, 14), at(0, 16), [2]),
        ("Dance Night", "Cultural Club", at(1, 17), at(1, 20), [2, 3]),
        ("Guest Lecture", "CSE Dept", at(2, 10), at(2, 12), [4, 5]),
        ("Workshop", "Coding Club", at(5, 11), at(5, 13), [6, 5]),
    ]
    for name, who, s, e, rids in existing:
        cur = con.execute("INSERT INTO bookings(event_name,requester,start,end,status) VALUES(?,?,?,?,'approved')",
                          (name, who, s, e))
        con.executemany("INSERT INTO booking_resources VALUES(?,?)",
                        [(cur.lastrowid, r) for r in expand(rids, con)])
    con.commit(); con.close()

def ensure_db():
    need = not os.path.exists(DB)
    if not need:
        con = sqlite3.connect(DB)
        need = con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION
        con.close()
    if need:
        init_db(); seed_bookings()

def demo_requests():
    return [
        {"event_name": "Band Practice",   "resource_ids": [2],    "start": at(0, 15), "end": at(0, 17)},
        {"event_name": "Quiz Finals",     "resource_ids": [4, 5], "start": at(2, 11), "end": at(2, 13)},
        {"event_name": "Fashion Show",    "resource_ids": [2, 3], "start": at(1, 18), "end": at(1, 21)},
        {"event_name": "Seminar A",       "resource_ids": [6],    "start": at(5, 12), "end": at(5, 14)},
        {"event_name": "Orientation",     "resource_ids": [4],    "start": at(2, 9),  "end": at(2, 11)},
        {"event_name": "Sound Check",     "resource_ids": [3],    "start": at(1, 16), "end": at(1, 18)},
        {"event_name": "Hackathon Demo",  "resource_ids": [5],    "start": at(5, 12), "end": at(5, 14)},
        {"event_name": "Farewell Rehearsal", "resource_ids": [2], "start": at(0, 13), "end": at(0, 15)},
    ]


# ---------- core logic ----------
def expand(resource_ids, con=None):
    """Main Stage chuna to Generator apne aap jud jaye."""
    con = con or db()
    out, stack = [], list(resource_ids)
    while stack:
        rid = stack.pop()
        if rid in out: continue
        out.append(rid)
        row = con.execute("SELECT requires_id FROM resources WHERE id=?", (rid,)).fetchone()
        if row and row["requires_id"]:
            stack.append(row["requires_id"])
    return sorted(out)

def _ph(n): return ",".join("?" * n)

def find_conflicts(resource_ids, start, end):
    """Rule: naya_start < purana_end AND purana_start < naya_end (sirf pending/approved)."""
    q = """SELECT DISTINCT r.name AS resource, b.event_name, b.start, b.end, b.status
           FROM bookings b
           JOIN booking_resources br ON br.booking_id = b.id
           JOIN resources r ON r.id = br.resource_id
           WHERE b.status IN ({}) AND br.resource_id IN ({})
             AND ? < b.end AND b.start < ?""".format(_ph(len(ACTIVE)), _ph(len(resource_ids)))
    return [dict(r) for r in db().execute(q, (*ACTIVE, *resource_ids, start, end)).fetchall()]

def suggest_slots(resource_ids, start, end, n=3):
    s0, e0 = datetime.strptime(start, FMT), datetime.strptime(end, FMT)
    dur = e0 - s0
    t = datetime.combine(s0.date(), datetime.min.time()).replace(hour=DAY_START_HOUR)
    horizon_end = t + timedelta(days=HORIZON_DAYS)
    options = []
    while t < horizon_end:
        t_end = t + dur
        in_hours = (t.hour >= DAY_START_HOUR and t_end.date() == t.date() and
                    (t_end.hour < DAY_END_HOUR or (t_end.hour == DAY_END_HOUR and t_end.minute == 0)))
        if in_hours and t != s0:
            if not find_conflicts(resource_ids, t.strftime(FMT), t_end.strftime(FMT)):
                options.append((abs((t - s0).total_seconds()), t, t_end))
        t += timedelta(minutes=STEP_MIN)
        if t.hour >= DAY_END_HOUR:
            t = datetime.combine(t.date() + timedelta(days=1), datetime.min.time()).replace(hour=DAY_START_HOUR)
    options.sort(key=lambda x: x[0])
    return [{"start": a.strftime(FMT), "end": b.strftime(FMT)} for _, a, b in options[:n]]

def run_check(data):
    rids = expand(data["resource_ids"])
    names = {r["id"]: r["name"] for r in db().execute("SELECT id,name FROM resources")}
    conflicts = find_conflicts(rids, data["start"], data["end"])
    return rids, {"ok": not conflicts,
                  "resources_checked": [names[r] for r in rids],
                  "conflicts": conflicts,
                  "suggestions": [] if not conflicts else suggest_slots(rids, data["start"], data["end"])}

def valid(data, need_name=False):
    try:
        s = datetime.strptime(data["start"], FMT); e = datetime.strptime(data["end"], FMT)
        if e <= s: return "end, start ke baad hona chahiye"
        ids = data.get("resource_ids")
        if not ids or not isinstance(ids, list): return "resource_ids khaali hai"
        known = {r["id"] for r in db().execute("SELECT id FROM resources")}
        if any((not isinstance(i, int)) or i not in known for i in ids): return "resource_ids me galat id hai"
        if need_name and not str(data.get("event_name", "")).strip(): return "event_name chahiye"
    except (KeyError, ValueError, TypeError):
        return "start, end (YYYY-MM-DDTHH:MM) aur resource_ids chahiye"
    return None

def _busy(rids, day):
    q = """SELECT DISTINCT b.event_name, b.start, b.end, b.status
           FROM bookings b JOIN booking_resources br ON br.booking_id = b.id
           WHERE b.status IN ({}) AND br.resource_id IN ({}) AND ? < b.end AND b.start < ?
           ORDER BY b.start""".format(_ph(len(ACTIVE)), _ph(len(rids)))
    return [dict(r) for r in db().execute(q, (*ACTIVE, *rids, day + "T00:00", day + "T23:59")).fetchall()]

def free_windows(busy, day):
    cur = datetime.strptime(day + "T%02d:00" % DAY_START_HOUR, FMT)
    end = datetime.strptime(day + "T%02d:00" % DAY_END_HOUR, FMT)
    out = []
    for b in busy:
        s, e = datetime.strptime(b["start"], FMT), datetime.strptime(b["end"], FMT)
        if s > cur: out.append({"start": cur.strftime(FMT), "end": min(s, end).strftime(FMT)})
        cur = max(cur, e)
    if cur < end: out.append({"start": cur.strftime(FMT), "end": end.strftime(FMT)})
    return [w for w in out if w["start"] < w["end"]]

def availability(day, resource_id=None):
    rows = db().execute("SELECT id,name FROM resources" + (" WHERE id=?" if resource_id else ""),
                        (resource_id,) if resource_id else ()).fetchall()
    out = []
    for r in rows:
        busy = _busy(expand([r["id"]]), day)
        out.append({"resource_id": r["id"], "name": r["name"], "busy": busy, "free": free_windows(busy, day)})
    return out


# ---------- flowcharts + FAQ ----------
PROCESS_STEPS = ["Choose resources and time", "Clash check (Generator added with Main Stage)",
                 "Request saved as pending", "Admin approves or rejects", "Slot confirmed on calendar"]

def booking_flow(status):
    base = ["Request submitted", "Clash check passed"]
    if status == "pending":   return base + ["Waiting for admin approval", "Confirmed"], 2
    if status == "approved":  return base + ["Admin approved", "Confirmed"], 3
    if status == "rejected":  return base + ["Rejected by admin"], 2
    return base + ["Cancelled by requester"], 2

FAQ = {
    "clash": "A clash means another pending or approved booking uses the same resource at an overlapping time. The system then suggests the 3 nearest free slots.",
    "generator": "Main Stage needs the Generator, so the Generator is added to your booking automatically and checked for clashes too.",
    "approval": "New requests start as pending and block the slot. An admin then approves or rejects them.",
    "cancel": "You can cancel a pending or approved booking. The slot becomes free for others immediately.",
    "timing": "Suggested slots are between 8 AM and 8 PM, in 30 minute steps, within the next 14 days.",
    "overlap": "Back-to-back bookings are fine: a booking that ends at 4 PM does not clash with one starting at 4 PM.",
}

def faq_lookup(q):
    ql = q.lower()
    for k, v in FAQ.items():
        if k in ql: return v
    return None


# ---------- AI assistant (Groq) ----------
_client = None
class LLMUnavailable(Exception): pass

def get_client():
    global _client
    key = os.environ.get("GROQ_API_KEY")
    if not key: raise LLMUnavailable("GROQ_API_KEY set nahi hai")
    if _client is None:
        from groq import Groq
        _client = Groq(api_key=key)
    return _client

def llm(prompt):
    r = get_client().chat.completions.create(model=MODEL_NAME, messages=[{"role": "user", "content": prompt}])
    return r.choices[0].message.content.strip()

def parse_json(text):
    try:
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, json.JSONDecodeError):
        return {}

def today_str():
    return os.environ.get("DEMO_TODAY") or date.today().isoformat()

def interpret(message, context):
    names = [r["name"] for r in db().execute("SELECT name FROM resources")]
    t = datetime.strptime(today_str(), "%Y-%m-%d")
    prompt = (
        "You are the assistant of a campus event resource booking system.\n"
        f"Today is {t.strftime('%A %Y-%m-%d')}. Resources: {', '.join(names)}.\n"
        "Classify the user's message and extract details. Reply with ONLY a JSON object:\n"
        '{"intent": "BOOK|CHECK|AVAILABILITY|RESOURCE_INFO|FAQ|FLOWCHART|CANCEL|GENERAL",\n'
        ' "resources": [resource names from the list, [] if none],\n'
        ' "date": "YYYY-MM-DD or null", "start": "HH:MM 24h or null", "end": "HH:MM 24h or null",\n'
        ' "event_name": "string or null", "reply": "short friendly answer, only for GENERAL/FAQ, in the same language and script the user writes in"}\n'
        "BOOK = wants to reserve. CHECK = asks if a time is free. AVAILABILITY = asks what is free/busy on a day. "
        "RESOURCE_INFO = asks about a resource (capacity, location, rules). FAQ = booking rules. "
        "FLOWCHART = asks for the process or a diagram. CANCEL = wants to cancel a booking.\n"
        "Resolve words like 'kal', 'tomorrow', 'next Monday' using today's date. "
        "If no end time is given for a booking, leave end null.\n"
        f"{('Recent conversation:' + chr(10) + context + chr(10)) if context else ''}"
        f"Message: '{message}'"
    )
    return parse_json(llm(prompt))

def match_resources(names):
    rows = db().execute("SELECT id,name FROM resources").fetchall()
    lookup = {r["name"].lower(): r["id"] for r in rows}
    ids, unknown = [], []
    for n in names or []:
        l = str(n).lower().strip()
        hit = lookup.get(l) or next((i for k, i in lookup.items() if k in l or l in k), None)
        if not hit:
            m = difflib.get_close_matches(l, list(lookup), n=1, cutoff=0.6)
            hit = lookup[m[0]] if m else None
        (ids.append(hit) if hit else unknown.append(n))
    return sorted(set(ids)), unknown

def fmt_t(s): return datetime.strptime(s, FMT).strftime("%d %b, %H:%M")

def describe_check(res):
    if res["ok"]:
        return f"{', '.join(res['resources_checked'])} are free for that time."
    lines = sorted({f"{c['resource']} is used by {c['event_name']} ({fmt_t(c['start'])} to {fmt_t(c['end'])})" for c in res["conflicts"]})
    txt = "That time clashes. " + "; ".join(lines) + "."
    if res["suggestions"]:
        txt += " Free slots nearby: " + ", ".join(fmt_t(s["start"]) for s in res["suggestions"]) + "."
    return txt

def resource_info_text(rid):
    r = db().execute("""SELECT r.*, d.name AS requires_name FROM resources r
                        LEFT JOIN resources d ON d.id = r.requires_id WHERE r.id=?""", (rid,)).fetchone()
    parts = [f"{r['name']}: {r['description']}", f"Location: {r['location']}."]
    if r["capacity"]: parts.append(f"Capacity: {r['capacity']}.")
    if r["requires_name"]: parts.append(f"Needs {r['requires_name']} (added automatically).")
    if r["rules"]: parts.append(f"Rules: {r['rules']}")
    return " ".join(parts)

def chat_reply(message, history):
    context = "\n".join(f"User: {t.get('query','')}\nAssistant: {t.get('answer','')}" for t in (history or [])[-2:])
    info = interpret(message, context)
    intent = str(info.get("intent", "GENERAL")).upper()
    out = {"text": "", "intent": intent, "draft": None, "check": None, "diagram": None,
           "matches": None, "availability": None, "missing": []}

    if intent in ("BOOK", "CHECK"):
        ids, unknown = match_resources(info.get("resources"))
        missing = []
        if not ids: missing.append("resources")
        if not (info.get("date") and info.get("start") and info.get("end")): missing.append("date, start and end time")
        if missing:
            out["missing"] = missing
            out["text"] = "Please tell me " + " and ".join(missing) + (f" (I don't know: {', '.join(map(str, unknown))})" if unknown else "") + "."
            return out
        draft = {"event_name": info.get("event_name") or "", "requester": "", "resource_ids": ids,
                 "start": f"{info['date']}T{info['start']}", "end": f"{info['date']}T{info['end']}"}
        err = valid(draft)
        if err:
            out["text"] = "I couldn't use those details: " + err
            return out
        _, res = run_check(draft)
        out["check"], out["draft"] = res, draft
        out["text"] = describe_check(res)
        if res["ok"] and intent == "BOOK":
            out["text"] += " Confirm to send the request" + ("" if draft["event_name"] else " (add an event name first)") + "."
    elif intent == "AVAILABILITY":
        ids, _ = match_resources(info.get("resources"))
        day = info.get("date") or today_str()
        out["availability"] = availability(day, ids[0] if len(ids) == 1 else None)
        free = {a["name"]: a["free"] for a in out["availability"]}
        out["text"] = f"Availability on {day}: " + "; ".join(
            f"{n}: " + (", ".join(f"{w['start'][11:]}-{w['end'][11:]}" for w in f) or "fully booked") for n, f in free.items())
    elif intent == "RESOURCE_INFO":
        ids, _ = match_resources(info.get("resources"))
        out["text"] = " ".join(resource_info_text(i) for i in ids) if ids else "Which resource do you want to know about?"
    elif intent == "FLOWCHART":
        out["diagram"] = {"title": "How a booking works", "steps": PROCESS_STEPS}
        out["text"] = "Here is the booking process step by step."
    elif intent == "CANCEL":
        name = info.get("event_name") or message
        rows = db().execute("SELECT id,event_name,start,end,status FROM bookings WHERE status IN ('pending','approved')").fetchall()
        hits = [dict(r) for r in rows if str(info.get("event_name") or "").lower() and str(info["event_name"]).lower() in r["event_name"].lower()]
        out["matches"] = hits
        out["text"] = (f"I found {len(hits)} active booking(s). Confirm which one to cancel." if hits
                       else "I couldn't find that booking. Tell me the event name.")
    else:  # FAQ / GENERAL
        out["text"] = faq_lookup(message) or info.get("reply") or "I can book resources, check clashes and show what is free. What do you need?"
    return out


# ---------- endpoints ----------
@app.before_request
def preflight():
    if request.method == "OPTIONS": return "", 204

@app.after_request
def cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Admin-Key"
    resp.headers["Access-Control-Allow-Methods"] = "GET,POST,PATCH,OPTIONS"
    return resp

@app.route("/")
def index():
    return jsonify({"service": "Event Resource Reservation API", "status": "running",
                    "ai_chat_enabled": bool(os.environ.get("GROQ_API_KEY")),
                    "endpoints": ["/resources", "/resources/<id>", "/bookings", "/bookings/check", "/availability",
                                  "/bookings/<id>/cancel", "/bookings/<id>/status", "/bookings/<id>/flow",
                                  "/process-flow", "/faq", "/chat", "/speech-to-text", "/demo-requests"]})

@app.route("/demo")
def demo_page():                      # optional: serves index.html if it sits next to this file
    if os.path.exists(os.path.join(HERE, "index.html")):
        return send_from_directory(HERE, "index.html")
    return jsonify({"error": "index.html not found"}), 404

@app.route("/resources")
def resources():
    rows = db().execute("""SELECT r.id, r.name, r.requires_id, d.name AS requires_name,
                                  r.capacity, r.location, r.description, r.rules
                           FROM resources r LEFT JOIN resources d ON d.id = r.requires_id""").fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/resources/<int:rid>")
def resource_one(rid):
    r = db().execute("""SELECT r.id, r.name, r.requires_id, d.name AS requires_name,
                               r.capacity, r.location, r.description, r.rules
                        FROM resources r LEFT JOIN resources d ON d.id = r.requires_id WHERE r.id=?""", (rid,)).fetchone()
    return (jsonify(dict(r)) if r else (jsonify({"error": "resource not found"}), 404))

@app.route("/bookings")
def bookings():
    f, t = request.args.get("from", "0000"), request.args.get("to", "9999")
    sts = [s for s in request.args.get("status", ",".join(ACTIVE)).split(",")
           if s in ("pending", "approved", "rejected", "cancelled")] or list(ACTIVE)
    rows = db().execute("""SELECT b.id, b.event_name, b.requester, b.start, b.end, b.status,
                                  GROUP_CONCAT(br.resource_id) AS resource_ids,
                                  GROUP_CONCAT(r.name, ', ') AS resource_names
                           FROM bookings b
                           JOIN booking_resources br ON br.booking_id = b.id
                           JOIN resources r ON r.id = br.resource_id
                           WHERE b.end >= ? AND b.start <= ? AND b.status IN ({})
                           GROUP BY b.id ORDER BY b.start""".format(_ph(len(sts))), (f, t + "~", *sts)).fetchall()
    return jsonify([dict(r) for r in rows])

@app.route("/availability")
def availability_ep():
    day = request.args.get("date", today_str())
    try: datetime.strptime(day, "%Y-%m-%d")
    except ValueError: return jsonify({"error": "date YYYY-MM-DD chahiye"}), 400
    rid = request.args.get("resource_id", type=int)
    return jsonify({"date": day, "resources": availability(day, rid)})

@app.route("/bookings/check", methods=["POST"])
def check():
    data = request.get_json(silent=True) or {}
    err = valid(data)
    if err: return jsonify({"error": err}), 400
    return jsonify(run_check(data)[1])

@app.route("/bookings", methods=["POST"])
def create_booking():
    data = request.get_json(silent=True) or {}
    err = valid(data, need_name=True)
    if err: return jsonify({"error": err}), 400
    rids, result = run_check(data)
    if not result["ok"]:
        return jsonify(result), 409
    con = db()
    cur = con.execute("INSERT INTO bookings(event_name,requester,start,end,status) VALUES(?,?,?,?,'pending')",
                      (data["event_name"].strip(), data.get("requester", ""), data["start"], data["end"]))
    con.executemany("INSERT INTO booking_resources VALUES(?,?)", [(cur.lastrowid, r) for r in rids])
    con.commit()
    return jsonify({"ok": True, "id": cur.lastrowid, "status": "pending",
                    "resources_booked": result["resources_checked"]}), 201

@app.route("/bookings/<int:bid>/cancel", methods=["POST"])
def cancel(bid):
    row = db().execute("SELECT status FROM bookings WHERE id=?", (bid,)).fetchone()
    if not row: return jsonify({"error": "booking not found"}), 404
    if row["status"] not in ACTIVE: return jsonify({"error": f"already {row['status']}"}), 400
    db().execute("UPDATE bookings SET status='cancelled' WHERE id=?", (bid,)); db().commit()
    return jsonify({"ok": True, "id": bid, "status": "cancelled"})

@app.route("/bookings/<int:bid>/status", methods=["POST", "PATCH"])
def set_status(bid):
    if request.headers.get("X-Admin-Key", "") != ADMIN_KEY:
        return jsonify({"error": "admin key chahiye (X-Admin-Key header)"}), 401
    new = (request.get_json(silent=True) or {}).get("status")
    if new not in ("approved", "rejected"): return jsonify({"error": "status approved ya rejected hona chahiye"}), 400
    row = db().execute("SELECT status FROM bookings WHERE id=?", (bid,)).fetchone()
    if not row: return jsonify({"error": "booking not found"}), 404
    if row["status"] != "pending": return jsonify({"error": f"sirf pending booking badal sakte hain (abhi {row['status']})"}), 400
    db().execute("UPDATE bookings SET status=? WHERE id=?", (new, bid)); db().commit()
    return jsonify({"ok": True, "id": bid, "status": new})

@app.route("/bookings/<int:bid>/flow")
def flow(bid):
    row = db().execute("SELECT status,event_name FROM bookings WHERE id=?", (bid,)).fetchone()
    if not row: return jsonify({"error": "booking not found"}), 404
    steps, cur = booking_flow(row["status"])
    return jsonify({"title": f"{row['event_name']} - booking status", "steps": steps, "current": cur, "status": row["status"]})

@app.route("/process-flow")
def process_flow():
    return jsonify({"title": "How a booking works", "steps": PROCESS_STEPS})

@app.route("/faq")
def faq():
    q = request.args.get("q")
    if not q: return jsonify(FAQ)
    a = faq_lookup(q)
    return jsonify({"answer": a}) if a else (jsonify({"answer": None, "topics": list(FAQ)}), 404)

@app.route("/chat", methods=["POST"])
def chat():
    data = request.get_json(silent=True) or {}
    msg = str(data.get("message", "")).strip()
    if not msg: return jsonify({"error": "empty_message"}), 400
    try:
        return jsonify(chat_reply(msg, data.get("history")))
    except LLMUnavailable as e:
        return jsonify({"error": "ai_unavailable", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"error": "chat_failed", "detail": str(e)}), 500

@app.route("/speech-to-text", methods=["POST"])
def speech_to_text():
    f = request.files.get("audio")
    if not f or not f.filename: return jsonify({"error": "no_audio_file"}), 400
    try:
        tr = get_client().audio.transcriptions.create(file=(f.filename, f.read()), model="whisper-large-v3")
        return jsonify({"text": (tr.text or "").strip()})
    except LLMUnavailable as e:
        return jsonify({"error": "ai_unavailable", "detail": str(e)}), 503
    except Exception as e:
        return jsonify({"error": "transcription_failed", "detail": str(e)}), 500

@app.route("/demo-requests")
def demo():
    return jsonify(demo_requests())


ensure_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)),
            debug=os.environ.get("FLASK_DEBUG") == "1", use_debugger=False)
