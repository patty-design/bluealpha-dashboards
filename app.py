from flask import Flask, send_from_directory, abort, request, Response, redirect, make_response
import os
import re
import functools
import json
import base64
import threading
import secrets
import requests as req_lib
import jwt as pyjwt

# Per-record lock registry — prevents race condition where two background threads
# both pass the _has_items check before either creates Return Items records.
_return_items_locks: dict = {}
_return_items_locks_mutex = threading.Lock()

def _get_return_items_lock(record_id: str) -> threading.Lock:
    with _return_items_locks_mutex:
        if record_id not in _return_items_locks:
            _return_items_locks[record_id] = threading.Lock()
        return _return_items_locks[record_id]

_BUILD_VERSION = "catalog-live"

AIRTABLE_OPS_TOKEN      = os.environ.get("AIRTABLE_OPS_TOKEN", "")
AIRTABLE_BASE_TOKEN     = os.environ.get("AIRTABLE_BASE_TOKEN", "")
AIRTABLE_WRITE_TOKEN    = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
RETURNS_WRITE_TOKEN     = os.environ.get("AIRTABLE_WRITE_TOKEN_2", AIRTABLE_WRITE_TOKEN)
APPLY_WRITE_TOKEN       = os.environ.get("APPLY_WRITE_TOKEN", "")

def _today_utc():
    """Return today's date in UTC (avoids Railway timezone drift)."""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).date()
FLASK_BASE_URL          = os.environ.get("FLASK_BASE_URL", "https://bluealpha-dashboards-production.up.railway.app")
AIRTABLE_BASE_ID        = "appA13jo4b3TIn4yT"
RETURNS_TABLE_ID        = os.environ.get("RETURNS_TABLE_ID", "tblxwbeaVHBzXcAen")
RETURN_ITEMS_TABLE_ID   = "tblThFm0UA6gLQShV"
PRODUCT_SKUS_TABLE_ID   = "tbljngm75r4Km2XIN"
RM_SNAPSHOTS_TABLE_ID   = os.environ.get("RM_SNAPSHOTS_TABLE_ID", "")
RM_SNAPSHOTS_BASE_ID    = os.environ.get("RM_SNAPSHOTS_BASE_ID", AIRTABLE_BASE_ID)
RAW_MATERIALS_TABLE_ID  = "tblokid4GHQCvdXuQ"
SIZING_EXCHANGE_STORE_ID = 185018

SHIPSTATION_KEY      = os.environ.get("SHIPSTATION_KEY", "")
SHIPSTATION_SECRET   = os.environ.get("SHIPSTATION_SECRET", "")
SENDGRID_API_KEY     = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL  = os.environ.get("SENDGRID_FROM_EMAIL", "info@bluealpha.us")
TEST_EMAIL_OVERRIDE  = os.environ.get("TEST_EMAIL_OVERRIDE", "")
CS_ADMIN_PASSWORD    = os.environ.get("CS_ADMIN_PASSWORD", "")

QUOTE_ADMIN_PASSWORD     = os.environ.get("QUOTE_ADMIN_PASSWORD", "")
QUOTE_CS_PASSWORD        = os.environ.get("QUOTE_CS_PASSWORD", "")
QUOTE_SECRET_KEY         = os.environ.get("QUOTE_SECRET_KEY", "change-me-ba-portal-2024")

STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
INT_EXCHANGE_TABLE_ID    = os.environ.get("INT_EXCHANGE_TABLE_ID", "")
RETURN_ADDRESS_INTL      = "35 Andrew St., Newnan, GA 30263 USA"

MANUAL_ORDERS_TABLE_ID   = "tblOOZ2wVzIsR1DyL"
MO_LINE_ITEMS_TABLE_ID   = "tblNDxbfgyZDMex7n"
CUSTOMERS_TABLE_ID       = "tblO4AdJE84kFDfEe"
EMPLOYEES_TABLE_ID       = "tblUDcItnhNhe2GgO"
# NOTE: These two tables must be created manually in Airtable and their IDs updated here.
# Table 1: "Account Applications" — see task description for required fields
# Table 2: "Portal Users" — see task description for required fields
APP_APPLICATIONS_TABLE_ID = os.environ.get("APP_APPLICATIONS_TABLE_ID", "tbl_REPLACE_APPLICATIONS")
PORTAL_USERS_TABLE_ID     = os.environ.get("PORTAL_USERS_TABLE_ID",     "tbl_REPLACE_PORTAL_USERS")
PARENT_PRODUCTS_TABLE_ID    = "tbl40th76YvjdQExS"
INVENTORY_ADJUSTMENTS_TABLE_ID = "tbl95iUeitvqYwggK"
COLORS_TABLE_ID             = "tblN08IV26TpRYSMf"
SIZES_TABLE_ID              = "tblUGwl1YLaVGCeIJ"
FEATURE_VARIATIONS_TABLE_ID = "tblwbWDNFSjJSV9hh"
ADDONS_TABLE_ID             = "tblW8N35cbaXQDuQv"
QUOTE_BASE_URL           = os.environ.get("QUOTE_BASE_URL", "https://quote.bluealphabelts.com")

app = Flask(__name__, static_folder="static")

@app.before_request
def redirect_http_to_https():
    # Railway sets X-Forwarded-Proto when behind the proxy
    proto = request.headers.get("X-Forwarded-Proto") or request.headers.get("X-Forwarded-Scheme")
    if proto == "http":
        return redirect(request.url.replace("http://", "https://", 1), 301)

@app.after_request
def add_security_headers(response):
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    return response

# In-memory status cache for return submissions (cleared on restart, only needed during ~60s poll window)
_return_status_cache = {}

# In-memory store for pending international exchange checkout sessions (keyed by UUID ref_id)
_intl_pending = {}

DASHBOARDS = {
    "kurt": "kurt.html",
    "jesse": "jesse.html",
    "kelly": "kelly.html",
    "patty": "patty.html",
}

OPS_DASHBOARDS = {
    "production": "production.html",
    "shipments":  "shipments.html",
    "waiting":    "waiting.html",
    "returns":    "returns.html",
}

USERNAME = "bluealpha"
PASSWORD = "bluealpha2026"

def check_auth(username, password):
    return username == USERNAME and password == PASSWORD

def require_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return Response(
                "Authentication required.",
                401,
                {"WWW-Authenticate": 'Basic realm="Blue Alpha Dashboards"'}
            )
        return f(*args, **kwargs)
    return decorated

@app.route("/_version")
def version():
    tok = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    return Response(json.dumps({
        "v": _BUILD_VERSION,
        "base_token_set": bool(AIRTABLE_BASE_TOKEN),
        "token_prefix": tok[:12] if tok else "(empty)",
        "write_token_set": bool(RETURNS_WRITE_TOKEN),
        "write_token_prefix": RETURNS_WRITE_TOKEN[:12] if RETURNS_WRITE_TOKEN else "(empty)",
    }), mimetype="application/json")

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)

@app.route("/")
def index():
    host = request.host.split(".")[0].lower()
    if host == "exchange":
        return redirect("/exchange")
    if host == "return":
        return send_from_directory("static", "returns.html")
    if host in DASHBOARDS:
        return dashboard(host)
    if host == "quote":
        return redirect("/quote")
    return "Blue Alpha Dashboards", 200

@app.route("/cs")
def cs_returns():
    return send_from_directory("static", "cs-returns.html")

@app.route("/<name>")
@require_auth
def dashboard(name):
    if name in DASHBOARDS:
        return send_from_directory("static", DASHBOARDS[name])
    if name in OPS_DASHBOARDS:
        filepath = os.path.join(app.static_folder, OPS_DASHBOARDS[name])
        with open(filepath, "r") as f:
            content = f.read()
        content = content.replace("%%AIRTABLE_OPS_TOKEN%%", AIRTABLE_OPS_TOKEN)
        return Response(content, mimetype="text/html", headers={
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0",
        })
    abort(404)

def ss_headers():
    creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}

def cors():
    return {"Access-Control-Allow-Origin": "*"}

def at_headers(token):
    return {"Authorization": f"Bearer {token}"}

def _next_order_id(read_token):
    """Return the next Manual Order ID string (zero-padded 4 digits) by fetching only the top record."""
    try:
        params = {"pageSize": 1, "fields[]": "Order ID",
                  "sort[0][field]": "Order ID", "sort[0][direction]": "desc"}
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers=at_headers(read_token), params=params, timeout=10,
        )
        records = r.json().get("records", [])
        if records:
            oid = records[0].get("fields", {}).get("Order ID", "0")
            try:
                return str(int(str(oid)) + 1).zfill(4)
            except (ValueError, TypeError):
                pass
    except Exception:
        pass
    import time as _t
    return str(int(_t.time()))[-6:]   # fallback: last 6 digits of timestamp

def at_get_all(table_id, token, fields=None, formula=None, base_id=None):
    """Paginate through all records in an Airtable table."""
    records = []
    offset = None
    bid = base_id or AIRTABLE_BASE_ID
    while True:
        params = {"pageSize": 100}
        if fields:
            for i, f in enumerate(fields):
                params[f"fields[{i}]"] = f
        if formula:
            params["filterByFormula"] = formula
        if offset:
            params["offset"] = offset
        r = req_lib.get(
            f"https://api.airtable.com/v0/{bid}/{table_id}",
            headers=at_headers(token),
            params=params,
            timeout=30,
        )
        data = r.json()
        records.extend(data.get("records", []))
        offset = data.get("offset")
        if not offset:
            break
    return records


# ─────────────────────────────────────────────────────────────────────────────
# Portal Auth Helpers
# ─────────────────────────────────────────────────────────────────────────────

def create_portal_token(user_id, customer_id, is_primary, role=None):
    from datetime import datetime, timezone, timedelta
    payload = {
        "user_id":     user_id,
        "customer_id": customer_id,
        "is_primary":  is_primary,
        "role":        role,   # None = legacy primary (treated as admin)
        "exp": datetime.now(timezone.utc) + timedelta(days=30),
    }
    return pyjwt.encode(payload, QUOTE_SECRET_KEY, algorithm="HS256")


def get_portal_user(req):
    token = req.cookies.get("ba_portal_session")
    if not token:
        return None
    try:
        return pyjwt.decode(token, QUOTE_SECRET_KEY, algorithms=["HS256"])
    except Exception:
        return None


def portal_login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        user = get_portal_user(request)
        if not user:
            return redirect("/login")
        return f(*args, user=user, **kwargs)
    return decorated


def _hash_password(password, salt=None):
    import hashlib, secrets as sec
    if salt is None:
        salt = sec.token_hex(16)
    h = hashlib.sha256((salt + password).encode()).hexdigest()
    return f"sha256${salt}${h}"

def _check_password(password, stored_hash):
    try:
        _, salt, _ = stored_hash.split("$")
        return _hash_password(password, salt) == stored_hash
    except Exception:
        return False

def _lookup_admin(username, password):
    """Check username+password against Employees table. Returns record dict or None."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"AND({{Portal Username}}='{username}',{{Quote Portal Admin}}=1)"
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula, "fields[]": ["Full Name", "Portal Username", "Password Hash", "Email"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return None
        rec = records[0]
        stored = rec.get("fields", {}).get("Password Hash", "")
        if not stored or not _check_password(password, stored):
            return None
        return rec
    except Exception:
        return None

def _lookup_portal_user(username, password):
    """Check username+password for EITHER admin OR CS role.
    Returns (record, role) where role is 'admin' or 'cs', or (None, None)."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"AND({{Portal Username}}='{username}',OR({{Quote Portal Admin}}=1,{{Quote Portal CS}}=1))"
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula, "fields[]": ["Full Name", "Portal Username", "Password Hash", "Email", "Quote Portal Admin", "Quote Portal CS"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return None, None
        rec = records[0]
        stored = rec.get("fields", {}).get("Password Hash", "")
        if not stored or not _check_password(password, stored):
            return None, None
        role = 'admin' if rec.get("fields", {}).get("Quote Portal Admin") else 'cs'
        return rec, role
    except Exception:
        return None, None

def _lookup_portal_customer(username, password):
    """Look up a B2B customer by Portal Username + Password Hash.
    Returns (record, role, customer_id) or (None, None, None).
    role is one of: 'admin', 'full_access', 'quotes_only', 'read_only', or None (legacy = admin).
    customer_id is Parent Company ID if set, else own record ID."""
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    formula = f"{{Portal Username}}='{username}'"
    try:
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Portal Username", "Portal Hash", "Portal Role",
                    "Parent Company", "Application Status"],
            formula=formula,
        )
        if not records:
            return None, None, None
        rec = records[0]
        f = rec.get("fields", {})
        stored_hash = f.get("Portal Hash", "")
        if not stored_hash or not _check_password(password, stored_hash):
            return None, None, None
        # Only approved customers can log in
        app_status = f.get("Application Status", "")
        if app_status and app_status != "Approved":
            return None, None, None
        role_raw = (f.get("Portal Role") or "").strip()
        role_map = {
            "Admin":       "admin",
            "Full Access": "full_access",
            "Quotes Only": "quotes_only",
            "Read Only":   "read_only",
        }
        role = role_map.get(role_raw, None)
        parent_ids = f.get("Parent Company", [])
        customer_id = parent_ids[0] if parent_ids else rec["id"]
        return rec, role, customer_id
    except Exception as e:
        print(f"[_lookup_portal_customer] error: {e}")
        return None, None, None


# Permission hierarchy (hierarchical — each role includes all below it)
_PORTAL_PERMISSIONS = {
    "read_only":   {"view"},
    "quotes_only": {"view", "create_quote"},
    "full_access": {"view", "create_quote", "accept_quote"},
    "admin":       {"view", "create_quote", "accept_quote", "manage_team"},
}

def portal_can(user, action):
    """Check if a portal user JWT payload can perform an action.
    user is the decoded JWT dict. action is one of:
    'view', 'create_quote', 'accept_quote', 'manage_team'.
    No role set (legacy primary users) → treated as admin."""
    role = user.get("role")
    if role is None:
        return True  # Legacy primary user — full admin
    return action in _PORTAL_PERMISSIONS.get(role, set())


def check_admin_session(req):
    token = req.cookies.get("ba_admin_session")
    if not token:
        return False
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("admin") is True
    except Exception:
        return False

def get_portal_role(req):
    """Returns 'admin', 'cs', or None from the admin session cookie."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("role") or ('admin' if data.get("admin") else None)
    except Exception:
        return None

def get_admin_username(req):
    """Return username from admin session, or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("username") if data.get("admin") else None
    except Exception:
        return None

def get_portal_username(req):
    """Return username from portal session (admin or CS), or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("username") if (data.get("admin") or data.get("role") in ('admin', 'cs')) else None
    except Exception:
        return None

def get_portal_record_id(req):
    """Return the Employees record_id from the portal session cookie, or None."""
    token = req.cookies.get("ba_admin_session")
    if not token:
        return None
    try:
        data = pyjwt.decode(token, QUOTE_SECRET_KEY + "_admin", algorithms=["HS256"])
        return data.get("record_id")
    except Exception:
        return None


def generate_magic_link(portal_user_record_id, expiry_hours=0.25):
    from datetime import datetime, timezone, timedelta
    token = secrets.token_urlsafe(32)
    expiry = datetime.now(timezone.utc) + timedelta(hours=expiry_hours)
    expiry_iso = expiry.isoformat().replace("+00:00", "Z")
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{portal_user_record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Magic Token": token, "Token Expiry": expiry_iso}},
            timeout=10,
        )
    except Exception as e:
        print(f"[generate_magic_link] Airtable PATCH failed: {e}")
    return f"{QUOTE_BASE_URL}/auth/{token}"


def send_approval_email(to_email, to_name, company, magic_link):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 20px;">
            Great news — your application for <strong>{company}</strong> has been approved!
            You can now access the Blue Alpha Government Agency Quote Portal.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            Click the button below to log in. This link is valid for 48 hours.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr><td>
              <a href="{magic_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:14px;font-weight:700;text-decoration:none;padding:13px 32px;border-radius:6px;">Access Portal →</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:12px;line-height:1.5;">
            If you have trouble with the button, copy and paste this link:<br>
            <a href="{magic_link}" style="color:#1B2438;">{magic_link}</a>
          </p>
          <p style="color:#6b7a8d;font-size:12px;margin-top:16px;">
            Questions? Contact us at <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Your Blue Alpha Portal Access Has Been Approved",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_approval_email] failed: {e}")


def send_denial_email(to_email, to_name, company, reason):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            Thank you for your interest in the Blue Alpha Government Agency Quote Portal.
            Unfortunately, we're unable to approve the application for <strong>{company}</strong> at this time.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            <strong>Reason:</strong> {reason}
          </p>
          <p style="color:#6b7a8d;font-size:13px;line-height:1.6;">
            If you have questions, please contact us at
            <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Blue Alpha Portal Application Update",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_denial_email] failed: {e}")


def send_magic_link_email(to_email, magic_link):
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 16px;">Your portal login link</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            Click the button below to log in to the Blue Alpha Government Portal. This link expires in 15 minutes and can only be used once.
          </p>
          <table cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
            <tr><td>
              <a href="{magic_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:14px;font-weight:700;text-decoration:none;padding:13px 32px;border-radius:6px;">Log In to Portal →</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:12px;line-height:1.5;">
            If you didn't request this, you can safely ignore this email.<br><br>
            Trouble with the button? Copy this link:<br>
            <a href="{magic_link}" style="color:#1B2438;word-break:break-all;">{magic_link}</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "Your Blue Alpha Portal Login Link",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_magic_link_email] failed: {e}")


@app.route("/api/raw-material-cost", methods=["GET"])
def raw_material_cost():
    c = cors()
    try:
        records = at_get_all(RAW_MATERIALS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                             fields=["Total Inventory Value"])
        total = sum(r["fields"].get("Total Inventory Value") or 0 for r in records)
        count = len(records)
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500,
                        headers=c, mimetype="application/json")

    snapshots = []
    if RM_SNAPSHOTS_TABLE_ID:
        try:
            snap_records = at_get_all(
                RM_SNAPSHOTS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                fields=["Month", "Total Inventory Value", "Record Count", "Notes"],
                base_id=RM_SNAPSHOTS_BASE_ID,
            )
            snapshots = sorted(
                [
                    {
                        "id": r["id"],
                        "month": r["fields"].get("Month"),
                        "total": r["fields"].get("Total Inventory Value"),
                        "count": r["fields"].get("Record Count"),
                        "notes": r["fields"].get("Notes", ""),
                    }
                    for r in snap_records
                    if r["fields"].get("Month")
                ],
                key=lambda x: x["month"],
            )
        except Exception:
            snapshots = []

    return Response(
        json.dumps({"current": {"total": round(total, 2), "count": count},
                    "snapshots": snapshots}),
        headers=c, mimetype="application/json",
    )


@app.route("/api/raw-material-cost/capture", methods=["POST", "OPTIONS"])
def capture_raw_material_cost():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(),
                                     "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    if not RM_SNAPSHOTS_TABLE_ID or not AIRTABLE_WRITE_TOKEN:
        return Response(
            json.dumps({"error": "RM_SNAPSHOTS_TABLE_ID or AIRTABLE_WRITE_TOKEN not configured"}),
            status=400, headers=c, mimetype="application/json",
        )

    try:
        records = at_get_all(RAW_MATERIALS_TABLE_ID, AIRTABLE_OPS_TOKEN,
                             fields=["Total Inventory Value"])
        total = sum(r["fields"].get("Total Inventory Value") or 0 for r in records)
        count = len(records)
    except Exception as e:
        return Response(json.dumps({"error": f"Airtable fetch failed: {str(e)}"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import date as dt_date
    body = request.get_json() or {}
    snap_date = body.get("date", _today_utc().isoformat())
    notes = body.get("notes", "Auto-captured snapshot")

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{RM_SNAPSHOTS_BASE_ID}/{RM_SNAPSHOTS_TABLE_ID}",
            headers={**at_headers(AIRTABLE_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Month": snap_date,
                "Total Inventory Value": round(total, 2),
                "Record Count": count,
                "Notes": notes,
            }},
            timeout=10,
        )
        if r.status_code in (200, 201):
            return Response(
                json.dumps({"success": True, "total": round(total, 2),
                            "count": count, "date": snap_date}),
                headers=c, mimetype="application/json",
            )
        else:
            return Response(json.dumps({"error": r.text}),
                            status=500, headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/verify-order", methods=["POST", "OPTIONS"])
def verify_order():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number = data.get("orderNumber", "").strip()
    last_name    = data.get("lastName", "").strip().lower()
    email_input  = data.get("email", "").strip().lower()

    if not order_number or (not last_name and not email_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        # Look up order in ShipStation
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name and last_name == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Check international — also block military overseas (APO/FPO/DPO)
        # which use state codes AA, AE, AP and are USPS-domestic but overseas
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "US")
        state   = order.get("shipTo", {}).get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Block returns on unshipped orders
        order_status = order.get("orderStatus", "")
        UNSHIPPED_STATUSES = {"awaiting_shipment", "awaiting_payment", "on_hold"}
        if order_status in UNSHIPPED_STATUSES:
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        # Also block if orderStatus isn't clearly shipped and there are no shipments
        if not ship_date_str and order_status != "shipped":
            return Response(json.dumps({"status": "not_shipped"}), headers=c, mimetype="application/json")

        def parse_dt(s):
            """Parse ISO date/datetime string, always return UTC-aware datetime."""
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=37)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Build items list — exclude empty SKUs and discount/fee line items
        def is_returnable_item(i):
            sku = (i.get("sku") or "").strip()
            if not sku:
                return False
            if "total-discount" in sku.lower():
                return False
            return bool(i.get("name"))
        items = [{"sku": i.get("sku","").strip(), "name": i.get("name",""), "quantity": i.get("quantity",1)}
                 for i in order.get("items", []) if is_returnable_item(i)]

        # Check for existing active returns — build qty-per-SKU of already-requested returns
        # Use AIRTABLE_OPS_TOKEN for reads (write token may not have read scope)
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        already_returned_qtys = {}  # {sku: total_qty_already_requested}
        if RETURNS_TABLE_ID and airtable_read_token:
            try:
                filter_formula = (f"AND({{Order Number}}='{order_number}',"
                                 f"OR({{Status}}='New',{{Status}}='Label Sent',"
                                 f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                                 f"{{Status}}='Refunded'))")
                ar = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                    params={"filterByFormula": filter_formula, "fields[]": ["Items to Return", "Status"]},
                    headers={"Authorization": f"Bearer {airtable_read_token}"},
                    timeout=10,
                )
                import re as re_lib
                for rec in ar.json().get("records", []):
                    items_text = rec.get("fields", {}).get("Items to Return", "")
                    for line in items_text.split("\n"):
                        # Format: "1x SKU-123 — Item Name"
                        m = re_lib.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                        if m:
                            qty  = int(m.group(1))
                            sku  = m.group(2).strip()
                            already_returned_qtys[sku] = already_returned_qtys.get(sku, 0) + qty
            except Exception:
                pass  # Don't block the flow if this check fails

        ship_to = order.get("shipTo", {})
        phone = (ship_to.get("phone") or
                 (order.get("billTo") or {}).get("phone") or "")
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "shipDate":      ship_date_str,
            "eligibleUntil": eligible_until.isoformat(),
            "email":         order.get("customerEmail", ""),
            "phone":         phone,
            "address": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
            },
            "items": items,
            "alreadyReturnedQtys": already_returned_qtys,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-lookup-order", methods=["POST", "OPTIONS"])
def cs_lookup_order():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip()
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        # Fetch order — no eligibility check, just pull the data
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        def is_returnable_item(i):
            sku = (i.get("sku") or "").strip()
            if not sku:
                return False
            if "total-discount" in sku.lower():
                return False
            return bool(i.get("name"))

        items = [{"sku": i.get("sku", "").strip(), "name": i.get("name", ""), "quantity": i.get("quantity", 1)}
                 for i in order.get("items", []) if is_returnable_item(i)]

        # Remove already-cancelled items/quantities from this form's previous submissions
        try:
            formula = f"AND({{Order Number}}='{order_number}',{{Type}}='Cancellation')"
            ac_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": formula, "fields[]": ["Items to Return"]},
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                timeout=10,
            )
            already_cancelled = {}
            for rec in ac_r.json().get("records", []):
                items_text = rec.get("fields", {}).get("Items to Return", "")
                for line in items_text.split("\n"):
                    m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                    if m:
                        already_cancelled[m.group(2).strip()] = \
                            already_cancelled.get(m.group(2).strip(), 0) + int(m.group(1))
            if already_cancelled:
                adjusted = []
                for item in items:
                    remaining = item["quantity"] - already_cancelled.get(item["sku"], 0)
                    if remaining > 0:
                        adjusted.append({**item, "quantity": remaining})
                items = adjusted
        except Exception:
            pass  # Don't block lookup if this check fails

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        active_shipments = [s for s in shipments if not s.get("voided", False)]
        ship_date_str    = active_shipments[0].get("shipDate", "") if active_shipments else ""
        tracking_number  = active_shipments[0].get("trackingNumber", "") if active_shipments else ""
        carrier_code     = active_shipments[0].get("carrierCode", "") if active_shipments else ""

        phone = (ship_to.get("phone") or (order.get("billTo") or {}).get("phone") or "")

        # Build already-returned qty map so CS portal can gray out items
        cs_already_returned = {}
        _cs_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        if RETURNS_TABLE_ID and _cs_read_token:
            try:
                _cs_ar_formula = (f"AND({{Order Number}}='{order_number}'"
                                  f",OR({{Status}}='New',{{Status}}='Label Sent',"
                                  f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                                  f"{{Status}}='Refunded'),{{Type}}='Return')")
                _cs_ar_resp = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                    params={"filterByFormula": _cs_ar_formula, "fields[]": ["Items to Return"], "maxRecords": 20},
                    headers={"Authorization": f"Bearer {_cs_read_token}"},
                    timeout=10,
                )
                for _rec in _cs_ar_resp.json().get("records", []):
                    for _line in _rec.get("fields", {}).get("Items to Return", "").split("\n"):
                        _m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', _line.strip())
                        if _m:
                            _sku = _m.group(2).strip()
                            cs_already_returned[_sku] = cs_already_returned.get(_sku, 0) + int(_m.group(1))
            except Exception:
                pass

        # Check for existing UPS Shipping Refund request for this order
        existing_shipping_refund = False
        try:
            _sr_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": f"AND({{Order Number}}='{order_number}',{{Type}}='UPS Shipping Refund')",
                        "maxRecords": 1, "fields[]": ["Order Number"]},
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                timeout=10,
            )
            if _sr_r.status_code == 200 and _sr_r.json().get("records"):
                existing_shipping_refund = True
        except Exception:
            pass

        return Response(json.dumps({
            "status":               "found",
            "orderId":              order.get("orderId"),
            "orderKey":             order.get("orderKey", ""),
            "orderStatus":          order.get("orderStatus", ""),
            "customerName":         ship_to.get("name", ""),
            "email":                order.get("customerEmail", ""),
            "phone":                phone,
            "shipDate":             ship_date_str,
            "trackingNumber":       tracking_number,
            "carrierCode":          carrier_code,
            "address": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
            },
            "items":                items,
            "alreadyReturnedQtys":  cs_already_returned,
            "shippingAmount":       float(order.get("shippingAmount") or 0),
            "existingShippingRefund": existing_shipping_refund,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        print(f"[cs_lookup_order] Exception: {e}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


def cancel_in_shipstation(order_id, items_to_cancel):
    """Void full order or remove specific items and re-save. Returns (success, note)."""
    try:
        # Fetch current order by ID
        r = req_lib.get(
            f"https://ssapi.shipstation.com/orders/{order_id}",
            headers=ss_headers(), timeout=15,
        )
        if r.status_code != 200:
            return False, f"Could not fetch order from ShipStation (HTTP {r.status_code})"
        order = r.json()

        current_items = order.get("items", [])

        # Build cancel map: sku → qty to remove
        cancel_map = {}
        for item in items_to_cancel:
            sku = (item.get("sku") or "").strip()
            if sku:
                cancel_map[sku] = cancel_map.get(sku, 0) + int(item.get("quantity", 1))

        # Determine remaining items after cancellation
        remaining = []
        for item in current_items:
            sku = (item.get("sku") or "").strip()
            qty = item.get("quantity", 1)
            if sku in cancel_map:
                remaining_qty = qty - cancel_map[sku]
                if remaining_qty > 0:
                    item = dict(item)
                    item["quantity"] = remaining_qty
                    remaining.append(item)
                # else: fully cancelled — omit from remaining
            else:
                remaining.append(item)

        if not remaining:
            # All items cancelled — mark order as cancelled in ShipStation
            internal_note = (order.get("internalNotes") or "").strip()
            internal_note += (" | " if internal_note else "") + "Cancelled via CS portal"
            cancel_payload = {
                "orderId":         order.get("orderId"),
                "orderNumber":     order.get("orderNumber"),
                "orderKey":        order.get("orderKey"),
                "orderDate":       order.get("orderDate"),
                "orderStatus":     "cancelled",
                "customerEmail":   order.get("customerEmail", ""),
                "billTo":          order.get("billTo", {}),
                "shipTo":          order.get("shipTo", {}),
                "items":           current_items,
                "amountPaid":      order.get("amountPaid", 0),
                "taxAmount":       order.get("taxAmount", 0),
                "shippingAmount":  order.get("shippingAmount", 0),
                "internalNotes":   internal_note,
                "advancedOptions": order.get("advancedOptions", {}),
            }
            cancel_r = req_lib.post(
                "https://ssapi.shipstation.com/orders/createorder",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json=cancel_payload, timeout=20,
            )
            if cancel_r.status_code in (200, 201):
                return True, "Order cancelled in ShipStation"
            return False, f"Cancel failed (HTTP {cancel_r.status_code}): {cancel_r.text[:200]}"
        else:
            # Partial: update order with only the remaining items
            internal_note = (order.get("internalNotes") or "").strip()
            internal_note += (" | " if internal_note else "") + "Partial cancellation — some items removed"
            payload = {
                "orderId":         order.get("orderId"),
                "orderNumber":     order.get("orderNumber"),
                "orderKey":        order.get("orderKey"),
                "orderDate":       order.get("orderDate"),
                "orderStatus":     order.get("orderStatus", "awaiting_shipment"),
                "customerEmail":   order.get("customerEmail", ""),
                "billTo":          order.get("billTo", {}),
                "shipTo":          order.get("shipTo", {}),
                "items":           remaining,
                "amountPaid":      order.get("amountPaid", 0),
                "taxAmount":       order.get("taxAmount", 0),
                "shippingAmount":  order.get("shippingAmount", 0),
                "internalNotes":   internal_note,
                "advancedOptions": order.get("advancedOptions", {}),
            }
            upd_r = req_lib.post(
                "https://ssapi.shipstation.com/orders/createorder",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json=payload, timeout=20,
            )
            if upd_r.status_code in (200, 201):
                return True, "Partial cancellation applied in ShipStation"
            return False, f"Order update failed (HTTP {upd_r.status_code}): {upd_r.text[:200]}"

    except Exception as e:
        return False, f"Exception during ShipStation cancellation: {e}"


@app.route("/api/cs-verify-exchange", methods=["POST", "OPTIONS"])
def cs_verify_exchange():
    """CS override: look up order for size exchange, skipping identity check and date window."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip().lstrip("#")
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        # Block international and military overseas orders
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = ship_to.get("country", "US")
        state   = ship_to.get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable (same logic as /api/verify-exchange)
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Determine next exchange order suffix
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[cs-verify-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        # CS override: do NOT filter out already-exchanged items (exception flow)

        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-verify-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-verify-intl-exchange", methods=["POST", "OPTIONS"])
def cs_verify_intl_exchange():
    """CS override: look up order for international exchange exception.
    Identical to cs-verify-exchange but skips the international/military block
    and the date eligibility window (already skipped in cs-verify-exchange).
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number = data.get("orderNumber", "").strip().lstrip("#")
    if not order_number:
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])
        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]
        ship_to = order.get("shipTo", {})

        # NOTE: International block intentionally omitted — this is the CS international exception flow

        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Determine next exchange order suffix
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
        except Exception as ex_check_err:
            print(f"[cs-verify-intl-exchange] suffix-check failed (non-fatal): {ex_check_err}")

        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-verify-intl-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cs-intl-exchange-submit", methods=["POST", "OPTIONS"])
def cs_intl_exchange_submit():
    """CS-initiated international exchange exception.
    Creates Airtable record + Stripe checkout session, returns a checkout URL
    CS can email to the customer. The existing /api/international-success handler
    takes over once the customer pays ($10 fee).
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"error": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    if not STRIPE_SECRET_KEY:
        return Response(json.dumps({"error": "Stripe not configured"}),
                        status=500, headers=c, mimetype="application/json")

    import uuid
    ref_id = str(uuid.uuid4())

    _intl_pending[ref_id] = {
        "orderId":         data.get("orderId"),
        "orderNumber":     data.get("orderNumber", ""),
        "customerName":    data.get("customerName", ""),
        "customerEmail":   data.get("customerEmail", ""),
        "items":           data.get("items", []),
        "deliveryAddress": data.get("deliveryAddress", {}),
        "nextSuffix":      data.get("nextSuffix", "-E"),
        "trackingNumber":  data.get("trackingNumber", ""),
        "carrier":         data.get("carrier", ""),
    }

    # Clean up any abandoned pending records for this order
    try:
        order_num_int = int(data.get("orderNumber", ""))
        read_token  = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f"AND({{Order #}}={order_num_int}, NOT({{Payment Confirmed}}))",
                "fields[]": ["Order #", "Payment Confirmed"],
                "maxRecords": 10,
            },
            headers=at_headers(read_token),
            timeout=10,
        )
        if search_resp.status_code == 200:
            stale = search_resp.json().get("records", [])
            print(f"[cs-intl-exchange-submit] Found {len(stale)} stale record(s) for order {order_num_int}")
            for rec in stale:
                del_resp = req_lib.delete(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{rec['id']}",
                    headers=at_headers(write_token),
                    timeout=10,
                )
                print(f"[cs-intl-exchange-submit] Delete stale {rec['id']}: {del_resp.status_code}")
        else:
            print(f"[cs-intl-exchange-submit] Stale record search failed: {search_resp.status_code}")
    except Exception as cleanup_err:
        print(f"[cs-intl-exchange-submit] cleanup error (non-fatal): {cleanup_err}")

    # Write to Airtable immediately so success handler survives redeploys
    try:
        tracking_str = (f"{data.get('trackingNumber', '')} ({data.get('carrier', '')})"
                        if data.get("carrier") else data.get("trackingNumber", ""))
        items = data.get("items", [])
        delivery_addr = data.get("deliveryAddress", {})
        items_to_exchange = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('originalSku', '')} — {i.get('originalName', i.get('selectedName', ''))}"
            for i in items
        )
        desired_items = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('selectedSku', '')} — {i.get('selectedName', '')}"
            for i in items
        )
        delivery_str = (
            f"{delivery_addr.get('name', '')}\n"
            f"{delivery_addr.get('street1', '')}"
            + (f"\n{delivery_addr['street2']}" if delivery_addr.get("street2") else "")
            + f"\n{delivery_addr.get('city', '')}, {delivery_addr.get('state', '')} {delivery_addr.get('postalCode', '')}\n"
            f"{delivery_addr.get('country', '')}"
        )
        at_fields = {
            "Customer Name":     data.get("customerName", ""),
            "Customer Email":    data.get("customerEmail", ""),
            "Items to Exchange": items_to_exchange,
            "Desired Items":     desired_items,
            "Delivery Address":  delivery_str,
            "Return Tracking #": tracking_str,
            "Stripe Payment ID": ref_id,
            "Original Order ID": str(data.get("orderId", "")) if data.get("orderId") else "",
            "Next Suffix":       data.get("nextSuffix", "-E"),
            "Payment Confirmed": False,
        }
        try:
            at_fields["Order #"] = int(data.get("orderNumber", ""))
        except (ValueError, TypeError):
            pass
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        at_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": at_fields},
            timeout=15,
        )
        if at_resp.status_code in (200, 201):
            _intl_pending[ref_id]["airtableRecordId"] = at_resp.json().get("id", "")
            print(f"[cs-intl-exchange-submit] Airtable record created: {_intl_pending[ref_id]['airtableRecordId']}")
        else:
            print(f"[cs-intl-exchange-submit] Airtable write failed: {at_resp.status_code} {at_resp.text}")
    except Exception as at_err:
        print(f"[cs-intl-exchange-submit] Airtable write error: {at_err}")

    success_url = f"https://exchange.bluealphabelts.com/exchange/international?success=1&ref={ref_id}"
    cancel_url  = "https://exchange.bluealphabelts.com/exchange/international?cancelled=1"

    try:
        stripe_resp = req_lib.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data={
                "line_items[0][price_data][currency]":           "usd",
                "line_items[0][price_data][product_data][name]": "International Belt Exchange Fee",
                "line_items[0][price_data][unit_amount]":        "1000",
                "line_items[0][quantity]":                       "1",
                "mode":                                          "payment",
                "success_url":                                   success_url,
                "cancel_url":                                    cancel_url,
                "client_reference_id":                           ref_id,
            },
            timeout=15,
        )
        if stripe_resp.status_code not in (200, 201):
            raise Exception(f"Stripe error {stripe_resp.status_code}: {stripe_resp.text}")

        session = stripe_resp.json()
        checkout_url = session.get("url")
        if not checkout_url:
            raise Exception("No checkout URL returned from Stripe")

        print(f"[cs-intl-exchange-submit] Stripe session created for order {data.get('orderNumber')}, ref={ref_id}")

        # Auto-email the payment link to the customer
        customer_email = data.get("customerEmail", "")
        customer_name  = data.get("customerName", "")
        order_number   = data.get("orderNumber", "")
        email_sent = False
        if SENDGRID_API_KEY and customer_email:
            try:
                first_name = customer_name.split()[0] if customer_name else "there"
                email_body = (
                    f"Hi {first_name},\n\n"
                    f"Our customer service team has initiated a size exchange for your order #{order_number}.\n\n"
                    f"To complete your exchange, please pay the $10 international shipping fee using the link below:\n\n"
                    f"{checkout_url}\n\n"
                    f"This link expires in 24 hours. Once payment is received, we'll begin preparing your new belt(s) "
                    f"and ship it once we see movement on your return shipment.\n\n"
                    f"Questions? Reply to this email.\n\n"
                    f"— Blue Alpha"
                )
                send_resp = req_lib.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                        "from":    {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                        "reply_to": {"email": SENDGRID_FROM_EMAIL},
                        "subject": f"Your Blue Alpha Size Exchange Payment — Order #{order_number}",
                        "content": [{"type": "text/plain", "value": email_body}],
                    },
                    timeout=15,
                )
                email_sent = send_resp.status_code in (200, 202)
                print(f"[cs-intl-exchange-submit] Email {'sent' if email_sent else 'failed'}: {send_resp.status_code}")
            except Exception as email_err:
                print(f"[cs-intl-exchange-submit] Email error: {email_err}")

        return Response(json.dumps({"checkoutUrl": checkout_url, "emailSent": email_sent}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[cs-intl-exchange-submit] ERROR: {e}\n{traceback.format_exc()}")
        _intl_pending.pop(ref_id, None)
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-lost-refund", methods=["POST", "OPTIONS"])
def submit_lost_refund():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    order_number  = data.get("orderNumber", "").strip()
    items         = data.get("items", [])
    customer_name = data.get("customerName", "").strip()

    if not order_number or not items:
        return Response(json.dumps({"status": "error", "message": "Missing required fields"}), headers=c, mimetype="application/json")

    items_text = "\n".join(
        f"{i.get('quantity', 1)}x {i.get('sku', '')} — {i.get('name', '')}" for i in items
    )
    wc_link = f"https://www.bluealphabelts.com/wp-admin/post.php?post={order_number}&action=edit"

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Items to Return":                items_text,
        "Reason for Return":              "[LOST ORDER] Refund requested",
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "Refund Lost Order",
        "Type":                           "Lost",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"status": "error", "message": r.text}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"status": "ok"}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"status": "error", "message": str(e)}), status=500, headers=c, mimetype="application/json")

