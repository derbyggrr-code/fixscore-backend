"""
FixScore API — customer + technician/shop accounts, live order lifecycle,
notifications (polling-based), geolocation capture, and a demo/sandbox
payment step.

Run:
    cd backend
    pip install -r requirements.txt
    python app.py
API starts at http://localhost:5000

NOTE ON WHAT IS "REAL" VS "DEMO" HERE (read this before launching):
- Auth, orders, statuses, notifications-by-polling, and geolocation capture
  are fully functional against a real SQLite database.
- Payment is a SANDBOX flow (no money moves). To go live you need a
  registered payment aggregator account (Razorpay / PayU / Cashfree are the
  common India options), their API keys, and to swap create_payment()/
  confirm_payment() below for their SDK calls + webhook verification.
- Notifications are in-app (polled every few seconds by the frontend). For
  real push/SMS/WhatsApp alerts you need a provider account (e.g. Firebase
  Cloud Messaging for push, Twilio/Gupshup for SMS/WhatsApp) — the hooks are
  marked below with TODO so you know exactly where to plug them in.
"""
from flask import Flask, request, jsonify, g
from werkzeug.security import generate_password_hash, check_password_hash
import sqlite3, secrets, datetime, os
import razorpay

app = Flask(__name__)
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixscore.db")

# --------------------------------------------------------- Razorpay -----
# TEST MODE keys — safe to keep here for now (no real money moves with
# rzp_test_ keys). Once KYC is approved, swap these for the LIVE keys from
# the Razorpay dashboard (they'll start with rzp_live_).
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "")
# SECURITY: the old test keys that used to be hardcoded here were removed —
# never commit keys to source. Set RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET as
# environment variables on your host (Render/Railway/etc "Environment"
# tab). If they were ever pushed to a public repo, rotate them in the
# Razorpay dashboard even though they were test-mode keys.
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID or "rzp_test_placeholder", RAZORPAY_KEY_SECRET or "placeholder"))

# ---------------------------------------------------------------- CORS -----
# flask-cors isn't assumed to be installed; this hand-rolled version covers
# the same need (the frontend is a static file served from a different
# origin than the API).
@app.after_request
def add_cors(resp):
    resp.headers["Access-Control-Allow-Origin"] = "*"
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
    return resp

@app.route("/api/<path:_any>", methods=["OPTIONS"])
def cors_preflight(_any):
    return ("", 204)

# ----------------------------------------------------------- database -----
def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
    return g.db

@app.teardown_appcontext
def close_db(_exc):
    d = g.pop("db", None)
    if d is not None:
        d.close()

def init():
    c = sqlite3.connect(DB)
    c.executescript("""
    CREATE TABLE IF NOT EXISTS users(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      role TEXT NOT NULL CHECK(role IN ('customer','technician','admin')),
      name TEXT NOT NULL,
      phone TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      shop_name TEXT,
      category TEXT,
      area TEXT,
      rating REAL DEFAULT 4.5,
      jobs_done INTEGER DEFAULT 0,
      verified INTEGER DEFAULT 1,
      token TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      reference TEXT UNIQUE NOT NULL,
      customer_id INTEGER NOT NULL,
      technician_id INTEGER,
      service TEXT NOT NULL,
      description TEXT NOT NULL,
      symptoms TEXT,
      preferred_date TEXT,
      preferred_time TEXT,
      location_text TEXT,
      lat REAL,
      lng REAL,
      diagnosis TEXT,
      fair_price TEXT,
      urgency TEXT,
      confidence TEXT,
      quote_amount INTEGER,
      status TEXT NOT NULL DEFAULT 'requested',
      payment_method TEXT,
      payment_status TEXT NOT NULL DEFAULT 'pending',
      rating INTEGER,
      feedback TEXT,
      customer_seen_at TEXT,
      technician_seen_at TEXT,
      commission_percent REAL,
      commission_amount REAL,
      technician_payout REAL,
      completed_at TEXT,
      free_revisit_of INTEGER,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS settings(
      key TEXT PRIMARY KEY,
      value TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS whatsapp_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      order_id INTEGER,
      to_phone TEXT NOT NULL,
      to_role TEXT NOT NULL,
      message TEXT NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS revenue_log(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      source TEXT NOT NULL,
      amount REAL NOT NULL,
      technician_id INTEGER,
      order_id INTEGER,
      note TEXT,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS parts_orders(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      technician_id INTEGER NOT NULL,
      order_id INTEGER,
      part_name TEXT NOT NULL,
      cost REAL NOT NULL,
      commission_percent REAL NOT NULL,
      commission_amount REAL NOT NULL,
      technician_payout REAL NOT NULL,
      created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS ads(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      title TEXT NOT NULL,
      description TEXT,
      link TEXT,
      advertiser_contact TEXT,
      monthly_fee REAL DEFAULT 0,
      active INTEGER DEFAULT 1,
      created_at TEXT NOT NULL
    );
    """)
    # ---- lightweight migrations for columns added after the first launch ----
    def add_col(table, coldef):
        colname = coldef.split()[0]
        existing = [r[1] for r in c.execute(f"PRAGMA table_info({table})").fetchall()]
        if colname not in existing:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
    add_col("users", "featured_until TEXT")
    add_col("users", "verified_badge_until TEXT")
    add_col("users", "plan TEXT DEFAULT 'free'")
    add_col("users", "plan_expires_at TEXT")
    add_col("users", "lat REAL")
    add_col("users", "lng REAL")
    add_col("users", "visiting_charge INTEGER DEFAULT 150")
    add_col("users", "next_available TEXT DEFAULT 'Today'")
    add_col("orders", "lead_fee_amount REAL DEFAULT 0")
    add_col("orders", "protection_plan TEXT")
    add_col("orders", "protection_fee REAL DEFAULT 0")
    # default platform commission — the % FixScore keeps from every paid order
    # once the free launch period (see FREE_PERIOD_DAYS below) is over.
    if not c.execute("SELECT 1 FROM settings WHERE key='commission_percent'").fetchone():
        c.execute("INSERT INTO settings(key,value) VALUES('commission_percent','15')")
    # launch_date marks day 0 of the free period — set once, on first run,
    # and never overwritten, so redeploys don't reset the free-period clock.
    if not c.execute("SELECT 1 FROM settings WHERE key='launch_date'").fetchone():
        c.execute("INSERT INTO settings(key,value) VALUES('launch_date',?)", (datetime.date.today().isoformat(),))
    # prices for the extra earning models — all waived automatically while
    # the free launch period is active (see is_free_period()/priced()).
    default_settings = {
        "lead_fee": "30",                 # ₹ charged to a technician per accepted lead
        "parts_commission_percent": "10", # % FixScore keeps on parts ordered through the app
        "featured_price": "999",          # ₹/month for top placement
        "verification_price": "499",      # ₹/year for the verified badge
        "subscription_price": "1499",     # ₹/month for the shop Pro plan
    }
    for k, v in default_settings.items():
        if not c.execute("SELECT 1 FROM settings WHERE key=?", (k,)).fetchone():
            c.execute("INSERT INTO settings(key,value) VALUES(?,?)", (k, v))
    # seed one admin account so the platform owner has a panel to log into.
    # CHANGE THIS PASSWORD before you put this anywhere but your own laptop.
    if not c.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():
        c.execute("""INSERT INTO users(role,name,phone,password_hash,token,created_at)
                     VALUES('admin','FixScore Owner','admin','ADMIN_HASH_PLACEHOLDER',NULL,?)""", (datetime.datetime.utcnow().isoformat(),))
        c.execute("UPDATE users SET password_hash=? WHERE role='admin'",
                  (generate_password_hash("admin123"),))
    c.commit(); c.close()

