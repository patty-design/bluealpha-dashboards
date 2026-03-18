"""
Monthly raw material cost snapshot cron script.
Run this on the last day of each month via Railway cron (or any scheduler).

Railway cron config: schedule "0 22 28-31 * *"
(Runs at 10pm UTC on days 28-31; the script skips if it's not actually the last day of the month.)
"""
import os
import json
import calendar
import datetime
import urllib.request

BASE_URL = os.environ.get("APP_BASE_URL", "https://bluealpha-dashboards-production.up.railway.app")
SECRET   = os.environ.get("CRON_SECRET", "")

def is_last_day_of_month():
    today = datetime.date.today()
    last = calendar.monthrange(today.year, today.month)[1]
    return today.day == last

def main():
    today = datetime.date.today()

    if not is_last_day_of_month():
        print(f"[{today}] Not the last day of the month — skipping.")
        return

    print(f"[{today}] Last day of month — capturing snapshot...")

    payload = json.dumps({
        "date": today.isoformat(),
        "notes": "End-of-month auto-snapshot",
    }).encode()

    headers = {"Content-Type": "application/json"}
    if SECRET:
        headers["X-Cron-Secret"] = SECRET

    req = urllib.request.Request(
        f"{BASE_URL}/api/raw-material-cost/capture",
        data=payload,
        headers=headers,
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        body = json.loads(resp.read())
        print(f"[{today}] Snapshot captured: {body}")

if __name__ == "__main__":
    main()