@app.route("/api/submit-shipping-refund", methods=["POST", "OPTIONS"])
def submit_shipping_refund():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                        status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    order_number  = data.get("orderNumber", "").strip()
    customer_name = data.get("customerName", "").strip()
    reason        = data.get("reason", "").strip()
    cs_notes      = data.get("csNotes", "").strip()

    if not order_number or not reason:
        return Response(json.dumps({"success": False, "error": "Missing required fields"}),
                        status=400, headers=c, mimetype="application/json")

    reason_str = f"[SHIPPING REFUND] {reason}" + (f" — {cs_notes}" if cs_notes else "")
    wc_link    = f"https://www.bluealphabelts.com/wp-admin/post.php?post={order_number}&action=edit"

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Reason for Return":              reason_str,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "UPS Shipping Needs Refund",
        "Type":                           "UPS Shipping Refund",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"success": False, "error": r.text}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-reshipment", methods=["POST", "OPTIONS"])
def submit_reshipment():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("password", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"status": "unauthorized"}), status=401, headers=c, mimetype="application/json")

    original_order_number = data.get("orderNumber", "").strip()
    items = data.get("items", [])  # [{"sku": ..., "name": ..., "quantity": ...}]
    ship_to = data.get("shipTo", {})  # edited address from CS

    if not original_order_number or not items or not ship_to:
        return Response(json.dumps({"status": "error", "message": "Missing required fields"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime as _dt
        now_str = _dt.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")

        # Look up original order to get customer email
        customer_email = ""
        try:
            orig_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                 params={"orderNumber": original_order_number},
                                 headers=ss_headers(), timeout=10)
            orig_data = orig_r.json() if orig_r.content else {}
            orig_orders = orig_data.get("orders", [])
            if orig_orders:
                customer_email = orig_orders[0].get("customerEmail", "") or ""
        except Exception:
            pass  # Proceed without email if lookup fails

        # Determine reshipment order number (-L, -L2, -L3...)
        reship_number = None
        for suffix in ["-L"] + [f"-L{i}" for i in range(2, 20)]:
            candidate = original_order_number + suffix
            r = req_lib.get("https://ssapi.shipstation.com/orders",
                           params={"orderNumber": candidate},
                           headers=ss_headers(), timeout=10)
            try:
                r_data = r.json()
            except Exception:
                r_data = {}
            if not r_data.get("orders"):
                reship_number = candidate
                break

        if not reship_number:
            return Response(json.dumps({"status": "error", "message": "Could not determine reshipment order number"}), headers=c, mimetype="application/json")

        # Look up "Lost Item" store ID
        lost_store_id = None
        try:
            stores_r = req_lib.get("https://ssapi.shipstation.com/stores",
                                   headers=ss_headers(), timeout=10)
            stores_data = stores_r.json() if stores_r.content else []
            for store in (stores_data if isinstance(stores_data, list) else []):
                if "lost" in store.get("storeName", "").lower():
                    lost_store_id = store.get("storeId")
                    break
        except Exception:
            pass  # Proceed without store if lookup fails

        # Build ShipStation order payload
        order_payload = {
            "orderNumber": reship_number,
            "orderDate": now_str,
            "paymentDate": now_str,
            "orderStatus": "awaiting_shipment",
            "amountPaid": 0,
            "taxAmount": 0,
            "shippingAmount": 0,
            "internalNotes": f"Reshipment of order {original_order_number}",
            "customerNotes": "",
            "customerEmail": customer_email,
            "shipTo": {
                "name": ship_to.get("name", ""),
                "street1": ship_to.get("street1", ""),
                "street2": ship_to.get("street2", ""),
                "city": ship_to.get("city", ""),
                "state": ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country": ship_to.get("country", "US"),
                "phone": ship_to.get("phone", ""),
                "residential": True,
            },
            "billTo": {
                "name": ship_to.get("name", ""),
                "street1": ship_to.get("street1", ""),
                "city": ship_to.get("city", ""),
                "state": ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country": ship_to.get("country", "US"),
            },
            "items": [
                {
                    "sku": item["sku"],
                    "name": item["name"],
                    "quantity": item["quantity"],
                    "unitPrice": 0,
                }
                for item in items
            ],
            "carrierCode": "stamps_com",
            "serviceCode": "usps_ground_advantage",
            "packageCode": "package",
            "confirmation": "delivery",
            "weight": {"value": 8, "units": "ounces"},
            "dimensions": {"units": "inches", "length": 8, "width": 8, "height": 2},
            "advancedOptions": {"storeId": lost_store_id} if lost_store_id else {},
        }

        create_r = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=15,
        )
        try:
            result = create_r.json()
        except Exception:
            return Response(json.dumps({"status": "error", "message": f"ShipStation returned unexpected response (HTTP {create_r.status_code}): {create_r.text[:300]}"}), headers=c, mimetype="application/json")
        new_order_id = result.get("orderId")
        if not new_order_id:
            return Response(json.dumps({"status": "error", "message": f"ShipStation error: {result}"}), headers=c, mimetype="application/json")

        return Response(json.dumps({
            "status": "ok",
            "reshipOrderNumber": reship_number,
            "reshipOrderId": new_order_id,
            "ssUrl": f"https://ship11.shipstation.com/orders/all-orders-search-result?quickSearch={reship_number}",
        }), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"status": "error", "message": str(e)}), headers=c, mimetype="application/json")


@app.route("/api/submit-cancellation", methods=["POST", "OPTIONS"])
def submit_cancellation():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # Validate CS password
    if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
        return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                        status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    order_number  = data.get("orderNumber", "")
    order_id      = data.get("orderId")
    customer_name = data.get("customerName", "")
    items         = data.get("items", [])   # [{sku, name, quantity}]
    reason        = data.get("reason", "")
    cs_notes      = data.get("csNotes", "").strip()

    # Block cancellation only if order has shipped OR has a tracking number.
    # If the order is already cancelled in ShipStation, always allow through.
    try:
        ss_r = req_lib.get(
            f"https://ssapi.shipstation.com/orders/{order_id}",
            headers=ss_headers(), timeout=10,
        )
        ss_status = ""
        if ss_r.status_code == 200:
            ss_status = ss_r.json().get("orderStatus", "")

        if ss_status == "cancelled":
            pass  # Always allow — already cancelled in ShipStation, just need Airtable record
        elif ss_status == "shipped":
            return Response(
                json.dumps({
                    "success": False,
                    "error": "Order cannot be cancelled — it has already shipped.",
                }),
                status=400, headers=c, mimetype="application/json",
            )
        else:
            # Check for tracking numbers on any non-voided shipment
            ship_r = req_lib.get(
                "https://ssapi.shipstation.com/shipments",
                params={"orderId": order_id},
                headers=ss_headers(), timeout=10,
            )
            if ship_r.status_code == 200:
                for s in ship_r.json().get("shipments", []):
                    tracking = (s.get("trackingNumber") or "").strip()
                    if tracking and not s.get("voided", False):
                        return Response(
                            json.dumps({
                                "success": False,
                                "error": "Order cannot be cancelled — a tracking number has already been assigned.",
                            }),
                            status=400, headers=c, mimetype="application/json",
                        )
    except Exception as e:
        print(f"[submit_cancellation] Status check failed: {e}")
        # Don't block if the check itself errors — proceed and let ShipStation handle it

    # Validate no items have already been cancelled via this form
    try:
        formula = f"AND({{Order Number}}='{order_number}',{{Type}}='Cancellation')"
        ac_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            params={"filterByFormula": formula, "fields[]": ["Items to Return"]},
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
            timeout=10,
        )
        already_cancelled = {}
        for rec in ac_r.json().get("records", []):
            items_text_existing = rec.get("fields", {}).get("Items to Return", "")
            for line in items_text_existing.split("\n"):
                m = re.match(r'(\d+)x\s+(\S+)\s+[—\-]', line.strip())
                if m:
                    already_cancelled[m.group(2).strip()] = \
                        already_cancelled.get(m.group(2).strip(), 0) + int(m.group(1))
        if already_cancelled:
            dupes = []
            for item in items:
                sku = item.get("sku", "")
                qty = int(item.get("quantity", 1))
                already = already_cancelled.get(sku, 0)
                if already >= qty:
                    dupes.append(item.get("name") or sku)
            if dupes:
                return Response(
                    json.dumps({
                        "success": False,
                        "error": f"Cancellation already submitted for: {', '.join(dupes)}.",
                    }),
                    status=400, headers=c, mimetype="application/json",
                )
    except Exception as e:
        print(f"[submit_cancellation] Duplicate check failed (non-fatal): {e}")

    items_text = "\n".join(
        f"{i.get('quantity', 1)}x {i.get('sku', '')} — {i.get('name', '')}"
        for i in items
    )

    wc_link = (f"https://www.bluealphabelts.com/wp-admin/post.php"
               f"?post={order_id}&action=edit")

    reason_str = f"[CANCELLATION] {reason}" + (f" — {cs_notes}" if cs_notes else "")

    fields = {
        "Order Number":                   order_number,
        "Customer Name from Shipstation": customer_name,
        "Items to Return":                items_text,
        "Reason for Return":              reason_str,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Status":                         "Cancellation Needs Refund",
        "Type":                           "Cancellation",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    # Create Airtable record
    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"success": False, "error": r.text}),
                            status=500, headers=c, mimetype="application/json")
        record_id = r.json().get("id", "")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    # Handle Return Items creation + ShipStation cancellation in background
    def process_cancellation(record_id, order_id, items):
        # Create Return Items with Received auto-checked (items never shipped)
        for item in items:
            try:
                qty = int(item.get("quantity", 1))
                req_lib.post(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}",
                             "Content-Type": "application/json"},
                    json={"fields": {
                        "Item Name":     item.get("name") or item.get("sku", ""),
                        "SKU":           item.get("sku", ""),
                        "Qty Submitted": qty,
                        "Qty Received":  qty,
                        "Received":      True,
                        "Return":        [record_id],
                    }},
                    timeout=10,
                )
            except Exception as e:
                print(f"[submit_cancellation] Return Item creation failed: {e}")

        # Cancel / update in ShipStation
        ss_success, ss_note = cancel_in_shipstation(order_id, items)
        status_notes = ss_note if not ss_success else ""

        try:
            patch_fields = {"Status Notes": (ss_note if not ss_success else "Cancelled in ShipStation")}
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}",
                         "Content-Type": "application/json"},
                json={"fields": patch_fields},
                timeout=10,
            )
        except Exception as e:
            print(f"[submit_cancellation] Airtable status update failed: {e}")

    threading.Thread(target=process_cancellation, args=(record_id, order_id, items), daemon=True).start()

    return Response(json.dumps({"success": True, "recordId": record_id}),
                    headers=c, mimetype="application/json")


LEAF_PRODUCT_TYPES = {"Base Product", "Resell", "Made-to-Order Base Product", "x-Base Product"}

def _expand_record(record_id, qty, visited, depth):
    """Recursively expand a Product SKUs record into leaf (base/resell) items.
    Returns list of (name, sku, qty)."""
    if depth > 6 or record_id in visited:
        return []
    visited.add(record_id)
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
            timeout=10,
        )
        f = r.json().get("fields", {})
        name        = f.get("Name + Variations", "")
        sku         = f.get("SKU ID", "")
        ptype       = f.get("Product Type", "")
        components  = f.get("Component(s)", [])
        # x-Base Products are leaves but multiply qty by Multi-Component Qty.
        if ptype == "x-Base Product":
            multi_qty = f.get("Multi-Component Qty.", 1) or 1
            return [(name, sku, qty * int(multi_qty))]
        if ptype in LEAF_PRODUCT_TYPES or not components:
            return [(name, sku, qty)]
        result = []
        for comp_id in components:
            result.extend(_expand_record(comp_id, qty, visited, depth + 1))
        return result if result else [(name, sku, qty)]
    except Exception as e:
        print(f"[expand_record] Error on {record_id}: {e}")
        return []

def expand_sku_to_leaf_items(sku, qty):
    """Look up a SKU in Product SKUs and recursively expand combos to base/resell items.
    Returns list of (name, sku, qty). Falls back to [(sku, sku, qty)] if not found."""
    if not AIRTABLE_OPS_TOKEN:
        return [(sku, sku, qty)]
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
            params={"filterByFormula": f"{{SKU ID}}='{sku}'", "maxRecords": 1,
                    "fields[]": ["SKU ID", "Name + Variations", "Product Type", "Component(s)"]},
            headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
            timeout=10,
        )
        records = r.json().get("records", [])
        if not records:
            return [(sku, sku, qty)]
        rec = records[0]
        f = rec.get("fields", {})
        ptype      = f.get("Product Type", "")
        components = f.get("Component(s)", [])
        name       = f.get("Name + Variations", sku)
        if ptype == "x-Base Product":
            multi_qty = f.get("Multi-Component Qty.", 1) or 1
            return [(name, sku, qty * int(multi_qty))]
        if ptype in LEAF_PRODUCT_TYPES or not components:
            return [(name, sku, qty)]
        # Combo — expand components
        result = []
        for comp_id in components:
            result.extend(_expand_record(comp_id, qty, {rec["id"]}, 1))
        return result if result else [(name, sku, qty)]
    except Exception as e:
        print(f"[expand_sku] Error expanding {sku}: {e}")
        return [(sku, sku, qty)]

def create_return_items(return_record_id, items_to_return_text):
    """Parse submitted items text, expand combos, and create Return Items records in Airtable."""
    import re as re_lib
    if not return_record_id or not items_to_return_text:
        return
    leaf_items = []
    for line in items_to_return_text.strip().split("\n"):
        m = re_lib.match(r'(\d+)x\s+(\S+)\s+[—\-]\s*(.*)', line.strip())
        if m:
            qty  = int(m.group(1))
            sku  = m.group(2).strip()
            name = m.group(3).strip()
            expanded = expand_sku_to_leaf_items(sku, qty)
            # If expansion found nothing useful, fall back to the original item
            leaf_items.extend(expanded if expanded else [(name, sku, qty)])
        else:
            print(f"[create_return_items] Could not parse line: {line!r}")

    # Deduplicate by SKU — combine quantities if same SKU appears via multiple combo paths
    seen = {}
    for (item_name, item_sku, item_qty) in leaf_items:
        if item_sku in seen:
            seen[item_sku] = (item_name, item_sku, seen[item_sku][2] + item_qty)
        else:
            seen[item_sku] = (item_name, item_sku, item_qty)

    for (item_name, item_sku, item_qty) in seen.values():
        try:
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {
                    "Item Name":     item_name or item_sku,
                    "SKU":           item_sku,
                    "Qty Submitted": item_qty,
                    "Return":        [return_record_id],
                }},
                timeout=10,
            )
        except Exception as e:
            print(f"[create_return_items] Failed to create item record for {item_sku}: {e}")


def send_return_label_email(to_email, customer_name, order_number, label_pdf_b64):
    """Send return label PDF to customer via SendGrid. Returns (success, error_message)."""
    if not SENDGRID_API_KEY:
        return False, "SendGrid not configured"
    try:
        first_name = customer_name.split()[0] if customer_name else "there"
        payload = {
            "personalizations": [{"to": [{"email": to_email}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": f"Your Blue Alpha Return Label — Order #{order_number}",
            "content": [{"type": "text/plain", "value": (
                f"Hi {first_name},\n\n"
                "Your return label is attached. Print it, attach it to your package, and drop it off at any USPS location.\n\n"
                "Please note: this label will expire 30 days from today. Be sure to ship your return before then.\n\n"
                "Once we receive your return, we'll process it within 3 business days.\n\n"
                "Questions? Reply to this email and our team will help you out.\n\n"
                "— Blue Alpha"
            )}],
            "attachments": [{
                "content": label_pdf_b64,
                "type": "application/pdf",
                "filename": f"return-label-{order_number}.pdf",
                "disposition": "attachment",
            }],
        }
        r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=15,
        )
        if r.status_code == 202:
            return True, None
        else:
            return False, f"SendGrid {r.status_code}: {r.text}"
    except Exception as e:
        return False, str(e)


def create_return_label(order_id, customer_addr, customer_email="", order_number=""):
    """Create a return label in ShipStation tied to the original order (no new order created).
    Returns (tracking_number, label_pdf_b64) or raises Exception.
    label_pdf_b64 is the base64-encoded PDF from ShipStation (may be empty string if not returned)."""
    from datetime import datetime, timezone

    # Inherit carrier/service/weight from original shipment
    sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                     params={"orderId": order_id},
                     headers=ss_headers(), timeout=10)
    ships = sr.json().get("shipments", [])
    carrier = "stamps_com"
    service = "usps_priority_mail"
    weight  = {"value": 16, "units": "ounces"}
    # Prefer outbound (non-return) shipments so we don't inherit zero weights
    # from previous failed return label attempts on this order
    outbound = [s for s in ships if not s.get("isReturnLabel", False)]
    for s in (outbound or ships):
        carrier = s.get("carrierCode") or carrier
        service = s.get("serviceCode") or service
        w = s.get("weight") or {}
        if w.get("value", 0) > 0:
            weight = w
        break

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    r = req_lib.post(
        "https://ssapi.shipstation.com/shipments/createlabel",
        headers={**ss_headers(), "Content-Type": "application/json"},
        json={
            "orderId":      int(order_id) if order_id else None,
            "carrierCode":  carrier,
            "serviceCode":  service,
            "packageCode":  "package",
            "shipDate":     today,
            "weight":       weight,
            "shipFrom": {
                "name":       customer_addr.get("name", ""),
                "street1":    customer_addr.get("street1", ""),
                "street2":    customer_addr.get("street2", ""),
                "city":       customer_addr.get("city", ""),
                "state":      customer_addr.get("state", ""),
                "postalCode": customer_addr.get("postalCode", ""),
                "country":    "US",
                "phone":      customer_addr.get("phone", ""),
            },
            "shipTo": {
                "name":       "Blue Alpha",
                "company":    "Blue Alpha",
                "street1":    "35 Andrew St",
                "city":       "Newnan",
                "state":      "GA",
                "postalCode": "30263",
                "country":    "US",
                "phone":      "6789822442",
            },
            "isReturnLabel": True,
            "testLabel":     False,
        },
        timeout=20,
    )
    result = r.json()
    if r.status_code not in (200, 201):
        raise Exception(f"ShipStation {r.status_code}: {result}")

    tracking   = result.get("trackingNumber", "")
    label_pdf  = result.get("labelData", "")   # base64-encoded PDF
    return tracking, label_pdf


@app.route("/api/submit-return", methods=["POST", "OPTIONS"])
def submit_return():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    # If this is a CS exception submission, validate the CS password
    if data.get("csException"):
        if not CS_ADMIN_PASSWORD or data.get("csPassword", "") != CS_ADMIN_PASSWORD:
            return Response(json.dumps({"success": False, "error": "Unauthorized"}),
                            status=401, headers=c, mimetype="application/json")

    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    from datetime import datetime, timezone

    addr = data.get("address", {})
    address_str = ", ".join(filter(None, [
        addr.get("name"), addr.get("street1"), addr.get("street2"),
        addr.get("city"), addr.get("state"), addr.get("postalCode")
    ]))
    wc_link = (f"https://www.bluealphabelts.com/wp-admin/post.php"
               f"?post={data.get('orderId', '')}&action=edit")

    # ── Create Airtable record immediately (Status: "New") ───────────────
    # Build reason string — CS exceptions get a visible prefix
    reason_for_return = data.get("reasonForReturn", "")
    if data.get("csException"):
        cs_notes = data.get("csNotes", "").strip()
        reason_for_return = "[CS Exception] " + reason_for_return + (f" — Notes: {cs_notes}" if cs_notes else "")

    fields = {
        "Order Number":                   data.get("orderNumber", ""),
        "Customer Name from Shipstation": data.get("customerName", ""),
        "Email Address":                  data.get("email", ""),
        "Phone Number":                   data.get("phone", ""),
        "Confirmed Shipping Address":     address_str,
        "Items to Return":                data.get("itemsToReturn", ""),
        "Reason for Return":              reason_for_return,
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Ship Date from Shipstation":     data.get("shipDate", "")[:10] if data.get("shipDate") else "",
        "Eligible Until":                 data.get("eligibleUntil", "")[:10] if data.get("eligibleUntil") else "",
        "Status":                         "New",
        "Type":                           "Return",
        "WooCommerce Order Link":         wc_link,
    }
    fields = {k: v for k, v in fields.items() if v}

    import re as _re_ret
    read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    # ── Server-side: filter out items already covered by an active return ─
    # Mirrors the alreadyReturnedQtys check in verify-order so the server
    # enforces the same rule even if the client sends stale/manipulated data.
    # CS exceptions bypass this check (CS may legitimately override).
    if data.get("orderNumber") and not data.get("csException") and RETURNS_TABLE_ID and read_token:
        try:
            _ar_formula = (f"AND({{Order Number}}='{data.get('orderNumber','')}'"
                           f",OR({{Status}}='New',{{Status}}='Label Sent',"
                           f"{{Status}}='Items Received',{{Status}}='Partial Received',"
                           f"{{Status}}='Refunded'),{{Type}}='Return')")
            _ar_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": _ar_formula, "fields[]": ["Items to Return"], "maxRecords": 20},
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            _already = {}
            for _rec in _ar_resp.json().get("records", []):
                for _line in _rec.get("fields", {}).get("Items to Return", "").split("\n"):
                    _m = _re_ret.match(r'(\d+)x\s+(\S+)\s+[—\-]', _line.strip())
                    if _m:
                        _sku = _m.group(2).strip()
                        _already[_sku] = _already.get(_sku, 0) + int(_m.group(1))
            if _already:
                _filtered = []
                for _line in data.get("itemsToReturn", "").strip().split("\n"):
                    _m = _re_ret.match(r'(\d+)x\s+(\S+)\s+[—\-](.*)', _line.strip())
                    if _m:
                        _qty  = int(_m.group(1))
                        _sku  = _m.group(2).strip()
                        _rest = _m.group(3)
                        _remaining = _qty - _already.get(_sku, 0)
                        if _remaining > 0:
                            _filtered.append(f"{_remaining}x {_sku} —{_rest}")
                    else:
                        if _line.strip():
                            _filtered.append(_line)
                if not _filtered:
                    return Response(json.dumps({"success": False,
                        "error": "A return has already been submitted for these items."}),
                        headers=c, mimetype="application/json")
                data = dict(data)
                data["itemsToReturn"] = "\n".join(_filtered)
                fields["Items to Return"] = data["itemsToReturn"]
        except Exception as _ar_err:
            print(f"[submit-return] Could not check already-returned items: {_ar_err}")

    # ── Reuse existing "Needs Review" or in-flight "New" record ──────────
    # Prevents duplicate records when label generation fails and the customer
    # retries, or when two requests race in nearly simultaneously.
    existing_nr_ids = []
    record_id = None
    if data.get("orderNumber"):
        try:
            nr_formula = (f"AND({{Order Number}}='{data.get('orderNumber','')}'"
                          f",OR({{Status}}='Needs Review',{{Status}}='New'),{{Type}}='Return')")
            nr_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                params={"filterByFormula": nr_formula, "fields[]": ["Status"], "maxRecords": 10},
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            existing_nr_ids = [r["id"] for r in nr_resp.json().get("records", [])]
        except Exception as nr_err:
            print(f"[submit-return] Could not check existing Needs Review records: {nr_err}")

    try:
        if existing_nr_ids:
            # Reuse the first existing record; delete any extras
            record_id = existing_nr_ids[0]
            patch_fields = {**fields, "Status": "New", "Status Notes": ""}
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": patch_fields},
                timeout=10,
            )
            for stale_id in existing_nr_ids[1:]:
                try:
                    req_lib.delete(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{stale_id}",
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                except Exception:
                    pass
        else:
            r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=10,
            )
            if r.status_code not in (200, 201):
                return Response(json.dumps({"success": False, "error": r.text}),
                                status=500, headers=c, mimetype="application/json")
            record_id = r.json().get("id", "")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")

    # ── Kick off label generation + email in background ──────────────────
    def process_label(record_id, data, addr):
        try:
            # ── Create Return Items (combo-expanded checklist for CS) ─────────
            # Use a per-record lock to prevent the race condition where two retry
            # threads both pass the _has_items check before either creates records.
            with _get_return_items_lock(record_id):
                _has_items = False
                try:
                    _ri_check = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                        params={"fields[]": ["Return Items"]},
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                    _has_items = len(_ri_check.json().get("fields", {}).get("Return Items", [])) > 0
                except Exception:
                    pass
                if not _has_items:
                    create_return_items(record_id, data.get("itemsToReturn", ""))

            addr_for_label = {
                "name":       addr.get("name", data.get("customerName", "")),
                "street1":    addr.get("street1", ""),
                "street2":    addr.get("street2", ""),
                "city":       addr.get("city", ""),
                "state":      addr.get("state", ""),
                "postalCode": addr.get("postalCode", ""),
                "phone":      data.get("phone", ""),
            }
            tracking_number, label_pdf_b64 = create_return_label(
                data.get("orderId"),
                addr_for_label,
                customer_email=data.get("email", ""),
                order_number=data.get("orderNumber", ""),
            )
            if not label_pdf_b64:
                raise Exception("Label generated but no PDF data returned by ShipStation")

            # Store tracking + PDF, build download URL
            label_download_url = f"{FLASK_BASE_URL}/api/return-label/{record_id}"
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {
                    "Return Tracking #": tracking_number,
                    "Label PDF Data":    label_pdf_b64,
                    "Return Label URL":  label_download_url,
                }},
                timeout=10,
            )

            # Send email
            customer_email = data.get("email", "")
            status_update = {"Status": "Needs Review", "Status Notes": "No customer email on file"}
            if customer_email:
                email_sent, email_error = send_return_label_email(
                    to_email=customer_email,
                    customer_name=data.get("customerName", ""),
                    order_number=data.get("orderNumber", ""),
                    label_pdf_b64=label_pdf_b64,
                )
                if email_sent:
                    status_update = {"Status": "Label Sent"}
                else:
                    status_update = {"Status": "Needs Review",
                                     "Status Notes": f"Label generated but email failed: {email_error}"}
                    print(f"[process_label] Email failed: {email_error}")

            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": status_update},
                timeout=10,
            )
            # Cache result so the poll endpoint can read it without needing Airtable read scope
            _return_status_cache[record_id] = status_update.get("Status", "Needs Review")

            # ── On success, delete any other stale "Needs Review" records for this order ──
            if status_update.get("Status") == "Label Sent":
                try:
                    order_num = data.get("orderNumber", "")
                    stale_formula = (f"AND({{Order Number}}='{order_num}'"
                                     f",{{Status}}='Needs Review',{{Type}}='Return')")
                    stale_resp = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                        params={"filterByFormula": stale_formula, "fields[]": ["Status"], "maxRecords": 10},
                        headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                        timeout=10,
                    )
                    for stale in stale_resp.json().get("records", []):
                        if stale["id"] != record_id:
                            req_lib.delete(
                                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{stale['id']}",
                                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
                                timeout=10,
                            )
                except Exception as cleanup_err:
                    print(f"[process_label] Stale record cleanup failed: {cleanup_err}")
        except Exception as e:
            print(f"[process_label] Failed for record {record_id}: {e}")
            _return_status_cache[record_id] = "Needs Review"
            try:
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                    headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                    json={"fields": {"Status": "Needs Review", "Status Notes": f"Label generation failed: {e}"}},
                    timeout=10,
                )
            except Exception:
                pass

    threading.Thread(target=process_label, args=(record_id, data, addr), daemon=True).start()

    # ── Respond immediately — label + email handled in background ─────────
    return Response(json.dumps({"success": True, "recordId": record_id}), headers=c, mimetype="application/json")