# ------------------------------------------------------------- helpers ----
FLOW = ["requested", "accepted", "on_the_way", "arrived", "in_progress", "completed"]

DIAGNOSIS_RULES = {
    "AC":        {"issue": "Cooling / gas-related issue", "fair_price": "₹500 – ₹1,500",   "urgency": "Medium",
                  "eta": "45–75 min", "steps": ["Gas pressure aur cooling coil check", "Filter/vents clean", "Leak ho to seal + gas top-up"], "parts": "Refrigerant gas, filter (agar chahiye ho)"},
    "RO":        {"issue": "Filter / water-flow issue",    "fair_price": "₹300 – ₹1,200",  "urgency": "Low",
                  "eta": "30–45 min", "steps": ["Filter/membrane check", "Flow-restrictor clean", "Leak seal ya part replace"], "parts": "Sediment/carbon filter, RO membrane (agar due ho)"},
    "Fridge":    {"issue": "Cooling system / thermostat issue", "fair_price": "₹600 – ₹2,500", "urgency": "Medium",
                  "eta": "40–90 min", "steps": ["Thermostat aur compressor test", "Coil defrost check", "Faulty part replace"], "parts": "Thermostat, defrost timer, gas (case-dependent)"},
    "Washing Machine": {"issue": "Drain / motor / sensor issue", "fair_price": "₹500 – ₹2,500", "urgency": "Medium",
                  "eta": "45–90 min", "steps": ["Drain pump/pipe check", "Motor + belt inspect", "Sensor/PCB test"], "parts": "Drain pump, belt, door lock (case-dependent)"},
    "Electrician": {"issue": "Switch / wiring / connection issue", "fair_price": "₹200 – ₹1,500", "urgency": "Medium",
                  "eta": "20–60 min", "steps": ["Wiring/connection test with meter", "Faulty switch/MCB isolate", "Part replace + safety test"], "parts": "Switch, MCB, wiring (case-dependent)"},
    "Plumber":   {"issue": "Pipe / tap leakage issue",      "fair_price": "₹200 – ₹1,200",  "urgency": "Low",
                  "eta": "20–60 min", "steps": ["Leak source locate", "Fitting/washer/pipe check", "Seal ya part replace"], "parts": "Tap washer, pipe fitting, sealant"},
}

def now():
    return datetime.datetime.utcnow().isoformat()

def row2dict(r):
    return dict(r) if r else None

def auth_user():
    """Reads Authorization: Bearer <token>, returns the user row or None."""
    h = request.headers.get("Authorization", "")
    if not h.startswith("Bearer "):
        return None
    token = h[7:].strip()
    if not token:
        return None
    return db().execute("SELECT * FROM users WHERE token=?", (token,)).fetchone()

def require_role(role):
    u = auth_user()
    if not u or u["role"] != role:
        return None
    return u

def get_setting(key, default=None):
    r = db().execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default

def set_setting(key, value):
    c = db()
    c.execute("""INSERT INTO settings(key,value) VALUES(?,?)
                 ON CONFLICT(key) DO UPDATE SET value=excluded.value""", (key, str(value)))
    c.commit()

FREE_PERIOD_DAYS = 180  # ~6 months of zero commission from launch_date

def target_commission_percent():
    """The commission % the owner has configured — takes effect once the
    free launch period is over. Editable anytime from the admin panel."""
    try:
        return float(get_setting("commission_percent", "15"))
    except (TypeError, ValueError):
        return 15.0

def free_status():
    launch = get_setting("launch_date")
    launch_date = datetime.date.fromisoformat(launch) if launch else datetime.date.today()
    free_until = launch_date + datetime.timedelta(days=FREE_PERIOD_DAYS)
    today = datetime.date.today()
    active = today < free_until
    days_left = max((free_until - today).days, 0)
    return {
        "active": active,
        "launch_date": launch_date.isoformat(),
        "free_until": free_until.isoformat(),
        "days_left": days_left,
        "target_commission_percent": target_commission_percent(),
    }

def commission_percent():
    """Effective commission % right now: 0 during the free launch period,
    then whatever the owner has set in target_commission_percent()."""
    if free_status()["active"]:
        return 0.0
    return target_commission_percent()

def is_free_period():
    return free_status()["active"]

def priced(setting_key, default="0"):
    """Reads a configurable price and zeroes it out during the free launch
    period — every extra earning model (lead fee, featured listing,
    verification badge, subscription, parts commission) is waived until
    FREE_PERIOD_DAYS has passed, same as the core commission."""
    if is_free_period():
        return 0.0
    try:
        return float(get_setting(setting_key, default))
    except (TypeError, ValueError):
        return float(default)

def log_revenue(source, amount, technician_id=None, order_id=None, note=None):
    if amount <= 0:
        return
    c = db()
    c.execute("""INSERT INTO revenue_log(source,amount,technician_id,order_id,note,created_at)
                 VALUES(?,?,?,?,?,?)""", (source, amount, technician_id, order_id, note, now()))
    c.commit()

def protection_price(quote_amount):
    """Tiered warranty/protection-plan pricing based on job size — like every
    other extra earning model, waived during the free launch period."""
    if is_free_period():
        return 0.0
    amt = float(quote_amount or 0)
    if amt <= 1000: return 49.0
    if amt <= 3000: return 149.0
    return 299.0

def send_whatsapp(phone, to_role, message, order_id=None):
    """SIMULATED WhatsApp send — logs the message so the app can show it was
    'sent', but no real WhatsApp message leaves this server.
    To make this real: sign up for the WhatsApp Business Platform via Meta,
    or use a provider like Gupshup/Twilio/Interakt as a reseller (they
    handle template approval for you), then replace the body of this
    function with their API call, e.g. for Gupshup:
        requests.post("https://api.gupshup.io/wa/api/v1/msg", ...,
                       headers={"apikey": GUPSHUP_API_KEY},
                       data={"channel": "whatsapp", "source": YOUR_WA_NUMBER,
                             "destination": phone, "message": message})
    You need: a Meta Business verification, a WhatsApp Business number, and
    pre-approved message templates (Meta doesn't allow free-form marketing
    text — transactional templates like order updates are approved fast).
    """
    if not phone:
        return None
    c = db()
    c.execute("""INSERT INTO whatsapp_log(order_id,to_phone,to_role,message,created_at)
                 VALUES(?,?,?,?,?)""", (order_id, phone, to_role, message, now()))
    c.commit()
    return {"phone": phone, "message": message}

