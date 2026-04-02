from flask import Flask, send_from_directory, abort, request, Response
import os
import functools
import json
import base64
import threading
import requests as req_lib

AIRTABLE_OPS_TOKEN      = os.environ.get("AIRTABLE_OPS_TOKEN", "")
AIRTABLE_WRITE_TOKEN    = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
RETURNS_WRITE_TOKEN     = os.environ.get("AIRTABLE_WRITE_TOKEN_2", AIRTABLE_WRITE_TOKEN)
FLASK_BASE_URL          = os.environ.get("FLASK_BASE_URL", "https://bluealpha-dashboards-production.up.railway.app")
AIRTABLE_BASE_ID        = "appA13jo4b3TIn4yT"
RETURNS_TABLE_ID        = os.environ.get("RETURNS_TABLE_ID", "")
RETURN_ITEMS_TABLE_ID   = "tblThFm0UA6gLQShV"
PRODUCT_SKUS_TABLE_ID   = "tbljngm75r4Km2XIN"
RM_SNAPSHOTS_TABLE_ID   = os.environ.get("RM_SNAPSHOTS_TABLE_ID", "")
RM_SNAPSHOTS_BASE_ID    = os.environ.get("RM_SNAPSHOTS_BASE_ID", AIRTABLE_BASE_ID)
RAW_MATERIALS_TABLE_ID  = "tblokid4GHQCvdXuQ"
SHIPSTATION_KEY      = os.environ.get("SHIPSTATION_KEY", "")
SHIPSTATION_SECRET   = os.environ.get("SHIPSTATION_SECRET", "")
SENDGRID_API_KEY     = os.environ.get("SENDGRID_API_KEY", "")
SENDGRID_FROM_EMAIL  = os.environ.get("SENDGRID_FROM_EMAIL", "info@bluealpha.us")

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

@app.route("/static/<path:filename>")
def serve_static(filename):
    return send_from_directory("static", filename)

@app.route("/")
def index():
    host = request.host.split(".")[0].lower()
    if host == "return":
        return send_from_directory("static", "returns.html")
    if host in DASHBOARDS:
        return dashboard(host)
    return "Blue Alpha Dashboards", 200

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

        # Check for existing active returns — build set of already-returned SKUs
        # Use AIRTABLE_OPS_TOKEN for reads (write token may not have read scope)
        airtable_read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
        already_returned_skus = []
        if RETURNS_TABLE_ID and airtable_read_token:
            try:
                filter_formula = f"AND({{Order Number}}='{order_number}',OR({{Status}}='New',{{Status}}='Label Sent'))"
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
                        m = re_lib.match(r'\d+x\s+(\S+)\s+[—\-]', line.strip())
                        if m:
                            already_returned_skus.append(m.group(1).strip())
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
            "alreadyReturnedSkus": already_returned_skus,
        }), headers=c, mimetype="application/json")

    except Exception as e:
        return Response(json.dumps({"status": "error", "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


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
                "Your return label is attached. Drop the package at any USPS location — "
                "no printer needed if you use USPS Label Broker (just show the barcode on your phone).\n\n"
                "Once we receive your return, we'll process it within 3–5 business days.\n\n"
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
    fields = {
        "Order Number":                   data.get("orderNumber", ""),
        "Customer Name from Shipstation": data.get("customerName", ""),
        "Email Address":                  data.get("email", ""),
        "Phone Number":                   data.get("phone", ""),
        "Confirmed Shipping Address":     address_str,
        "Items to Return":                data.get("itemsToReturn", ""),
        "Reason for Return":              data.get("reasonForReturn", ""),
        "Submission Date":                datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Ship Date from Shipstation":     data.get("shipDate", "")[:10] if data.get("shipDate") else "",
        "Eligible Until":                 data.get("eligibleUntil", "")[:10] if data.get("eligibleUntil") else "",
        "Status":                         "New",
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
            # ── Duplicate guard: check for existing Label Sent return for same order+items ──
            order_number = data.get("orderNumber", "")
            items_submitted = data.get("itemsToReturn", "")
            read_token = AIRTABLE_OPS_TOKEN or RETURNS_WRITE_TOKEN
            if order_number and RETURNS_TABLE_ID and read_token:
                try:
                    filter_formula = (f"AND({{Order Number}}='{order_number}',"
                                      f"OR({{Status}}='New',{{Status}}='Label Sent'),"
                                      f"NOT(RECORD_ID()='{record_id}'))")
                    dup_r = req_lib.get(
                        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
                        params={"filterByFormula": filter_formula, "fields[]": ["Items to Return"]},
                        headers={"Authorization": f"Bearer {read_token}"},
                        timeout=10,
                    )
                    existing = dup_r.json().get("records", [])
                    for rec in existing:
                        prev_items = rec.get("fields", {}).get("Items to Return", "")
                        # Check for any SKU overlap
                        submitted_skus = {part.split("—")[0].split("x")[-1].strip()
                                          for part in items_submitted.split("\n") if "—" in part}
                        prev_skus = {part.split("—")[0].split("x")[-1].strip()
                                     for part in prev_items.split("\n") if "—" in part}
                        if submitted_skus & prev_skus:
                            req_lib.patch(
                                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}/{record_id}",
                                headers={"Authorization": f"Bearer {RETURNS_WRITE_TOKEN}", "Content-Type": "application/json"},
                                json={"fields": {"Status": "Needs Review",
                                                 "Status Notes": "Duplicate — return already requested for these items"}},
                                timeout=10,
                            )
                            print(f"[process_label] Duplicate return blocked for order {order_number}")
                            return
                except Exception as dup_e:
                    print(f"[process_label] Duplicate check failed (continuing): {dup_e}")

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


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