@app.route("/api/return-status/<record_id>")
def return_status(record_id):
    """Poll for the current status of a return record (reads from in-memory cache set by background thread)."""
    status = _return_status_cache.get(record_id)
    if status:
        return Response(json.dumps({"status": status}), headers=cors(), mimetype="application/json")
    # Not in cache yet — background thread still processing
    return Response(json.dumps({"status": "New"}), headers=cors(), mimetype="application/json")


def _create_inventory_adjustment_for_return(sku_text, qty, read_token, write_token):
    """Look up Product SKU record by SKU ID and create an Inventory Adjustment for a return receipt.
    Returns (success: bool, message: str).
    NOTE: 'Return Received' must exist as an option in the Adjustment Reason field in Airtable."""
    try:
        # Look up the Product SKU record ID and current Finished Inventory
        sku_recs = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
            params={"filterByFormula": f'{{SKU ID}}="{sku_text}"', "maxRecords": 1,
                    "fields[]": ["SKU ID", "Name + Variations", "Finished Inventory"]},
            headers=at_headers(read_token),
            timeout=10,
        ).json().get("records", [])
        if not sku_recs:
            return False, f"SKU '{sku_text}' not found in Product SKUs"
        sku_record_id    = sku_recs[0]["id"]
        sku_name         = sku_recs[0]["fields"].get("Name + Variations", sku_text)
        current_inv      = int(sku_recs[0]["fields"].get("Finished Inventory") or 0)
        new_inv          = current_inv + int(qty)

        # Create the Inventory Adjustment record (audit trail)
        adj_fields = {
            "Adjustment Reason":  "Return Received",
            "Amount":             int(qty),
            "Product SKU":        [sku_record_id],
            "Ready":              True,
            "Adjusted":           True,
        }
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INVENTORY_ADJUSTMENTS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": adj_fields},
            timeout=10,
        )
        if r.status_code not in (200, 201):
            return False, f"Airtable error {r.status_code}: {r.text[:200]}"

        # Directly increment Finished Inventory on the Product SKU record
        upd = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{sku_record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Finished Inventory": new_inv}},
            timeout=10,
        )
        if upd.status_code not in (200, 201):
            return False, f"Adjustment created but inventory update failed ({upd.status_code}): {upd.text[:200]}"

        return True, f"Adjustment created for {sku_name}: {current_inv} → {new_inv} (+{qty})"
    except Exception as e:
        return False, str(e)


@app.route("/api/mark-all-received/<record_id>")
def mark_all_received(record_id):
    """Mark all Return Items as Received (Qty Received = Qty Submitted), create inventory
    adjustments for each, and update the parent Returns status to 'Items Received'."""
    if not RETURN_ITEMS_TABLE_ID or not AIRTABLE_OPS_TOKEN:
        return Response("<h2>Not configured</h2>", status=500, mimetype="text/html")
    try:
        read_token  = AIRTABLE_OPS_TOKEN
        write_token = RETURNS_WRITE_TOKEN

        # Fetch the return record to get linked Return Items record IDs
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {read_token}"},
            timeout=10,
        )
        item_ids = ret_r.json().get("fields", {}).get("Return Items", [])
        if not item_ids:
            return Response("<h2 style='font-family:sans-serif'>No items found for this return.</h2>",
                            status=404, mimetype="text/html")

        # Fetch each Return Item
        items = []
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={"Authorization": f"Bearer {read_token}"},
                timeout=10,
            )
            items.append({"id": item_id, "fields": ir.json().get("fields", {})})

        # Mark each item received
        updated = []
        for item in items:
            item_id       = item["id"]
            fields        = item.get("fields", {})
            qty_submitted = int(fields.get("Qty Submitted", 1) or 1)
            item_name     = fields.get("Item Name", item_id)

            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {"Received": True, "Qty Received": qty_submitted}},
                timeout=10,
            )
            updated.append(item_name)

        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8">
<style>body{{font-family:sans-serif;max-width:500px;margin:60px auto;text-align:center}}
h2{{color:#2d7a2d}}ul{{text-align:left;display:inline-block}}
p{{color:#555;margin-top:20px}}</style></head><body>
<h2>✓ All Items Marked as Received</h2>
<ul>{items}</ul>
<p>You can close this tab and refresh the interface.</p>
</body></html>""".format(items="".join(f"<li>{name}</li>" for name in updated))
        return Response(html, mimetype="text/html")
    except Exception as e:
        return Response(f"<h2>Error: {e}</h2>", status=500, mimetype="text/html")


@app.route("/api/return-received-webhook/<record_id>", methods=["GET", "POST"])
def return_received_webhook(record_id):
    """Called by an Airtable automation when a Returns record status changes to
    'Items Received' or 'Partial Received'. Finds all Return Items where
    Received=True and Qty Received > 0, then creates Inventory Adjustment records."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch the Return record to get linked Return Items
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token),
            timeout=10,
        )
        if ret_r.status_code != 200:
            return Response(json.dumps({"ok": False, "error": "Return record not found"}),
                            status=404, mimetype="application/json")

        ret_fields = ret_r.json().get("fields", {})
        item_ids   = ret_fields.get("Return Items", [])
        status     = ret_fields.get("Status", "")

        if status not in ("Items Received", "Partial Received"):
            return Response(json.dumps({"ok": False,
                "error": f"Status '{status}' does not require inventory adjustment"}),
                mimetype="application/json")

        if not item_ids:
            return Response(json.dumps({"ok": True, "message": "No Return Items linked", "adjustments": []}),
                            mimetype="application/json")

        # Fetch each Return Item, process only those marked Received with a Qty
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers=at_headers(read_token),
                timeout=10,
            )
            if ir.status_code != 200:
                continue
            f        = ir.json().get("fields", {})
            received = f.get("Received", False)
            qty      = int(f.get("Qty Received") or 0)
            sku      = (f.get("SKU") or "").strip()
            name     = f.get("Item Name", sku)

            if not received or qty <= 0 or not sku:
                results.append({"item": name, "ok": False,
                    "reason": "Skipped — not received, zero qty, or no SKU"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[return-received-webhook] {name}: {msg}")

        return Response(json.dumps({"ok": True, "record_id": record_id,
                                     "status": status, "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[return-received-webhook] Error for {record_id}: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/cron-return-inventory", methods=["GET", "POST"])
def cron_return_inventory():
    """Cron endpoint — runs at 1am daily.
    Finds all Return Items where Received=True AND Inventory Added is not checked,
    creates an Inventory Adjustment for each, then marks Inventory Added=True."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch all received Return Items that haven't been added to inventory yet
        filter_formula = "AND({Received}=TRUE(), NOT({Inventory Added}=TRUE()), {Qty Received}>0)"
        all_items = []
        offset = None
        while True:
            params = {
                "filterByFormula": filter_formula,
                "fields[]": ["Item Name", "SKU", "Qty Received"],
            }
            if offset:
                params["offset"] = offset
            r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}",
                headers=at_headers(read_token),
                params=params,
                timeout=15,
            )
            data = r.json()
            all_items.extend(data.get("records", []))
            offset = data.get("offset")
            if not offset:
                break

        if not all_items:
            print("[cron-return-inventory] No pending items found.")
            return Response(json.dumps({"ok": True, "message": "No pending items", "adjustments": []}),
                            mimetype="application/json")

        for item in all_items:
            item_id = item["id"]
            f       = item.get("fields", {})
            sku     = (f.get("SKU") or "").strip()
            name    = f.get("Item Name", sku)
            qty     = int(f.get("Qty Received") or 0)

            if not sku or qty <= 0:
                results.append({"item": name, "ok": False, "reason": "No SKU or zero qty"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[cron-return-inventory] {name}: {msg}")

            if ok:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                        headers={**at_headers(write_token), "Content-Type": "application/json"},
                        json={"fields": {"Inventory Added": True}},
                        timeout=10,
                    )
                except Exception as mark_err:
                    print(f"[cron-return-inventory] Could not mark Inventory Added for {item_id}: {mark_err}")

        return Response(json.dumps({"ok": True, "processed": len(results), "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[cron-return-inventory] Error: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/refunded-webhook/<record_id>", methods=["GET", "POST"])
def refunded_webhook(record_id):
    """Called by an Airtable automation when a Returns record status changes to 'Refunded'.
    Looks up all linked Return Items and creates an Inventory Adjustment (+qty) for each
    item that has a SKU and Qty Submitted > 0.  Skips items already adjusted (Inventory Added = True)
    and marks each processed item with Inventory Added = True to prevent double-counting."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    results = []

    try:
        # Fetch the Return record
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token),
            timeout=10,
        )
        if ret_r.status_code != 200:
            return Response(json.dumps({"ok": False, "error": "Return record not found"}),
                            status=404, mimetype="application/json")

        ret_fields = ret_r.json().get("fields", {})
        item_ids   = ret_fields.get("Return Items", [])
        status     = ret_fields.get("Status", "")

        if status != "Refunded":
            return Response(json.dumps({"ok": False,
                "error": f"Status is '{status}', not 'Refunded' — skipping"}),
                mimetype="application/json")

        if not item_ids:
            return Response(json.dumps({"ok": True, "message": "No Return Items linked", "adjustments": []}),
                            mimetype="application/json")

        # Process each Return Item
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers=at_headers(read_token),
                timeout=10,
            )
            if ir.status_code != 200:
                continue
            f    = ir.json().get("fields", {})
            sku  = (f.get("SKU") or "").strip()
            name = f.get("Item Name", sku)
            qty  = int(f.get("Qty Submitted") or 0)

            # Skip if already adjusted (idempotency guard)
            if f.get("Inventory Added"):
                results.append({"item": name, "sku": sku, "ok": True,
                    "message": "Skipped — already added to inventory"})
                continue

            if not sku or qty <= 0:
                results.append({"item": name, "ok": False,
                    "reason": "Skipped — no SKU or zero qty"})
                continue

            ok, msg = _create_inventory_adjustment_for_return(sku, qty, read_token, write_token)
            results.append({"item": name, "sku": sku, "qty": qty, "ok": ok, "message": msg})
            print(f"[refunded-webhook] {name}: {msg}")

            # Mark item so we don't double-count if webhook fires again
            if ok:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                        headers={**at_headers(write_token), "Content-Type": "application/json"},
                        json={"fields": {"Inventory Added": True}},
                        timeout=10,
                    )
                except Exception as mark_err:
                    print(f"[refunded-webhook] Could not mark Inventory Added for {item_id}: {mark_err}")

        return Response(json.dumps({"ok": True, "record_id": record_id,
                                     "status": status, "adjustments": results}),
                        mimetype="application/json")

    except Exception as e:
        print(f"[refunded-webhook] Error for {record_id}: {e}")
        return Response(json.dumps({"ok": False, "error": str(e)}),
                        status=500, mimetype="application/json")


@app.route("/api/return-label/<record_id>")
def return_label_pdf(record_id):
    """Serve a return label PDF from the base64 data stored in Airtable."""
    if not RETURNS_TABLE_ID or not RETURNS_WRITE_TOKEN:
        return Response("Not configured", status=500)
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}"},
            timeout=10,
        )
        if r.status_code != 200:
            return Response("Label not found", status=404)
        pdf_b64 = r.json().get("fields", {}).get("Label PDF Data", "")
        if not pdf_b64:
            return Response("No label PDF on file — please contact support", status=404)
        pdf_bytes = base64.b64decode(pdf_b64)
        order_num = r.json().get("fields", {}).get("Order Number", record_id)
        filename = f"return-label-{order_num}.pdf"
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )
    except Exception as e:
        return Response(f"Error: {e}", status=500)


_ONTIME_CACHE = {"ts": 0, "data": None}
_ONTIME_REFRESHING = False
_ONTIME_LAST_ERROR = {"msg": None, "ts": 0}

_ONTIME_CONTRACT = 137893
_ONTIME_RULES = [
    ({49845},                                   3,  True),
    ({109623, 137358, 105813},                  5,  True),
    ({137359, 68484, 119374, 123911, 102014},  10, False),
    ({55571},                                  14, False),
    ({139291},                                 17, False),
]


def _business_days(start, end):
    from datetime import timedelta
    days = 0
    d = start
    while d < end:
        d += timedelta(days=1)
        if d.weekday() < 5:
            days += 1
    return days


def _refresh_ontime_cache():
    """Run in a background thread — fetches ShipStation data and populates _ONTIME_CACHE."""
    global _ONTIME_REFRESHING
    import time as _time
    from datetime import datetime, timedelta
    try:
        if not SHIPSTATION_KEY or not SHIPSTATION_SECRET:
            raise RuntimeError("SHIPSTATION_KEY or SHIPSTATION_SECRET not configured")
        thirty_days_ago = (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d")
        page = 1
        all_orders = []
        MAX_PAGES = 40   # 20K orders — more than enough for a statistically reliable on-time %
        while page <= MAX_PAGES:
            if page > 1:
                _time.sleep(1.5)   # stay under ShipStation's 40 req/min rate limit
            retries = 3
            r = None
            while retries > 0:
                r = req_lib.get(
                    "https://ssapi.shipstation.com/orders",
                    params={"orderStatus": "shipped", "shipDateStart": thirty_days_ago,
                            "pageSize": 500, "page": page},
                    headers=ss_headers(), timeout=30,
                )
                if r.status_code == 429:
                    retry_after = int(r.headers.get("Retry-After", 60))
                    print(f"[on-time] 429 rate limit on page {page}, sleeping {retry_after}s")
                    _time.sleep(retry_after)
                    retries -= 1
                    continue
                r.raise_for_status()
                break
            if retries == 0:
                raise RuntimeError(f"ShipStation rate limit: exhausted retries on page {page}")
            body = r.json()
            all_orders.extend(body.get("orders", []))
            total_pages = body.get("pages", 1)
            print(f"[on-time] fetched page {page}/{total_pages} ({len(all_orders)} orders so far)")
            if page >= total_pages:
                break
            page += 1

        on_time = 0
        total = 0
        for order in all_orders:
            tag_ids = set(order.get("tagIds") or [])
            if _ONTIME_CONTRACT in tag_ids:
                continue

            # Sizing exchange store: 3 business days
            store_id = (order.get("advancedOptions") or {}).get("storeId")
            if store_id == SIZING_EXCHANGE_STORE_ID:
                sla_days = 3
                use_business = True
            else:
                sla_days = None
                use_business = False
                for rule_tags, days, biz in _ONTIME_RULES:
                    if tag_ids & rule_tags:
                        if sla_days is None or days > sla_days:
                            sla_days = days
                            use_business = biz
            if sla_days is None:
                continue
            create_str = order.get("createDate") or ""
            ship_str   = order.get("shipDate") or ""
            try:
                create_d = datetime.strptime(create_str[:10], "%Y-%m-%d").date()
                ship_d   = datetime.strptime(ship_str[:10],   "%Y-%m-%d").date()
            except (ValueError, TypeError):
                continue
            age = _business_days(create_d, ship_d) if use_business else (ship_d - create_d).days
            total += 1
            if age < sla_days:
                on_time += 1

        pct = round(on_time * 100 / total) if total else 0
        _ONTIME_CACHE["data"] = {"percent": pct, "onTime": on_time, "total": total, "window": "30d"}
        _ONTIME_CACHE["ts"]   = _time.time()
        print(f"[on-time] cache refreshed — {pct}% on-time ({on_time}/{total})")
    except Exception as e:
        import time as _time
        _ONTIME_LAST_ERROR["msg"] = str(e)
        _ONTIME_LAST_ERROR["ts"]  = _time.time()
        print(f"[on-time] background refresh failed: {e}")
    finally:
        _ONTIME_REFRESHING = False


@app.route("/api/on-time-shipments")
def on_time_shipments():
    import time as _time
    import threading
    global _ONTIME_REFRESHING
    cors_headers = {"Access-Control-Allow-Origin": "*"}

    now = _time.time()
    cache_fresh = _ONTIME_CACHE["data"] is not None and now - _ONTIME_CACHE["ts"] < 90000

    if cache_fresh:
        return Response(json.dumps(_ONTIME_CACHE["data"]), headers=cors_headers,
                        mimetype="application/json")

    # If last error was recent (within 2 min), back off — don't hammer ShipStation
    error_recent = now - _ONTIME_LAST_ERROR["ts"] < 120

    # Kick off background refresh if not already running and not in backoff
    if not _ONTIME_REFRESHING and not error_recent:
        _ONTIME_REFRESHING = True
        threading.Thread(target=_refresh_ontime_cache, daemon=True).start()

    # Return stale data while refreshing
    if _ONTIME_CACHE["data"] is not None:
        return Response(json.dumps({**_ONTIME_CACHE["data"], "stale": True}),
                        headers=cors_headers, mimetype="application/json")

    # No data yet — report error or pending
    if error_recent and _ONTIME_LAST_ERROR["msg"]:
        return Response(json.dumps({"error": _ONTIME_LAST_ERROR["msg"]}),
                        headers=cors_headers, mimetype="application/json")
    return Response(json.dumps({"pending": True}),
                    headers=cors_headers, mimetype="application/json")


@app.route("/api/on-time-shipments/debug")
def on_time_debug():
    import time as _t
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    return Response(json.dumps({
        "cache_data":      _ONTIME_CACHE["data"],
        "cache_age_secs":  round(_t.time() - _ONTIME_CACHE["ts"], 1),
        "refreshing":      _ONTIME_REFRESHING,
        "last_error":      _ONTIME_LAST_ERROR["msg"],
        "error_age_secs":  round(_t.time() - _ONTIME_LAST_ERROR["ts"], 1),
        "ss_key_set":      bool(SHIPSTATION_KEY),
        "ss_secret_set":   bool(SHIPSTATION_SECRET),
    }), headers=cors_headers, mimetype="application/json")


_AWAITING_CACHE = {"ts": 0, "data": None}

@app.route("/api/awaiting")
def awaiting_shipment():
    import time as _time
    cors_headers = {"Access-Control-Allow-Origin": "*"}
    if not SHIPSTATION_KEY or not SHIPSTATION_SECRET:
        return Response(json.dumps({"error": "ShipStation not configured"}),
                        status=500, headers=cors_headers, mimetype="application/json")

    from datetime import datetime, timezone, timedelta
    creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
    ss_auth = {"Authorization": f"Basic {creds}"}

    # Today's date in Eastern time (UTC-4 DST / UTC-5 standard)
    eastern = timezone(timedelta(hours=-4))
    now_eastern = datetime.now(eastern)
    today = now_eastern.strftime("%Y-%m-%d")
    tomorrow = (now_eastern + timedelta(days=1)).strftime("%Y-%m-%d")

    # Fetch awaiting shipment count
    count = None
    try:
        r = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderStatus": "awaiting_shipment", "pageSize": 1},
            headers=ss_auth, timeout=10
        )
        count = r.json().get("total", 0)
    except Exception as e:
        print(f"[awaiting] count fetch error: {e}")

    # Fetch orders placed today
    placed_today = 0
    placed_error = None
    try:
        r2 = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderDateStart": today, "orderDateEnd": tomorrow, "pageSize": 1},
            headers=ss_auth, timeout=10
        )
        body = r2.json()
        if "total" in body:
            placed_today = body["total"]
        else:
            placed_error = body.get("message", f"HTTP {r2.status_code}: {str(body)[:200]}")
    except Exception as e:
        placed_error = str(e)
        print(f"[awaiting] placedToday fetch error: {e}")

    # If both calls failed, return stale cache rather than zeroes
    if count is None and placed_today == 0 and _AWAITING_CACHE["data"] is not None:
        return Response(json.dumps({**_AWAITING_CACHE["data"], "stale": True}),
                        headers=cors_headers, mimetype="application/json")

    result = {"count": count, "placedToday": placed_today}
    if placed_error:
        result["placedTodayError"] = placed_error

    # Cache only successful responses
    if count is not None:
        _AWAITING_CACHE["data"] = result
        _AWAITING_CACHE["ts"]   = _time.time()

    return Response(json.dumps(result), headers=cors_headers, mimetype="application/json")


@app.route("/exchange")
def exchange_portal():
    return send_from_directory("static", "exchange.html")


