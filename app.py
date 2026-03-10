from flask import Flask, send_from_directory, abort, request, Response
import os
import functools

app = Flask(__name__, static_folder="static")

DASHBOARDS = {
    "kurt": "kurt.html",
    "jesse": "jesse.html",
    "kelly": "kelly.html",
    "patty": "patty.html",
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
    abort(404)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
