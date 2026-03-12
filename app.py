from flask import Flask, send_from_directory, abort, request, Response
import os
import functools
import json
import base64
import requests as req_lib

AIRTABLE_OPS_TOKEN  = os.environ.get("AIRTABLE_OPS_TOKEN", "")
SHIPSTATION_KEY     = os.environ.get("SHIPSTATION_KEY", "")
SHIPSTATION_SECRET  = os.environ.get("SHIPSTATION_SECRET", "")

app = Flask(__name__, static_folder="static")

DASHBOARDS = {
    "kurt": "kurt.html",
    "jesse": "jesse.html",
    "kelly": "kelly.html",
    "patty": "patty.html",
}

OPS_DASHBOARDS = {
    "production": "production.html",
    "shipments": "shipments.html",
    "waiting": "waiting.html",
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

        # Today's date in Eastern time (UTC-5 / UTC-4)
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