@app.route("/api/verify-exchange", methods=["POST", "OPTIONS"])
def verify_exchange():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number  = data.get("orderNumber", "").strip().lstrip("#")
    email_input   = data.get("email", "").strip().lower()
    last_name_input = data.get("lastName", "").strip().lower()

    if not order_number or (not email_input and not last_name_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        r = req_lib.get("https://ssapi.shipstation.com/orders",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match (same logic as returns)
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name_input and last_name_input == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Block international and military overseas orders
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "US")
        state   = order.get("shipTo", {}).get("state", "").upper()
        if country not in ("US", "USA") or state in MILITARY_STATES:
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Check ship date within 37 days
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        def parse_dt(s):
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=37)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable
        # "Can Exchange = TRUE" marks exchange *targets* (options), not the customer's item.
        # Eligibility = customer's SKU exists in Airtable with a parent that has Can Exchange options.
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Fetch all exchange options once (used to check parent eligibility per item)
        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        print(f"[verify-exchange] eligible_parent_ids ({len(eligible_parent_ids)}): {eligible_parent_ids}")
        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            print(f"[verify-exchange] checking item SKU: '{sku}' name: '{item.get('name','')}'")
            # Look up the SKU without Can Exchange filter
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            print(f"[verify-exchange] Airtable direct lookup for '{sku}': {len(records)} record(s)")
            # Fallback: if outer-only SKU (ends in -O) not found, try base SKU without -O suffix
            lookup_sku = sku
            if not records and re.search(r'-O$', sku, re.IGNORECASE):
                base_sku = re.sub(r'-O$', '', sku, flags=re.IGNORECASE)
                fallback_r = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                    params={"filterByFormula": f'{{SKU ID}}="{base_sku}"', "maxRecords": 1,
                            "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                    headers=at_headers(airtable_read_token),
                    timeout=10,
                )
                records = fallback_r.json().get("records", [])
                print(f"[verify-exchange] Fallback lookup for '{base_sku}': {len(records)} record(s)")
                if records:
                    lookup_sku = base_sku
            if not records:
                print(f"[verify-exchange] No Airtable record found for SKU '{sku}' — skipping")
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            # Only eligible if parent has exchange options available
            print(f"[verify-exchange] SKU '{sku}' → parent '{parent_product_id}' | in eligible set: {parent_product_id in eligible_parent_ids}")
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,  # keep original SKU for duplicate-exchange detection
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Check for existing exchange orders (-E, -E2, -E3, ...) and collect already-exchanged SKUs
        # Original SKU is stored in customField3 of each exchange order
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": f"{order_number}{suffix}"},
                                    headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        # Fallback: parse "Original SKUs: ..." from internalNotes (for older orders)
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[verify-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        # Filter out already-exchanged items
        eligible_items = [i for i in eligible_items if i["sku"] not in already_exchanged_skus]

        if not eligible_items:
            return Response(json.dumps({"status": "already_exchanged"}), headers=c, mimetype="application/json")

        ship_to = order.get("shipTo", {})
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[verify-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/exchange-options", methods=["POST", "OPTIONS"])
def exchange_options():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    parent_product_id = data.get("parentProductId", "").strip()

    if not parent_product_id:
        return Response(json.dumps({"options": []}), headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Name + Variations", "SKU ID", "Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        options = []
        for rec in records:
            fields = rec.get("fields", {})
            if parent_product_id not in fields.get("Parent Product", []):
                continue
            raw_name = fields.get("Name + Variations", "")
            clean_name = raw_name.replace(" - Base Only (-ONB)", "").replace(" - Base Only", "").replace(" (-ONB)", "").strip()
            options.append({
                "id":   rec["id"],
                "name": clean_name,
                "sku":  fields.get("SKU ID", ""),
            })
        options.sort(key=lambda x: x["name"])
        return Response(json.dumps({"options": options}), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/submit-exchange", methods=["POST", "OPTIONS"])
def submit_exchange():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    from datetime import datetime, timezone

    original_order_id     = data.get("originalOrderId")
    original_order_number = data.get("originalOrderNumber", "")
    customer_email        = data.get("customerEmail", "").strip()
    customer_name         = data.get("customerName", "")
    ship_to               = data.get("shipTo", {})
    notes                 = data.get("notes", "")
    next_suffix           = data.get("nextSuffix", "-E")
    items_payload         = data.get("items", [])

    if not items_payload:
        return Response(json.dumps({"success": False, "error": "No items provided"}),
                        status=400, headers=c, mimetype="application/json")

    exchange_order_number = f"{original_order_number}{next_suffix}"

    # Routing: if ANY selected belt is EDC/Low Profile → EDC shipper; otherwise battle/duty
    all_names_lower = " ".join(i.get("selectedName", "") for i in items_payload).lower()
    if any(k in all_names_lower for k in ["edc", "low profile", "inner only", "1.5"]):
        tag_id  = 105813
        user_id = "c2fc99de-a9ec-4dfb-8b74-1d263eab34b8"  # Lisa Barnes
    else:
        tag_id  = 102014
        user_id = "62230ab9-eefe-4dd9-8175-949f097fa363"  # Janna Frei

    today     = datetime.now(timezone.utc)
    today_iso = today.isoformat()

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        order_items = []

        for item_idx, item_data in enumerate(items_payload):
            original_sku = item_data.get("originalSku", "")
            selected_sku = item_data.get("selectedSku", "")
            selected_name = item_data.get("selectedName", "")
            quantity      = int(item_data.get("quantity", 1))

            order_items.append({
                "lineItemKey":    f"exchange-{item_idx + 1}",
                "name":          selected_name,
                "sku":           selected_sku,
                "quantity":      quantity,
                "unitPrice":     0.00,
                "taxAmount":     0.00,
                "shippingAmount": 0.00,
            })

            # ── LP Inner lookup ───────────────────────────────────────────────
            selected_is_onb = bool(re.search(r'-ONB$', selected_sku, re.IGNORECASE))
            inner_added = False

            # Shared color map used by all inner lookup paths
            LP_COLOR_MAP = [
                (["mc black", "mc tropic", "woodland", "black"], "Black"),
                (["coyote brown", "coyote", "mc australian", "mc arid"], "Coyote Brown"),
                (["mc classic", "multicam"],                             "Multicam"),
                (["ranger green", "ranger", "od green"],                 "OD Green"),
                (["wolf gray"],                                          "Wolf Gray"),
            ]

            def _lp_inner_item(color, size, key_suffix):
                """Look up LP INNER ONLY Belt by color+size; return order item dict or None."""
                search_str    = f"LP INNER ONLY Belt {color} {size}"
                inner_formula = (
                    f'AND(NOT(SEARCH("WPS",{{Name + Variations}})),'
                    f'SEARCH("{search_str}",{{Name + Variations}}))'
                )
                recs = at_get_all(
                    PRODUCT_SKUS_TABLE_ID, airtable_read_token,
                    fields=["Name + Variations", "SKU ID"],
                    formula=inner_formula,
                )
                if recs:
                    ir = recs[0]["fields"]
                    return {
                        "lineItemKey":    key_suffix,
                        "name":          ir.get("Name + Variations", ""),
                        "sku":           ir.get("SKU ID", ""),
                        "quantity":      quantity,
                        "unitPrice":     0.00,
                        "taxAmount":     0.00,
                        "shippingAmount": 0.00,
                    }
                return None

            def _extract_lp_color_size(name):
                """Extract LP inner color and belt size from a product name."""
                name_lower = name.lower()
                color = None
                for keywords, c in LP_COLOR_MAP:
                    for kw in keywords:
                        if kw in name_lower:
                            color = c
                            break
                    if color:
                        break
                # Find a 2-digit number in belt-size range (24–64) not adjacent to other digits
                size_matches = re.findall(r'(?<!\d)(\d{2})(?!\d)', name)
                size = next((s for s in size_matches if 24 <= int(s) <= 64), None)
                return color, size

            if selected_is_onb:
                # Path 1 — ONB selection: extract color+size from SKU/name
                try:
                    size_m     = re.search(r'-(\d+)-ONB$', selected_sku, re.IGNORECASE)
                    inner_size = size_m.group(1) if size_m else None
                    name_lower = selected_name.lower()
                    inner_color = None
                    for keywords, color in LP_COLOR_MAP:
                        for kw in keywords:
                            if kw in name_lower:
                                inner_color = color
                                break
                        if inner_color:
                            break
                    if inner_color and inner_size:
                        item = _lp_inner_item(inner_color, inner_size,
                                              f"exchange-{item_idx + 1}-inner")
                        if item:
                            order_items.append(item)
                            inner_added = True
                        else:
                            print(f"[submit-exchange] No LP inner found for {inner_color} size {inner_size}")
                except Exception as lp_err:
                    print(f"[submit-exchange] ONB LP inner lookup failed for {selected_sku}: {lp_err}")

            elif not re.search(r'(-O)$', original_sku, re.IGNORECASE):
                # Path 2 — Full-combo selected belt: find inner via Component(s) in Airtable
                try:
                    combo_sku = re.sub(r'(-ONB|-O)$', '', selected_sku, flags=re.IGNORECASE).strip()
                    combo_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{combo_sku}"', "maxRecords": 1,
                                "fields[]": ["Component(s)"]},
                        headers=at_headers(airtable_read_token), timeout=10
                    ).json().get("records", [])
                    component_ids = combo_recs[0]["fields"].get("Component(s)", []) if combo_recs else []
                    for comp_id in component_ids:
                        comp_rec    = req_lib.get(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{comp_id}",
                            headers=at_headers(airtable_read_token), timeout=10
                        ).json()
                        comp_fields = comp_rec.get("fields", {})
                        comp_name   = comp_fields.get("Name + Variations", "")
                        comp_sku    = comp_fields.get("SKU ID", "")
                        if "inner" not in comp_name.lower():
                            continue
                        order_items.append({
                            "lineItemKey":    f"exchange-{item_idx + 1}-inner",
                            "name":          comp_name,
                            "sku":           comp_sku,
                            "quantity":      quantity,
                            "unitPrice":     0.00,
                            "taxAmount":     0.00,
                            "shippingAmount": 0.00,
                        })
                        inner_added = True
                        break
                except Exception as lp_err:
                    print(f"[submit-exchange] Component LP inner lookup failed for {selected_sku}: {lp_err}")

            # Path 3 — Parent-based LP inner lookup (for products whose exchange options are
            # outer-only base SKUs with no Component(s), e.g. 1.75" Battle Belt, 2" MOLLE Duty Belt)
            # Skip if the original order was outer-only (-O suffix) — customer only ordered the outer belt
            original_is_outer_only = bool(re.search(r'-O$', original_sku, re.IGNORECASE))
            if not inner_added and not original_is_outer_only:
                item_parent_id = item_data.get("parentProductId", "")
                if item_parent_id in _LP_INNER_REQUIRED_PARENT_IDS:
                    try:
                        inner_color, inner_size = _extract_lp_color_size(selected_name)
                        if inner_color and inner_size:
                            item = _lp_inner_item(inner_color, inner_size,
                                                  f"exchange-{item_idx + 1}-inner")
                            if item:
                                order_items.append(item)
                                inner_added = True
                            else:
                                print(f"[submit-exchange] Path3: No LP inner for {inner_color} size {inner_size} (parent {item_parent_id})")
                        else:
                            print(f"[submit-exchange] Path3: Could not extract color/size from '{selected_name}'")
                    except Exception as lp_err:
                        print(f"[submit-exchange] Path3 LP inner lookup failed for {selected_sku}: {lp_err}")

        original_skus_csv = ",".join(i.get("originalSku", "") for i in items_payload)
        selected_names    = ", ".join(i.get("selectedName", "") for i in items_payload)

        # Create ShipStation exchange order
        order_payload = {
            "orderNumber":   exchange_order_number,
            "orderDate":     today_iso,
            "paymentDate":   today_iso,
            "orderStatus":   "awaiting_shipment",
            "customerEmail": customer_email,
            "billTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    "US",
            },
            "shipTo": ship_to,
            "items": order_items,
            "amountPaid":     0.00,
            "taxAmount":      0.00,
            "shippingAmount": 0.00,
            "carrierCode":    "stamps_com",
            "serviceCode":    "usps_ground_advantage",
            "packageCode":    "package",
            "confirmation":   "delivery",
            "dimensions": {
                "units":  "inches",
                "length": 8,
                "width":  8,
                "height": 2,
            },
            "internalNotes":  f"Exchange for original order #{original_order_number}. Original SKUs: {original_skus_csv}. Include return label. Customer note: {notes}",
            "advancedOptions": {
                "storeId":      SIZING_EXCHANGE_STORE_ID,
                "customField1": f"Exchange for order #{original_order_number}",
                "customField2": notes,
                "customField3": original_skus_csv,
            },
        }

        r = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=20,
        )
        if r.status_code not in (200, 201):
            raise Exception(f"ShipStation order creation failed: {r.status_code} {r.text}")

        new_order    = r.json()
        new_order_id = new_order.get("orderId")

        # Add routing tag (EDC or Manual Label)
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": tag_id},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[submit-exchange] Routing tag failed: {tag_err}")

        # Add Expedite (2 Days) tag to all exchange orders
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": 49845},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[submit-exchange] Expedite tag failed: {tag_err}")

        # Assign user
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/assignuser",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderIds": [new_order_id], "userId": user_id},
                timeout=10,
            )
        except Exception as assign_err:
            print(f"[submit-exchange] User assign failed: {assign_err}")

        # Send confirmation email via SendGrid
        if SENDGRID_API_KEY and customer_email:
            first_name = customer_name.split()[0] if customer_name else "there"
            belt_lines = "\n".join(f"  • {i['selectedName']}" for i in items_payload)
            email_body = (
                f"Hi {first_name},\n\n"
                f"Your size exchange request has been received!\n\n"
                f"Original Order: #{original_order_number}\n"
                f"New Belt(s):\n{belt_lines}\n\n"
                f"We'll ship your new belt(s) to the address on your original order. "
                f"Your package will include a prepaid return label — please use it to send back "
                f"your original belt(s) within 30 days.\n\n"
                f"Questions? Reply to this email and our team will help you out.\n\n"
                f"— Blue Alpha"
            )
            try:
                req_lib.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                    json={
                        "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                        "from":    {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                        "subject": f"Your Blue Alpha Size Exchange — Order #{original_order_number}",
                        "content": [{"type": "text/plain", "value": email_body}],
                    },
                    timeout=15,
                )
            except Exception as email_err:
                print(f"[submit-exchange] Email failed: {email_err}")

        return Response(
            json.dumps({"success": True, "exchangeOrderNumber": exchange_order_number}),
            headers=c, mimetype="application/json",
        )

    except Exception as e:
        return Response(
            json.dumps({"success": False, "error": str(e)}),
            status=500, headers=c, mimetype="application/json",
        )


# ─────────────────────────────────────────────────────────────────────────────
# Quote Portal
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/quote-builder")
def quote_builder_page():
    resp = send_from_directory("static", "quote.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/quote")
def quote_gate_page():
    """Gate page — redirect to portal if logged in, else show apply/login page."""
    user = get_portal_user(request)
    if user:
        return redirect("/portal")
    resp = send_from_directory("static", "portal-gate.html")
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    return resp

@app.route("/view-quote/<record_id>")
def view_quote_page(record_id):
    return send_from_directory("static", "view-quote.html")


def _clean_product_name(name):
    """Strip marketing suffixes from product names."""
    for suffix in [" - Base Only (-ONB)", " - Base Only", " (-ONB)"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def send_quote_email(to_email, to_name, company, quote_number, record_id, expiry_date, quote_data=None):
    """Send quote notification email via SendGrid."""
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    quote_link = f"{QUOTE_BASE_URL}/view-quote/{record_id}"
    first_name = to_name.split()[0] if to_name else "there"
    subject = f"Your Blue Alpha Quote \u2013 {quote_number}"
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:28px 40px;text-align:left;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:15px;line-height:1.6;margin:0 0 28px;">
            Your quote from Blue Alpha is ready. Click the button below to view your items, make changes, or accept your order.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border:1px solid #dde3ea;border-radius:8px;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;width:110px;">Quote Number</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{quote_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Company</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{company}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Expires</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{expiry_date}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Terms</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">Net 30</td>
                </tr>
              </table>
            </td></tr>
          </table>
          <table width="100%" cellpadding="0" cellspacing="0">
            <tr><td align="center">
              <a href="{quote_link}" style="display:inline-block;background:#1B2438;color:#ffffff;font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:6px;letter-spacing:0.5px;">View Your Quote &rarr;</a>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:13px;margin:28px 0 0;line-height:1.6;">
            This link is unique to your quote. Bookmark it or save this email — you can return anytime to edit or accept before the expiry date.<br><br>
            Questions? Contact us at <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a> or 678-961-3304.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:20px 40px;text-align:center;">
          <p style="color:#6b7a8d;font-size:12px;margin:0;">Blue Alpha &bull; bluealphabelts.com &bull; info@bluealpha.us &bull; 678-961-3304</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        payload = {
            "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": subject,
            "content": [{"type": "text/html", "value": html_body}],
        }
        # Attach PDF if quote data available
        if quote_data:
            try:
                import base64
                pdf_bytes = _build_quote_pdf_bytes(quote_data)
                payload["attachments"] = [{
                    "content":     base64.b64encode(pdf_bytes).decode("utf-8"),
                    "type":        "application/pdf",
                    "filename":    f"{quote_number}.pdf",
                    "disposition": "attachment",
                }]
            except Exception as pdf_err:
                print(f"[send_quote_email] PDF generation failed: {pdf_err}")

        sg_r = req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json=payload,
            timeout=30,
        )
        print(f"[send_quote_email] to={actual_to} status={sg_r.status_code} body={sg_r.text[:200]}")
    except Exception as e:
        print(f"[send_quote_email] failed: {e}")


