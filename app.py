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
RAZORPAY_KEY_ID = os.environ.get("RAZORPAY_KEY_ID", "rzp_test_TXUNnPir1RrovT")
RAZORPAY_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET", "ZchwHMrdoRG7R041bK4j1u10")
razorpay_client = razorpay.Client(auth=(RAZORPAY_KEY_ID, RAZORPAY_KEY_SECRET))

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
    """)
    # default platform commission — the % FixScore keeps from every paid order
    if not c.execute("SELECT 1 FROM settings WHERE key='commission_percent'").fetchone():
        c.execute("INSERT INTO settings(key,value) VALUES('commission_percent','15')")
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
    "AC":        {"issue": "Cooling / gas-related issue", "fair_price": "₹500 – ₹1,500",   "urgency": "Medium"},
    "RO":        {"issue": "Filter / water-flow issue",    "fair_price": "₹300 – ₹1,200",  "urgency": "Low"},
    "Fridge":    {"issue": "Cooling system / thermostat issue", "fair_price": "₹600 – ₹2,500", "urgency": "Medium"},
    "Washing Machine": {"issue": "Drain / motor / sensor issue", "fair_price": "₹500 – ₹2,500", "urgency": "Medium"},
    "Electrician": {"issue": "Switch / wiring / connection issue", "fair_price": "₹200 – ₹1,500", "urgency": "Medium"},
    "Plumber":   {"issue": "Pipe / tap leakage issue",      "fair_price": "₹200 – ₹1,200",  "urgency": "Low"},
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

def commission_percent():
    try:
        return float(get_setting("commission_percent", "15"))
    except (TypeError, ValueError):
        return 15.0

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

def diagnose(service, symptoms):
    """Turns free-text + a symptom checklist into a structured, explainable
    estimate. This is a rule-based stand-in for a real ML/LLM diagnosis
    model — swap the body of this function for an API call when you have one."""
    base = DIAGNOSIS_RULES.get(service, {"issue": "Inspection required", "fair_price": "Variable", "urgency": "Medium"})
    symptoms = symptoms or []
    matched = len(symptoms)
    confidence = min(60 + matched * 8, 93)
    reasons = []
    if symptoms:
        reasons.append(f"Matched {matched} of the symptoms you selected to known {service} faults")
    else:
        reasons.append("Based only on your description — select symptoms above for a sharper estimate")
    urgency = base["urgency"]
    if any(("spark" in s.lower() or "burn" in s.lower() or "jalne" in s.lower()) for s in symptoms):
        urgency = "High"
        reasons.append("Marked high urgency because of a safety-related symptom (sparking / burning smell)")
    return {
        "issue": base["issue"],
        "fair_price": base["fair_price"],
        "urgency": urgency,
        "confidence": f"{confidence}%",
        "reasons": reasons,
    }

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
    c.execute("UPDATE orders SET technician_id=?, status='accepted', updated_at=? WHERE id=?",
              (u["id"], now(), order_id))
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
def _finalize_payment(order_id, u, method, status):
    """Shared by cash (instant) and UPI/card (after Razorpay verification).
    Splits the paid amount into FixScore's commission + technician payout."""
    c = db()
    o = c.execute("SELECT * FROM orders WHERE id=? AND customer_id=?", (order_id, u["id"])).fetchone()
    pct = commission_percent()
    amount = float(o["quote_amount"] or 0)
    commission_amount = round(amount * pct / 100, 2)
    technician_payout = round(amount - commission_amount, 2)
    c.execute("""UPDATE orders SET payment_method=?, payment_status=?, commission_percent=?,
                 commission_amount=?, technician_payout=?, updated_at=? WHERE id=?""",
              (method, status, pct, commission_amount, technician_payout, now(), order_id))
    c.commit()
    updated = c.execute("SELECT * FROM orders WHERE id=?", (order_id,)).fetchone()
    tech = c.execute("SELECT * FROM users WHERE id=?", (o["technician_id"],)).fetchone() if o["technician_id"] else None
    send_whatsapp(u["phone"], "customer",
        f"FixScore: Payment of ₹{amount:,.0f} received for {o['reference']} ({method.upper()}). Thanks for using FixScore!", order_id)
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
    amount_paise = int(round(float(o["quote_amount"] or 0) * 100))
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

# ================================================================ ADMIN ====
# This is FixScore's own back-office: revenue, commission and technician
# payouts. Log in with phone "admin" / password "admin123" (seeded in
# init() above) — change that password before this goes anywhere public.
@app.post("/api/admin/settings")
def admin_set_settings():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    pct = (request.json or {}).get("commission_percent")
    try:
        pct = float(pct)
    except (TypeError, ValueError):
        return jsonify({"error": "commission_percent must be a number"}), 400
    if not (0 <= pct <= 100):
        return jsonify({"error": "commission_percent must be between 0 and 100"}), 400
    set_setting("commission_percent", pct)
    return jsonify({"commission_percent": pct})

@app.get("/api/admin/settings")
def admin_get_settings():
    u = require_role("admin")
    if not u:
        return jsonify({"error": "admin login required"}), 401
    return jsonify({"commission_percent": commission_percent()})

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
    return jsonify({
        "total_orders": total_orders, "completed_orders": completed,
        "total_paid_volume": paid, "commission_earned": commission_earned,
        "pending_cash_commission": pending_cash_commission,
        "technicians": technicians, "customers": customers,
        "commission_percent": commission_percent(),
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
                            COALESCE(SUM(o.commission_amount),0) AS commission_generated,
                            COALESCE(SUM(o.technician_payout),0) AS total_payout,
                            COALESCE(SUM(CASE WHEN o.payment_status='pay_on_visit' THEN o.commission_amount ELSE 0 END),0) AS commission_owed
                            FROM users t LEFT JOIN orders o ON o.technician_id=t.id AND o.payment_status IN ('paid','pay_on_visit')
                            WHERE t.role='technician' GROUP BY t.id ORDER BY commission_generated DESC""").fetchall()
    return jsonify([row2dict(r) for r in rows])

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
