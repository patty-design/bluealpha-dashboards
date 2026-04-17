from flask import Flask, send_from_directory, abort, request, Response
import os
import re
import functools
import json
import base64
import threading
import requests as req_lib

_BUILD_VERSION = "no-formula-v1"  # bump to verify Railway deployment

AIRTABLE_OPS_TOKEN      = os.environ.get("AIRTABLE_OPS_TOKEN", "")
AIRTABLE_BASE_TOKEN     = os.environ.get("AIRTABLE_BASE_TOKEN", "")
AIRTABLE_WRITE_TOKEN    = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
RETURNS_WRITE_TOKEN     = os.environ.get("AIRTABLE_WRITE_TOKEN_2", AIRTABLE_WRITE_TOKEN)
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

MANUAL_ORDERS_TABLE_ID   = "tblOOZ2wVzIsR1DyL"
MO_LINE_ITEMS_TABLE_ID   = "tblNjwm5SRsfE38Xu"
CUSTOMERS_TABLE_ID       = "tblO4AdJE84kFDfEe"
PARENT_PRODUCTS_TABLE_ID = "tbl40th76YvjdQExS"
COLORS_TABLE_ID          = "tblN08IV26TpRYSMf"
SIZES_TABLE_ID           = "tblUGwl1YLaVGCeIJ"
QUOTE_BASE_URL           = os.environ.get("QUOTE_BASE_URL", "https://quote.bluealphabelts.com")

app = Flask(__name__, static_folder="static")

# In-memory status cache for return submissions (cleared on restart, only needed during ~60s poll window)
_return_status_cache = {}

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
    return Response(json.dumps({"v": _BUILD_VERSION, "base_token_set": bool(AIRTABLE_BASE_TOKEN)}),
                    mimetype="application/json")

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)

@app.route("/")
def index():
    from flask import redirect
    host = request.host.split(".")[0].lower()
    if host == "exchange":
        return redirect("/exchange")
    if host == "return":
        return send_from_directory("static", "returns.html")
    if host in DASHBOARDS:
        return dashboard(host)
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
    snap_date = body.get("date", dt_date.today().isoformat())
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

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

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
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        phone = (ship_to.get("phone") or (order.get("billTo") or {}).get("phone") or "")

        return Response(json.dumps({
            "status":       "found",
            "orderId":      order.get("orderId"),
            "orderKey":     order.get("orderKey", ""),
            "orderStatus":  order.get("orderStatus", ""),
            "customerName": ship_to.get("name", ""),
            "email":        order.get("customerEmail", ""),
            "phone":        phone,
            "shipDate":     ship_date_str,
            "address": {
                "name":       ship_to.get("name", ""),
                "street1":    ship_to.get("street1", ""),
                "street2":    ship_to.get("street2", ""),
                "city":       ship_to.get("city", ""),
                "state":      ship_to.get("state", ""),
                "postalCode": ship_to.get("postalCode", ""),
            },
            "items": items,
        }), headers=c, mimetype="application/json")

    except Exception as e:
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
    if ships:
        s = ships[0]
        carrier = s.get("carrierCode") or carrier
        service = s.get("serviceCode") or service
        if s.get("weight"):
            weight = s["weight"]

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

    # ── Kick off label generation + email in background ──────────────────
    def process_label(record_id, data, addr):
        try:
            # ── Create Return Items (combo-expanded checklist for CS) ─────────
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