def send_quote_accepted_email(to_email, to_name, org_name, qu_number, so_number):
    """Send order confirmation email after quote acceptance."""
    if not SENDGRID_API_KEY:
        return
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    first_name = to_name.split()[0] if to_name else "there"
    subject = f"Blue Alpha Quote {qu_number} \u2013 Order Confirmed"
    html_body = f"""<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="560" cellpadding="0" cellspacing="0" style="background:#ffffff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:28px 40px;">
          <span style="font-family:Arial,Helvetica,sans-serif;font-size:22px;font-weight:800;color:#ffffff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:36px 40px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:15px;line-height:1.6;margin:0 0 20px;">
            Great news \u2014 your Blue Alpha order has been confirmed! We've created your sales order and our team will be in touch about shipping and invoicing shortly.
          </p>
          <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border:1px solid #dde3ea;border-radius:8px;margin-bottom:28px;">
            <tr><td style="padding:20px 24px;">
              <table cellpadding="0" cellspacing="0">
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;width:110px;">Organization</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{org_name}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Quote</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">{qu_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Sales Order</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;font-weight:700;">{so_number}</td>
                </tr>
                <tr>
                  <td style="padding:4px 0;color:#6b7a8d;font-size:13px;">Terms</td>
                  <td style="padding:4px 0;color:#1a2633;font-size:13px;">Net 30</td>
                </tr>
              </table>
            </td></tr>
          </table>
          <p style="color:#6b7a8d;font-size:13px;margin:0 0 0;line-height:1.6;">
            Questions? Contact us at <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a> or 678-961-3304.
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:20px 40px;text-align:center;">
          <p style="color:#6b7a8d;font-size:12px;margin:0;">Blue Alpha &bull; bluealphabelts.com &bull; info@bluealpha.us &bull; 678-961-3304</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": subject,
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=15,
        )
    except Exception as e:
        print(f"[send_quote_accepted_email] failed: {e}")


def _fetch_quote_data(record_id):
    """Shared logic: fetch full quote data dict from Airtable. Returns dict or raises."""
    from datetime import date as dt_date
    # Use broadest available read token (write token may not have read scope on all tables)
    token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    # Fetch MO record
    r = req_lib.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
        headers=at_headers(token),
        timeout=15,
    )
    if r.status_code != 200:
        return None
    mo = r.json()
    fields = mo.get("fields", {})
    order_type = fields.get("Order Type", "")
    if order_type not in ("Quote", "Sales Order"):
        return None

    order_id     = fields.get("Order ID", "")
    quote_number = fields.get("Document ID", f"QU-{order_id}")
    date_str     = fields.get("Date", "")
    expiry_str   = fields.get("Expiry Date", "")
    is_accepted  = bool(fields.get("MO Is Approved", False))
    po_number    = fields.get("Purchase Order #", "")
    notes        = fields.get("Notes from Customer", "")

    today = _today_utc()
    is_expired = False
    if expiry_str:
        try:
            exp_d = dt_date.fromisoformat(expiry_str)
            is_expired = exp_d < today
        except Exception:
            pass

    # Fetch customer
    customer_ids = fields.get("Customer", [])
    customer = {}
    if customer_ids:
        cr = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_ids[0]}",
            headers=at_headers(token),
            timeout=15,
        )
        if cr.status_code == 200:
            cf = cr.json().get("fields", {})
            customer = {
                "orgName":      cf.get("Organization Name", ""),
                "contactName":  cf.get("Main Contact Name", ""),
                "email":        cf.get("Main Contact Email", ""),
                "phone":        cf.get("Main Contact Phone #", ""),
                "address1":     cf.get("Customer Address (Line 1)", ""),
                "address2":     cf.get("Customer Address (Line 2)", ""),
                "city":         cf.get("Customer City", ""),
                "state":        cf.get("Customer State", ""),
                "zip":          cf.get("Customer Zip Code", ""),
                "billToName":   cf.get("Bill-To Contact Name", "") or cf.get("Main Contact Name", ""),
                "billToEmail":  cf.get("Bill-To Contact Email", "") or cf.get("Main Contact Email", ""),
                "billToOrg":    cf.get("Bill-To Org Name", "") or cf.get("Organization Name", ""),
                "billToAddr1":  cf.get("Bill-To Address (Line 1)", ""),
                "billToAddr2":  cf.get("Bill-To Address (Line 2)", ""),
            }

    # Fetch line items in parallel
    li_record_ids = fields.get("MO Line Items", [])
    line_items = []
    if li_record_ids:
        import concurrent.futures as _cf
        def _fetch_li_full(li_id):
            try:
                r2 = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                    headers=at_headers(token), timeout=15,
                )
                if r2.status_code == 200:
                    return li_id, r2.json()
            except Exception:
                pass
            return li_id, None
        with _cf.ThreadPoolExecutor(max_workers=20) as _ex:
            _results = list(_ex.map(_fetch_li_full, li_record_ids))
        _results_map = {li_id: data for li_id, data in _results if data}
        for li_id in li_record_ids:
            li = _results_map.get(li_id)
            if not li:
                continue
            lf = li.get("fields", {})
            sku_ids   = lf.get("Product SKU", [])
            sku_names = lf.get("Name + Variations (from Product SKU)", [])
            sku_ids_f = lf.get("SKU ID (from Product SKU)", [])
            line_items.append({
                "lineItemId":  li["id"],
                "skuRecordId": sku_ids[0] if sku_ids else "",
                "skuId":       sku_ids_f[0] if sku_ids_f else "",
                "name":        sku_names[0] if sku_names else "",
                "qty":         lf.get("Qty.", 0),
                "unitPrice":   lf.get("Confirmed Unit Price", 0),
            })

    subtotal = sum(i["qty"] * i["unitPrice"] for i in line_items)

    return {
        "recordId":    record_id,
        "orderId":     order_id,
        "quoteNumber": quote_number,
        "date":        date_str,
        "expiryDate":  expiry_str,
        "isExpired":   is_expired,
        "isAccepted":  is_accepted,
        "poNumber":    po_number,
        "notes":       notes,
        "customer":    customer,
        "lineItems":   line_items,
        "subtotal":    round(subtotal, 2),
        "shipping":    0,
        "total":       round(subtotal, 2),
    }


_CATALOG_CACHE      = {"data": None, "ts": 0}
_CATALOG_TTL        = 1800  # 30 minutes
_CATALOG_REFRESHING = False  # prevent duplicate background refreshes


def _build_catalog():
    """Fetch and assemble the quote catalog. Fetches all tables in parallel."""
    import concurrent.futures
    token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        fut_skus    = ex.submit(at_get_all, PRODUCT_SKUS_TABLE_ID, token,
                                fields=["SKU ID", "Name + Variations", "Sale Price",
                                        "Parent Product", "Color", "Size", "Category",
                                        "Feature Variation", "Add-ons"],
                                formula="AND({Sale Price},{Category}!='Contract')")
        fut_parents = ex.submit(at_get_all, PARENT_PRODUCTS_TABLE_ID,    token, fields=["Name"])
        fut_colors  = ex.submit(at_get_all, COLORS_TABLE_ID,             token, fields=["Name"])
        fut_sizes   = ex.submit(at_get_all, SIZES_TABLE_ID,              token, fields=["Name"])
        fut_fvars   = ex.submit(at_get_all, FEATURE_VARIATIONS_TABLE_ID, token, fields=["Name"])
        fut_addons  = ex.submit(at_get_all, ADDONS_TABLE_ID,             token,
                                fields=["Name", "Parent Products"])

        sku_records_all = fut_skus.result()
        parent_records  = fut_parents.result()
        color_records   = fut_colors.result()
        size_records    = fut_sizes.result()
        fvar_records    = fut_fvars.result()
        addon_records   = fut_addons.result()

    sku_records = [
        r for r in sku_records_all
        if r.get("fields", {}).get("Sale Price")
        and r.get("fields", {}).get("Category", "") != "Contract"
    ]

    parent_map = {r["id"]: r["fields"].get("Name", "") for r in parent_records}
    color_map  = {r["id"]: r["fields"].get("Name", "") for r in color_records}
    size_map   = {r["id"]: r["fields"].get("Name", "") for r in size_records}
    fvar_map   = {r["id"]: r["fields"].get("Name", "").strip() for r in fvar_records}

    # Build add-on name map and parent → addons list
    addon_name_map   = {}  # addonId → name
    addon_parent_map = {}  # parentId → [{id, name}]
    for r in addon_records:
        name = r["fields"].get("Name", "").strip()
        if not name:
            continue
        if name.lower() in _EXCLUDED_ADDONS_GLOBAL:
            continue
        addon_name_map[r["id"]] = name
        for pid in r["fields"].get("Parent Products", []):
            parent_name_for_addon = _clean_product_name(parent_map.get(pid, "")).lower()
            excluded_for_parent = _EXCLUDED_ADDONS_BY_PARENT.get(parent_name_for_addon, set())
            if name.lower() in excluded_for_parent:
                continue
            addon_parent_map.setdefault(pid, []).append({"id": r["id"], "name": name})

    # Build add-on SKU map: addonId → colorId → {recordId, price, name, sizeId}
    # Uses pre-filter SKU list so add-on SKUs are found even if their parent is excluded
    addon_sku_map = {}
    for r in sku_records_all:
        f = r["fields"]
        if not f.get("Sale Price"):
            continue
        addon_ids = f.get("Add-ons", [])
        if not addon_ids:
            continue
        color_ids = f.get("Color", [])
        color_id  = color_ids[0] if color_ids else ""
        size_ids  = f.get("Size", [])
        size_id   = size_ids[0] if size_ids else ""
        price     = f.get("Sale Price", 0)
        name      = _clean_product_name(f.get("Name + Variations", ""))
        for aid in addon_ids:
            if aid not in addon_name_map:
                continue  # skip "None" and unlisted add-ons
            addon_sku_map.setdefault(aid, {})
            if color_id not in addon_sku_map[aid]:  # keep first match per color
                addon_sku_map[aid][color_id] = {
                    "recordId": r["id"],
                    "price":    price,
                    "name":     name,
                    "sizeId":   size_id,
                }

    skus = []
    seen_parents = {}
    for r in sku_records:
        f = r["fields"]
        parent_ids = f.get("Parent Product", [])
        if not parent_ids:
            continue
        parent_id   = parent_ids[0]
        parent_name = _clean_product_name(parent_map.get(parent_id, ""))
        if not parent_name:
            continue
        if parent_name.lower() in _EXCLUDED_PARENTS:
            continue

        color_ids  = f.get("Color", [])
        size_ids   = f.get("Size", [])
        color_id   = color_ids[0] if color_ids else ""
        size_id    = size_ids[0]  if size_ids  else ""
        color_name = color_map.get(color_id, "")
        color_name = _COLOR_NAME_OVERRIDES.get(color_name.lower(), color_name)
        size_name  = size_map.get(size_id, "")
        # Treat placeholder sizes as no-size so frontend handles correctly
        if size_name.strip().lower() in ("none", "n/a", "one size"):
            size_id = ""; size_name = ""

        excluded_colors = _EXCLUDED_COLORS_BY_PARENT.get(parent_name.lower(), set())
        _cn_lower = color_name.lower()
        _globally_excluded = (
            _cn_lower in _EXCLUDED_COLORS_GLOBAL
            and parent_name.lower() not in _EXCLUDED_COLORS_GLOBAL_EXCEPT.get(_cn_lower, set())
        )
        if _cn_lower in excluded_colors or _globally_excluded:
            continue

        fvar_ids  = f.get("Feature Variation", [])
        fvar_id   = fvar_ids[0] if fvar_ids else ""
        fvar_name = fvar_map.get(fvar_id, "").strip() if fvar_id else ""
        if fvar_name.lower() in ("none", ""):
            fvar_id = ""; fvar_name = ""
        if fvar_name.lower() in _EXCLUDED_FEATURE_VARS_GLOBAL:
            continue

        sku_id = f.get("SKU ID", "")
        if "-onb" in sku_id.lower():
            continue

        addon_ids_sku  = f.get("Add-ons", [])
        addon_id_sku   = addon_ids_sku[0] if addon_ids_sku else ""
        addon_name_sku = addon_name_map.get(addon_id_sku, "") if addon_id_sku else ""
        # If add-on record exists but has no name, treat as base (no add-on)
        if addon_id_sku and not addon_name_sku.strip():
            addon_id_sku = ""; addon_name_sku = ""

        raw_name   = f.get("Name + Variations", "")
        clean_name = _clean_product_name(raw_name)
        category   = f.get("Category", "") or ""

        skus.append({
            "recordId":       r["id"],
            "sku":            f.get("SKU ID", ""),
            "name":           clean_name,
            "price":          f.get("Sale Price", 0),
            "parentId":       parent_id,
            "parentName":     parent_name,
            "colorId":        color_id,
            "colorName":      color_name,
            "sizeId":         size_id,
            "sizeName":       size_name,
            "featureVarId":   fvar_id,
            "featureVarName": fvar_name,
            "addonId":        addon_id_sku,
            "addonName":      addon_name_sku,
            "category":       category,
        })
        if parent_id not in seen_parents:
            display_name = _PARENT_NAME_OVERRIDES.get(parent_name.lower(), parent_name)
            seen_parents[parent_id] = {"name": display_name, "category": category}

    parents = sorted(
        [{"id": k, "name": v["name"], "category": v["category"],
          "addons": addon_parent_map.get(k, [])}
         for k, v in seen_parents.items()],
        key=lambda x: (-1 if x["name"].lower() in _SORT_FIRST_PARENTS else 1 if x["name"].lower() in _SORT_LAST_PARENTS else 0, x["name"]),
    )
    return {"parents": parents, "skus": skus, "addonSkus": addon_sku_map}


def _refresh_catalog_bg():
    """Build catalog in background and update cache. Clears refreshing flag when done."""
    global _CATALOG_REFRESHING
    import time as _time
    try:
        result = _build_catalog()
        _CATALOG_CACHE["data"] = result
        _CATALOG_CACHE["ts"]   = _time.time()
        print("[catalog] background refresh complete")
    except Exception as e:
        print(f"[catalog] background refresh failed: {e}")
    finally:
        _CATALOG_REFRESHING = False


# Warm cache on startup so the first visitor never waits
threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
# Parent product Airtable IDs whose exchange selections are outer-only SKUs (no components),
# but still require an LP inner belt to be added to the exchange order.
# These use the same LP INNER ONLY Belt color+size lookup as the ONB path.
_LP_INNER_REQUIRED_PARENT_IDS = {
    "recMx2geTxsMGq4H8",  # 1.75" Battle Belt - Aluminum COBRA® Buckle
    "recyoI521Kdbbkz6x",  # 1.75" Battle Belt - D-ring COBRA® Buckle
    "rec8H08vrdCtZ0yrn",  # 2" Duty Belt Lite - MOLLE
    "recobDC5byTEvR81w",  # 2" Duty Belt Lite - Standard
    "rec7KI77AOb7qMe0g",  # 2" MOLLE Duty Belt
}

_EXCLUDED_PARENTS = {
    # WPS / NeoMag products
    "wps lp inner", "wps low profile", "wps 1.75\" cobra", "wps 1.75\" d-ring",
    "belt lanyardwps", "sentry strapwps",
    "sentry strapneomag", "sentry strapneomag ",
    "sentry strap extensionneomag",
    "beltless backer pad - neomag", "beltless backer tegris - neomag",
    "tray insert - neomag",
    # Internal / components
    "dog collar", "dog leash", "gps pouch", "belt resize service",
    "service", "stock sock",
    "buckle", "shock cord", "scrim", "fife loop", "overlap inner",
    "med components", "med pouch handles", "packy sack parts",
    "combo packets", "sandwich bags",
    # Test / placeholder
    "test parent", "test3", "new parent", "second test parent",
    "see manager", "anklemdkt",
    # Excluded from portal
    "adapter",
    # Specific exclusions
    "1.75\" standard belt - both buckles",
    "misc.",
    "hat", "hoodie", "t-shirt",
    "sentry strap - ba",
    "fanny pack",
    "breaching rescue bar",
    "medical pouch - side pull outer",
    "medical pouch - top/bottom pull outer",
}

# Feature variations to exclude globally (lowercase)
_EXCLUDED_FEATURE_VARS_GLOBAL = {
    "base only (-onb)",
}

# Add-ons to exclude globally (lowercase add-on names)
_EXCLUDED_ADDONS_GLOBAL = {
    "easy tape",
    "one wrap",
}

# Add-ons to exclude per parent (lowercase parent name → set of lowercase add-on names)
_EXCLUDED_ADDONS_BY_PARENT = {
    "1.75\" battle belt lite": {"outer only"},
}

# Colors to exclude globally (lowercase color names)
_EXCLUDED_COLORS_GLOBAL = {
    "splatter",
    "mc arid",
    "mc tropic",
    "mc australian",
}

# Parents exempt from a global color exclusion (lowercase color → set of lowercase parent names)
_EXCLUDED_COLORS_GLOBAL_EXCEPT = {
    "mc arid":   {"thigh strap", "sandwich bags"},
    "mc tropic": {"thigh strap", "sandwich bags"},
}

# Colors to exclude per parent (lowercase parent name → set of lowercase color names)
_EXCLUDED_COLORS_BY_PARENT = {
    "radio pouch": {"wolf gray"},
}

# Display name overrides (post-clean lowercase → desired display name)
_PARENT_NAME_OVERRIDES = {
    "1.5\" lp inner only belt": "1.5\" Low Profile Inner Only Belt",
    "keepers": "Keepers (set of 4)",
    "1.75\" battle belt - aluminum cobra® buckle": "1.75\" MOLLE Battle Belt - Aluminum COBRA® Buckle",
    "1.75\" battle belt - d-ring cobra® buckle":   "1.75\" MOLLE Battle Belt - D-ring COBRA® Buckle",
}

# Color display name overrides (lowercase color name from Airtable → desired display name)
_COLOR_NAME_OVERRIDES = {
    "mc classic": "Multicam Classic",
    "mc black":   "Multicam Black",
}

# Parents that should sort first within their category (post-clean lowercase)
_SORT_FIRST_PARENTS = {
    "battle belt lite",
}

# Parents that should sort last within their category (post-clean lowercase)
_SORT_LAST_PARENTS = {
    "1.5\" low profile inner only belt",
}

@app.route("/api/admin/retry-label/<return_record_id>", methods=["POST"])
def admin_retry_label(return_record_id):
    """Re-trigger label generation + email for an existing return record."""
    read_token  = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    if not read_token or not write_token or not RETURNS_TABLE_ID:
        return Response(json.dumps({"ok": False, "error": "Not configured"}), status=500, mimetype="application/json")
    try:
        # Fetch the return record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{return_record_id}",
            headers={"Authorization": f"Bearer {read_token}"}, timeout=10,
        )
        fields = r.json().get("fields", {})
        if not fields:
            return Response(json.dumps({"ok": False, "error": "Record not found or no fields"}), status=404, mimetype="application/json")

        # Address stored as "Name, Street1, City, State, PostalCode" (5-part)
        addr_str = fields.get("Confirmed Shipping Address", "")
        parts = [p.strip() for p in addr_str.split(",")]
        addr = {
            "name":       parts[0] if len(parts) > 0 else "",
            "street1":    parts[1] if len(parts) > 1 else "",
            "street2":    "",
            "city":       parts[2] if len(parts) > 2 else "",
            "state":      parts[3].strip() if len(parts) > 3 else "",
            "postalCode": parts[4].strip() if len(parts) > 4 else "",
        }
        body    = request.get_json() or {}
        # orderId not stored in Airtable — caller must supply it, or we parse from WC link
        wc_link = fields.get("WooCommerce Order Link", "")
        import re as _re_rl
        wc_id_m = _re_rl.search(r'post=(\d+)', wc_link)
        order_id = body.get("orderId") or (wc_id_m.group(1) if wc_id_m else "")
        data = {
            "orderNumber":    fields.get("Order Number", ""),
            "customerName":   fields.get("Customer Name from Shipstation", ""),
            "email":          fields.get("Email Address", ""),
            "phone":          fields.get("Phone Number", ""),
            "itemsToReturn":  fields.get("Items to Return", ""),
            "reasonForReturn":fields.get("Reason for Return", ""),
            "orderId":        order_id,
        }

        # Reset status to New so background thread can update it
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{return_record_id}",
            headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
            json={"fields": {"Status": "New", "Status Notes": "Retrying label generation"}},
            timeout=10,
        )

        # Re-run label generation in background
        def _retry(rid, d, a):
            try:
                addr_for_label = {
                    "name": a.get("name", d.get("customerName", "")),
                    "street1": a.get("street1", ""), "street2": a.get("street2", ""),
                    "city": a.get("city", ""), "state": a.get("state", ""),
                    "postalCode": a.get("postalCode", ""), "phone": d.get("phone", ""),
                }
                tracking_number, label_pdf_b64 = create_return_label(
                    d.get("orderId"), addr_for_label,
                    customer_email=d.get("email", ""), order_number=d.get("orderNumber", ""),
                )
                if not label_pdf_b64:
                    raise Exception("No PDF data returned by ShipStation")
                import time as _t
                label_url = f"{FLASK_BASE_URL}/api/return-label/{rid}"
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": {"Return Tracking #": tracking_number, "Label PDF Data": label_pdf_b64, "Return Label URL": label_url}},
                    timeout=10,
                )
                status_update = {"Status": "Needs Review", "Status Notes": "No customer email on file"}
                if d.get("email"):
                    ok, err = send_return_label_email(d["email"], d.get("customerName", ""), d.get("orderNumber", ""), label_pdf_b64)
                    status_update = {"Status": "Label Sent"} if ok else {"Status": "Needs Review", "Status Notes": f"Email failed: {err}"}
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": status_update}, timeout=10,
                )
                print(f"[retry-label] {rid} → {status_update.get('Status')}")
            except Exception as ex:
                print(f"[retry-label] failed for {rid}: {ex}")
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{rid}",
                    headers={"Authorization": f"Bearer {write_token}", "Content-Type": "application/json"},
                    json={"fields": {"Status": "Needs Review", "Status Notes": f"Retry failed: {ex}"}}, timeout=10,
                )

        threading.Thread(target=_retry, args=(return_record_id, data, addr), daemon=True).start()
        return Response(json.dumps({"ok": True, "message": "Label generation re-triggered"}), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


@app.route("/api/admin/delete-return-items", methods=["POST"])
def admin_delete_return_items():
    """Delete specific Return Item records by ID list."""
    write_token = RETURNS_WRITE_TOKEN
    if not write_token or not RETURN_ITEMS_TABLE_ID:
        return Response(json.dumps({"ok": False, "error": "Not configured"}), status=500, mimetype="application/json")
    body = request.get_json() or {}
    ids = body.get("ids", [])
    if not ids:
        return Response(json.dumps({"ok": False, "error": "No ids provided"}), status=400, mimetype="application/json")
    try:
        import time as _time
        deleted, failed = [], []
        for rid in ids:
            r = req_lib.delete(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{rid}",
                headers={"Authorization": f"Bearer {write_token}"},
                timeout=10,
            )
            (deleted if r.status_code == 200 else failed).append(rid)
            _time.sleep(0.1)
        return Response(json.dumps({"ok": True, "deleted": deleted, "failed": failed}), mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


@app.route("/api/admin/refresh-catalog", methods=["POST"])
def admin_refresh_catalog():
    """Force an immediate synchronous catalog rebuild (internal use)."""
    global _CATALOG_REFRESHING
    try:
        result = _build_catalog()
        import time as _time
        _CATALOG_CACHE["data"] = result
        _CATALOG_CACHE["ts"]   = _time.time()
        _CATALOG_REFRESHING = False
        return Response(json.dumps({"ok": True, "parents": len(result["parents"]), "skus": len(result["skus"])}),
                        mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"ok": False, "error": str(e)}), status=500, mimetype="application/json")


def _catalog_response(data, extra_headers=None):
    """Return a gzip-compressed JSON response for the catalog."""
    import gzip as _gzip
    c = cors()
    body = json.dumps(data).encode("utf-8")
    accept = request.headers.get("Accept-Encoding", "")
    if "gzip" in accept:
        body = _gzip.compress(body, compresslevel=6)
        c["Content-Encoding"] = "gzip"
    if extra_headers:
        c.update(extra_headers)
    return Response(body, headers=c, mimetype="application/json")


@app.route("/api/quote-catalog", methods=["GET", "OPTIONS"])
def quote_catalog():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    global _CATALOG_REFRESHING
    import time as _time
    now = _time.time()

    # Fresh cache — serve immediately
    if _CATALOG_CACHE["data"] and (now - _CATALOG_CACHE["ts"]) < _CATALOG_TTL:
        return _catalog_response(_CATALOG_CACHE["data"])

    # Stale cache — serve instantly and kick off a background refresh
    if _CATALOG_CACHE["data"]:
        if not _CATALOG_REFRESHING:
            _CATALOG_REFRESHING = True
            threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
        return _catalog_response(_CATALOG_CACHE["data"])

    # No cache yet — kick off background build if not already running, return loading state
    if not _CATALOG_REFRESHING:
        _CATALOG_REFRESHING = True
        threading.Thread(target=_refresh_catalog_bg, daemon=True).start()
    return Response(json.dumps({"loading": True}),
                    headers={**cors()}, mimetype="application/json")


@app.route("/api/create-quote", methods=["POST", "OPTIONS"])
def create_quote():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce create_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    from datetime import date as dt_date, timedelta
    data = request.get_json() or {}
    token = RETURNS_WRITE_TOKEN

    org_name = (data.get("orgName") or "").strip()
    email    = (data.get("email") or "").strip()
    items    = data.get("items", [])

    if not org_name or not email or not items:
        return Response(json.dumps({"error": "orgName, email, and items are required"}),
                        status=400, headers=c, mimetype="application/json")

    contact_name = (data.get("contactName") or "").strip()
    phone        = (data.get("phone") or "").strip()
    address1     = (data.get("address1") or "").strip()
    address2     = (data.get("address2") or "").strip()
    city         = (data.get("city") or "").strip()
    state        = (data.get("state") or "").strip()
    zip_code     = (data.get("zip") or "").strip()
    country      = (data.get("country") or "US").strip()
    po_number    = (data.get("poNumber") or "").strip()
    notes        = (data.get("notes") or "").strip()

    provided_cust_id = (data.get("customerId") or "").strip()

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # 1. Use provided customer ID if available, otherwise find/create by email
        # Build address lines (shared by both paths)
        _line1 = address1
        if address2:
            _line1 = f"{address1}, {address2}"
        _line2 = ""
        if city and state and zip_code:
            _line2 = f"{city}, {state} {zip_code}"
        elif city or state or zip_code:
            _line2 = " ".join(filter(None, [city, state, zip_code]))

        if provided_cust_id:
            cust_id = provided_cust_id
            # Update customer record with any form changes
            _cust_update = {}
            if org_name:     _cust_update["Organization Name"]             = org_name
            if contact_name: _cust_update["Main Contact Name"]             = contact_name
            if phone:        _cust_update["Main Contact Phone #"]          = phone
            if _line1:       _cust_update["Customer Address (Line 1)"]     = _line1
            if _line2:       _cust_update["Customer Address (Line 2)"]     = _line2
            if _cust_update:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{cust_id}",
                        headers={**at_headers(token), "Content-Type": "application/json"},
                        json={"fields": _cust_update},
                        timeout=15,
                    )
                except Exception as _ue:
                    print(f"[create_quote] customer update failed: {_ue}")
        else:
          existing = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Main Contact Email", "Organization Name"],
            formula=f"{{Main Contact Email}}='{email}'",
          )
          if existing:
            cust_id = existing[0]["id"]
          else:
            new_cust = {
                "Organization Name":      org_name,
                "Main Contact Name":      contact_name,
                "Main Contact Email":     email,
            }
            if phone:    new_cust["Main Contact Phone #"]        = phone
            if _line1:   new_cust["Customer Address (Line 1)"]   = _line1
            if _line2:   new_cust["Customer Address (Line 2)"]   = _line2

            cr = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": new_cust},
                timeout=15,
            )
            cr.raise_for_status()
            cust_id = cr.json()["id"]

        # 2. Get next order ID — use read token (write token may lack read scope)
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        order_id_str = _next_order_id(read_token)
        quote_number = f"QU-{order_id_str}"

        today       = _today_utc()
        expiry_date = today + timedelta(days=90)
        today_str   = today.isoformat()
        expiry_str  = expiry_date.isoformat()

        # 3. Create Manual Order
        # Collect per-item size notes and append to order notes
        item_size_notes = []
        for it in items:
            sn = (it.get("sizeNote") or "").strip()
            if sn:
                item_name = (it.get("name") or f"Item {items.index(it)+1}").strip()
                item_size_notes.append(f"  - {item_name}: {sn}")
        if item_size_notes:
            size_note_block = "Size/fit specs:\n" + "\n".join(item_size_notes)
            notes = (notes + "\n\n" + size_note_block).strip() if notes else size_note_block

        mo_body = {
            "fields": {
                "Order Type":  "Quote",
                "Order ID":    order_id_str,
                "Date":        today_str,
                "Expiry Date": expiry_str,
                "Customer":    [cust_id],
            }
        }
        if po_number:
            mo_body["fields"]["Purchase Order #"] = po_number
        if notes:
            mo_body["fields"]["Notes from Customer"] = notes

        mo_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=mo_body,
            timeout=15,
        )
        if not mo_r.ok:
            err_detail = ""
            try: err_detail = mo_r.json()
            except Exception: err_detail = mo_r.text
            print(f"[create_quote] MO creation failed {mo_r.status_code}: {err_detail}")
            return Response(json.dumps({"error": f"Order creation failed ({mo_r.status_code}): {err_detail}"}),
                            status=500, headers=c, mimetype="application/json")
        mo_record_id = mo_r.json()["id"]

        # 4. Create line items
        for item in items:
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":         [mo_record_id],
                    "Product SKU":          [item["skuRecordId"]],
                    "Qty.":                 int(item["qty"]),
                    "Confirmed Unit Price": float(item["unitPrice"]),
                }},
                timeout=15,
            )
            if not li_r.ok:
                err_detail = ""
                try: err_detail = li_r.json()
                except Exception: err_detail = li_r.text
                print(f"[create_quote] line item creation failed {li_r.status_code}: {err_detail} | item={item}")
            li_r.raise_for_status()

        # 5. Send email with PDF attachment
        try:
            quote_data = _fetch_quote_data(mo_record_id)
            send_quote_email(email, contact_name or org_name, org_name,
                             quote_number, mo_record_id, expiry_str, quote_data=quote_data)
        except Exception as email_err:
            print(f"[create_quote] email failed: {email_err}")

        # Bust quotes cache for this customer
        _QUOTES_CACHE.pop(cust_id, None)
        _ORDERS_CACHE.pop(cust_id, None)

        return Response(
            json.dumps({"success": True, "quoteNumber": quote_number, "recordId": mo_record_id}),
            headers=c, mimetype="application/json",
        )
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/get-quote/<record_id>", methods=["GET", "OPTIONS"])
def get_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    c = cors()
    try:
        data = _fetch_quote_data(record_id)
        if not data:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        return Response(json.dumps(data), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/update-quote/<record_id>", methods=["POST", "OPTIONS"])
def update_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce create_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    write_token = RETURNS_WRITE_TOKEN
    # Use broadest available read token for verification (write token may lack read scope)
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    data = request.get_json() or {}
    items = data.get("items", [])

    try:
        # Verify it's a quote and not accepted
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})
        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Quote already accepted"}), status=400, headers=c, mimetype="application/json")

        # Delete existing line items using IDs stored on the MO record (formula
        # filter via ARRAYJOIN doesn't return record IDs, so would miss all items)
        existing_li_ids = mo_fields.get("MO Line Items", [])
        for li_id in existing_li_ids:
            req_lib.delete(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(write_token), timeout=10,
            )

        # Create new line items
        for item in items:
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order": [record_id],
                    "Product SKU":  [item["skuRecordId"]],
                    "Qty.":         int(item["qty"]),
                    "Confirmed Unit Price": float(item["unitPrice"]),
                }},
                timeout=15,
            )
            li_r.raise_for_status()

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/accept-quote/<record_id>", methods=["POST", "OPTIONS"])
def accept_quote(record_id):
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    # If called from a portal session, enforce accept_quote permission
    _pu = get_portal_user(request)
    if _pu is not None and not portal_can(_pu, "accept_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    from datetime import date as dt_date
    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or write_token
    token       = write_token  # used for all write operations below

    data = request.get_json() or {}
    billing      = data.get("billing") or {}
    shipping_obj = data.get("shipping")  # may be None (same as billing)

    try:
        # Fetch MO record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            err_body = ""
            try: err_body = r.json()
            except Exception: err_body = r.text[:200]
            return Response(json.dumps({"error": f"Quote not found (AT {r.status_code}: {err_body})"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})

        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Already accepted"}), status=400, headers=c, mimetype="application/json")

        expiry_str = mo_fields.get("Expiry Date", "")
        if expiry_str:
            try:
                if dt_date.fromisoformat(expiry_str) < _today_utc():
                    return Response(json.dumps({"error": "Quote has expired"}), status=400, headers=c, mimetype="application/json")
            except Exception:
                pass

        order_id_str = mo_fields.get("Order ID", "")
        quote_number = mo_fields.get("Document ID", f"QU-{order_id_str}")
        so_number    = f"SO-{order_id_str}"
        customer_ids = mo_fields.get("Customer", [])
        customer_id  = customer_ids[0] if customer_ids else None
        po_number    = mo_fields.get("Purchase Order #", "")
        notes        = mo_fields.get("Notes from Customer", "")
        date_str     = mo_fields.get("Date", _today_utc().isoformat())

        # Create SO record
        # Note: Document ID, MO Is Approved, Ready for ShipStation (SOs), Origin Quote are all
        # formula fields — Airtable computes them automatically. Do NOT write them.
        # Sales Order Status = "Approved" drives both MO Is Approved and Ready for ShipStation.
        so_body = {
            "fields": {
                "Order Type":         "Sales Order",
                "Order ID":           order_id_str,
                "Date":               date_str,
                "Sales Order Status": "Approved",
            }
        }
        if customer_ids:
            so_body["fields"]["Customer"] = customer_ids
        if po_number:
            so_body["fields"]["Purchase Order #"] = po_number
        if notes:
            so_body["fields"]["Notes from Customer"] = notes

        so_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=so_body,
            timeout=15,
        )
        if not so_r.ok:
            try:
                at_err = so_r.json()
            except Exception:
                at_err = so_r.text[:500]
            return Response(json.dumps({"error": f"SO create failed ({so_r.status_code}): {at_err}"}),
                            status=500, headers=c, mimetype="application/json")
        so_record_id = so_r.json()["id"]

        # Update customer billing address if provided
        if billing and customer_id:
            bill_line1 = billing.get("addr1", "")
            if billing.get("addr2"):
                bill_line1 = f"{bill_line1}, {billing['addr2']}"
            bill_line2 = ""
            city_  = billing.get("city", "")
            state_ = billing.get("state", "")
            zip_   = billing.get("zip", "")
            if city_ and state_:
                bill_line2 = f"{city_}, {state_} {zip_}".strip()
            cust_update = {}
            if billing.get("org"):   cust_update["Organization Name"] = billing["org"]
            if billing.get("name"):  cust_update["Main Contact Name"] = billing["name"]
            if bill_line1: cust_update["Customer Address (Line 1)"] = bill_line1
            if bill_line2: cust_update["Customer Address (Line 2)"] = bill_line2
            if cust_update:
                try:
                    req_lib.patch(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                        headers={**at_headers(token), "Content-Type": "application/json"},
                        json={"fields": cust_update},
                        timeout=15,
                    )
                except Exception as cust_err:
                    print(f"[accept_quote] customer update failed: {cust_err}")

        # Add ship-to to SO notes if different from billing
        if shipping_obj:
            ship_note = "Ship To: {org} {name}, {addr1}{addr2}, {city}, {state} {zip}".format(
                org   = shipping_obj.get("org", ""),
                name  = shipping_obj.get("name", ""),
                addr1 = shipping_obj.get("addr1", ""),
                addr2 = f", {shipping_obj['addr2']}" if shipping_obj.get("addr2") else "",
                city  = shipping_obj.get("city", ""),
                state = shipping_obj.get("state", ""),
                zip   = shipping_obj.get("zip", ""),
            ).strip()
            try:
                req_lib.patch(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{so_record_id}",
                    headers={**at_headers(token), "Content-Type": "application/json"},
                    json={"fields": {"Notes from Customer": ship_note}},
                    timeout=15,
                )
            except Exception:
                pass

        # Copy line items from QU to SO
        # Use the MO Line Items IDs already on the quote record (formula query doesn't work
        # because ARRAYJOIN on linked records returns display names, not record IDs)
        li_ids = mo_fields.get("MO Line Items", [])
        for li_id in li_ids:
            li_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if li_r.status_code != 200:
                print(f"[accept_quote] could not fetch line item {li_id}: {li_r.status_code}")
                continue
            lf = li_r.json().get("fields", {})
            # Adj. Unit Price is a formula — use Confirmed Unit Price if set, else Unit Price lookup
            price = lf.get("Confirmed Unit Price") or (lf.get("Unit Price") or [None])[0] or 0
            new_li = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order":         [so_record_id],
                    "Product SKU":          lf.get("Product SKU", []),
                    "Qty.":                 lf.get("Qty.", 0),
                    "Confirmed Unit Price": float(price),
                }},
                timeout=15,
            )
            if not new_li.ok:
                print(f"[accept_quote] line item copy failed {new_li.status_code}: {new_li.text[:200]}")

        # PATCH QU: set Quote Status = "Approved" (drives MO Is Approved formula)
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json={"fields": {"Quote Status": "Approved"}},
            timeout=15,
        )

        # Send confirmation email
        to_email = ""
        to_name  = ""
        org_name = ""
        if customer_ids:
            try:
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_ids[0]}",
                    headers=at_headers(token), timeout=10,
                )
                if cr.status_code == 200:
                    cf = cr.json().get("fields", {})
                    to_email = cf.get("Main Contact Email", "")
                    to_name  = cf.get("Main Contact Name", "")
                    org_name = cf.get("Organization Name", "")
            except Exception:
                pass
        if to_email:
            try:
                send_quote_accepted_email(to_email, to_name, org_name, quote_number, so_number)
            except Exception as email_err:
                print(f"[accept_quote] email failed: {email_err}")

        # Bust quotes cache
        if customer_id:
            _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)

        return Response(json.dumps({"success": True, "soNumber": so_number}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _build_quote_pdf_bytes(quote, doc_type="quote"):
    """Generate PDF bytes for a quote dict. Returns bytes."""
    import os, tempfile
    from fpdf import FPDF

    def _fmt_date(iso_str):
        """Convert YYYY-MM-DD to M-D-YYYY."""
        try:
            from datetime import date as _d
            d = _d.fromisoformat(iso_str)
            return f"{d.month}-{d.day}-{d.year}"
        except Exception:
            return iso_str

    cust       = quote.get("customer", {})
    line_items = quote.get("lineItems", [])
    subtotal   = quote.get("subtotal", 0.0)
    shipping   = quote.get("shipping", 0.0)
    total      = quote.get("total",    0.0)
    q_number   = quote.get("quoteNumber", "")
    q_date     = _fmt_date(quote.get("date", ""))
    q_expiry   = _fmt_date(quote.get("expiryDate", ""))
    q_po       = quote.get("poNumber", "")
    q_notes    = quote.get("notes", "")
    q_record   = quote.get("recordId", "")
    portal_link = (f"{QUOTE_BASE_URL}/portal?tab=our-orders" if doc_type == "order"
                   else f"{QUOTE_BASE_URL}/portal?tab=our-quotes") if q_record else ""

    bill_org   = cust.get("billToOrg")  or cust.get("orgName", "")
    bill_name  = cust.get("billToName") or cust.get("contactName", "")
    bill_addr1 = cust.get("billToAddr1", "")
    bill_addr2 = cust.get("billToAddr2", "")
    addr1      = cust.get("address1", "")
    city       = cust.get("city", "")
    state_v    = cust.get("state", "")
    zip_v      = cust.get("zip", "")

    # Subclass for small header on pages 2+
    _static_dir    = os.path.dirname(os.path.abspath(__file__))
    logo_local_ref = None
    for _c in ["ba-logo-white-bg.jpg", "ba-logo-dark.png", "ba-logo.jpg"]:
        _p = os.path.join(_static_dir, "static", _c)
        if os.path.exists(_p):
            logo_local_ref = _p
            break
    _qnum_ref = q_number

    class QuotePDF(FPDF):
        def header(self):
            if self.page_no() <= 1:
                return
            # Small logo + quote # for continuation pages
            self.set_fill_color(255, 255, 255)
            self.rect(19, 6, 28, 10, style="F")
            try:
                self.image(logo_local_ref, x=19, y=6, w=28)
            except Exception:
                self.set_xy(19, 8)
                self.set_font("Helvetica", "B", 9)
                self.set_text_color(27, 36, 56)
                self.cell(28, 5, "BLUE ALPHA", border=0)
            self.set_xy(49, 9)
            self.set_font("Helvetica", "B", 8)
            self.set_text_color(27, 36, 56)
            self.cell(0, 5, _qnum_ref, border=0)
            self.set_draw_color(27, 36, 56)
            self.set_line_width(0.4)
            self.line(19, 18, 19 + 177, 18)
            self.ln(4)

    pdf = QuotePDF(orientation="P", unit="mm", format="letter")
    pdf.set_margins(19, 19, 19)
    pdf.set_auto_page_break(auto=True, margin=25)
    pdf.add_page()

    W     = 177.0   # usable width (215.9mm - 2×19mm margins)
    NAVY  = (27,  36,  56)
    MUTED = (107, 122, 141)
    TEXT  = (26,  38,  51)
    LG    = (245, 247, 250)
    BD    = (221, 227, 234)

    # ── Logo (top-left) ───────────────────────────────────────────────
    LOGO_W   = 58.0   # mm width to render logo
    LOGO_TOP = 13.0   # mm from top of page
    # Prefer transparent PNG; fall back to JPG, then URL download, then text
    _static    = os.path.dirname(os.path.abspath(__file__))
    logo_url   = "https://www.bluealphabelts.com/wp-content/uploads/2024/04/logo-1.png"
    logo_tmp   = None
    # Prefer white-bg jpg (clean navy logo), then fallbacks
    for _candidate in ["ba-logo-white-bg.jpg", "ba-logo-dark.png", "ba-logo.jpg"]:
        _path = os.path.join(_static, "static", _candidate)
        if os.path.exists(_path):
            logo_file = _path
            break
    else:
        logo_file = None
    try:
        if not logo_file:
            import urllib.request
            logo_tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
            urllib.request.urlretrieve(logo_url, logo_tmp.name)
            logo_file = logo_tmp.name
        pdf.image(logo_file, x=19, y=LOGO_TOP, w=LOGO_W)
    except Exception:
        # Fallback: text logo
        pdf.set_xy(19, LOGO_TOP)
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(*NAVY)
        pdf.cell(LOGO_W, 10, "BLUE ALPHA", border=0)
    finally:
        if logo_tmp:
            try: os.unlink(logo_tmp.name)
            except Exception: pass

    # ── "QUOTE" heading + company info (top-right) ────────────────────
    right_x = 19 + W * 0.55
    right_w = W * 0.45

    pdf.set_xy(right_x, LOGO_TOP)
    pdf.set_font("Helvetica", "B", 28)
    pdf.set_text_color(*NAVY)
    _doc_label = "SALES ORDER" if doc_type == "order" else "QUOTE"
    pdf.cell(right_w, 12, _doc_label, align="R", new_x="LMARGIN", new_y="NEXT")

    for cline, bold, size in [
        ("Blue Alpha",           True,  8),
        ("35 Andrew St.",        False, 8),
        ("Newnan, GA 30263",     False, 8),
        ("678-961-3304",         False, 8),
        ("info@bluealpha.us",    False, 8),
    ]:
        pdf.set_xy(right_x, pdf.get_y())
        pdf.set_font("Helvetica", "B" if bold else "", size)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(right_w, 4.5, cline, align="R", new_x="LMARGIN", new_y="NEXT")

    # Move cursor below header block
    pdf.set_y(max(pdf.get_y(), LOGO_TOP + LOGO_W * 0.39 + 2))  # logo aspect ~0.45h/w

    # Navy divider
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.7)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(5)

    # ── Quote details (left) + Bill To / Ship To (right) ─────────────
    y_info = pdf.get_y()
    col_w  = W / 2
    right_col_w = col_w / 2  # split right half for Bill To | Ship To

    def kv(label, value):
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(34, 5.5, label, border=0)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.cell(col_w - 34, 5.5, str(value), border=0, new_x="LMARGIN", new_y="NEXT")

    if doc_type == "order":
        kv("Order Number:", q_number)
        kv("Order Date:",   q_date)
        kv("Terms:",        "Net 30")
        if q_po:
            kv("PO #:", q_po)
    else:
        kv("Quote Number:", q_number)
        kv("Quote Date:",   q_date)
        kv("Expiry Date:",  q_expiry)
        kv("Terms:",        "Net 30")
        if q_po:
            kv("PO #:", q_po)

    y_after_details = pdf.get_y()

    # Right: Bill To (left half of right column)
    bill_x = 19 + col_w
    pdf.set_xy(bill_x, y_info)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(right_col_w, 5.5, "Bill To", border=0, new_x="LEFT", new_y="NEXT")
    for ln in [bill_org, bill_name, bill_addr1, bill_addr2]:
        if ln:
            pdf.set_x(bill_x)
            pdf.set_font("Helvetica", "B" if ln == bill_org else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(right_col_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")

    # Right: Ship To (right half of right column)
    ship_x = 19 + col_w + right_col_w
    pdf.set_xy(ship_x, y_info)
    pdf.set_font("Helvetica", "B", 8)
    pdf.set_text_color(*MUTED)
    pdf.cell(right_col_w, 5.5, "Ship To", border=0, new_x="LEFT", new_y="NEXT")
    ship_lines = [
        cust.get("orgName", ""),
        cust.get("contactName", ""),
        addr1,
        ", ".join(filter(None, [city, f"{state_v} {zip_v}".strip()])),
    ]
    for ln in ship_lines:
        if ln:
            pdf.set_x(ship_x)
            pdf.set_font("Helvetica", "B" if ln == cust.get("orgName", "") else "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(right_col_w, 5, ln, border=0, new_x="LEFT", new_y="NEXT")

    pdf.set_y(max(y_after_details, pdf.get_y()) + 3)

    # Validity note
    pdf.ln(2)

    # ── Line items table ──────────────────────────────────────────────
    sku_w  = 36
    name_w = W - sku_w - 14 - 25 - 25
    col_widths = [sku_w, name_w, 14, 25, 25]
    headers    = ["SKU", "DESCRIPTION", "QTY", "UNIT PRICE", "TOTAL"]
    aligns     = ["L",   "L",           "R",   "R",          "R"    ]
    row_h      = 7

    pdf.set_fill_color(*NAVY)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("Helvetica", "B", 8)
    for w, h, a in zip(col_widths, headers, aligns):
        pdf.cell(w, row_h, h, border=0, align=a, fill=True)
    pdf.ln()

    pdf.set_font("Helvetica", "", 8)
    for idx, item in enumerate(line_items):
        pdf.set_fill_color(*LG)
        pdf.set_text_color(*TEXT)
        line_total = item["qty"] * item["unitPrice"]
        row_data = [
            item.get("skuId", ""),
            item.get("name", ""),
            str(item["qty"]),
            f"${item['unitPrice']:.2f}",
            f"${line_total:.2f}",
        ]
        for w, cell_val, a in zip(col_widths, row_data, aligns):
            pdf.cell(w, row_h, cell_val, border=0, align=a, fill=(idx % 2 == 0))
        pdf.ln()

    pdf.ln(3)

    # ── Totals ────────────────────────────────────────────────────────
    def totals_row(label, value, bold=False):
        pdf.set_font("Helvetica", "B" if bold else "", 9)
        pdf.cell(W - 50, 6, "", border=0)
        pdf.set_text_color(*TEXT if bold else MUTED)
        pdf.cell(25, 6, label, border=0, align="R")
        pdf.set_text_color(*TEXT)
        pdf.cell(25, 6, value, border=0, align="R", new_x="LMARGIN", new_y="NEXT")

    totals_row("Subtotal", f"${subtotal:.2f}")
    if shipping > 0:
        totals_row("Shipping", f"${shipping:.2f}")
    y_rule = pdf.get_y()
    pdf.set_draw_color(*NAVY)
    pdf.set_line_width(0.4)
    pdf.line(19 + W - 50, y_rule, 19 + W, y_rule)
    pdf.ln(1)
    totals_row("Total", f"${total:.2f}", bold=True)
    pdf.ln(5)

    # ── Notes + Footer pinned to bottom ──────────────────────────────
    clean_notes = (q_notes or "").strip()
    clean_notes = clean_notes if clean_notes.lower() not in ("no notes", "none", "n/a", "") else ""

    # Pin footer to bottom of current page; disable auto page break so it never overflows
    PAGE_H     = 279.4   # letter height in mm
    BOT_MARGIN = 6       # mm from bottom for footer start
    footer_h   = 20 + (10 if clean_notes else 0)
    footer_y   = PAGE_H - BOT_MARGIN - footer_h
    # Only jump down if we're above the footer zone (never jump backward past content)
    if pdf.get_y() < footer_y:
        pdf.set_y(footer_y)
    pdf.set_auto_page_break(auto=False)

    if clean_notes:
        pdf.set_draw_color(*BD)
        pdf.set_line_width(0.3)
        pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
        pdf.ln(1)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(W, 4, "NOTES", border=0, new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*TEXT)
        pdf.multi_cell(W, 4, clean_notes, border=0)
        pdf.ln(1)

    # Footer divider
    pdf.set_draw_color(*BD)
    pdf.set_line_width(0.3)
    pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
    pdf.ln(2)
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)

    # Line 1: payment terms / order confirmation
    if doc_type == "order":
        pdf.write(4.5, "Thank you for your order. Payment terms are Net 30 from date of invoice.  ")
        if portal_link:
            pdf.write(4.5, "View your order at ")
            pdf.set_text_color(91, 127, 160)
            pdf.set_font("Helvetica", "U", 7.5)
            pdf.write(4.5, "your Blue Alpha Portal", link=portal_link)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(*MUTED)
            pdf.write(4.5, ".")
    else:
        pdf.write(4.5, "Payment terms are Net 30 upon acceptance.  ")
        pdf.write(4.5, "To accept this quote, ")
        pdf.set_text_color(91, 127, 160)   # Steel Blue
        pdf.set_font("Helvetica", "U", 7.5)
        pdf.write(4.5, "click here to view it in your Blue Alpha Quote Portal", link=portal_link)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.write(4.5, ".")
    pdf.ln(5)

    # Line 2: questions
    pdf.set_font("Helvetica", "", 7.5)
    pdf.set_text_color(*MUTED)
    pdf.cell(W, 4.5,
        "Questions? Contact us at info@bluealpha.us or 678-961-3304.",
        border=0, new_x="LMARGIN", new_y="NEXT")

    return bytes(pdf.output())


@app.route("/quote-pdf/<record_id>", methods=["GET"])
def quote_pdf(record_id):
    c = cors()
    try:
        quote = _fetch_quote_data(record_id)
        if not quote:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        pdf_bytes = _build_quote_pdf_bytes(quote)
        q_number  = quote.get("quoteNumber", record_id)
        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{q_number}.pdf"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=cors(), mimetype="application/json")


@app.route("/order-pdf/<record_id>", methods=["GET"])
def order_pdf(record_id):
    c = cors()
    try:
        order = _fetch_quote_data(record_id)
        if not order:
            return Response(json.dumps({"error": "Order not found"}), status=404,
                            headers=c, mimetype="application/json")
        pdf_bytes = _build_quote_pdf_bytes(order, doc_type="order")
        so_number = order.get("quoteNumber", record_id)
        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{so_number}.pdf"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=cors(), mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal Routes
# ─────────────────────────────────────────────────────────────────────────────

def _send_application_received_email(to_email, to_name, company):
    if not SENDGRID_API_KEY:
        return
    first_name = to_name.split()[0] if to_name else "there"
    actual_to = TEST_EMAIL_OVERRIDE or to_email
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="margin:0;padding:0;background:#f5f7fa;font-family:Arial,Helvetica,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;padding:32px 0;">
    <tr><td align="center">
      <table width="520" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.08);">
        <tr><td style="background:#1B2438;padding:24px 36px;">
          <span style="font-family:Arial;font-size:20px;font-weight:800;color:#fff;letter-spacing:2px;">BLUE ALPHA</span>
        </td></tr>
        <tr><td style="padding:32px 36px;">
          <p style="color:#1a2633;font-size:16px;margin:0 0 8px;">Hi {first_name},</p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 16px;">
            Thank you for applying for access to the Blue Alpha Government Agency Quote Portal.
            We've received your application for <strong>{company}</strong> and will review it within 2 business days.
          </p>
          <p style="color:#6b7a8d;font-size:14px;line-height:1.6;margin:0 0 24px;">
            You'll receive another email once your application has been reviewed. If you have any questions in the meantime, feel free to reach out to us.
          </p>
          <p style="color:#6b7a8d;font-size:12px;margin-top:16px;">
            Questions? Contact us at <a href="mailto:info@bluealpha.us" style="color:#1B2438;">info@bluealpha.us</a>
          </p>
        </td></tr>
        <tr><td style="background:#f5f7fa;border-top:1px solid #dde3ea;padding:16px 36px;text-align:center;">
          <p style="color:#6b7a8d;font-size:11px;margin:0;">Blue Alpha &bull; bluealphabelts.com</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""
    try:
        req_lib.post(
            "https://api.sendgrid.com/v3/mail/send",
            headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
            json={
                "personalizations": [{"to": [{"email": actual_to, "name": to_name}]}],
                "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                "subject": "We received your Blue Alpha portal application",
                "content": [{"type": "text/html", "value": html_body}],
            },
            timeout=10,
        )
    except Exception as e:
        print(f"[apply] confirmation email error: {e}")


@app.route("/apply", methods=["GET", "POST"])
def apply_page():
    if request.method == "GET":
        return send_from_directory("static", "apply.html")

    from datetime import datetime, timezone
    c = cors()

    # Accept both multipart/form-data (new, supports file upload) and JSON (legacy)
    if request.content_type and request.content_type.startswith("multipart/form-data"):
        def gf(k): return (request.form.get(k) or "").strip()
        cert_file = request.files.get("exemptionCert")
    else:
        data = request.get_json() or {}
        def gf(k): return (data.get(k) or "").strip()
        cert_file = None

    company_name              = gf("companyName")
    ein                       = gf("ein")
    website                   = gf("website")
    tax_exemption_number      = gf("taxExemptionNumber")

    # Shipping fields (always filled)
    shipping_contact_name  = gf("shippingContactName")
    shipping_contact_email = gf("shippingContactEmail")
    shipping_contact_phone = gf("shippingContactPhone")
    shipping_addr1         = gf("shippingAddr1")
    shipping_addr2         = gf("shippingAddr2")
    shipping_city          = gf("shippingCity")
    shipping_state         = gf("shippingState")
    shipping_zip           = gf("shippingZip")

    # Billing fields — fall back to shipping when same-as-shipping is checked
    billing_same_raw = gf("billingSameAsShipping")
    billing_same     = billing_same_raw.lower() not in ("false", "0", "no")
    billing_contact_name   = gf("billingContactName")  if not billing_same else shipping_contact_name
    billing_contact_email  = gf("billingContactEmail") if not billing_same else shipping_contact_email
    billing_contact_phone  = gf("billingContactPhone") if not billing_same else shipping_contact_phone
    billing_addr1          = gf("billingAddr1")        if not billing_same else shipping_addr1
    billing_addr2          = gf("billingAddr2")        if not billing_same else shipping_addr2
    billing_city           = gf("billingCity")         if not billing_same else shipping_city
    billing_state          = gf("billingState")        if not billing_same else shipping_state
    billing_zip            = gf("billingZip")          if not billing_same else shipping_zip

    required_fields = [company_name, ein, tax_exemption_number,
                       shipping_contact_name, shipping_contact_email, shipping_contact_phone,
                       shipping_addr1, shipping_city, shipping_state, shipping_zip]
    if not all(required_fields):
        return Response(json.dumps({"error": "Please fill in all required fields."}),
                        status=400, headers=c, mimetype="application/json")

    # Customer Address = SHIPPING address (city/state/zip auto-parsed by Airtable formula)
    # Bill-To Address  = BILLING address (plain text)
    def pack_addr2(city, state, zip_code, line2=""):
        csz = f"{city}, {state} {zip_code}"
        return (line2 + "\n" + csz) if line2 else csz

    bill_addr2_full = pack_addr2(billing_city, billing_state, billing_zip, billing_addr2)
    ship_addr2_full = f"{shipping_city}, {shipping_state} {shipping_zip}"
    if shipping_addr2:
        ship_addr2_full = shipping_addr2 + "\n" + ship_addr2_full

    fields = {
        "Organization Name":            company_name,
        "EIN":                          ein,
        # Main Contact = shipping contact (person submitting form; receives goods)
        "Main Contact Name":            shipping_contact_name,
        "Main Contact Email":           shipping_contact_email,
        "Main Contact Phone #":         shipping_contact_phone,
        # Customer Address = shipping address (city/state/zip auto-parsed by Airtable formula)
        "Customer Address (Line 1)":    shipping_addr1,
        "Customer Address (Line 2)":    ship_addr2_full,
        # Bill-To = billing contact & address
        "Bill-To Org Name":             company_name,
        "Bill-To Contact Name":         billing_contact_name,
        "Bill-To Contact Email":        billing_contact_email,
        "Bill-To Phone #":              billing_contact_phone,
        "Bill-To Address (Line 1)":     billing_addr1,
        "Bill-To Address (Line 2)":     bill_addr2_full,
        "State Tax Exemption #":        tax_exemption_number,
        "Application Status":           "Pending",
        "Applied Date":                 datetime.now(timezone.utc).strftime("%Y-%m-%d"),
    }
    if website: fields["Website"] = website

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
            headers={**at_headers(APPLY_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=15,
        )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"error": "Failed to save application. Please try again."}),
                            status=500, headers=c, mimetype="application/json")

        record_id = r.json().get("id")

        # Upload tax exemption certificate to Airtable if provided
        # Strategy: upload file to tmpfiles.org → get public URL → PATCH Airtable record
        if cert_file and cert_file.filename and record_id:
            try:
                file_bytes = cert_file.read()
                filename   = cert_file.filename or "exemption-certificate"
                # 1. Upload to tmpfiles.org to get a public URL
                tmp_resp = req_lib.post(
                    "https://tmpfiles.org/api/v1/upload",
                    files={"file": (filename, file_bytes, cert_file.content_type or "application/octet-stream")},
                    timeout=30,
                )
                if tmp_resp.status_code == 200:
                    tmp_data = tmp_resp.json()
                    tmp_url  = tmp_data.get("data", {}).get("url", "")
                    # tmpfiles.org returns http:// — Airtable needs https://
                    if tmp_url.startswith("http://"):
                        tmp_url = "https://" + tmp_url[7:]
                    if tmp_url:
                        # 2. PATCH Airtable record with the attachment URL
                        req_lib.patch(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
                            headers={**at_headers(APPLY_WRITE_TOKEN), "Content-Type": "application/json"},
                            json={"fields": {"Tax Exemption Certificate": [{"url": tmp_url, "filename": filename}]}},
                            timeout=15,
                        )
            except Exception:
                pass  # Don't fail the whole application if cert upload fails

        # Send confirmation email to applicant
        threading.Thread(
            target=_send_application_received_email,
            args=(shipping_contact_email, shipping_contact_name, company_name),
            daemon=True
        ).start()

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if request.method == "GET":
        return send_from_directory("static", "login.html")

    c = cors()
    data = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    # Always return same message — do the real work silently
    def _try_send_magic_link(email):
        try:
            read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            records = at_get_all(
                CUSTOMERS_TABLE_ID, read_token,
                fields=["Main Contact Email", "Main Contact Name", "Application Status"],
                formula=f"AND(LOWER({{Main Contact Email}})='{email}',{{Application Status}}='Approved')",
            )
            if not records:
                return
            user_rec = records[0]
            user_id  = user_rec["id"]  # Customer record ID is both user_id and customer_id
            magic_link = generate_magic_link(user_id, expiry_hours=0.25)
            send_magic_link_email(email, magic_link)
        except Exception as e:
            print(f"[login] magic link error: {e}")

    if email:
        threading.Thread(target=_try_send_magic_link, args=(email,), daemon=True).start()

    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


@app.route("/setup-account/<token>")
def setup_account_page(token):
    """Serves the account setup page for new sub-users (invited via email)."""
    return send_from_directory("static", "setup-account.html")


@app.route("/api/portal/setup-account", methods=["POST"])
def portal_setup_account():
    """Complete account setup: validate invite token, set username + password."""
    from datetime import datetime, timezone
    c = cors()
    data     = request.get_json() or {}
    token    = (data.get("token") or "").strip()
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not token or not username or not password:
        return Response(json.dumps({"error": "token, username, and password are required"}),
                        status=400, headers=c, mimetype="application/json")
    if len(password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}),
                        status=400, headers=c, mimetype="application/json")

    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Find record by invite token
        records = at_get_all(CUSTOMERS_TABLE_ID, read_token,
                             fields=["Magic Token", "Token Expiry", "Portal Hash",
                                     "Portal Role", "Parent Company", "Main Contact Email"],
                             formula=f"{{Magic Token}}='{token}'")
        if not records:
            return Response(json.dumps({"error": "Invalid or expired invite link"}),
                            status=400, headers=c, mimetype="application/json")
        rec = records[0]
        f   = rec["fields"]

        # Check token expiry
        expiry_str = f.get("Token Expiry", "")
        if expiry_str:
            try:
                exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
                if exp_dt.tzinfo is None:
                    exp_dt = exp_dt.replace(tzinfo=timezone.utc)
                if datetime.now(timezone.utc) > exp_dt:
                    return Response(json.dumps({"error": "Invite link has expired. Ask your admin to resend."}),
                                    status=400, headers=c, mimetype="application/json")
            except Exception:
                pass

        # Check username not already taken
        existing = at_get_all(CUSTOMERS_TABLE_ID, read_token,
                              fields=["Portal Username"],
                              formula=f"LOWER({{Portal Username}})='{username}'")
        if existing:
            return Response(json.dumps({"error": "Username already taken — please choose another"}),
                            status=400, headers=c, mimetype="application/json")

        # Set username + password, clear invite token
        pw_hash = _hash_password(password)
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{rec['id']}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Portal Username": username,
                "Portal Hash":   pw_hash,
                "Magic Token":     "",
                "Token Expiry":    None,
                "Last Login":      datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
            }},
            timeout=10,
        )

        # Log them in immediately
        parent_ids  = f.get("Parent Company", [])
        customer_id = parent_ids[0] if parent_ids else rec["id"]
        role_raw    = (f.get("Portal Role") or "").strip()
        _rmap = {"Admin": "admin", "Full Access": "full_access",
                 "Quotes Only": "quotes_only", "Read Only": "read_only"}
        role = _rmap.get(role_raw, "admin" if not parent_ids else "read_only")
        jwt_token = create_portal_token(rec["id"], customer_id, not bool(parent_ids), role)
        resp = make_response(Response(json.dumps({"ok": True}), headers=c, mimetype="application/json"))
        resp.set_cookie("ba_portal_session", jwt_token, max_age=30*24*3600,
                        httponly=True, samesite="Lax", secure=True)
        return resp
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/auth/<token>")
def auth_magic_link(token):
    from datetime import datetime, timezone
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Magic Token", "Token Expiry", "Application Status", "Portal Role"],
            formula=f"{{Magic Token}}='{token}'",
        )
        if not records:
            return send_from_directory("static", "auth-error.html"), 401

        user_rec = records[0]
        uf = user_rec.get("fields", {})
        expiry_str = uf.get("Token Expiry", "")
        if not expiry_str:
            return send_from_directory("static", "auth-error.html"), 401

        # Check Application Status
        if uf.get("Application Status") != "Approved":
            return send_from_directory("static", "auth-error.html"), 401

        # Check expiry
        try:
            exp_dt = datetime.fromisoformat(expiry_str.replace("Z", "+00:00"))
            if exp_dt.tzinfo is None:
                from datetime import timezone as tz
                exp_dt = exp_dt.replace(tzinfo=tz.utc)
        except Exception:
            return send_from_directory("static", "auth-error.html"), 401

        if datetime.now(timezone.utc) > exp_dt:
            return send_from_directory("static", "auth-error.html"), 401

        # Customer record ID is both user_id and customer_id for magic-link users
        user_id     = user_rec["id"]
        customer_id = user_rec["id"]
        is_primary  = True

        # Fetch Portal Role from Customer record (legacy users with no role → admin)
        portal_role = None
        try:
            role_raw = uf.get("Portal Role", "")
            if not role_raw:
                # Re-fetch to get the Portal Role field if not in token fields
                _role_map = {"Admin": "admin", "Full Access": "full_access",
                             "Quotes Only": "quotes_only", "Read Only": "read_only"}
                portal_role = None  # legacy → admin
            else:
                _role_map = {"Admin": "admin", "Full Access": "full_access",
                             "Quotes Only": "quotes_only", "Read Only": "read_only"}
                portal_role = _role_map.get(role_raw.strip(), None)
        except Exception:
            portal_role = None

        # Clear magic token + update last login
        try:
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
                json={"fields": {
                    "Magic Token":  "",
                    "Token Expiry": None,
                    "Last Login":   datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                }},
                timeout=10,
            )
        except Exception as e:
            print(f"[auth_magic_link] token clear failed: {e}")

        # Create JWT session cookie
        jwt_token = create_portal_token(user_id, customer_id, is_primary, role=portal_role)
        resp = make_response(redirect("/portal"))
        resp.set_cookie(
            "ba_portal_session",
            jwt_token,
            max_age=30 * 24 * 3600,
            httponly=True,
            samesite="Lax",
            secure=True,
        )
        return resp

    except Exception as e:
        print(f"[auth_magic_link] error: {e}")
        return send_from_directory("static", "auth-error.html"), 500


@app.route("/logout")
def portal_logout():
    resp = make_response(redirect("/quote"))
    resp.delete_cookie("ba_portal_session")
    return resp


@app.route("/portal")
@portal_login_required
def portal_page(user):
    return send_from_directory("static", "portal.html")


@app.route("/api/portal/me")
@portal_login_required
def portal_me(user):
    c = cors()
    import time as _time_mod
    customer_id = user.get("customer_id", "")
    is_primary  = user.get("is_primary", False)
    role = user.get("role")
    can_manage_team  = portal_can(user, "manage_team")
    can_create_quote = portal_can(user, "create_quote")
    can_accept_quote = portal_can(user, "accept_quote")

    # Use cached agency name if fresh
    _me_cached = _ME_CACHE.get(customer_id)
    if _me_cached and (_time_mod.time() - _me_cached["ts"]) < _ME_CACHE_TTL:
        agency_name = _me_cached["data"]
    else:
        agency_name = ""
        if customer_id:
            try:
                read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
                cr = req_lib.get(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                    headers=at_headers(read_token),
                    params={"fields[]": ["Organization Name"]},
                    timeout=10,
                )
                if cr.status_code == 200:
                    agency_name = cr.json().get("fields", {}).get("Organization Name", "")
            except Exception as e:
                print(f"[portal_me] customer fetch failed: {e}")
        _ME_CACHE[customer_id] = {"ts": _time_mod.time(), "data": agency_name}

    return Response(json.dumps({
        "agencyName":      agency_name,
        "isPrimary":       is_primary,
        "customerId":      customer_id,
        "role":            role,
        "canManageTeam":   can_manage_team,
        "canCreateQuote":  can_create_quote,
        "canAcceptQuote":  can_accept_quote,
    }), headers=c, mimetype="application/json")


@app.route("/api/portal/profile", methods=["GET", "OPTIONS"])
@portal_login_required
def portal_profile(user):
    """Return full contact/address info for the logged-in customer."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Methods": "GET"})
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        cr = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers=at_headers(read_token),
            timeout=10,
        )
        if cr.status_code != 200:
            return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")
        f = cr.json().get("fields", {})
        profile = {
            "customerId":  customer_id,
            "orgName":     f.get("Organization Name", ""),
            "contactName": f.get("Main Contact Name", ""),
            "email":       f.get("Main Contact Email", ""),
            "phone":       f.get("Main Contact Phone #", ""),
            "addr1":       f.get("Customer Address (Line 1)", ""),
            "city":        f.get("Customer City", ""),
            "state":       f.get("Customer State", ""),
            "zip":         f.get("Customer Zip Code", ""),
        }
        return Response(json.dumps({"profile": profile}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_profile] error: {e}")
        return Response(json.dumps({"profile": {}}), headers=c, mimetype="application/json")


@app.route("/api/portal/update-profile", methods=["POST", "OPTIONS"])
@portal_login_required
def portal_update_profile(user):
    """Update the logged-in customer's contact/address info in Airtable."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"error": "Not authenticated"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    def pack(city, state, zip_code):
        parts = [p for p in [city, f"{state} {zip_code}".strip()] if p]
        return ", ".join(parts)
    fields = {}
    if data.get("orgName"):     fields["Organization Name"]            = data["orgName"].strip()
    if data.get("contactName"): fields["Main Contact Name"]            = data["contactName"].strip()
    if data.get("email"):       fields["Main Contact Email"]           = data["email"].strip()
    if data.get("phone"):       fields["Main Contact Phone #"]         = data["phone"].strip()
    if data.get("addr1"):       fields["Customer Address (Line 1)"]    = data["addr1"].strip()
    city  = data.get("city",  "").strip()
    state = data.get("state", "").strip()
    zip_  = data.get("zip",   "").strip()
    if city or state or zip_:
        fields["Customer Address (Line 2)"] = pack(city, state, zip_)
    if not fields:
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    try:
        write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=15,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_update_profile] error: {e}")
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


_QUOTES_CACHE: dict = {}   # {customer_id: {"ts": float, "data": list}}
_QUOTES_CACHE_TTL = 60    # seconds
_ORDERS_CACHE: dict = {}  # {customer_id: {"ts": float, "data": list}}
_ORDERS_CACHE_TTL = 60    # seconds
_ME_CACHE: dict = {}      # {customer_id: {"ts": float, "data": dict}}
_ME_CACHE_TTL = 300       # seconds (5 min — agency name rarely changes)

@app.route("/api/portal/quotes")
@portal_login_required
def portal_quotes(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"quotes": []}), headers=c, mimetype="application/json")

    # Return cached result if fresh enough
    import time as _time_mod
    _cached = _QUOTES_CACHE.get(customer_id)
    if _cached and (_time_mod.time() - _cached["ts"]) < _QUOTES_CACHE_TTL:
        return Response(json.dumps({"quotes": _cached["data"]}), headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Note: ARRAYJOIN({Customer}) in Airtable formulas returns the linked record's
        # primary field (name), not its record ID — so we can't filter by customer_id
        # in the formula. Fetch all open quotes and filter in Python instead.
        formula = '{Order Type}="Quote"'
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "Expiry Date", "MO Is Approved",
                    "Customer", "Hidden from Customer", "Total Gross"],
            formula=formula,
        )
        records = [r for r in records
                   if customer_id in r.get("fields", {}).get("Customer", [])
                   and not r.get("fields", {}).get("Hidden from Customer", False)]
        from datetime import date as dt_date
        today = _today_utc()

        # Batch-fetch all line items in one AT query using OR(RECORD_ID()=...)
        all_li_ids = []
        for r in records:
            all_li_ids.extend(r.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}  # li_id -> Dynamic Line Item Total
        if all_li_ids:
            chunks = [all_li_ids[i:i+30] for i in range(0, len(all_li_ids), 30)]
            for chunk in chunks:
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in chunk) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Dynamic Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Dynamic Line Item Total") or 0)

        quotes = []
        for r in records:
            f = r.get("fields", {})
            quote_number = f.get("Document ID", f"QU-{f.get('Order ID','')}")
            expiry_str   = f.get("Expiry Date", "")
            is_expired   = False
            if expiry_str:
                try:
                    is_expired = dt_date.fromisoformat(expiry_str) < today
                except Exception:
                    pass
            total = sum(li_total_map.get(lid, 0) for lid in f.get("MO Line Items", []))
            quotes.append({
                "record_id":    r["id"],
                "quote_number": quote_number,
                "date":         f.get("Date", ""),
                "expiry_date":  expiry_str,
                "is_expired":   is_expired,
                "is_accepted":  bool(f.get("MO Is Approved")),
                "total":        round(total, 2),
            })
        # Sort most recent first
        quotes.sort(key=lambda x: x.get("date", ""), reverse=True)
        _QUOTES_CACHE[customer_id] = {"ts": _time_mod.time(), "data": quotes}
        return Response(json.dumps({"quotes": quotes}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/orders")
@portal_login_required
def portal_orders(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"orders": []}), headers=c, mimetype="application/json")

    import time as _time_mod
    import concurrent.futures as _cf

    # Return cached result if fresh
    _cached = _ORDERS_CACHE.get(customer_id)
    if _cached and (_time_mod.time() - _cached["ts"]) < _ORDERS_CACHE_TTL:
        return Response(json.dumps({"orders": _cached["data"]}), headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Fetch all SOs then filter in Python (ARRAYJOIN formula returns display names not record IDs)
        records = at_get_all(
            MANUAL_ORDERS_TABLE_ID, read_token,
            fields=["Document ID", "Order ID", "Date", "MO Line Items", "Customer", "Sales Order Status", "Go-to PDF"],
            formula='{Order Type}="Sales Order"',
        )
        records = [r for r in records
                   if customer_id in r.get("fields", {}).get("Customer", [])
                   and r.get("fields", {}).get("Sales Order Status") == "Approved"]

        # Batch-fetch all line items in one AT query
        all_li_ids = []
        for r in records:
            all_li_ids.extend(r.get("fields", {}).get("MO Line Items", []))
        li_total_map = {}
        if all_li_ids:
            chunks = [all_li_ids[i:i+30] for i in range(0, len(all_li_ids), 30)]
            for chunk in chunks:
                formula = "OR(" + ",".join(f'RECORD_ID()="{lid}"' for lid in chunk) + ")"
                li_recs = at_get_all(MO_LINE_ITEMS_TABLE_ID, read_token,
                                     fields=["Dynamic Line Item Total"],
                                     formula=formula)
                for lr in li_recs:
                    li_total_map[lr["id"]] = float(lr.get("fields", {}).get("Dynamic Line Item Total") or 0)

        orders = []
        for r in records:
            f = r.get("fields", {})
            so_number = f.get("Document ID", f'SO-{f.get("Order ID","")}')
            total = sum(li_total_map.get(lid, 0) for lid in f.get("MO Line Items", []))
            go_to_pdf_field = f.get("Go-to PDF") or {}
            go_to_pdf_url   = go_to_pdf_field.get("url", "") if isinstance(go_to_pdf_field, dict) else ""
            orders.append({
                "record_id":  r["id"],
                "so_number":  so_number,
                "date":       f.get("Date", ""),
                "total":      round(total, 2),
                "go_to_pdf":  go_to_pdf_url,
            })
        orders.sort(key=lambda x: x.get("date", ""), reverse=True)
        _ORDERS_CACHE[customer_id] = {"ts": _time_mod.time(), "data": orders}
        return Response(json.dumps({"orders": orders}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/users")
@portal_login_required
def portal_users(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    # One login per agency — return just the current user from the Customer record
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        cr = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers=at_headers(read_token),
            params={"fields[]": ["Main Contact Name", "Main Contact Email"]},
            timeout=10,
        )
        if cr.status_code == 200:
            cf = cr.json().get("fields", {})
            current_user = {
                "id":         customer_id,
                "name":       cf.get("Main Contact Name", ""),
                "email":      cf.get("Main Contact Email", ""),
                "is_primary": True,
            }
        else:
            current_user = {"id": customer_id, "name": "", "email": "", "is_primary": True}
        return Response(json.dumps({"users": [current_user]}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/users/add", methods=["POST"])
@portal_login_required
def portal_users_add(user):
    c = cors()
    # One login per agency — additional users are not supported
    return Response(
        json.dumps({"error": "Additional portal users are not supported. Each agency has one login."}),
        status=400, headers=c, mimetype="application/json",
    )


@app.route("/api/portal/users/remove", methods=["POST"])
@portal_login_required
def portal_users_remove(user):
    c = cors()
    # One login per agency — user removal is not supported
    return Response(
        json.dumps({"error": "User removal is not supported. Each agency has one login."}),
        status=400, headers=c, mimetype="application/json",
    )


@app.route("/api/portal/request-magic-link", methods=["POST"])
def portal_request_magic_link():
    c = cors()
    data  = request.get_json() or {}
    email = (data.get("email") or "").strip().lower()

    def _send(email):
        try:
            read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            records = at_get_all(
                CUSTOMERS_TABLE_ID, read_token,
                fields=["Main Contact Email", "Application Status"],
                formula=f"AND(LOWER({{Main Contact Email}})='{email}',{{Application Status}}='Approved')",
            )
            if records:
                link = generate_magic_link(records[0]["id"])
                send_magic_link_email(email, link)
        except Exception as ex:
            print(f"[request_magic_link] error: {ex}")

    if email:
        threading.Thread(target=_send, args=(email,), daemon=True).start()
    return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Username/Password Login
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/login", methods=["POST", "OPTIONS"])
def portal_login():
    """Authenticate a B2B customer with username + password. Sets ba_portal_session cookie."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type",
                                     "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "Username and password are required"}),
                        status=400, headers=c, mimetype="application/json")
    rec, role, customer_id = _lookup_portal_customer(username, password)
    if not rec:
        return Response(json.dumps({"error": "Invalid username or password"}),
                        status=401, headers=c, mimetype="application/json")
    user_id    = rec["id"]
    is_primary = not rec.get("fields", {}).get("Parent Company")
    jwt_token  = create_portal_token(user_id, customer_id, is_primary, role=role)
    resp = make_response(Response(json.dumps({"ok": True}), headers=c, mimetype="application/json"))
    resp.set_cookie(
        "ba_portal_session", jwt_token,
        max_age=30 * 24 * 3600,
        httponly=True,
        samesite="Lax",
        secure=True,
    )
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Set Password (migration path for magic-link-only users)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/set-password", methods=["POST"])
@portal_login_required
def portal_set_password(user):
    """Allows a logged-in customer to set Portal Username and Password Hash for the first time."""
    c = cors()
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    password = (data.get("password") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "username and password are required"}),
                        status=400, headers=c, mimetype="application/json")
    if len(password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}),
                        status=400, headers=c, mimetype="application/json")
    user_id = user.get("user_id", "")
    if not user_id:
        return Response(json.dumps({"error": "Invalid session"}), status=401,
                        headers=c, mimetype="application/json")
    pw_hash     = _hash_password(password)
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Portal Username": username, "Portal Hash": pw_hash}},
            timeout=10,
        )
        r.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Team Management
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/team", methods=["GET"])
@portal_login_required
def portal_team_list(user):
    """List all team members for this customer account. Requires manage_team permission."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch all customers with Portal Username set
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Main Contact Name", "Organization Name", "Portal Username",
                    "Portal Role", "Parent Company", "Main Contact Email", "Portal Hash"],
            formula=f"OR({{Portal Username}}!='',{{Main Contact Email}}!='')",
        )
        # Also fetch the primary record (may not have Portal Username)
        primary_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers=at_headers(read_token), timeout=10,
        )
        _role_map = {"Admin": "admin", "Full Access": "full_access",
                     "Quotes Only": "quotes_only", "Read Only": "read_only"}
        team     = []
        seen_ids = set()

        # Primary record first
        if primary_r.status_code == 200:
            pf    = primary_r.json().get("fields", {})
            prole = _role_map.get((pf.get("Portal Role") or "").strip(), "admin")
            team.append({
                "id":       customer_id,
                "name":     pf.get("Main Contact Name") or pf.get("Organization Name", ""),
                "username": pf.get("Portal Username", ""),
                "role":     prole,
                "isOwner":  True,
            })
            seen_ids.add(customer_id)

        # Sub-users
        for r in records:
            if r["id"] in seen_ids:
                continue
            f = r.get("fields", {})
            parent_ids = f.get("Parent Company", [])
            if customer_id not in parent_ids:
                continue
            srole = _role_map.get((f.get("Portal Role") or "").strip(), "read_only")
            team.append({
                "id":           r["id"],
                "name":         f.get("Main Contact Name", ""),
                "username":     f.get("Portal Username", ""),
                "email":        f.get("Main Contact Email", ""),
                "role":         srole,
                "isOwner":      False,
                "pendingSetup": not bool(f.get("Portal Hash")),
            })
            seen_ids.add(r["id"])

        return Response(json.dumps({"team": team}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


def _send_portal_invite_email(to_email, invitee_name, inviter_company, setup_link):
    """Send a portal account setup invitation email."""
    if not SENDGRID_API_KEY:
        return
    try:
        first = invitee_name.split()[0] if invitee_name else "there"
        payload = {
            "personalizations": [{"to": [{"email": to_email, "name": invitee_name}]}],
            "from": {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
            "subject": f"You've been invited to the Blue Alpha Portal",
            "content": [{"type": "text/html", "value": f"""
