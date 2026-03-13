from flask import Flask, send_from_directory, abort, request, Response
import os
import functools
import json
import base64
import requests as req_lib

AIRTABLE_OPS_TOKEN   = os.environ.get("AIRTABLE_OPS_TOKEN", "")
AIRTABLE_WRITE_TOKEN = os.environ.get("AIRTABLE_WRITE_TOKEN", "")
AIRTABLE_BASE_ID     = "appA13jo4b3TIn4yT"
RETURNS_TABLE_ID     = os.environ.get("RETURNS_TABLE_ID", "")
SHIPSTATION_KEY      = os.environ.get("SHIPSTATION_KEY", "")
SHIPSTATION_SECRET   = os.environ.get("SHIPSTATION_SECRET", "")

app = Flask(__name__, static_folder="static")

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
        return Response(content, mimetype="text/html")
    abort(404)

def ss_headers():
    creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
    return {"Authorization": f"Basic {creds}"}

def cors():
    return {"Access-Control-Allow-Origin": "*"}


@app.route("/api/verify-order", methods=["POST", "OPTIONS"])
def verify_order():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}
    order_number = data.get("orderNumber", "").strip()
    last_name    = data.get("lastName", "").strip().lower()

    if not order_number or not last_name:
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

        # Verify last name
        ship_name = order.get("shipTo", {}).get("name", "").strip()
        order_last = ship_name.split()[-1].lower() if ship_name else ""
        if last_name != order_last:
            return Response(json.dumps({"status": "not_found"}), headers=c, mimetype="application/json")

        # Check international
        country = order.get("shipTo", {}).get("country", "US")
        if country not in ("US", "USA"):
            return Response(json.dumps({"status": "international"}), headers=c, mimetype="application/json")

        # Get ship date from shipments
        sr = req_lib.get("https://ssapi.shipstation.com/shipments",
                          params={"orderNumber": order_number},
                          headers=ss_headers(), timeout=10)
        shipments = sr.json().get("shipments", [])
        ship_date_str = shipments[0].get("shipDate", "") if shipments else ""

        if ship_date_str:
            ship_date = datetime.fromisoformat(ship_date_str.replace("Z", "+00:00"))
        else:
            od = order.get("orderDate", "")
            ship_date = datetime.fromisoformat(od.replace("Z", "+00:00")) if od else datetime.now(timezone.utc)

        eligible_until = ship_date + timedelta(days=37)
        if datetime.now(timezone.utc) > eligible_until:
            return Response(json.dumps({"status": "outside_window"}), headers=c, mimetype="application/json")

        # Build items list
        items = [{"sku": i.get("sku",""), "name": i.get("name",""), "quantity": i.get("quantity",1)}
                 for i in order.get("items", []) if i.get("name")]

        ship_to = order.get("shipTo", {})
        return Response(json.dumps({
            "status":        "eligible",
            "orderId":       order.get("orderId"),
            "orderKey":      order.get("orderKey", ""),
            "customerName":  ship_to.get("name", ""),
            "shipDate":      ship_date_str,
            "eligibleUntil": eligible_until.isoformat(),
            "email":         order.get("customerEmail", ""),
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


@app.route("/api/submit-return", methods=["POST", "OPTIONS"])
def submit_return():
    if request.method == "OPTIONS":
        return Response("", headers={**cors(), "Access-Control-Allow-Headers": "Content-Type", "Access-Control-Allow-Methods": "POST"})
    c = cors()
    data = request.get_json() or {}

    if not RETURNS_TABLE_ID or not AIRTABLE_WRITE_TOKEN:
        return Response(json.dumps({"success": False, "error": "Airtable not configured"}),
                        status=500, headers=c, mimetype="application/json")

    addr = data.get("address", {})
    address_str = ", ".join(filter(None, [
        addr.get("street1"), addr.get("street2"),
        addr.get("city"), addr.get("state"), addr.get("postalCode")
    ]))

    from datetime import datetime, timezone
    wc_link = f"https://www.bluealphabelts.com/wp-admin/post.php?post={data.get('orderKey','')}&action=edit"

    fields = {
        "Order Number":            data.get("orderNumber", ""),
        "Customer Name from Shipstation": data.get("customerName", ""),
        "Email Address":           data.get("email", ""),
        "Phone Number":            data.get("phone", ""),
        "Confirmed Shipping Address": address_str,
        "Items to Return":         data.get("itemsToReturn", ""),
        "Reason for Return":       data.get("reasonForReturn", ""),
        "Submission Date":         datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "Ship Date from Shipstation": data.get("shipDate", "")[:10] if data.get("shipDate") else "",
        "Eligible Until":          data.get("eligibleUntil", "")[:10] if data.get("eligibleUntil") else "",
        "Status":                  "New",
        "WooCommerce Order Link":  wc_link,
    }
    # Remove empty fields
    fields = {k: v for k, v in fields.items() if v}

    try:
        r = req_lib.post(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RETURNS_TABLE_ID}",
            headers={"Authorization": f"Bearer {AIRTABLE_WRITE_TOKEN}", "Content-Type": "application/json"},
            json={"fields": fields},
            timeout=10
        )
        if r.status_code in (200, 201):
            return Response(json.dumps({"success": True}), headers=c, mimetype="application/json")
        else:
            return Response(json.dumps({"success": False, "error": r.text}),
                            status=500, headers=c, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"success": False, "error": str(e)}),
                        status=500, headers=c, mimetype="application/json")


@app.route("/api/awaiting")
def awaiting_shipment():
    cors = {"Access-Control-Allow-Origin": "*"}
    if not SHIPSTATION_KEY or not SHIPSTATION_SECRET:
        return Response(json.dumps({"error": "ShipStation not configured"}),
                        status=500, headers=cors, mimetype="application/json")
    try:
        from datetime import datetime, timezone, timedelta
        creds = base64.b64encode(f"{SHIPSTATION_KEY}:{SHIPSTATION_SECRET}".encode()).decode()
        headers = {"Authorization": f"Basic {creds}"}

        # Today's date in Eastern time (UTC-4 DST / UTC-5 standard)
        eastern = timezone(timedelta(hours=-4))
        today = datetime.now(eastern).strftime("%Y-%m-%d")

        awaiting, placed = req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderStatus": "awaiting_shipment", "pageSize": 1},
            headers=headers, timeout=10
        ), req_lib.get(
            "https://ssapi.shipstation.com/orders",
            params={"orderDateStart": today, "orderDateEnd": today, "pageSize": 1},
            headers=headers, timeout=10
        )

        return Response(json.dumps({
            "count":       awaiting.json().get("total", 0),
            "placedToday": placed.json().get("total", 0),
        }), headers=cors, mimetype="application/json")
    except Exception as e:
        return Response(json.dumps({"error": str(e)}),
                        status=500, headers=cors, mimetype="application/json")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