@app.route("/api/mark-all-received/<record_id>")
def mark_all_received(record_id):
    """Mark all Return Items linked to a return record as Received, Qty Received = Qty Submitted."""
    if not RETURN_ITEMS_TABLE_ID or not AIRTABLE_OPS_TOKEN:
        return Response("<h2>Not configured</h2>", status=500, mimetype="text/html")
    try:
        # Fetch the return record to get linked Return Items record IDs
        ret_r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
            headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
            timeout=10,
        )
        item_ids = ret_r.json().get("fields", {}).get("Return Items", [])
        if not item_ids:
            return Response("<h2 style='font-family:sans-serif'>No items found for this return.</h2>",
                            status=404, mimetype="text/html")
        # Fetch each Return Item to get Qty Submitted and Item Name
        items = []
        for item_id in item_ids:
            ir = req_lib.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={"Authorization": f"Bearer {AIRTABLE_OPS_TOKEN}"},
                timeout=10,
            )
            items.append({"id": item_id, "fields": ir.json().get("fields", {})})
        # Patch each item
        updated = []
        for item in items:
            item_id = item["id"]
            qty_submitted = item.get("fields", {}).get("Qty Submitted", 1) or 1
            req_lib.patch(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURN_ITEMS_TABLE_ID}/{item_id}",
                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                json={"fields": {"Received": True, "Qty Received": qty_submitted}},
                timeout=10,
            )
            updated.append(item.get("fields", {}).get("Item Name", item_id))
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


@app.route("/api/awaiting")
def awaiting_shipment():
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
    count = 0
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

    result = {"count": count, "placedToday": placed_today}
    if placed_error:
        result["placedTodayError"] = placed_error

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

        eligible_items = []
        for item in order.get("items", []):
            sku = (item.get("sku") or "").strip()
            if not sku:
                continue
            # Look up the SKU without Can Exchange filter
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
            # Only eligible if parent has exchange options available
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

            if selected_is_onb:
                # ONB selection → find LP inner by color + size from selected belt name
                try:
                    # Extract size from SKU (e.g. MOL-RAN-36-ONB → 36)
                    size_m = re.search(r'-(\d+)-ONB$', selected_sku, re.IGNORECASE)
                    inner_size = size_m.group(1) if size_m else None

                    # Map belt color → LP inner color
                    COLOR_MAP = [
                        (["mc black", "mc tropic", "woodland", "black"], "Black"),
                        (["coyote brown", "mc australian", "mc arid"],   "Coyote Brown"),
                        (["mc classic", "multicam"],                      "Multicam"),
                        (["ranger green", "ranger", "od green"],          "OD Green"),
                        (["wolf gray"],                                    "Wolf Gray"),
                    ]
                    name_lower = selected_name.lower()
                    inner_color = None
                    for keywords, color in COLOR_MAP:
                        for kw in keywords:
                            if kw in name_lower:
                                inner_color = color
                                break
                        if inner_color:
                            break

                    if inner_color and inner_size:
                        search_str = f"LP INNER ONLY Belt {inner_color} {inner_size}"
                        inner_formula = (
                            f'AND(NOT(SEARCH("WPS",{{Name + Variations}})),'
                            f'SEARCH("{search_str}",{{Name + Variations}}))'
                        )
                        inner_recs = at_get_all(
                            PRODUCT_SKUS_TABLE_ID, airtable_read_token,
                            fields=["Name + Variations", "SKU ID"],
                            formula=inner_formula,
                        )
                        if inner_recs:
                            ir = inner_recs[0]["fields"]
                            order_items.append({
                                "lineItemKey":    f"exchange-{item_idx + 1}-inner",
                                "name":          ir.get("Name + Variations", ""),
                                "sku":           ir.get("SKU ID", ""),
                                "quantity":      quantity,
                                "unitPrice":     0.00,
                                "taxAmount":     0.00,
                                "shippingAmount": 0.00,
                            })
                        else:
                            print(f"[submit-exchange] No LP inner found for {inner_color} size {inner_size}")
                except Exception as lp_err:
                    print(f"[submit-exchange] ONB LP inner lookup failed for {selected_sku}: {lp_err}")

            elif not re.search(r'(-O)$', original_sku, re.IGNORECASE):
                # Full-combo selected belt → find inner via Component(s) in Airtable
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
                        break
                except Exception as lp_err:
                    print(f"[submit-exchange] Component LP inner lookup failed for {selected_sku}: {lp_err}")

        original_skus_csv = ",".join(i.get("originalSku", "") for i in items_payload)
        selected_names    = ", ".join(i.get("selectedName", "") for i in items_payload)

        # Create ShipStation exchange order
        order_payload = {
            "orderNumber":   exchange_order_number,
            "orderDate":     today_iso,
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

@app.route("/quote")
def quote_page():
    return send_from_directory("static", "quote.html")

@app.route("/view-quote/<record_id>")
def view_quote_page(record_id):
    return send_from_directory("static", "view-quote.html")


def _clean_product_name(name):
    """Strip marketing suffixes from product names."""
    for suffix in [" - Base Only (-ONB)", " - Base Only", " (-ONB)"]:
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.strip()


def send_quote_email(to_email, to_name, company, quote_number, record_id, expiry_date):
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
    token = RETURNS_WRITE_TOKEN

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
    if fields.get("Order Type") != "Quote":
        return None

    order_id     = fields.get("Order ID", "")
    quote_number = fields.get("Document ID", f"QU-{order_id}")
    date_str     = fields.get("Date", "")
    expiry_str   = fields.get("Expiry Date", "")
    is_accepted  = bool(fields.get("MO Is Approved", False))
    po_number    = fields.get("Purchase Order #", "")
    notes        = fields.get("Notes", "")

    today = dt_date.today()
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
            }

    # Fetch line items
    li_formula = f'FIND("{record_id}", ARRAYJOIN({{Manual Order}}))'
    li_records = at_get_all(
        MO_LINE_ITEMS_TABLE_ID, token,
        fields=["Manual Order", "Product SKU", "Qty.", "Adj. Unit Price",
                "Name + Variations (from Product SKU)", "SKU ID (from Product SKU)"],
        formula=li_formula,
    )
    line_items = []
    for li in li_records:
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
            "unitPrice":   lf.get("Adj. Unit Price", 0),
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