KEYWORD_HINTS = {
    # description keywords -> (issue phrase, urgency bump, extra confidence)
    "no cooling": ("Gas leak / compressor issue likely", "High", 12),
    "not cooling": ("Gas leak / compressor issue likely", "High", 12),
    "thanda nahi": ("Gas leak / compressor issue likely", "High", 12),
    "water leak": ("Drain choke / seal leakage", "Medium", 10),
    "paani": ("Drain choke / seal leakage", "Medium", 10),
    "noise": ("Motor / bearing wear likely", "Medium", 8),
    "awaaz": ("Motor / bearing wear likely", "Medium", 8),
    "smell": ("Burnt wiring / component — check urgently", "High", 12),
    "jal": ("Burnt wiring / component — check urgently", "High", 12),
    "spark": ("Burnt wiring / component — check urgently", "High", 12),
    "not turning on": ("Power supply / PCB fault likely", "High", 10),
    "on nahi ho": ("Power supply / PCB fault likely", "High", 10),
    "slow": ("Filter / flow restriction", "Low", 6),
    "trip": ("Overload / short-circuit on that line", "High", 10),
}

def diagnose(service, symptoms, description="", has_photo=False):
    """Turns free-text + a symptom checklist (+ an optional photo flag) into
    a structured, explainable estimate.

    NOTE ON "AI" HERE: this is a smart rule-based/keyword-matching engine,
    not a real vision or LLM model — it's built so the *shape* of the
    response (issue, price range, urgency, confidence, reasons) never has
    to change when you're ready to go real. To plug in real AI:
      - Text: send `description` to an LLM (e.g. Anthropic/OpenAI chat
        completion) asking it to return JSON matching this same shape.
      - Photo: send the uploaded image to a vision-capable model
        (e.g. Claude/GPT-4o vision) alongside `description` and ask it to
        identify the visible fault. Requires an API key as an environment
        variable — see ANTHROPIC_API_KEY / OPENAI_API_KEY below.
    This keeps working with zero frontend changes either way."""
    base = DIAGNOSIS_RULES.get(service, {"issue": "Inspection required", "fair_price": "Variable", "urgency": "Medium"})
    symptoms = symptoms or []
    description = (description or "").lower()
    matched = len(symptoms)
    confidence = 55 + matched * 8
    reasons = []
    issue = base["issue"]
    urgency = base["urgency"]

    if symptoms:
        reasons.append(f"Matched {matched} of the symptoms you selected to known {service} faults")

    # keyword pass over the free-text description customer typed
    hit = False
    for kw, (phrase, urg, bump) in KEYWORD_HINTS.items():
        if kw in description:
            issue = phrase
            confidence += bump
            hit = True
            if urg == "High" or urgency != "High":
                urgency = urg if urg == "High" else urgency
            reasons.append(f"Your description mentioned '{kw}' — a common sign of {phrase.lower()}")
            break
    if not hit and description:
        reasons.append("Estimate based on your description — add photo/symptoms above for a sharper number")
    if not symptoms and not description and not has_photo:
        reasons.append("Very rough estimate — describe the problem or upload a photo for accuracy")

    if has_photo:
        confidence += 15
        reasons.append("Photo received — technician will confirm exact fault + final price on-site")

    if any(("spark" in s.lower() or "burn" in s.lower() or "jalne" in s.lower()) for s in symptoms):
        urgency = "High"
        reasons.append("Marked high urgency because of a safety-related symptom (sparking / burning smell)")

    confidence = min(confidence, 96)
    return {
        "issue": issue,
        "fair_price": base["fair_price"],
        "urgency": urgency,
        "confidence": f"{confidence}%",
        "reasons": reasons,
        "eta_repair": base.get("eta", "30–60 min"),
        "fix_steps": base.get("steps", []),
        "likely_parts": base.get("parts", "On-site inspection ke baad confirm hoga"),
    }

def haversine_km(lat1, lng1, lat2, lng2):
    import math
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(a**0.5, (1-a)**0.5)

def rank_technicians(service, area=None, lat=None, lng=None, limit=6):
    """'AI suggest' technician ranking. Scores every verified technician in
    this category and returns them best-match-first, with enough profile
    detail (distance, visiting charge, next available slot, rating,
    experience) for the customer to decide before booking. Score weights,
    in order of importance:
      1. Real distance in km (if both customer + technician have GPS) —
         closer wins. Falls back to area-name match if GPS isn't set.
      2. Paid 'featured' placement — still outweighed by a much closer or
         much better-rated technician; featured buys visibility, not a
         rigged #1 spot.
      3. Verified badge
      4. Rating (0-5) and total completed jobs (experience)
    Swap-in point for real ML ranking later: replace the `score` formula
    below with a model call that also takes the diagnosis text as input."""
    c = db()
    rows = c.execute("""SELECT * FROM users WHERE role='technician' AND category=?""", (service,)).fetchall()
    ts = now()
    scored = []
    for r in rows:
        d = row2dict(r)
        score = 0.0
        distance_km = None
        if lat is not None and lng is not None and d.get("lat") is not None and d.get("lng") is not None:
            distance_km = round(haversine_km(lat, lng, d["lat"], d["lng"]), 1)
            score += max(0, 50 - distance_km * 4)  # closer = big boost, decays with distance
        elif area and d.get("area") and area.strip().lower() == d["area"].strip().lower():
            score += 30
        featured = bool(d.get("featured_until") and d["featured_until"] > ts)
        verified = bool(d.get("verified_badge_until") and d["verified_badge_until"] > ts) or bool(d.get("verified"))
        if featured:
            score += 12
        if verified:
            score += 10
        score += min(float(d.get("rating") or 0), 5.0) * 8
        score += min(float(d.get("jobs_done") or 0), 200) * 0.15
        d["featured"] = featured
        d["verified_badge"] = verified
        d["distance_km"] = distance_km
        d["eta_visit"] = (f"~{max(10, int(distance_km*3)+10)} min away" if distance_km is not None
                           else ("Same area" if area and d.get("area") and area.strip().lower()==d["area"].strip().lower() else "Distance unknown"))
        d["visiting_charge"] = d.get("visiting_charge") if d.get("visiting_charge") is not None else 150
        d["next_available"] = d.get("next_available") or "Today"
        d["match_score"] = round(score, 1)
        d.pop("password_hash", None); d.pop("token", None)
        scored.append(d)
    scored.sort(key=lambda x: x["match_score"], reverse=True)
    return scored[:limit]