<div style="font-family:Arial,Helvetica,sans-serif;max-width:560px;margin:0 auto;padding:32px 16px;color:#1a2633;">
  <img src="https://bluealphabelts.com/wp-content/uploads/2021/01/Blue-Alpha-Logo-White-Background.png" alt="Blue Alpha" style="height:40px;margin-bottom:24px;">
  <h2 style="font-size:20px;font-weight:700;margin:0 0 12px;">You've been invited</h2>
  <p style="font-size:15px;margin:0 0 16px;">Hi {first},</p>
  <p style="font-size:15px;margin:0 0 24px;">{inviter_company} has invited you to join their Blue Alpha quote portal account. Click the button below to set up your username and password.</p>
  <a href="{setup_link}" style="display:inline-block;background:#1B2438;color:#fff;font-family:Arial;font-size:15px;font-weight:700;text-decoration:none;padding:14px 36px;border-radius:6px;">Set Up My Account →</a>
  <p style="font-size:12px;color:#888;margin-top:24px;">This link expires in 48 hours. If you weren't expecting this invitation, you can safely ignore this email.</p>
</div>"""}],
        }
        req_lib.post("https://api.sendgrid.com/v3/mail/send",
                     headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                     json=payload, timeout=10)
    except Exception as e:
        print(f"[portal_invite_email] failed: {e}")


def _generate_portal_invite(record_id, write_token):
    """Generate an invite token for a sub-user and store it in Airtable. Returns setup URL."""
    import secrets
    from datetime import datetime, timezone, timedelta
    token = secrets.token_urlsafe(32)
    expiry = (datetime.now(timezone.utc) + timedelta(hours=48)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    req_lib.patch(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
        headers={**at_headers(write_token), "Content-Type": "application/json"},
        json={"fields": {"Magic Token": token, "Token Expiry": expiry}},
        timeout=10,
    )
    return f"{QUOTE_BASE_URL}/setup-account/{token}"


@app.route("/api/portal/team/add", methods=["POST"])
@portal_login_required
def portal_team_add(user):
    """Add a new team member (sub-user). Sends invite email — sub-user sets own password."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data        = request.get_json() or {}
    first_name  = (data.get("firstName") or "").strip()
    last_name   = (data.get("lastName") or "").strip()
    email       = (data.get("email") or "").strip().lower()
    role        = (data.get("role") or "").strip()

    if not first_name or not last_name or not email:
        return Response(json.dumps({"error": "Please fill in all fields (first name, last name, and email)."}),
                        status=400, headers=c, mimetype="application/json")
    import re as _re
    if not _re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return Response(json.dumps({"error": "Please enter a valid email address."}),
                        status=400, headers=c, mimetype="application/json")
    if role not in ("Full Access", "Quotes Only", "Read Only"):
        return Response(json.dumps({"error": "Role must be Full Access, Quotes Only, or Read Only"}),
                        status=400, headers=c, mimetype="application/json")

    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch company name for invite email
        company_name = ""
        try:
            pr = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                             headers=at_headers(read_token), timeout=10)
            company_name = pr.json().get("fields", {}).get("Organization Name", "Blue Alpha")
        except Exception:
            company_name = "Blue Alpha"

        # Create sub-user record (no username/password yet — pending setup)
        fields = {
            "Main Contact Name":  f"{first_name} {last_name}",
            "Main Contact Email": email,
            "Portal Role":        role,
            "Parent Company":     [customer_id],
            "Application Status": "Approved",
        }
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": fields}, timeout=15,
        )
        if not r.ok:
            print(f"[portal_team_add] Airtable error {r.status_code}: {r.text[:300]}")
            return Response(json.dumps({"error": "Failed to create team member. Please try again."}),
                            status=500, headers=c, mimetype="application/json")
        new_id = r.json().get("id", "")

        # Generate invite token and send email
        setup_link = _generate_portal_invite(new_id, write_token)
        _send_portal_invite_email(email, f"{first_name} {last_name}", company_name, setup_link)

        return Response(json.dumps({"ok": True, "id": new_id}), headers=c, mimetype="application/json")
    except Exception as e:
        print(f"[portal_team_add] exception: {e}")
        return Response(json.dumps({"error": "Something went wrong. Please try again."}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/resend-invite", methods=["POST"])
@portal_login_required
def portal_team_resend_invite(user, record_id):
    """Resend invite email to a sub-user who hasn't completed setup (no Password Hash)."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Verify record belongs to this account and has no password yet
        r = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
                        headers=at_headers(read_token), timeout=10)
        f = r.json().get("fields", {})
        if customer_id not in f.get("Parent Company", []):
            return Response(json.dumps({"error": "Not found"}), status=404, headers=c, mimetype="application/json")
        if f.get("Portal Hash"):
            return Response(json.dumps({"error": "User has already completed setup"}),
                            status=400, headers=c, mimetype="application/json")

        # Fetch company name
        company_name = "Blue Alpha"
        try:
            pr = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                             headers=at_headers(read_token), timeout=10)
            company_name = pr.json().get("fields", {}).get("Organization Name", "Blue Alpha")
        except Exception:
            pass

        setup_link = _generate_portal_invite(record_id, write_token)
        _send_portal_invite_email(f.get("Main Contact Email", ""), f.get("Main Contact Name", ""),
                                  company_name, setup_link)
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/role", methods=["PATCH"])
@portal_login_required
def portal_team_change_role(user, record_id):
    """Change a team member's role. Cannot change Admin's role."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data     = request.get_json() or {}
    new_role = (data.get("role") or "").strip()
    if new_role not in ("Full Access", "Quotes Only", "Read Only"):
        return Response(json.dumps({"error": "Role must be Full Access, Quotes Only, or Read Only"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        if customer_id not in parent_ids:
            return Response(json.dumps({"error": "Cannot change role of this user"}),
                            status=403, headers=c, mimetype="application/json")
        if (f.get("Portal Role") or "").strip() == "Admin":
            return Response(json.dumps({"error": "Cannot change Admin's role"}),
                            status=403, headers=c, mimetype="application/json")
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Portal Role": new_role}},
            timeout=10,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>/reset-password", methods=["POST"])
@portal_login_required
def portal_team_reset_password(user, record_id):
    """Reset a team member's password. Admins can reset any sub-user's password."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id  = user.get("customer_id", "")
    data         = request.get_json() or {}
    new_password = (data.get("newPassword") or "").strip()
    if len(new_password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        # Allow if this is the primary record or a sub-user of this customer
        is_primary_record = (record_id == customer_id)
        is_sub_user = (customer_id in parent_ids)
        if not is_primary_record and not is_sub_user:
            return Response(json.dumps({"error": "Cannot reset password for this user"}),
                            status=403, headers=c, mimetype="application/json")
        new_hash = _hash_password(new_password)
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Portal Hash": new_hash}},
            timeout=10,
        )
        pr.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/team/<record_id>", methods=["DELETE"])
@portal_login_required
def portal_team_delete(user, record_id):
    """Delete a sub-user. Cannot delete the primary account or Admin users."""
    c = cors()
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    if record_id == customer_id:
        return Response(json.dumps({"error": "Cannot delete the primary account"}),
                        status=400, headers=c, mimetype="application/json")
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "User not found"}), status=404,
                            headers=c, mimetype="application/json")
        f = r.json().get("fields", {})
        parent_ids = f.get("Parent Company", [])
        if customer_id not in parent_ids:
            return Response(json.dumps({"error": "Cannot delete this user"}),
                            status=403, headers=c, mimetype="application/json")
        if (f.get("Portal Role") or "").strip() == "Admin":
            return Response(json.dumps({"error": "Cannot delete Admin users"}),
                            status=403, headers=c, mimetype="application/json")
        dr = req_lib.delete(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{record_id}",
            headers=at_headers(RETURNS_WRITE_TOKEN), timeout=10,
        )
        if dr.status_code not in (200, 204):
            return Response(json.dumps({"error": "Failed to delete user"}),
                            status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Customer Portal — Quote Actions (duplicate, hide)
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/api/portal/account-info", methods=["GET", "PATCH"])
@portal_login_required
def portal_account_info(user):
    c = cors()
    customer_id = user.get("customer_id", "")
    if not customer_id:
        return Response(json.dumps({"error": "No customer"}), status=400, headers=c, mimetype="application/json")

    if request.method == "GET":
        try:
            token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
                headers=at_headers(token),
                timeout=10,
            )
            f = {}
            if r.status_code == 200:
                f = r.json().get("fields", {})
            else:
                print(f"[account_info] AT fetch failed: id={customer_id} status={r.status_code} body={r.text[:300]}")
            return Response(json.dumps({"info": {
                "shipOrg":   f.get("Organization Name", ""),
                "shipName":  f.get("Main Contact Name", ""),
                "shipEmail": f.get("Main Contact Email", ""),
                "shipPhone": f.get("Main Contact Phone #", ""),
                "shipAddr1": f.get("Customer Address (Line 1)", ""),
                "shipAddr2": f.get("Customer Address (Line 2)", ""),
                "billOrg":   f.get("Bill-To Org Name", ""),
                "billName":  f.get("Bill-To Contact Name", ""),
                "billEmail": f.get("Bill-To Contact Email", ""),
                "billPhone": f.get("Bill-To Phone #", ""),
                "billAddr1": f.get("Bill-To Address (Line 1)", ""),
                "billAddr2": f.get("Bill-To Address (Line 2)", ""),
            }}), headers=c, mimetype="application/json")
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    # PATCH
    if not portal_can(user, "manage_team"):
        return Response(json.dumps({"error": "Admin access required"}), status=403, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    _MAP = {
        "shipOrg":   "Organization Name",      "shipName":  "Main Contact Name",
        "shipEmail": "Main Contact Email",      "shipPhone": "Main Contact Phone #",
        "shipAddr1": "Customer Address (Line 1)", "shipAddr2": "Customer Address (Line 2)",
        "billOrg":   "Bill-To Org Name",        "billName":  "Bill-To Contact Name",
        "billEmail": "Bill-To Contact Email",   "billPhone": "Bill-To Phone #",
        "billAddr1": "Bill-To Address (Line 1)", "billAddr2": "Bill-To Address (Line 2)",
    }
    fields = {at_f: data[k] for k, at_f in _MAP.items() if k in data}
    if not fields:
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{customer_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": fields}, timeout=15,
        )
        r.raise_for_status()
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/update-quote-addresses/<record_id>", methods=["POST"])
@portal_login_required
def portal_update_quote_addresses(user, record_id):
    """Update billing/shipping addresses on a quote's customer record without touching expiry or prices."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    data = request.get_json() or {}
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        # Verify quote belongs to this customer
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields   = r.json().get("fields", {})
        cust_ids    = mo_fields.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Not authorized"}), status=403, headers=c, mimetype="application/json")
        cust_id = cust_ids[0]

        # Build update fields — bill-to and ship-to address only; no expiry/price changes
        fields = {}
        if data.get("billOrg"):   fields["Bill-To Org Name"]            = data["billOrg"]
        if data.get("billName"):  fields["Bill-To Contact Name"]         = data["billName"]
        if "billAddr1" in data:   fields["Bill-To Address (Line 1)"]     = data["billAddr1"]
        if "billAddr2" in data:   fields["Bill-To Address (Line 2)"]     = data["billAddr2"]
        if data.get("shipOrg"):   fields["Organization Name"]            = data["shipOrg"]
        if data.get("shipName"):  fields["Main Contact Name"]            = data["shipName"]
        if "shipAddr1" in data:   fields["Customer Address (Line 1)"]    = data["shipAddr1"]
        if "shipLine2" in data:   fields["Customer Address (Line 2)"]    = data["shipLine2"]

        if fields:
            wr = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{cust_id}",
                headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
            wr.raise_for_status()

        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/duplicate-quote/<record_id>", methods=["POST"])
@portal_login_required
def portal_duplicate_quote(user, record_id):
    """Duplicate an expired quote with current catalog prices. Requires create_quote permission."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}),
                        status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    from datetime import date as dt_date, timedelta
    token      = RETURNS_WRITE_TOKEN
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch original quote
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        mo     = r.json()
        fields = mo.get("fields", {})
        if fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400,
                            headers=c, mimetype="application/json")
        cust_ids = fields.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")

        li_ids = fields.get("MO Line Items", [])

        # Get next order ID
        order_id_str = _next_order_id(read_token)
        quote_number = f"QU-{order_id_str}"

        today      = _today_utc()
        today_str  = today.isoformat()
        expiry_str = (today + timedelta(days=90)).isoformat()

        # Create new Manual Order
        mo_body = {"fields": {
            "Order Type":  "Quote",
            "Order ID":    order_id_str,
            "Date":        today_str,
            "Expiry Date": expiry_str,
            "Customer":    [customer_id],
        }}
        if fields.get("Notes from Customer"):
            mo_body["fields"]["Notes from Customer"] = fields["Notes from Customer"]
        if fields.get("Purchase Order #"):
            mo_body["fields"]["Purchase Order #"] = fields["Purchase Order #"]

        mo_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=mo_body, timeout=15,
        )
        mo_r.raise_for_status()
        new_mo_id = mo_r.json()["id"]

        # Fetch all line items and SKU prices in parallel
        import concurrent.futures as _cf
        def _fetch_li_dup(li_id):
            try:
                r2 = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li_id}",
                                  headers=at_headers(read_token), timeout=10)
                if r2.status_code == 200:
                    return r2.json().get("fields", {})
            except Exception: pass
            return {}
        with _cf.ThreadPoolExecutor(max_workers=20) as ex:
            li_fields_list = list(ex.map(_fetch_li_dup, li_ids))

        sku_ids_to_fetch = [lf.get("Product SKU", [None])[0] for lf in li_fields_list if lf.get("Product SKU")]
        def _fetch_sku_price(sku_id):
            try:
                r2 = req_lib.get(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{sku_id}",
                                  headers=at_headers(read_token), timeout=10)
                if r2.status_code == 200:
                    return sku_id, r2.json().get("fields", {}).get("Sale Price", 0)
            except Exception: pass
            return sku_id, 0
        sku_prices = {}
        if sku_ids_to_fetch:
            with _cf.ThreadPoolExecutor(max_workers=20) as ex:
                for sid, price in ex.map(_fetch_sku_price, sku_ids_to_fetch):
                    sku_prices[sid] = price

        for lf in li_fields_list:
            sku_ids = lf.get("Product SKU", [])
            if not sku_ids: continue
            sku_id        = sku_ids[0]
            qty           = int(lf.get("Qty.", 1) or 1)
            current_price = float(sku_prices.get(sku_id) or lf.get("Confirmed Unit Price", 0))
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {"Manual Order": [new_mo_id], "Product SKU": [sku_id],
                                 "Qty.": qty, "Confirmed Unit Price": current_price}}, timeout=15,
            ).raise_for_status()

        # Bust per-customer quotes cache
        _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)

        return Response(json.dumps({"success": True, "quoteNumber": quote_number, "recordId": new_mo_id}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/hide-quote/<record_id>", methods=["POST"])
@portal_login_required
def portal_hide_quote(user, record_id):
    """Mark a quote as Hidden from Customer (soft delete from customer view)."""
    c = cors()
    if not portal_can(user, "create_quote"):
        return Response(json.dumps({"error": "Insufficient permissions"}), status=403, headers=c, mimetype="application/json")
    customer_id = user.get("customer_id", "")
    token      = RETURNS_WRITE_TOKEN
    read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        f        = r.json().get("fields", {})
        cust_ids = f.get("Customer", [])
        if customer_id not in cust_ids:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")
        pr = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json={"fields": {"Hidden from Customer": True}},
            timeout=10,
        )
        pr.raise_for_status()
        _QUOTES_CACHE.pop(customer_id, None)
        _ORDERS_CACHE.pop(customer_id, None)
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# Admin Routes
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/admin")
def admin_page():
    return send_from_directory("static", "admin.html")

@app.route("/cs-status")
def cs_status_page():
    return send_from_directory("static", "cs-status.html")

@app.route("/api/cs-status/applications", methods=["GET", "POST", "OPTIONS"])
def cs_status_applications():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET, POST"})
    c = cors()
    # Accept session-based auth (admin or CS role)
    if not get_portal_role(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name", "Main Contact Name", "Main Contact Email",
                    "Application Status", "Applied Date"],
        )
        apps = []
        for r in records:
            f = r.get("fields", {})
            apps.append({
                "id":           r["id"],
                "orgName":      f.get("Organization Name", ""),
                "contactName":  f.get("Main Contact Name", ""),
                "contactEmail": f.get("Main Contact Email", ""),
                "status":       f.get("Application Status", "Pending"),
                "appliedDate":  f.get("Applied Date", ""),
            })
        # Sort: Pending first, then Approved, then Denied
        order = {"Pending": 0, "Approved": 1, "Denied": 2}
        apps.sort(key=lambda x: (order.get(x["status"], 9), x["appliedDate"]))
        return Response(json.dumps({"applications": apps}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users", methods=["GET"])
def admin_list_users():
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        formula = "OR({Quote Portal Admin}=1,{Quote Portal CS}=1)"
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": formula,
                    "fields[]": ["Full Name", "Portal Username", "Email", "Quote Portal Admin", "Quote Portal CS"]},
            timeout=10,
        )
        records = r.json().get("records", [])
        users = []
        for rec in records:
            f = rec.get("fields", {})
            users.append({
                "id":       rec["id"],
                "username": f.get("Portal Username", ""),
                "fullName": f.get("Full Name", ""),
                "email":    f.get("Email", ""),
                "role":     "admin" if f.get("Quote Portal Admin") else "cs",
            })
        return Response(json.dumps({"users": users}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/add", methods=["POST"])
def admin_add_user():
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    username  = (data.get("username") or "").strip().lower()
    password  = (data.get("password") or "").strip()
    role      = (data.get("role") or "cs").strip()
    first_name = (data.get("firstName") or "").strip()
    last_name  = (data.get("lastName") or "").strip()
    if not username or not password:
        return Response(json.dumps({"error": "username and password are required"}), status=400, headers=c, mimetype="application/json")
    if role not in ("admin", "cs"):
        return Response(json.dumps({"error": "role must be 'admin' or 'cs'"}), status=400, headers=c, mimetype="application/json")
    if len(password) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}), status=400, headers=c, mimetype="application/json")
    pw_hash = _hash_password(password)
    fields = {
        "Portal Username":    username,
        "Portal Hash":      pw_hash,
        "Quote Portal Admin": role == "admin",
        "Quote Portal CS":    role == "cs",
    }
    if first_name:
        fields["First Name"] = first_name
    if last_name:
        fields["Last Name"] = last_name
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    write_token = RETURNS_WRITE_TOKEN
    try:
        # Check if an employee with this username already exists — update instead of creating
        existing = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
            headers=at_headers(read_token),
            params={"filterByFormula": f"{{Portal Username}}='{username}'", "maxRecords": 1},
            timeout=10,
        )
        existing_records = existing.json().get("records", []) if existing.status_code == 200 else []
        if existing_records:
            record_id = existing_records[0]["id"]
            r = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
        else:
            r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": fields},
                timeout=15,
            )
        if r.status_code not in (200, 201):
            return Response(json.dumps({"error": f"Airtable error: {r.text}"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True, "id": r.json().get("id")}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/reset-password/<record_id>", methods=["POST"])
def admin_reset_user_password(record_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    data = request.get_json() or {}
    new_pw = (data.get("newPassword") or "").strip()
    if len(new_pw) < 8:
        return Response(json.dumps({"error": "Password must be at least 8 characters"}), status=400, headers=c, mimetype="application/json")
    new_hash = _hash_password(new_pw)
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Portal Hash": new_hash}},
            timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/users/<record_id>", methods=["DELETE"])
def admin_remove_user(record_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    write_token = RETURNS_WRITE_TOKEN
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {
                "Portal Username":    "",
                "Portal Hash":      "",
                "Quote Portal Admin": False,
                "Quote Portal CS":    False,
            }},
            timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to remove portal access"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/portal/change-password", methods=["POST"])
def portal_change_password():
    """Change password for B2B customers OR admin/CS users."""
    c = cors()
    data       = request.get_json() or {}
    current_pw = (data.get("currentPassword") or "").strip()
    new_pw     = (data.get("newPassword") or "").strip()
    if not current_pw or not new_pw:
        return Response(json.dumps({"error": "currentPassword and newPassword are required"}), status=400, headers=c, mimetype="application/json")
    if len(new_pw) < 8:
        return Response(json.dumps({"error": "New password must be at least 8 characters"}), status=400, headers=c, mimetype="application/json")

    # ── B2B customer path (ba_portal_session) ──────────────────────────────
    customer_user = get_portal_user(request)
    if customer_user:
        user_id = customer_user.get("user_id", "")
        if not user_id:
            return Response(json.dumps({"error": "Session missing user info — please log in again"}), status=401, headers=c, mimetype="application/json")
        read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        write_token = APPLY_WRITE_TOKEN or RETURNS_WRITE_TOKEN
        try:
            # Fetch current hash from Customers table
            cr = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers=at_headers(read_token), timeout=10,
            )
            if cr.status_code != 200:
                return Response(json.dumps({"error": "Could not verify identity"}), status=500, headers=c, mimetype="application/json")
            stored_hash = cr.json().get("fields", {}).get("Portal Hash", "")
            if not stored_hash or not _check_password(current_pw, stored_hash):
                return Response(json.dumps({"error": "Current password is incorrect"}), status=401, headers=c, mimetype="application/json")
            new_hash = _hash_password(new_pw)
            pr = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{user_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {"Portal Hash": new_hash}}, timeout=10,
            )
            if pr.status_code != 200:
                return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
            return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
        except Exception as e:
            return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")

    # ── Admin / CS path (ba_admin_session) ────────────────────────────────
    role = get_portal_role(request)
    if not role:
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")
    record_id = get_portal_record_id(request)
    username  = get_portal_username(request)
    if not record_id or not username:
        return Response(json.dumps({"error": "Session missing record info — please log in again"}), status=401, headers=c, mimetype="application/json")
    rec, _ = _lookup_portal_user(username, current_pw)
    if not rec:
        return Response(json.dumps({"error": "Current password is incorrect"}), status=401, headers=c, mimetype="application/json")
    new_hash = _hash_password(new_pw)
    try:
        r = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{EMPLOYEES_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {"Password Hash": new_hash}}, timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Failed to update password"}), status=500, headers=c, mimetype="application/json")
        return Response(json.dumps({"ok": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    from datetime import datetime, timezone, timedelta
    c = cors()
    data = request.get_json() or {}
    username = (data.get("username") or "").strip().lower()
    pw       = (data.get("password") or "").strip()
    rec, role = _lookup_portal_user(username, pw)
    if not rec:
        return Response(json.dumps({"error": "Invalid username or password"}), status=401, headers=c, mimetype="application/json")

    payload = {
        "admin":    role == 'admin',  # backward compat: approve/deny checks use this
        "role":     role,             # 'admin' or 'cs'
        "username": username,
        "name":     rec.get("fields", {}).get("Full Name", username),
        "record_id": rec.get("id"),
        "exp":      datetime.now(timezone.utc) + timedelta(hours=12),
    }
    token = pyjwt.encode(payload, QUOTE_SECRET_KEY + "_admin", algorithm="HS256")
    resp = make_response(Response(json.dumps({"ok": True, "name": payload["name"], "role": role}), headers=c, mimetype="application/json"))
    resp.set_cookie("ba_admin_session", token, max_age=12*3600, httponly=True, samesite="Lax", secure=True)
    return resp


@app.route("/api/admin/change-password", methods=["POST"])
def admin_change_password():
    """Change password for the logged-in portal user (admin or CS). Kept for backward compat."""
    return portal_change_password()


@app.route("/api/admin/applications")
def admin_applications():
    c = cors()
    if not get_portal_role(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    try:
        read_token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            CUSTOMERS_TABLE_ID, read_token,
            fields=["Organization Name", "EIN", "Main Contact Name", "Main Contact Email",
                    "Main Contact Phone #", "Website",
                    "Customer Address (Line 1)", "Customer City", "Customer State", "Customer Zip Code",
                    "Bill-To Contact Name", "Bill-To Contact Email", "Bill-To Phone #",
                    "Bill-To Address (Line 1)", "Bill-To Address (Line 2)",
                    "State Tax Exemption #", "Tax Exempt", "Tax Exemption Certificate",
                    "Application Status", "Denial Reason", "Applied Date"],
            formula="NOT({Application Status}='')",
        )
        apps = []
        for r in records:
            f = r.get("fields", {})
            apps.append({
                "id":                    r["id"],
                "company_name":          f.get("Organization Name", ""),
                "ein":                   f.get("EIN", ""),
                "business_phone":        f.get("Main Contact Phone #", ""),
                "website":               f.get("Website", ""),
                "shipping_contact_name":  f.get("Main Contact Name", ""),
                "shipping_contact_email": f.get("Main Contact Email", ""),
                "shipping_contact_phone": f.get("Main Contact Phone #", ""),
                "shipping_addr1":         f.get("Customer Address (Line 1)", ""),
                "shipping_city":          f.get("Customer City", ""),
                "shipping_state":         f.get("Customer State", ""),
                "shipping_zip":           f.get("Customer Zip Code", ""),
                "billing_contact_name":  f.get("Bill-To Contact Name", ""),
                "billing_contact_email": f.get("Bill-To Contact Email", ""),
                "billing_contact_phone": f.get("Bill-To Phone #", ""),
                "billing_addr1":         f.get("Bill-To Address (Line 1)", ""),
                "billing_addr2":         f.get("Bill-To Address (Line 2)", ""),
                "tax_exemption_number":  f.get("State Tax Exemption #", ""),
                "tax_cert_url":          (f.get("Tax Exemption Certificate") or [{}])[0].get("url", ""),
                "tax_cert_filename":     (f.get("Tax Exemption Certificate") or [{}])[0].get("filename", ""),
                "status":                f.get("Application Status", "Pending"),
                "denial_reason":         f.get("Denial Reason", ""),
                "applied_date":          f.get("Applied Date", ""),
            })
        # Sort most recent first
        apps.sort(key=lambda x: x.get("applied_date", ""), reverse=True)
        return Response(json.dumps({"applications": apps}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/approve/<app_id>", methods=["POST"])
def admin_approve(app_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    try:
        # Fetch application
        # Fetch Customer record — app_id IS the Customer record ID
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Application not found."}), status=404, headers=c, mimetype="application/json")
        f = r.json().get("fields", {})

        company_name  = f.get("Organization Name", "")
        contact_name  = f.get("Main Contact Name", "")
        contact_email = f.get("Main Contact Email", "")

        # Update Application Status to Approved on the same Customer record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Application Status": "Approved"}},
            timeout=10,
        )

        # Generate magic link (48hr for first login) — writes to Customer record
        magic_link = generate_magic_link(app_id, expiry_hours=48)

        # Send approval email
        send_approval_email(contact_email, contact_name, company_name, magic_link)

        return Response(json.dumps({"success": True, "customerId": app_id}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/admin/deny/<app_id>", methods=["POST"])
def admin_deny(app_id):
    c = cors()
    if not check_admin_session(request):
        return Response(json.dumps({"error": "Unauthorized"}), status=401, headers=c, mimetype="application/json")

    data   = request.get_json() or {}
    reason = (data.get("reason") or "").strip()
    if not reason:
        return Response(json.dumps({"error": "Denial reason is required."}), status=400, headers=c, mimetype="application/json")

    write_token = RETURNS_WRITE_TOKEN
    read_token  = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

    try:
        # Fetch Customer record — app_id IS the Customer record ID
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers=at_headers(read_token), timeout=10,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Application not found."}), status=404, headers=c, mimetype="application/json")
        f = r.json().get("fields", {})

        contact_name  = f.get("Main Contact Name", "")
        contact_email = f.get("Main Contact Email", "")
        company_name  = f.get("Organization Name", "")

        # Update Application Status to Denied on the Customer record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}/{app_id}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": {"Application Status": "Denied", "Denial Reason": reason}},
            timeout=10,
        )

        # Send denial email
        if contact_email:
            send_denial_email(contact_email, contact_name, company_name, reason)

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


# ─────────────────────────────────────────────────────────────────────────────
# International Exchange Portal
# ─────────────────────────────────────────────────────────────────────────────

@app.route("/exchange/international")
def international_exchange_portal():
    return send_from_directory("static", "exchange-international.html")


@app.route("/api/verify-international-exchange", methods=["POST", "OPTIONS"])
def verify_international_exchange():
    """Like verify-exchange but: 45-day window, no international/military block."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number    = data.get("orderNumber", "").strip().lstrip("#")
    email_input     = data.get("email", "").strip().lower()
    last_name_input = data.get("lastName", "").strip().lower()

    if not order_number or (not email_input and not last_name_input):
        return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

    try:
        from datetime import datetime, timezone, timedelta

        r = req_lib.get("https://ssapi.shipstation.com/orders",
                        params={"orderNumber": order_number},
                        headers=ss_headers(), timeout=10)
        orders = r.json().get("orders", [])

        if not orders:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        order = orders[0]

        # Verify identity — last name OR email must match
        ship_name   = order.get("shipTo", {}).get("name", "").strip()
        order_last  = ship_name.split()[-1].lower() if ship_name else ""
        order_email = (order.get("customerEmail") or "").strip().lower()
        name_match  = last_name_input and last_name_input == order_last
        email_match = email_input and email_input == order_email
        if not name_match and not email_match:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Block US domestic orders — they should use the domestic exchange form
        MILITARY_STATES = {"AA", "AE", "AP"}
        country = order.get("shipTo", {}).get("country", "").strip().upper()
        state   = order.get("shipTo", {}).get("state", "").strip().upper()
        if country in ("US", "USA") and state not in MILITARY_STATES:
            return Response(json.dumps({"status": "domestic"}), headers=c, mimetype="application/json")

        # Check ship date within 45 days
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                         params={"orderNumber": order_number},
                         headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        def parse_dt(s):
            s = s.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt

        if ship_date_str:
            ship_date = parse_dt(ship_date_str)
        else:
            od = order.get("orderDate", "")
            ship_date = parse_dt(od) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=45)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Find exchange-eligible items via Airtable
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        all_exchange_options = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        eligible_parent_ids = set()
        for opt in all_exchange_options:
            for pid in opt["fields"].get("Parent Product", []):
                eligible_parent_ids.add(pid)

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            at_r = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                params={"filterByFormula": f'{{SKU ID}}="{sku}"', "maxRecords": 1,
                        "fields[]": ["Name + Variations", "SKU ID", "Parent Product"]},
                headers=at_headers(airtable_read_token),
                timeout=10,
            )
            records = at_r.json().get("records", [])
            if not records:
                continue
            rec = records[0]
            parent_products = rec["fields"].get("Parent Product", [])
            parent_product_id = parent_products[0] if parent_products else ""
            if not parent_product_id or parent_product_id not in eligible_parent_ids:
                continue
            eligible_items.append({
                "name":            item.get("name", ""),
                "sku":             sku,
                "quantity":        int(item.get("quantity", 1)),
                "airtableId":      rec["id"],
                "parentProductId": parent_product_id,
            })

        if not eligible_items:
            return Response(json.dumps({"status": "no_eligible_items"}), headers=c, mimetype="application/json")

        # Check for existing exchange orders
        already_exchanged_skus = set()
        next_suffix = "-E"
        try:
            for n in range(1, 10):
                suffix = "-E" if n == 1 else f"-E{n}"
                ex_r = req_lib.get("https://ssapi.shipstation.com/orders",
                                   params={"orderNumber": f"{order_number}{suffix}"},
                                   headers=ss_headers(), timeout=10)
                ex_orders = ex_r.json().get("orders", [])
                if not ex_orders:
                    next_suffix = suffix
                    break
                for ex_order in ex_orders:
                    orig_sku = ((ex_order.get("advancedOptions") or {}).get("customField3") or "").strip()
                    if not orig_sku:
                        notes_text = ex_order.get("internalNotes") or ""
                        m = re.search(r'Original SKUs?:\s*([^\.\n]+)', notes_text)
                        if m:
                            orig_sku = m.group(1).strip()
                    for s in orig_sku.split(","):
                        s = s.strip()
                        if s:
                            already_exchanged_skus.add(s)
        except Exception as ex_check_err:
            print(f"[verify-international-exchange] exchange-order check failed (non-fatal): {ex_check_err}")

        eligible_items = [i for i in eligible_items if i["sku"] not in already_exchanged_skus]

        if not eligible_items:
            return Response(json.dumps({"status": "already_exchanged"}), headers=c, mimetype="application/json")

        ship_to = order.get("shipTo", {})
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderNumber":   order_number,
            "customerName":  ship_to.get("name", ""),
            "customerEmail": order.get("customerEmail", ""),
            "eligibleUntil": eligible_until.strftime("%B %-d, %Y"),
            "shipTo": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", "US"),
            },
            "eligibleItems": eligible_items,
            "nextSuffix":    next_suffix,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[verify-international-exchange] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/intl-exchange-options", methods=["POST", "OPTIONS"])
def intl_exchange_options():
    """Same logic as /api/exchange-options — reused for international flow."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    parent_product_id = data.get("parentProductId", "").strip()

    if not parent_product_id:
        return Response(json.dumps({"options": []}), headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        records = at_get_all(
            PRODUCT_SKUS_TABLE_ID,
            airtable_read_token,
            fields=["Name + Variations", "SKU ID", "Parent Product"],
            formula="{Can Exchange}=TRUE()",
        )
        options = []
        for rec in records:
            fields = rec.get("fields", {})
            if parent_product_id not in fields.get("Parent Product", []):
                continue
            raw_name = fields.get("Name + Variations", "")
            clean_name = raw_name.replace(" - Base Only (-ONB)", "").replace(" - Base Only", "").replace(" (-ONB)", "").strip()
            options.append({
                "id":   rec["id"],
                "name": clean_name,
                "sku":  fields.get("SKU ID", ""),
            })
        options.sort(key=lambda x: x["name"])
        return Response(json.dumps({"options": options}), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/create-international-checkout", methods=["POST", "OPTIONS"])
def create_international_checkout():
    """Store pending exchange data and create a Stripe Checkout session."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not STRIPE_SECRET_KEY:
        return Response(json.dumps({"error": "Stripe not configured"}),
                        status=500, headers=c, mimetype="application/json")

    import uuid
    ref_id = str(uuid.uuid4())

    # Store all exchange data keyed by ref_id (in-memory cache; Airtable is the source of truth)
    _intl_pending[ref_id] = {
        "orderId":       data.get("orderId"),
        "orderNumber":   data.get("orderNumber", ""),
        "customerName":  data.get("customerName", ""),
        "customerEmail": data.get("customerEmail", ""),
        "items":         data.get("items", []),        # list of {originalSku, selectedSku, selectedName, quantity, parentProductId}
        "deliveryAddress": data.get("deliveryAddress", {}),  # {name, street1, street2, city, state, postalCode, country}
        "nextSuffix":    data.get("nextSuffix", "-E"),
        "trackingNumber": data.get("trackingNumber", ""),
        "carrier":        data.get("carrier", ""),
    }

    # Clean up any abandoned pending records for this order
    try:
        order_num_int = int(data.get("orderNumber", ""))
        read_token = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f'AND({{Order #}}={order_num_int}, NOT({{Payment Confirmed}}))',
                "fields[]": ["Order #", "Payment Confirmed"],
                "maxRecords": 10,
            },
            headers=at_headers(read_token),
            timeout=10,
        )
        if search_resp.status_code == 200:
            stale = search_resp.json().get("records", [])
            print(f"[create-international-checkout] Found {len(stale)} stale record(s) to clean up for order {order_num_int}")
            for rec in stale:
                del_resp = req_lib.delete(
                    f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{rec['id']}",
                    headers=at_headers(write_token),
                    timeout=10,
                )
                if del_resp.status_code in (200, 204):
                    print(f"[create-international-checkout] Deleted stale record {rec['id']}")
                else:
                    print(f"[create-international-checkout] DELETE failed for {rec['id']}: {del_resp.status_code} {del_resp.text}")
        else:
            print(f"[create-international-checkout] Stale record search failed: {search_resp.status_code} {search_resp.text}")
    except Exception as cleanup_err:
        print(f"[create-international-checkout] cleanup error (non-fatal): {cleanup_err}")

    # Write to Airtable immediately so success handler survives redeploys
    try:
        tracking_str = f"{data.get('trackingNumber', '')} ({data.get('carrier', '')})" if data.get('carrier') else data.get('trackingNumber', '')
        items = data.get('items', [])
        delivery_addr = data.get('deliveryAddress', {})
        items_to_exchange = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('originalSku', '')} — {i.get('originalName', i.get('selectedName', ''))}"
            for i in items
        )
        desired_items = "\n".join(
            f"{i.get('quantity', 1)}x {i.get('selectedSku', '')} — {i.get('selectedName', '')}"
            for i in items
        )
        delivery_str = (
            f"{delivery_addr.get('name', '')}\n"
            f"{delivery_addr.get('street1', '')}"
            + (f"\n{delivery_addr['street2']}" if delivery_addr.get('street2') else "")
            + f"\n{delivery_addr.get('city', '')}, {delivery_addr.get('state', '')} {delivery_addr.get('postalCode', '')}\n"
            f"{delivery_addr.get('country', '')}"
        )
        at_fields = {
            "Customer Name":     data.get("customerName", ""),
            "Customer Email":    data.get("customerEmail", ""),
            "Items to Exchange": items_to_exchange,
            "Desired Items":     desired_items,
            "Delivery Address":  delivery_str,
            "Return Tracking #": tracking_str,
            "Stripe Payment ID": ref_id,
            "Original Order ID": str(data.get("orderId", "")) if data.get("orderId") else "",
            "Next Suffix":       data.get("nextSuffix", "-E"),
            "Payment Confirmed": False,
        }
        try:
            at_fields["Order #"] = int(data.get("orderNumber", ""))
        except (ValueError, TypeError):
            pass
        write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
        at_resp = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            headers={**at_headers(write_token), "Content-Type": "application/json"},
            json={"fields": at_fields},
            timeout=15,
        )
        if at_resp.status_code in (200, 201):
            _intl_pending[ref_id]["airtableRecordId"] = at_resp.json().get("id", "")
        else:
            print(f"[create-international-checkout] Airtable pre-write failed: {at_resp.status_code} {at_resp.text}")
    except Exception as at_err:
        print(f"[create-international-checkout] Airtable pre-write error: {at_err}")

    success_url = f"https://exchange.bluealphabelts.com/exchange/international?success=1&ref={ref_id}"
    cancel_url  = "https://exchange.bluealphabelts.com/exchange/international?cancelled=1"

    try:
        stripe_resp = req_lib.post(
            "https://api.stripe.com/v1/checkout/sessions",
            auth=(STRIPE_SECRET_KEY, ""),
            data={
                "line_items[0][price_data][currency]":                  "usd",
                "line_items[0][price_data][product_data][name]":        "International Belt Exchange Fee",
                "line_items[0][price_data][unit_amount]":               "1000",
                "line_items[0][quantity]":                              "1",
                "mode":                                                 "payment",
                "success_url":                                          success_url,
                "cancel_url":                                           cancel_url,
                "client_reference_id":                                  ref_id,
            },
            timeout=15,
        )
        if stripe_resp.status_code not in (200, 201):
            raise Exception(f"Stripe error {stripe_resp.status_code}: {stripe_resp.text}")

        session = stripe_resp.json()
        checkout_url = session.get("url")
        if not checkout_url:
            raise Exception("No checkout URL returned from Stripe")

        return Response(json.dumps({"url": checkout_url}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[create-international-checkout] ERROR: {e}\n{traceback.format_exc()}")
        # Clean up pending record on failure
        _intl_pending.pop(ref_id, None)
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/international-success", methods=["GET"])
def international_success():
    """
    Stripe redirects here after successful payment.
    Looks up the Airtable record by Stripe Payment ID (ref_id), sets the
    "Payment Confirmed" checkbox, sends a confirmation email, then redirects.
    Falls back to Airtable lookup if _intl_pending was wiped by a redeploy.
    """
    ref_id = request.args.get("ref", "").strip()

    if not ref_id:
        return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Resolve data from in-memory cache or Airtable ---
    airtable_record_id = None
    order_number   = ""
    customer_name  = ""
    customer_email = ""

    if ref_id in _intl_pending:
        # Fast path: still in memory
        pending = _intl_pending.pop(ref_id)
        airtable_record_id = pending.get("airtableRecordId", "")
        order_number   = pending.get("orderNumber", "")
        customer_name  = pending.get("customerName", "")
        customer_email = pending.get("customerEmail", "")
    else:
        # Slow path: redeploy wiped in-memory state — look up from Airtable
        try:
            import urllib.parse
            read_token = AIRTABLE_OPS_TOKEN or AIRTABLE_BASE_TOKEN or RETURNS_WRITE_TOKEN
            formula = urllib.parse.quote(f'{{Stripe Payment ID}}="{ref_id}"')
            lookup_resp = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}"
                f"?filterByFormula={formula}&maxRecords=1",
                headers=at_headers(read_token),
                timeout=15,
            )
            if lookup_resp.status_code == 200:
                records = lookup_resp.json().get("records", [])
                if not records:
                    print(f"[international-success] No Airtable record found for ref_id={ref_id}")
                    return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")
                rec = records[0]
                airtable_record_id = rec.get("id", "")
                fields = rec.get("fields", {})
                order_number   = str(fields.get("Order #", ""))
                customer_name  = fields.get("Customer Name", "")
                customer_email = fields.get("Customer Email", "")
            else:
                print(f"[international-success] Airtable lookup failed: {lookup_resp.status_code} {lookup_resp.text}")
                return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")
        except Exception as lookup_err:
            import traceback
            print(f"[international-success] Airtable lookup error: {lookup_err}\n{traceback.format_exc()}")
            return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Set "Payment Confirmed" ---
    # Skip GET idempotency check — token only has write access, not read.
    # PATCH is idempotent: setting true when already true is harmless.
    already_processed = False
    if airtable_record_id:
        try:
            write_token = os.environ.get("AIRTABLE_WRITE_TOKEN_2", RETURNS_WRITE_TOKEN)
            from datetime import datetime as _dt
            today_str = _dt.utcnow().strftime("%Y-%m-%d")
            patch_resp = req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{airtable_record_id}",
                headers={**at_headers(write_token), "Content-Type": "application/json"},
                json={"fields": {
                    "Payment Confirmed": True,
                    "Date Submitted": today_str,
                }},
                timeout=15,
            )
            print(f"[international-success] PATCH Payment Confirmed: {patch_resp.status_code} {patch_resp.text[:200]}")
            if patch_resp.status_code not in (200, 201):
                print(f"[international-success] Airtable PATCH failed: {patch_resp.status_code} {patch_resp.text}")
        except Exception as patch_err:
            import traceback
            print(f"[international-success] Airtable PATCH error: {patch_err}\n{traceback.format_exc()}")

    if already_processed:
        # Already handled (e.g. duplicate redirect) — just redirect
        return redirect("https://exchange.bluealphabelts.com/exchange/international?success=1")

    # --- Send confirmation email ---
    try:
        if SENDGRID_API_KEY and customer_email:
            first_name = customer_name.split()[0] if customer_name else "there"
            email_body = (
                f"Hi {first_name},\n\n"
                f"We've received your size exchange request for order #{order_number}.\n\n"
                f"We'll begin preparing your new belt(s) once we see movement on your shipment.\n\n"
                f"Please allow 2-4 weeks for international delivery.\n\n"
                f"Questions? Reply to this email.\n\n"
                f"— Blue Alpha"
            )
            req_lib.post(
                "https://api.sendgrid.com/v3/mail/send",
                headers={"Authorization": f"Bearer {SENDGRID_API_KEY}", "Content-Type": "application/json"},
                json={
                    "personalizations": [{"to": [{"email": TEST_EMAIL_OVERRIDE or customer_email}]}],
                    "from":    {"email": SENDGRID_FROM_EMAIL, "name": "Blue Alpha"},
                    "reply_to": {"email": SENDGRID_FROM_EMAIL},
                    "subject": f"Your Blue Alpha Size Exchange — Order #{order_number}",
                    "content": [{"type": "text/plain", "value": email_body}],
                },
                timeout=15,
            )
    except Exception as email_err:
        print(f"[international-success] Email failed: {email_err}")

    # If called via fetch() from the frontend, return JSON instead of redirect
    if request.headers.get("X-Requested-With") == "fetch" or "application/json" in request.headers.get("Accept", ""):
        return Response(json.dumps({"ok": True}), headers=cors(), mimetype="application/json")
    return redirect(f"https://exchange.bluealphabelts.com/exchange/international?success=1")