@app.route("/api/quote-catalog", methods=["GET", "OPTIONS"])
def quote_catalog():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "GET"})
    c = cors()
    # Use full-access token for catalog reads; write token is scoped only to Returns table
    token = AIRTABLE_BASE_TOKEN or AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
    try:
        # Fetch all SKUs (no formula — filter in Python to avoid encoding issues)
        sku_records_all = at_get_all(
            "tbljngm75r4Km2XIN", token,
            fields=["SKU ID", "Name + Variations", "Sale Price", "Parent Product", "Color", "Size", "Category"],
        )
        if not sku_records_all:
            tok_hint = (token[:8] + "…") if token else "(empty)"
            return Response(json.dumps({"error": f"No SKU records returned (token={tok_hint})"}),
                            status=500, headers=c, mimetype="application/json")
        # Keep only SKUs with a Sale Price and not in Contract category
        sku_records = [
            r for r in sku_records_all
            if r.get("fields", {}).get("Sale Price")
            and r.get("fields", {}).get("Category", "") != "Contract"
        ]
        parent_records = at_get_all(PARENT_PRODUCTS_TABLE_ID, token, fields=["Name"])
        color_records  = at_get_all(COLORS_TABLE_ID, token, fields=["Name"])
        size_records   = at_get_all(SIZES_TABLE_ID, token, fields=["Name"])

        parent_map = {r["id"]: r["fields"].get("Name", "") for r in parent_records}
        color_map  = {r["id"]: r["fields"].get("Name", "") for r in color_records}
        size_map   = {r["id"]: r["fields"].get("Name", "") for r in size_records}

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

            color_ids  = f.get("Color", [])
            size_ids   = f.get("Size", [])
            color_id   = color_ids[0] if color_ids else ""
            size_id    = size_ids[0]  if size_ids  else ""
            color_name = color_map.get(color_id, "")
            size_name  = size_map.get(size_id, "")

            raw_name = f.get("Name + Variations", "")
            clean_name = _clean_product_name(raw_name)

            category = f.get("Category", "") or ""
            skus.append({
                "recordId":   r["id"],
                "sku":        f.get("SKU ID", ""),
                "name":       clean_name,
                "price":      f.get("Sale Price", 0),
                "parentId":   parent_id,
                "parentName": parent_name,
                "colorId":    color_id,
                "colorName":  color_name,
                "sizeId":     size_id,
                "sizeName":   size_name,
                "category":   category,
            })
            if parent_id not in seen_parents:
                seen_parents[parent_id] = {"name": parent_name, "category": category}

        parents = sorted(
            [{"id": k, "name": v["name"], "category": v["category"]} for k, v in seen_parents.items()],
            key=lambda x: x["name"],
        )
        return Response(json.dumps({"parents": parents, "skus": skus}),
                        headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/api/create-quote", methods=["POST", "OPTIONS"])
def create_quote():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
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

    try:
        # 1. Find or create customer by email
        existing = at_get_all(
            CUSTOMERS_TABLE_ID, token,
            fields=["Main Contact Email", "Organization Name"],
            formula=f"{{Main Contact Email}}='{email}'",
        )
        if existing:
            cust_id = existing[0]["id"]
        else:
            cr = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CUSTOMERS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Organization Name":      org_name,
                    "Main Contact Name":      contact_name,
                    "Main Contact Email":     email,
                    "Main Contact Phone #":   phone,
                    "Customer Address (Line 1)": address1,
                    "Customer Address (Line 2)": address2,
                    "Customer City":          city,
                    "Customer State":         state,
                    "Customer Zip Code":      zip_code,
                }},
                timeout=15,
            )
            cr.raise_for_status()
            cust_id = cr.json()["id"]

        # 2. Get next order ID
        all_mos = at_get_all(MANUAL_ORDERS_TABLE_ID, token, fields=["Order ID"])
        max_id = 0
        for mo in all_mos:
            oid_str = mo["fields"].get("Order ID", "")
            try:
                val = int(oid_str)
                if val > max_id:
                    max_id = val
            except (ValueError, TypeError):
                pass
        next_id = max_id + 1
        order_id_str = str(next_id).zfill(4)
        quote_number = f"QU-{order_id_str}"

        today       = dt_date.today()
        expiry_date = today + timedelta(days=90)
        today_str   = today.isoformat()
        expiry_str  = expiry_date.isoformat()

        # 3. Create Manual Order
        mo_body = {
            "fields": {
                "Order Type":    "Quote",
                "Document ID":   quote_number,
                "Order ID":      order_id_str,
                "Date":          today_str,
                "Expiry Date":   expiry_str,
                "Customer":      [cust_id],
            }
        }
        if po_number:
            mo_body["fields"]["Purchase Order #"] = po_number
        if notes:
            mo_body["fields"]["Notes"] = notes

        mo_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=mo_body,
            timeout=15,
        )
        mo_r.raise_for_status()
        mo_record_id = mo_r.json()["id"]

        # 4. Create line items
        for item in items:
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order": [mo_record_id],
                    "Product SKU":  [item["skuRecordId"]],
                    "Qty.":         int(item["qty"]),
                    "Adj. Unit Price": float(item["unitPrice"]),
                }},
                timeout=15,
            )
            li_r.raise_for_status()

        # 5. Send email
        try:
            send_quote_email(email, contact_name or org_name, org_name,
                             quote_number, mo_record_id, expiry_str)
        except Exception as email_err:
            print(f"[create_quote] email failed: {email_err}")

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
    token = RETURNS_WRITE_TOKEN
    data = request.get_json() or {}
    items = data.get("items", [])

    try:
        # Verify it's a quote and not accepted
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})
        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Quote already accepted"}), status=400, headers=c, mimetype="application/json")

        # Delete existing line items
        li_formula = f'FIND("{record_id}", ARRAYJOIN({{Manual Order}}))'
        existing_lis = at_get_all(MO_LINE_ITEMS_TABLE_ID, token, fields=["Manual Order"], formula=li_formula)
        for li in existing_lis:
            req_lib.delete(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}/{li['id']}",
                headers=at_headers(token), timeout=10,
            )

        # Create new line items
        for item in items:
            li_r = req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order": [record_id],
                    "Product SKU":  [item["skuRecordId"]],
                    "Qty.":         int(item["qty"]),
                    "Adj. Unit Price": float(item["unitPrice"]),
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
    from datetime import date as dt_date
    token = RETURNS_WRITE_TOKEN

    try:
        # Fetch MO record
        r = req_lib.get(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers=at_headers(token), timeout=15,
        )
        if r.status_code != 200:
            return Response(json.dumps({"error": "Quote not found"}), status=404, headers=c, mimetype="application/json")
        mo_fields = r.json().get("fields", {})

        if mo_fields.get("Order Type") != "Quote":
            return Response(json.dumps({"error": "Not a quote"}), status=400, headers=c, mimetype="application/json")
        if mo_fields.get("MO Is Approved"):
            return Response(json.dumps({"error": "Already accepted"}), status=400, headers=c, mimetype="application/json")

        expiry_str = mo_fields.get("Expiry Date", "")
        if expiry_str:
            try:
                if dt_date.fromisoformat(expiry_str) < dt_date.today():
                    return Response(json.dumps({"error": "Quote has expired"}), status=400, headers=c, mimetype="application/json")
            except Exception:
                pass

        order_id_str = mo_fields.get("Order ID", "")
        quote_number = mo_fields.get("Document ID", f"QU-{order_id_str}")
        so_number    = f"SO-{order_id_str}"
        customer_ids = mo_fields.get("Customer", [])
        po_number    = mo_fields.get("Purchase Order #", "")
        notes        = mo_fields.get("Notes", "")
        date_str     = mo_fields.get("Date", dt_date.today().isoformat())

        # Create SO record
        so_body = {
            "fields": {
                "Order Type":                "Sales Order",
                "Document ID":               so_number,
                "Order ID":                  order_id_str,
                "Date":                      date_str,
                "MO Is Approved":            True,
                "Ready for ShipStation (SOs)": True,
                "Origin Quote":              quote_number,
            }
        }
        if customer_ids:
            so_body["fields"]["Customer"] = customer_ids
        if po_number:
            so_body["fields"]["Purchase Order #"] = po_number
        if notes:
            so_body["fields"]["Notes"] = notes

        so_r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json=so_body,
            timeout=15,
        )
        so_r.raise_for_status()
        so_record_id = so_r.json()["id"]

        # Copy line items from QU to SO
        li_formula = f'FIND("{record_id}", ARRAYJOIN({{Manual Order}}))'
        li_records = at_get_all(
            MO_LINE_ITEMS_TABLE_ID, token,
            fields=["Product SKU", "Qty.", "Adj. Unit Price"],
            formula=li_formula,
        )
        for li in li_records:
            lf = li["fields"]
            req_lib.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MO_LINE_ITEMS_TABLE_ID}",
                headers={**at_headers(token), "Content-Type": "application/json"},
                json={"fields": {
                    "Manual Order": [so_record_id],
                    "Product SKU":  lf.get("Product SKU", []),
                    "Qty.":         lf.get("Qty.", 0),
                    "Adj. Unit Price": lf.get("Adj. Unit Price", 0),
                }},
                timeout=15,
            )

        # PATCH QU: set MO Is Approved = true
        req_lib.patch(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{MANUAL_ORDERS_TABLE_ID}/{record_id}",
            headers={**at_headers(token), "Content-Type": "application/json"},
            json={"fields": {"MO Is Approved": True}},
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

        return Response(json.dumps({"success": True, "soNumber": so_number}), headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}), status=500, headers=c, mimetype="application/json")