# ============================================================ AI TOOLS ====
@app.post("/api/ai/estimate")
def ai_estimate():
    """Public, no-login-required. Powers the front-page 'AI Repair
    Assistant' card: customer either uploads a photo, types a description,
    or both — gets an instant issue + price-range + urgency estimate
    before they've even created an account."""
    x = request.json or {}
    service = x.get("service")
    if service not in DIAGNOSIS_RULES:
        return jsonify({"error": "select a valid service category"}), 400
    description = x.get("description") or ""
    symptoms = x.get("symptoms") or []
    has_photo = bool(x.get("has_photo") or x.get("photo_base64"))
    # NOTE: photo bytes are intentionally not stored/analysed server-side
    # yet (no vision-AI key configured) — `has_photo` only nudges
    # confidence + tells the technician a photo exists. See diagnose()
    # docstring for how to wire in real photo analysis.
    diag = diagnose(service, symptoms, description, has_photo)
    return jsonify(diag)

@app.get("/api/technicians/suggest")
def technicians_suggest():
    """Public. Powers 'AI suggested technicians for you' — ranked list with
    full profile (name/shop, category, area, distance, ETA, visiting
    charge, rating, jobs done, badges, next available slot)."""
    service = request.args.get("service")
    if service not in DIAGNOSIS_RULES:
        return jsonify({"error": "service query param required"}), 400
    area = request.args.get("area")
    lat = request.args.get("lat", type=float)
    lng = request.args.get("lng", type=float)
    return jsonify(rank_technicians(service, area, lat, lng))

@app.post("/api/technician/profile")
def technician_profile_update():
    """Lets a technician/shop set their live location, visiting charge and
    next-available slot — used by AI ranking for real distance + ETA."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    x = request.json or {}
    fields, vals = [], []
    if "lat" in x and "lng" in x:
        fields += ["lat=?", "lng=?"]; vals += [x.get("lat"), x.get("lng")]
    if "visiting_charge" in x:
        fields.append("visiting_charge=?"); vals.append(x.get("visiting_charge"))
    if "next_available" in x:
        fields.append("next_available=?"); vals.append((x.get("next_available") or "").strip()[:40] or "Today")
    if "area" in x:
        fields.append("area=?"); vals.append((x.get("area") or "").strip())
    if not fields:
        return jsonify({"error": "nothing to update"}), 400
    vals.append(u["id"])
    db().execute(f"UPDATE users SET {','.join(fields)} WHERE id=?", vals)
    db().commit()
    row = db().execute("SELECT * FROM users WHERE id=?", (u["id"],)).fetchone()
    return jsonify(public_user(row))

# ================================================================ AUTH ====
@app.post("/api/signup")
def signup():
    x = request.json or {}
    role = x.get("role")
    name = (x.get("name") or "").strip()
    phone = (x.get("phone") or "").strip()
    password = x.get("password") or ""
    if role not in ("customer", "technician"):
        return jsonify({"error": "role must be customer or technician"}), 400
    if not name or not phone or len(password) < 4:
        return jsonify({"error": "name, phone and a password (4+ chars) are required"}), 400
    if role == "technician" and not x.get("category"):
        return jsonify({"error": "category (e.g. AC, Plumber) is required for a technician/shop account"}), 400

    c = db()
    if c.execute("SELECT id FROM users WHERE phone=?", (phone,)).fetchone():
        return jsonify({"error": "An account with this mobile number already exists"}), 409

    token = secrets.token_hex(24)
    c.execute("""INSERT INTO users(role,name,phone,password_hash,shop_name,category,area,token,created_at)
                 VALUES(?,?,?,?,?,?,?,?,?)""",
              (role, name, phone, generate_password_hash(password), x.get("shop_name"),
               x.get("category"), x.get("area"), token, now()))
    c.commit()
    u = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    return jsonify({"token": token, "user": public_user(u)}), 201

@app.post("/api/login")
def login():
    x = request.json or {}
    phone = (x.get("phone") or "").strip()
    password = x.get("password") or ""
    c = db()
    u = c.execute("SELECT * FROM users WHERE phone=?", (phone,)).fetchone()
    if not u or not check_password_hash(u["password_hash"], password):
        return jsonify({"error": "Mobile number or password is incorrect"}), 401
    token = secrets.token_hex(24)
    c.execute("UPDATE users SET token=? WHERE id=?", (token, u["id"]))
    c.commit()
    return jsonify({"token": token, "user": public_user(u)})

@app.get("/api/me")
def me():
    u = auth_user()
    if not u:
        return jsonify({"error": "not logged in"}), 401
    return jsonify(public_user(u))

def public_user(u):
    d = row2dict(u)
    d.pop("password_hash", None)
    d.pop("token", None)
    return d

# ============================================================== ORDERS ====
@app.post("/api/orders")
def create_order():
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    x = request.json or {}
    service = x.get("service")
    description = (x.get("description") or "").strip()
    if not service or not description:
        return jsonify({"error": "service and description are required"}), 400

    diag = diagnose(service, x.get("symptoms") or [])
    ref = "FS-" + secrets.token_hex(4).upper()
    c = db(); ts = now()
    c.execute("""INSERT INTO orders(reference,customer_id,service,description,symptoms,
                 preferred_date,preferred_time,location_text,lat,lng,
                 diagnosis,fair_price,urgency,confidence,status,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (ref, u["id"], service, description, ",".join(x.get("symptoms") or []),
               x.get("date"), x.get("time"), x.get("location_text"), x.get("lat"), x.get("lng"),
               diag["issue"], diag["fair_price"], diag["urgency"], diag["confidence"],
               "requested", ts, ts))
    c.commit()
    order = c.execute("SELECT * FROM orders WHERE reference=?", (ref,)).fetchone()
    # TODO: this is where you'd push a real notification (FCM/SMS/WhatsApp)
    # to every technician of matching `category`/`area` instead of relying
    # on their dashboard polling /api/orders/incoming.
    return jsonify({**row2dict(order), "diagnosis_reasons": diag["reasons"]}), 201

@app.get("/api/orders/mine")
def orders_mine():
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    rows = db().execute("""SELECT o.*, t.name AS technician_name, t.shop_name AS technician_shop,
                            t.rating AS technician_rating, t.phone AS technician_phone
                            FROM orders o LEFT JOIN users t ON t.id=o.technician_id
                            WHERE o.customer_id=? ORDER BY o.id DESC""", (u["id"],)).fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/orders/incoming")