@app.route("/api/submit-international-tracking", methods=["POST", "OPTIONS"])
def submit_international_tracking():
    """Customer submits return tracking number for their international exchange."""
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number    = data.get("orderNumber", "").strip().lstrip("#")
    tracking_number = data.get("trackingNumber", "").strip()
    carrier         = data.get("carrier", "").strip()

    if not order_number or not tracking_number:
        return Response(json.dumps({"success": False, "error": "Missing order number or tracking number"}),
                        status=400, headers=c, mimetype="application/json")

    try:
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Find the Airtable record for this order number
        search_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}",
            params={
                "filterByFormula": f'{{Order Number}}="{order_number}"',
                "maxRecords": 1,
            },
            headers=at_headers(airtable_read_token),
            timeout=10,
        )
        records = search_resp.json().get("records", [])
        if not records:
            return Response(json.dumps({"success": False, "error": "No exchange request found for this order number"}),
                            headers=c, mimetype="application/json")

        record = records[0]
        record_id = record["id"]
        current_status = record.get("fields", {}).get("Status", "")

        # Only update if in a valid state
        if current_status not in ("Awaiting Return Shipment", "Tracking Submitted"):
            return Response(json.dumps({
                "success": False,
                "error": f"Cannot submit tracking — current status is '{current_status}'"
            }), headers=c, mimetype="application/json")

        # Update the record
        update_resp = req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Return Tracking #": tracking_number,
                "Return Carrier":    carrier,
                "Status":            "Tracking Submitted",
            }},
            timeout=10,
        )
        if update_resp.status_code not in (200, 201):
            raise Exception(f"Airtable update failed: {update_resp.status_code} {update_resp.text}")

        return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[submit-international-tracking] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/confirm-international-movement/<record_id>", methods=["POST", "OPTIONS"])