@app.route("/quote-pdf/<record_id>", methods=["GET"])
def quote_pdf(record_id):
    c = cors()
    try:
        from fpdf import FPDF
        import io

        quote = _fetch_quote_data(record_id)
        if not quote:
            return Response(json.dumps({"error": "Quote not found"}), status=404,
                            headers=c, mimetype="application/json")

        # ── Build PDF with fpdf2 ────────────────────────────────────────────
        cust       = quote.get("customer", {})
        line_items = quote.get("lineItems", [])
        subtotal   = quote.get("subtotal", 0.0)
        shipping   = quote.get("shipping", 0.0)
        total      = quote.get("total",    0.0)
        q_number   = quote.get("quoteNumber", "")
        q_date     = quote.get("date", "")
        q_expiry   = quote.get("expiryDate", "")
        q_po       = quote.get("poNumber", "")
        q_notes    = quote.get("notes", "")

        bill_org  = cust.get("billToOrg")  or cust.get("orgName", "")
        bill_name = cust.get("billToName") or cust.get("contactName", "")
        addr1     = cust.get("address1", "")
        city      = cust.get("city", "")
        state_v   = cust.get("state", "")
        zip_v     = cust.get("zip", "")

        pdf = FPDF(orientation="P", unit="mm", format="letter")
        pdf.set_margins(19, 19, 19)
        pdf.set_auto_page_break(auto=True, margin=19)
        pdf.add_page()

        W = 177.0  # usable width (letter 215.9 - 2×19mm margins)

        # Navy / Red colours
        NAVY = (27,  36,  56)
        RED  = (189, 51,  51)
        MUTED = (107, 122, 141)
        TEXT  = (26,  38,  51)
        LG    = (245, 247, 250)
        BD    = (221, 227, 234)

        # ── Header ────────────────────────────────────────────────────────
        pdf.set_font("Helvetica", "B", 20)
        pdf.set_text_color(*NAVY)
        pdf.cell(W * 0.6, 10, "BLUE ALPHA", border=0, align="L")
        pdf.set_font("Helvetica", "B", 26)
        pdf.set_text_color(*RED)
        pdf.cell(W * 0.4, 10, "QUOTE", border=0, align="R", new_x="LMARGIN", new_y="NEXT")

        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(W, 6, "bluealphabelts.com  |  info@bluealpha.us  |  678-961-3304",
                 border=0, align="L", new_x="LMARGIN", new_y="NEXT")

        # Navy rule
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(0.7)
        pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
        pdf.ln(5)

        # ── Meta two-column ───────────────────────────────────────────────
        y_meta = pdf.get_y()
        col_w  = W / 2

        def meta_row(label, value):
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*MUTED)
            pdf.cell(28, 5.5, label, border=0)
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*TEXT)
            pdf.cell(col_w - 28, 5.5, str(value), border=0, new_x="LMARGIN", new_y="NEXT")

        meta_rows = [("Quote Number:", q_number), ("Date:", q_date),
                     ("Expires:", q_expiry), ("Terms:", "Net 30")]
        if q_po:
            meta_rows.append(("PO #:", q_po))

        for label, value in meta_rows:
            meta_row(label, value)

        # Bill To (right column)
        pdf.set_xy(19 + col_w, y_meta)
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_text_color(*MUTED)
        pdf.cell(col_w, 5.5, "Bill To", border=0, new_x="LEFT", new_y="NEXT")
        pdf.set_x(19 + col_w)

        for line in [bill_org, bill_name, addr1,
                     f"{city}, {state_v} {zip_v}".strip(", ")]:
            if line:
                pdf.set_font("Helvetica", "B" if line == bill_org else "", 9)
                pdf.set_text_color(*TEXT)
                pdf.set_x(19 + col_w)
                pdf.cell(col_w, 5.5, line, border=0, new_x="LEFT", new_y="NEXT")

        pdf.ln(6)

        # ── Line items table ──────────────────────────────────────────────
        col_widths = [W - 60, 14, 23, 23]  # Product, Qty, Unit Price, Total
        headers    = ["PRODUCT", "QTY", "UNIT PRICE", "TOTAL"]
        aligns     = ["L", "R", "R", "R"]
        row_h      = 7

        # Header row
        pdf.set_fill_color(*NAVY)
        pdf.set_text_color(255, 255, 255)
        pdf.set_font("Helvetica", "B", 8)
        for w, h, a in zip(col_widths, headers, aligns):
            pdf.cell(w, row_h, h, border=0, align=a, fill=True)
        pdf.ln()

        # Data rows
        pdf.set_font("Helvetica", "", 8)
        for idx, item in enumerate(line_items):
            fill = idx % 2 == 0
            pdf.set_fill_color(*LG)
            pdf.set_text_color(*TEXT)
            line_total = item["qty"] * item["unitPrice"]
            row_data = [item["name"], str(item["qty"]),
                        f"${item['unitPrice']:.2f}", f"${line_total:.2f}"]
            for w, cell, a in zip(col_widths, row_data, aligns):
                pdf.cell(w, row_h, cell, border=0, align=a, fill=fill)
            pdf.ln()

        pdf.ln(3)

        # ── Totals ────────────────────────────────────────────────────────
        def totals_row(label, value, bold=False):
            pdf.set_font("Helvetica", "B" if bold else "", 9)
            pdf.set_text_color(*TEXT if bold else MUTED)
            pdf.cell(W - 46, 6, "", border=0)
            pdf.set_text_color(*TEXT if bold else MUTED)
            pdf.cell(23, 6, label, border=0, align="R")
            pdf.set_text_color(*TEXT)
            pdf.cell(23, 6, value, border=0, align="R", new_x="LMARGIN", new_y="NEXT")

        totals_row("Subtotal", f"${subtotal:.2f}")
        if shipping > 0:
            totals_row("Shipping", f"${shipping:.2f}")
        # Rule above total
        y_rule = pdf.get_y()
        pdf.set_draw_color(*NAVY)
        pdf.set_line_width(0.4)
        pdf.line(19 + W - 46, y_rule, 19 + W, y_rule)
        pdf.ln(1)
        totals_row("Total", f"${total:.2f}", bold=True)
        pdf.ln(5)

        # ── Notes ─────────────────────────────────────────────────────────
        if q_notes:
            pdf.set_draw_color(*BD)
            pdf.set_line_width(0.3)
            pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
            pdf.ln(4)
            pdf.set_font("Helvetica", "B", 8)
            pdf.set_text_color(*MUTED)
            pdf.cell(W, 5, "NOTES", border=0, new_x="LMARGIN", new_y="NEXT")
            pdf.set_font("Helvetica", "", 8)
            pdf.set_text_color(*TEXT)
            pdf.multi_cell(W, 5, q_notes, border=0)
            pdf.ln(3)

        # ── Footer ────────────────────────────────────────────────────────
        pdf.set_draw_color(*BD)
        pdf.set_line_width(0.3)
        pdf.line(19, pdf.get_y(), 19 + W, pdf.get_y())
        pdf.ln(4)
        pdf.set_font("Helvetica", "", 7.5)
        pdf.set_text_color(*MUTED)
        pdf.multi_cell(W, 4.5,
            "This quote is valid for 90 days from the date of issue. Payment terms are Net 30 upon acceptance. "
            "To accept this quote, visit the link provided in your email. "
            "Questions? Contact us at info@bluealpha.us or 678-961-3304.",
            border=0)

        pdf_bytes = bytes(pdf.output())
        filename  = f"{q_number or record_id}.pdf"

        return Response(
            pdf_bytes,
            headers={
                **cors(),
                "Content-Disposition": f'attachment; filename="{filename}"',
                "Content-Type": "application/pdf",
            },
        )
    except Exception as e:
        import traceback; traceback.print_exc()
        return Response(json.dumps({"error": str(e)}), status=500, headers=cors(), mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