def orders_incoming():
    """New, unclaimed orders matching this technician's category — the
    'shop ke paas notification' queue."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    rows = db().execute("""SELECT * FROM orders WHERE status='requested' AND service=?
                            ORDER BY id DESC""", (u["category"],)).fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/orders/assigned")
def orders_assigned():
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    rows = db().execute("""SELECT o.*, c.name AS customer_name, c.phone AS customer_phone
                            FROM orders o JOIN users c ON c.id=o.customer_id
                            WHERE o.technician_id=? ORDER BY o.id DESC""", (u["id"],)).fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/orders/<int:order_id>")
def order_detail(order_id):
    u = auth_user()
    if not u:
        return jsonify({"error": "login first"}), 401
    o = db().execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or (u["role"] == "customer" and o["customer_id"] != u["id"]) or \
       (u["role"] == "technician" and o["technician_id"] not in (None, u["id"])):
        return jsonify({"error": "not found"}), 404
    return jsonify(row2dict(o))

@app.post("/api/orders/<int:order_id>/accept")
def accept_order(order_id):
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["status"] != "requested":
        return jsonify({"error": "this order is no longer available"}), 409
    # Lead Fee: a Pro-plan shop doesn't pay per-lead (that's the subscription
    # perk); everyone else is charged the configured lead fee — waived
    # automatically during the free launch period via priced().
    on_pro_plan = u["plan"] == "pro" and u["plan_expires_at"] and u["plan_expires_at"] >= now()
    lead_fee = 0.0 if on_pro_plan else priced("lead_fee")
    c.execute("""UPDATE orders SET technician_id=?, status='accepted', lead_fee_amount=?, updated_at=?
                 WHERE id=?""", (u["id"], lead_fee, now(), order_id))
    if lead_fee > 0:
        log_revenue("lead_fee", lead_fee, technician_id=u["id"], order_id=order_id,
                     note=f"Lead fee for {o['reference']}")
    c.commit()
    updated = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    customer = c.execute("SELECT * FROM users WHERE id=?", (o["customer_id"],)).fetchone()
    shop = u["shop_name"] or u["name"]
    send_whatsapp(customer["phone"], "customer",
        f"FixScore: Your {o['service']} repair ({o['reference']}) has been accepted by {shop} "
        f"(⭐ {u['rating']}). They'll update you as they head your way.", order_id)
    return jsonify(row2dict(updated))

@app.post("/api/orders/<int:order_id>/cancel")
def cancel_order(order_id):
    """Customer can cancel only before a technician has accepted — once
    someone's on the hook for the job, cancelling needs a phone call, not
    a button (keeps this simple and avoids technicians being ghosted)."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    if o["status"] != "requested":
        return jsonify({"error": "this order already has a technician assigned — call them to cancel"}), 409
    c.execute("UPDATE orders SET status='cancelled', updated_at=? WHERE id=?", (now(), order_id))
    c.commit()
    return jsonify(row2dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))

@app.post("/api/orders/<int:order_id>/free-revisit")
def free_revisit(order_id):
    """Implements the homepage promise: 'same issue repeats within 7 days
    = free revisit'. Re-opens the same job with the same technician, at
    zero cost and zero commission — nobody pays twice for an unfixed
    problem."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    if o["status"] != "completed" or not o["completed_at"]:
        return jsonify({"error": "free revisit is only available for a completed job"}), 409
    completed = datetime.datetime.fromisoformat(o["completed_at"])
    if datetime.datetime.utcnow() - completed > datetime.timedelta(days=7):
        return jsonify({"error": "the 7-day free-revisit window for this job has passed"}), 409
    if not o["technician_id"]:
        return jsonify({"error": "original job has no technician on record"}), 409

    ref = "FS-" + secrets.token_hex(4).upper()
    ts = now()
    c.execute("""INSERT INTO orders(reference,customer_id,technician_id,service,description,
                 location_text,lat,lng,diagnosis,fair_price,urgency,confidence,quote_amount,
                 status,payment_method,payment_status,commission_percent,commission_amount,
                 technician_payout,free_revisit_of,created_at,updated_at)
                 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,'accepted',NULL,'paid',0,0,0,?,?,?)""",
              (ref, u["id"], o["technician_id"], o["service"],
               f"FREE REVISIT for {o['reference']}: {o['description']}",
               o["location_text"], o["lat"], o["lng"], o["diagnosis"], o["fair_price"],
               "High", o["confidence"], o["id"], ts, ts))
    c.commit()
    new_order = c.execute("SELECT * FROM orders WHERE reference=?", (ref,)).fetchone()
    tech = c.execute("SELECT * FROM users WHERE id=?", (o["technician_id"],)).fetchone()
    send_whatsapp(tech["phone"], "technician",
        f"FixScore: Free revisit booked for {o['reference']} (new ref {ref}) — same issue reported again "
        f"within 7 days. This one's on the house, no commission charged.", new_order["id"])
    return jsonify(row2dict(new_order)), 201

@app.post("/api/orders/<int:order_id>/status")
def update_status(order_id):
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    x = request.json or {}
    new_status = x.get("status")
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["technician_id"] != u["id"]:
        return jsonify({"error": "not found"}), 404
    if new_status not in FLOW:
        return jsonify({"error": "invalid status"}), 400
    cur_i, new_i = FLOW.index(o["status"]), FLOW.index(new_status)
    if new_i != cur_i + 1:
        return jsonify({"error": f"can't jump from '{o['status']}' to '{new_status}'"}), 409
    c.execute("UPDATE orders SET status=?, updated_at=? WHERE id=?", (new_status, now(), order_id))
    if new_status == "completed":
        c.execute("UPDATE users SET jobs_done = jobs_done + 1 WHERE id=?", (u["id"],))
        c.execute("UPDATE orders SET completed_at=? WHERE id=?", (now(), order_id))
    c.commit()
    customer = c.execute("SELECT * FROM users WHERE id=?", (o["customer_id"],)).fetchone()
    status_msgs = {
        "on_the_way": f"FixScore: {u['shop_name'] or u['name']} is on the way for your {o['reference']} repair.",
        "arrived": f"FixScore: {u['shop_name'] or u['name']} has arrived for your {o['reference']} repair.",
        "in_progress": f"FixScore: Work has started on your {o['reference']} repair.",
        "completed": f"FixScore: Your {o['reference']} repair is complete! Open the app to pay and rate.",
    }
    if new_status in status_msgs:
        send_whatsapp(customer["phone"], "customer", status_msgs[new_status], order_id)
    return jsonify(row2dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))

@app.post("/api/orders/<int:order_id>/quote")
def set_quote(order_id):
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    amount = (request.json or {}).get("amount")
    if not isinstance(amount, (int, float)) or amount <= 0:
        return jsonify({"error": "a positive amount is required"}), 400
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    if not o or o["technician_id"] != u["id"]:
        return jsonify({"error": "not found"}), 404
    c.execute("UPDATE orders SET quote_amount=?, updated_at=? WHERE id=?", (amount, now(), order_id))
    c.commit()
    return jsonify(row2dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))

# ============================================================= PAYMENT ====
@app.post("/api/orders/<int:order_id>/protection")
def set_protection(order_id):
    """Warranty/Protection add-on — customer opts in on the payment screen,
    before paying. Flat fee (tiered by job size), goes entirely to FixScore,
    not split with the technician."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    opt_in = bool((request.json or {}).get("opt_in"))
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    if o["status"] != "completed" or o["payment_status"] != "pending":
        return jsonify({"error": "protection plan can only be chosen before paying"}), 409
    fee = protection_price(o["quote_amount"]) if opt_in else 0.0
    c.execute("UPDATE orders SET protection_plan=?, protection_fee=?, updated_at=? WHERE id=?",
              ("standard" if opt_in else None, fee, now(), order_id))
    c.commit()
    return jsonify(row2dict(c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()))

