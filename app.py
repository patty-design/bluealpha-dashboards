from flask import Flask, send_from_directory, abort
import os

app = Flask(__name__, static_folder="static")

DASHBOARDS = {
    "kurt": "kurt.html",
    "jesse": "jesse.html",
    "kelly": "kelly.html",
    "patty": "patty.html",
}

@app.route("/")
def index():
    return "Blue Alpha Dashboards", 200

@app.route("/<name>")
def dashboard(name):
    if name in DASHBOARDS:
        return send_from_directory("static", DASHBOARDS[name])
    abort(404)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