def confirm_international_movement(record_id):
    """
    CS-triggered endpoint: looks up the Airtable record, creates a ShipStation exchange order,
    and updates the record with the new exchange order number and status 'Order Created'.
    """
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()

    try:
        from datetime import datetime, timezone

        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # 1. Look up the AT record
        rec_resp = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers=at_headers(airtable_read_token),
            timeout=10,
        )
        if rec_resp.status_code == 404:
            return Response(json.dumps({"success": False, "error": "Record not found"}),
                            status=404, headers=c, mimetype="application/json")
        rec_resp.raise_for_status()
        record = rec_resp.json()
        fields = record.get("fields", {})

        order_number    = str(fields.get("Order #", "") or fields.get("Order Number", ""))
        customer_name   = fields.get("Customer Name", "")
        customer_email  = fields.get("Customer Email", "")
        desired_items_raw = fields.get("Desired Items", "")
        delivery_addr_raw = fields.get("Delivery Address", "")
        next_suffix     = fields.get("Next Suffix", "-E")
        original_order_id = fields.get("Original Order ID", "")
        date_submitted  = fields.get("Date Submitted", today_iso) or today_iso

        # Find next available suffix — skip any existing cancelled orders
        def _next_intl_suffix(base_order_num, start_suffix):
            idx = 1
            if start_suffix.startswith("-E") and len(start_suffix) > 2:
                try: idx = int(start_suffix[2:])
                except: idx = 1
            for attempt in range(idx, idx + 20):
                sfx = "-E" if attempt == 1 else f"-E{attempt}"
                candidate = f"{base_order_num}{sfx}"
                try:
                    r = req_lib.get("https://ssapi.shipstation.com/orders",
                                    params={"orderNumber": candidate, "pageSize": 5},
                                    headers=ss_headers(), timeout=10)
                    if r.status_code == 200:
                        orders = r.json().get("orders", [])
                        if not orders:
                            return sfx  # Unused — take it
                        active = [o for o in orders if o.get("orderStatus") != "cancelled"]
                        if not active:
                            return sfx  # Only cancelled orders — reuse this number
                        # Active order exists, try next suffix
                    else:
                        return sfx
                except Exception:
                    return sfx
            return f"-E{idx + 20}"

        next_suffix = _next_intl_suffix(order_number, next_suffix)
        exchange_order_number = f"{order_number}{next_suffix}"

        # 2. Parse desired items  (format: "1x SKU-123 — Item Name\n...")
        items_payload = []
        for line in desired_items_raw.strip().splitlines():
            line = line.strip()
            if not line:
                continue
            # e.g. "2x BA-SKU-XL — Blue Alpha Duty Belt XL Black"
            m = re.match(r'^(\d+)x\s+(\S+)\s+[—\-]+\s+(.+)$', line)
            if m:
                qty          = int(m.group(1))
                selected_sku = m.group(2)
                selected_name = m.group(3)
            else:
                # Fallback: try splitting on whitespace
                parts = line.split(None, 1)
                qty_str = parts[0].rstrip('x') if parts else "1"
                try:
                    qty = int(qty_str)
                except ValueError:
                    qty = 1
                selected_sku  = parts[0] if len(parts) > 0 else ""
                selected_name = parts[1] if len(parts) > 1 else line
            items_payload.append({
                "selectedSku":  selected_sku,
                "selectedName": selected_name,
                "quantity":     qty,
            })

        # 3. Parse delivery address (multi-line text field)
        addr_lines = [l.strip() for l in delivery_addr_raw.strip().splitlines() if l.strip()]
        ship_to = {
            "name":       addr_lines[0] if len(addr_lines) > 0 else customer_name,
            "street1":    addr_lines[1] if len(addr_lines) > 1 else "",
            "street2":    addr_lines[2] if len(addr_lines) > 2 else "",
            "city":       "",
            "state":      "",
            "postalCode": "",
            "country":    "",
        }
        # Try to parse "City, State PostalCode" line
        if len(addr_lines) >= 3:
            city_state_line = addr_lines[-2] if len(addr_lines) >= 4 else (addr_lines[2] if len(addr_lines) == 3 else "")
            country_line    = addr_lines[-1]
            # "city, state postalcode" pattern
            csz_m = re.match(r'^(.+),\s*(\S+)\s+(\S+)$', city_state_line)
            if csz_m:
                ship_to["city"]       = csz_m.group(1).strip()
                ship_to["state"]      = csz_m.group(2).strip()
                ship_to["postalCode"] = csz_m.group(3).strip()
            ship_to["country"] = country_line

        # 4. Build ShipStation order items (same LP inner logic as submit-exchange)
        airtable_rt = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN

        # Routing tag: EDC/LP vs Battle/Duty — based on item names
        all_names_lower = " ".join(i.get("selectedName", "") for i in items_payload).lower()
        if any(k in all_names_lower for k in ["edc", "low profile", "inner only", "1.5"]):
            tag_id = 105813
        else:
            tag_id = 102014

        today     = datetime.now(timezone.utc)
        today_iso = today.isoformat()

        LP_COLOR_MAP = [
            (["mc black", "mc tropic", "woodland", "black"], "Black"),
            (["coyote brown", "coyote", "mc australian", "mc arid"], "Coyote Brown"),
            (["mc classic", "multicam"],                             "Multicam"),
            (["ranger green", "ranger", "od green"],                 "OD Green"),
            (["wolf gray"],                                          "Wolf Gray"),
        ]

        def _lp_inner_item_intl(color, size, key_suffix):
            search_str    = f"LP INNER ONLY Belt {color} {size}"
            inner_formula = (
                f'AND(NOT(SEARCH("WPS",{{Name + Variations}})),'
                f'SEARCH("{search_str}",{{Name + Variations}}))'
            )
            recs = at_get_all(
                PRODUCT_SKUS_TABLE_ID, airtable_rt,
                fields=["Name + Variations", "SKU ID"],
                formula=inner_formula,
            )
            if recs:
                ir = recs[0]["fields"]
                return {
                    "lineItemKey":    key_suffix,
                    "name":          ir.get("Name + Variations", ""),
                    "sku":           ir.get("SKU ID", ""),
                    "quantity":      1,
                    "unitPrice":     0.00,
                    "taxAmount":     0.00,
                    "shippingAmount": 0.00,
                }
            return None

        def _extract_lp_color_size_intl(name):
            name_lower = name.lower()
            color = None
            for keywords, c_name in LP_COLOR_MAP:
                for kw in keywords:
                    if kw in name_lower:
                        color = c_name
                        break
                if color:
                    break
            size_matches = re.findall(r'(?<!\d)(\d{2})(?!\d)', name)
            size = next((s for s in size_matches if 24 <= int(s) <= 64), None)
            return color, size

        order_items = []
        for item_idx, item_data in enumerate(items_payload):
            selected_sku  = item_data.get("selectedSku", "")
            selected_name = item_data.get("selectedName", "")
            quantity      = int(item_data.get("quantity", 1))

            order_items.append({
                "lineItemKey":    f"exchange-{item_idx + 1}",
                "name":          selected_name,
                "sku":           selected_sku,
                "quantity":      quantity,
                "unitPrice":     0.00,
                "taxAmount":     0.00,
                "shippingAmount": 0.00,
            })

            # LP inner lookup (same three paths as domestic)
            selected_is_onb = bool(re.search(r'-ONB$', selected_sku, re.IGNORECASE))
            inner_added = False

            if selected_is_onb:
                try:
                    size_m     = re.search(r'-(\d+)-ONB$', selected_sku, re.IGNORECASE)
                    inner_size = size_m.group(1) if size_m else None
                    name_lower = selected_name.lower()
                    inner_color = None
                    for keywords, color in LP_COLOR_MAP:
                        for kw in keywords:
                            if kw in name_lower:
                                inner_color = color
                                break
                        if inner_color:
                            break
                    if inner_color and inner_size:
                        item = _lp_inner_item_intl(inner_color, inner_size,
                                                   f"exchange-{item_idx + 1}-inner")
                        if item:
                            item["quantity"] = quantity
                            order_items.append(item)
                            inner_added = True
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] ONB LP inner lookup failed: {lp_err}")

            elif not re.search(r'(-O)$', selected_sku, re.IGNORECASE):
                try:
                    combo_sku = re.sub(r'(-ONB|-O)$', '', selected_sku, flags=re.IGNORECASE).strip()
                    combo_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{combo_sku}"', "maxRecords": 1,
                                "fields[]": ["Component(s)"]},
                        headers=at_headers(airtable_rt), timeout=10
                    ).json().get("records", [])
                    component_ids = combo_recs[0]["fields"].get("Component(s)", []) if combo_recs else []
                    for comp_id in component_ids:
                        comp_rec    = req_lib.get(
                            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}/{comp_id}",
                            headers=at_headers(airtable_rt), timeout=10
                        ).json()
                        comp_fields = comp_rec.get("fields", {})
                        comp_name   = comp_fields.get("Name + Variations", "")
                        comp_sku    = comp_fields.get("SKU ID", "")
                        if "inner" not in comp_name.lower():
                            continue
                        order_items.append({
                            "lineItemKey":    f"exchange-{item_idx + 1}-inner",
                            "name":          comp_name,
                            "sku":           comp_sku,
                            "quantity":      quantity,
                            "unitPrice":     0.00,
                            "taxAmount":     0.00,
                            "shippingAmount": 0.00,
                        })
                        inner_added = True
                        break
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] Component LP inner lookup failed: {lp_err}")

            # Path 3: parent-based LP inner lookup
            if not inner_added:
                # We don't have parentProductId stored in Airtable, so look it up via SKU
                try:
                    sku_recs = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PRODUCT_SKUS_TABLE_ID}",
                        params={"filterByFormula": f'{{SKU ID}}="{selected_sku}"', "maxRecords": 1,
                                "fields[]": ["Parent Product"]},
                        headers=at_headers(airtable_rt), timeout=10
                    ).json().get("records", [])
                    item_parent_id = ""
                    if sku_recs:
                        parents = sku_recs[0]["fields"].get("Parent Product", [])
                        item_parent_id = parents[0] if parents else ""
                    if item_parent_id in _LP_INNER_REQUIRED_PARENT_IDS:
                        inner_color, inner_size = _extract_lp_color_size_intl(selected_name)
                        if inner_color and inner_size:
                            item = _lp_inner_item_intl(inner_color, inner_size,
                                                       f"exchange-{item_idx + 1}-inner")
                            if item:
                                item["quantity"] = quantity
                                order_items.append(item)
                                inner_added = True
                except Exception as lp_err:
                    print(f"[confirm-intl-movement] Path3 LP inner lookup failed: {lp_err}")

        original_skus_csv = fields.get("Items to Exchange", "")

        # 5. Create ShipStation order — GlobalPost Economy International
        order_payload = {
            "orderNumber":   exchange_order_number,
            "orderDate":     date_submitted,
            "paymentDate":   date_submitted,
            "orderStatus":   "awaiting_shipment",
            "customerEmail": customer_email,
            "billTo": {
                "name":       ship_to.get("name", customer_name),
                "street1":    ship_to.get("street1", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", ""),
            },
            "shipTo": {
                "name":       ship_to.get("name", customer_name),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
                "country":    ship_to.get("country", ""),
            },
            "items": order_items,
            "amountPaid":     0.00,
            "taxAmount":      0.00,
            "shippingAmount": 0.00,
            "carrierCode":    "stamps_com",
            "serviceCode":    "globalpost_economy",
            "internalNotes":  f"International exchange for order #{order_number}. Customer ships belt(s) to us first.",
            "advancedOptions": {
                "storeId":      SIZING_EXCHANGE_STORE_ID,
                "customField1": f"Intl exchange for order #{order_number}",
                "customField3": original_skus_csv,
            },
        }

        ss_resp = req_lib.post(
            "https://ssapi.shipstation.com/orders/createorder",
            headers={**ss_headers(), "Content-Type": "application/json"},
            json=order_payload,
            timeout=20,
        )
        if ss_resp.status_code not in (200, 201):
            raise Exception(f"ShipStation order creation failed: {ss_resp.status_code} {ss_resp.text}")

        new_order    = ss_resp.json()
        new_order_id = new_order.get("orderId")

        # Add routing tag
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": tag_id},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[confirm-intl-movement] Routing tag failed: {tag_err}")

        # Add Expedite tag
        try:
            req_lib.post(
                "https://ssapi.shipstation.com/orders/addtag",
                headers={**ss_headers(), "Content-Type": "application/json"},
                json={"orderId": new_order_id, "tagId": 49845},
                timeout=10,
            )
        except Exception as tag_err:
            print(f"[confirm-intl-movement] Expedite tag failed: {tag_err}")

        # 6. Update Airtable record
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{INT_EXCHANGE_TABLE_ID}/{record_id}",
            headers={**at_headers(RETURNS_WRITE_TOKEN), "Content-Type": "application/json"},
            json={"fields": {
                "Exchange Order #": exchange_order_number,
                "Review Status":    "Complete",
            }},
            timeout=10,
        )

        return Response(json.dumps({
            "success":             True,
            "exchangeOrderNumber": exchange_order_number,
            "shipStationOrderId":  new_order_id,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        import traceback
        print(f"[confirm-international-movement] ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/cron-intl-exchange", methods=["GET", "POST"])
def cron_intl_exchange():
    """
    Called by Make daily at 11 PM ET.
    Finds international exchange records where Movement Confirmed = true
    and Exchange Order # is blank, then creates ShipStation orders for each.
    """
    read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    results = []
    try:
        formula = 'AND({Movement Confirmed}=TRUE(), {Exchange Order #}="")'
        records = at_get_all(
            INT_EXCHANGE_TABLE_ID, read_token,
            fields=["Exchange Order #", "Customer Name"],
            formula=formula,
        )
        print(f"[cron-intl-exchange] {len(records)} record(s) to process")
        for rec in records:
            record_id = rec["id"]
            try:
                resp = req_lib.post(
                    f"https://exchange.bluealphabelts.com/api/confirm-international-movement/{record_id}",
                    timeout=30,
                )
                result = resp.json()
                results.append({"recordId": record_id, "success": result.get("success"),
                                 "order": result.get("exchangeOrderNumber"), "error": result.get("error")})
                print(f"[cron-intl-exchange] {record_id} → {result.get('exchangeOrderNumber')} ok={result.get('success')}")
            except Exception as rec_err:
                results.append({"recordId": record_id, "success": False, "error": str(rec_err)})
                print(f"[cron-intl-exchange] {record_id} ERROR: {rec_err}")
    except Exception as e:
        import traceback
        print(f"[cron-intl-exchange] OUTER ERROR: {e}\n{traceback.format_exc()}")
        return Response(json.dumps({"success": False, "error": str(e), "results": results}),
                        status=500, mimetype="application/json")
    return Response(json.dumps({"success": True, "processed": len(results), "results": results}),
                    mimetype="application/json")


def _ontime_bg_worker():
    """Refresh on-time cache on startup (if no data), then daily at 9 PM ET."""
    import time as _t
    from zoneinfo import ZoneInfo
    from datetime import timedelta
    _t.sleep(5)  # brief delay so app finishes starting up
    # Refresh on startup if we have no data yet
    if _ONTIME_CACHE["data"] is None:
        try:
            _refresh_ontime_cache()
        except Exception as exc:
            print(f'[ontime-bg] startup refresh error: {exc}')
    while True:
        try:
            et_tz  = ZoneInfo('America/New_York')
            now_et = datetime.now(et_tz)
            next_run = now_et.replace(hour=21, minute=0, second=0, microsecond=0)
            if now_et >= next_run:
                next_run += timedelta(days=1)
            sleep_secs = (next_run - now_et).total_seconds()
            print(f'[ontime-bg] sleeping {sleep_secs/3600:.1f}h until {next_run.strftime("%Y-%m-%d %H:%M ET")}')
            _t.sleep(sleep_secs)
            _refresh_ontime_cache()
        except Exception as exc:
            print(f'[ontime-bg] error: {exc}')
            _t.sleep(300)  # back off 5 min on error

threading.Thread(target=_ontime_bg_worker, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