def _finalize_payment(order_id, u, method, status):
    """Shared by cash (instant) and UPI/card (after Razorpay verification).
    Splits the repair amount into FixScore's commission + technician payout;
    any protection-plan fee is added on top and kept entirely by FixScore."""
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    pct = commission_percent()
    amount = float(o["quote_amount"] or 0)
    protection_fee = float(o["protection_fee"] or 0)
    commission_amount = round(amount * pct / 100, 2)
    technician_payout = round(amount - commission_amount, 2)
    total_charged = amount + protection_fee
    c.execute("""UPDATE orders SET payment_method=?, payment_status=?, commission_percent=?,
                 commission_amount=?, technician_payout=?, updated_at=? WHERE id=?""",
              (method, status, pct, commission_amount, technician_payout, now(), order_id))
    c.commit()
    if protection_fee > 0:
        log_revenue("warranty", protection_fee, technician_id=o["technician_id"], order_id=order_id,
                     note=f"Protection plan for {o['reference']}")
    updated = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    tech = c.execute("SELECT * FROM users WHERE id=?", (o["technician_id"],)).fetchone() if o["technician_id"] else None
    send_whatsapp(u["phone"], "customer",
        f"FixScore: Payment of ₹{total_charged:,.0f} received for {o['reference']} ({method.upper()})"
        + (f", including ₹{protection_fee:,.0f} protection plan" if protection_fee else "")
        + ". Thanks for using FixScore!", order_id)
    if tech:
        note = (f"FixScore: ₹{amount:,.0f} collected for {o['reference']}. Platform fee ₹{commission_amount:,.0f} "
                f"({pct:.0f}%) — your payout: ₹{technician_payout:,.0f}." if method != "cash" else
                f"FixScore: {o['reference']} marked cash-on-visit (₹{amount:,.0f}). You owe FixScore its "
                f"₹{commission_amount:,.0f} ({pct:.0f}%) platform fee — settle this from the admin panel.")
        send_whatsapp(tech["phone"], "technician", note, order_id)
    return updated

@app.post("/api/orders/<int:order_id>/payment")
def pay_order(order_id):
    """Cash-on-visit only. UPI/card go through the Razorpay endpoints below —
    a payment status is never set to 'paid' straight from the client for
    real money, only after Razorpay's signature is verified server-side."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    method = (request.json or {}).get("method")
    if method != "cash":
        return jsonify({"error": "for upi/card, call /razorpay-order then /razorpay-verify"}), 400
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    if o["status"] != "completed":
        return jsonify({"error": "payment opens once the job is marked completed"}), 409
    updated = _finalize_payment(order_id, u, "cash", "pay_on_visit")
    return jsonify(row2dict(updated))

@app.post("/api/orders/<int:order_id>/razorpay-order")
def razorpay_order(order_id):
    """Step 1 of a real UPI/card payment: create a Razorpay order and hand
    the frontend just enough to open Razorpay's checkout widget."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    if o["status"] != "completed":
        return jsonify({"error": "payment opens once the job is marked completed"}), 409
    total = float(o["quote_amount"] or 0) + float(o["protection_fee"] or 0)
    amount_paise = int(round(total * 100))
    if amount_paise <= 0:
        return jsonify({"error": "invalid amount"}), 400
    rp_order = razorpay_client.order.create({
        "amount": amount_paise,
        "currency": "INR",
        "receipt": o["reference"],
        "notes": {"fixscore_order_id": str(order_id)},
    })
    return jsonify({
        "razorpay_order_id": rp_order["id"],
        "amount": amount_paise,
        "currency": "INR",
        "key_id": RAZORPAY_KEY_ID,
        "name": "FixScore",
        "description": f"Repair {o['reference']}",
        "customer_name": u["name"],
        "customer_phone": u["phone"],
    })

@app.post("/api/orders/<int:order_id>/razorpay-verify")
def razorpay_verify(order_id):
    """Step 2: verify Razorpay's signature server-side before ever marking
    the order paid. This is the check that stops a client from faking a
    'payment successful' message."""
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    x = request.json or {}
    method = x.get("method")
    if method not in ("upi", "card"):
        return jsonify({"error": "method must be upi or card"}), 400
    try:
        razorpay_client.utility.verify_payment_signature({
            "razorpay_order_id": x.get("razorpay_order_id"),
            "razorpay_payment_id": x.get("razorpay_payment_id"),
            "razorpay_signature": x.get("razorpay_signature"),
        })
    except razorpay.errors.SignatureVerificationError:
        return jsonify({"error": "payment verification failed"}), 400
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o:
        return jsonify({"error": "not found"}), 404
    updated = _finalize_payment(order_id, u, method, "paid")
    return jsonify(row2dict(updated))

@app.post("/api/orders/<int:order_id>/feedback")
def feedback(order_id):
    u = require_role("customer")
    if not u:
        return jsonify({"error": "login as a customer first"}), 401
    x = request.json or {}
    rating = x.get("rating")
    if rating not in (1, 2, 3, 4, 5):
        return jsonify({"error": "rating must be 1-5"}), 400
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    if not o or o["status"] != "completed":
        return jsonify({"error": "feedback opens once the job is marked completed"}), 409
    c.execute("UPDATE orders SET rating=?, feedback=?, updated_at=? WHERE id=?",
              (rating, x.get("comment", ""), now(), order_id))
    if o["technician_id"]:
        # jobs_done was already incremented when the job was marked completed,
        # so the prior sample count is (jobs_done - 1).
        c.execute("""UPDATE users SET rating = ROUND((rating * (jobs_done - 1) + ?) / jobs_done, 2)
                     WHERE id=?""", (rating, o["technician_id"]))
    c.commit()
    return jsonify({"ok": True})

# ============================================== TECHNICIAN GROWTH ADD-ONS ==
# Featured listing, verified badge, Pro subscription, and the parts
# marketplace — all instant/simulated "purchases" for now (same sandbox
# spirit as the cash/Razorpay demo payment above). Every price is waived
# automatically during the free launch period via priced().
@app.post("/api/technician/featured")
def buy_featured():
    """Top-placement badge for 30 days."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    price = priced("featured_price")
    until = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    c = db()
    c.execute("UPDATE users SET featured_until=? WHERE id=?", (until, u["id"]))
    log_revenue("featured_listing", price, technician_id=u["id"], note="30-day featured placement")
    c.commit()
    return jsonify({"featured_until": until, "amount_charged": price})

@app.post("/api/technician/verify-badge")
def buy_verification_badge():
    """Verified badge for 1 year — on top of the basic account verification
    every shop already gets at signup."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    price = priced("verification_price")
    until = (datetime.date.today() + datetime.timedelta(days=365)).isoformat()
    c = db()
    c.execute("UPDATE users SET verified_badge_until=? WHERE id=?", (until, u["id"]))
    log_revenue("verification_badge", price, technician_id=u["id"], note="1-year verified badge")
    c.commit()
    return jsonify({"verified_badge_until": until, "amount_charged": price})

@app.post("/api/technician/subscribe")
def buy_subscription():
    """Business Pro plan for 30 days — perk: no per-lead fee while active
    (see accept_order())."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    price = priced("subscription_price")
    until = (datetime.date.today() + datetime.timedelta(days=30)).isoformat()
    c = db()
    c.execute("UPDATE users SET plan='pro', plan_expires_at=? WHERE id=?", (until, u["id"]))
    log_revenue("subscription", price, technician_id=u["id"], note="Business Pro plan, 30 days")
    c.commit()
    return jsonify({"plan": "pro", "plan_expires_at": until, "amount_charged": price})

@app.post("/api/technician/parts-order")
def log_parts_order():
    """Parts Marketplace — technician logs a spare part bought through
    FixScore for a job; FixScore takes a % commission on the part cost,
    same as the repair commission."""
    u = require_role("technician")
    if not u:
        return jsonify({"error": "login as a technician/shop first"}), 401
    x = request.json or {}
    part_name = (x.get("part_name") or "").strip()
    cost = x.get("cost")
    if not part_name or not isinstance(cost, (int, float)) or cost <= 0:
        return jsonify({"error": "part_name and a positive cost are required"}), 400
    pct = 0.0 if is_free_period() else float(get_setting("parts_commission_percent", "10"))
    commission_amount = round(cost * pct / 100, 2)
    payout = round(cost - commission_amount, 2)
    c = db()
    c.execute("""INSERT INTO parts_orders(technician_id,order_id,part_name,cost,commission_percent,
                 commission_amount,technician_payout,created_at)
                 VALUES(?,?,?,?,?,?,?,?)""",
              (u["id"], x.get("order_id"), part_name, cost, pct, commission_amount, payout, now()))
    if commission_amount > 0:
        log_revenue("parts_marketplace", commission_amount, technician_id=u["id"], order_id=x.get("order_id"),
                     note=f"Parts commission on {part_name}")
    c.commit()
    return jsonify({"part_name": part_name, "cost": cost, "commission_percent": pct,
                     "commission_amount": commission_amount, "technician_payout": payout}), 201

# ==================================================================== ADS ==
@app.get("/api/ads/active")
def ads_active():
    """Public — powers a small sponsored banner on the customer homepage."""
    rows = db().execute("SELECT id,title,description,link FROM ads WHERE active=1 ORDER BY id DESC").fetchall()
    return jsonify([row2dict(r) for r in rows])

# ================================================================ ADMIN ====
# This is FixScore's own back-office: revenue, commission and technician
# payouts. Log in with phone "admin" / password "admin123" (seeded in
# init() above) — change that password before this goes anywhere public.
@app.post("/api/admin/settings")
def admin_set_settings():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    x = request.json or {}
    updates = {}
    if "commission_percent" in x:
        try:
            pct = float(x["commission_percent"])
        except (TypeError, ValueError):
            return jsonify({"error": "commission_percent must be a number"}), 400
        if not (0 <= pct <= 100):
            return jsonify({"error": "commission_percent must be between 0 and 100"}), 400
        updates["commission_percent"] = pct
    for key in ("lead_fee", "featured_price", "verification_price", "subscription_price"):
        if key in x:
            try:
                v = float(x[key])
                if v < 0: raise ValueError
            except (TypeError, ValueError):
                return jsonify({"error": f"{key} must be a non-negative number"}), 400
            updates[key] = v
    if "parts_commission_percent" in x:
        try:
            v = float(x["parts_commission_percent"])
            if not (0 <= v <= 100): raise ValueError
        except (TypeError, ValueError):
            return jsonify({"error": "parts_commission_percent must be between 0 and 100"}), 400
        updates["parts_commission_percent"] = v
    if not updates:
        return jsonify({"error": "no recognised settings in request"}), 400
    for k, v in updates.items():
        set_setting(k, v)
    return jsonify(updates)

@app.get("/api/admin/settings")
def admin_get_settings():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    return jsonify({
        "commission_percent": target_commission_percent(),
        "lead_fee": float(get_setting("lead_fee", "30")),
        "featured_price": float(get_setting("featured_price", "999")),
        "verification_price": float(get_setting("verification_price", "499")),
        "subscription_price": float(get_setting("subscription_price", "1499")),
        "parts_commission_percent": float(get_setting("parts_commission_percent", "10")),
        **free_status(),
    })

@app.get("/api/admin/overview")
def admin_overview():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    c = db()
    total_orders = c.execute("SELECT COUNT(*) n FROM orders").fetchone()["n"]
    completed = c.execute("SELECT COUNT(*) n FROM orders WHERE status='completed'").fetchone()["n"]
    paid = c.execute("SELECT COALESCE(SUM(quote_amount),0) s FROM orders WHERE payment_status IN ('paid','pay_on_visit')").fetchone()["s"]
    commission_earned = c.execute("SELECT COALESCE(SUM(commission_amount),0) s FROM orders WHERE payment_status IN ('paid','pay_on_visit')").fetchone()["s"]
    pending_cash_commission = c.execute("""SELECT COALESCE(SUM(commission_amount),0) s FROM orders
                                            WHERE payment_status='pay_on_visit'""").fetchone()["s"]
    technicians = c.execute("SELECT COUNT(*) n FROM users WHERE role='technician'").fetchone()["n"]
    customers = c.execute("SELECT COUNT(*) n FROM users WHERE role='customer'").fetchone()["n"]
    extra_rows = c.execute("SELECT source, COALESCE(SUM(amount),0) s FROM revenue_log GROUP BY source").fetchall()
    extra_by_source = {r["source"]: r["s"] for r in extra_rows}
    extra_total = sum(extra_by_source.values())
    today_iso = datetime.date.today().isoformat()
    featured_count = c.execute("SELECT COUNT(*) n FROM users WHERE featured_until >= ?", (today_iso,)).fetchone()["n"]
    verified_badge_count = c.execute("SELECT COUNT(*) n FROM users WHERE verified_badge_until >= ?", (today_iso,)).fetchone()["n"]
    pro_count = c.execute("SELECT COUNT(*) n FROM users WHERE plan='pro' AND plan_expires_at >= ?", (today_iso,)).fetchone()["n"]
    return jsonify({
        "total_orders": total_orders, "completed_orders": completed,
        "total_paid_volume": paid, "commission_earned": commission_earned,
        "pending_cash_commission": pending_cash_commission,
        "technicians": technicians, "customers": customers,
        "commission_percent": commission_percent(),
        "extra_revenue_total": extra_total, "extra_revenue_by_source": extra_by_source,
        "featured_count": featured_count, "verified_badge_count": verified_badge_count, "pro_count": pro_count,
        **free_status(),
    })

@app.get("/api/admin/orders")
def admin_orders():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    rows = db().execute("""SELECT o.*, c.name AS customer_name, t.name AS technician_name,
                            t.shop_name AS technician_shop
                            FROM orders o LEFT JOIN users c ON c.id=o.customer_id
                            LEFT JOIN users t ON t.id=o.technician_id
                            ORDER BY o.id DESC""").fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/admin/technicians")
def admin_technicians():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    rows = db().execute("""SELECT t.id, t.name, t.shop_name, t.category, t.area, t.rating, t.jobs_done,
                            t.featured_until, t.verified_badge_until, t.plan, t.plan_expires_at,
                            COALESCE(SUM(o.commission_amount),0) AS commission_generated,
                            COALESCE(SUM(o.technician_payout),0) AS total_payout,
                            COALESCE(SUM(CASE WHEN o.payment_status='pay_on_visit' THEN o.commission_amount ELSE 0 END),0) AS commission_owed
                            FROM users t LEFT JOIN orders o ON o.technician_id=t.id AND o.payment_status IN ('paid','pay_on_visit')
                            WHERE t.role='technician' GROUP BY t.id ORDER BY commission_generated DESC""").fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/admin/revenue-log")
def admin_revenue_log():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    rows = db().execute("""SELECT r.*, t.name AS technician_name, t.shop_name AS technician_shop
                            FROM revenue_log r LEFT JOIN users t ON t.id=r.technician_id
                            ORDER BY r.id DESC LIMIT 100""").fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.get("/api/admin/ads")
def admin_ads():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    rows = db().execute("SELECT * FROM ads ORDER BY id DESC").fetchall()
    return jsonify([row2dict(r) for r in rows])

@app.post("/api/admin/ads")
def admin_create_ad():
    """Admin records a new ad slot sold to a brand/shop — monthly_fee is
    logged as revenue immediately (assumes the advertiser paid offline)."""
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    x = request.json or {}
    title = (x.get("title") or "").strip()
    if not title:
        return jsonify({"error": "title is required"}), 400
    fee = float(x.get("monthly_fee") or 0)
    c = db()
    c.execute("""INSERT INTO ads(title,description,link,advertiser_contact,monthly_fee,active,created_at)
                 VALUES(?,?,?,?,?,1,?)""",
              (title, x.get("description"), x.get("link"), x.get("advertiser_contact"), fee, now()))
    ad_id = c.execute("SELECT last_insert_rowid() id").fetchone()["id"]
    if fee > 0:
        log_revenue("ads", fee, note=f"Ad slot: {title} (month 1)")
    c.commit()
    return jsonify(row2dict(c.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone())), 201

@app.post("/api/admin/ads/<int:ad_id>/toggle")
def admin_toggle_ad(ad_id):
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    c = db()
    ad = c.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not ad:
        return jsonify({"error": "not found"}), 404
    c.execute("UPDATE ads SET active=? WHERE id=?", (0 if ad["active"] else 1, ad_id))
    c.commit()
    return jsonify(row2dict(c.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()))

@app.post("/api/admin/ads/<int:ad_id>/renew")
def admin_renew_ad(ad_id):
    """Log another month's payment for an existing ad slot."""
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    c = db()
    ad = c.execute("SELECT * FROM ads WHERE id=?", (ad_id,)).fetchone()
    if not ad:
        return jsonify({"error": "not found"}), 404
    if ad["monthly_fee"] > 0:
        log_revenue("ads", ad["monthly_fee"], note=f"Ad slot: {ad['title']} (renewal)")
    return jsonify({"ok": True})

@app.get("/api/admin/whatsapp-log")
def admin_whatsapp_log():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    rows = db().execute("""SELECT w.*, o.reference FROM whatsapp_log w
                            LEFT JOIN orders o ON o.id=w.order_id
                            ORDER BY w.id DESC LIMIT 40""").fetchall()
    return jsonify([row2dict(r) for r in rows])

# ================================================================ WHATSAPP ==
@app.get("/api/whatsapp/mine")
def whatsapp_mine():
    """In-app preview of the (simulated) WhatsApp messages sent to the
    logged-in user — lets you demo the notification flow without a real
    WhatsApp Business API account. See send_whatsapp() for what going live
    actually requires."""
    u = auth_user()
    if not u:
        return jsonify({"error": "login first"}), 401
    rows = db().execute("""SELECT * FROM whatsapp_log WHERE to_phone=? ORDER BY id DESC LIMIT 20""",
                         (u["phone"],)).fetchall()
    return jsonify([row2dict(r) for r in rows])

# ======================================================= NOTIFICATIONS ====
@app.get("/api/notifications")
def notifications():
    """Lightweight polling badge — counts things this account hasn't seen
    yet. Call every few seconds from the frontend."""
    u = auth_user()
    if not u:
        return jsonify({"error": "login first"}), 401
    c = db()
    if u["role"] == "technician":
        n = c.execute("SELECT COUNT(*) n FROM orders WHERE status='requested' AND service=?",
                      (u["category"],)).fetchone()["n"]
    else:
        # active = accepted-but-not-yet-completed orders for this customer
        n = c.execute("""SELECT COUNT(*) n FROM orders WHERE customer_id=?
                          AND status NOT IN ('requested','completed')""", (u["id"],)).fetchone()["n"]
    return jsonify({"count": n})

@app.get("/api/commission")
def public_commission():
    """Public so the technician dashboard can show an estimated payout
    before payment happens — no auth needed, it's not sensitive."""
    return jsonify({"commission_percent": commission_percent()})

@app.get("/api/promo")
def public_promo():
    """Public — powers the 'free for 6 months' banner on the homepage and
    technician dashboard. No auth needed, nothing sensitive here."""
    return jsonify(free_status())

@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "FixScore API"})

# Always initialize the database on import — this runs whether the app is
# started directly (`python app.py`) or via a production server like
# gunicorn (`gunicorn app:app`), which never executes the __main__ block.
init()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
