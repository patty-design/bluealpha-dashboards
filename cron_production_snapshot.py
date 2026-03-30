"""
Weekly production snapshot cron script.
Captures total tasks completed, units produced, and hours logged for the previous Mon–Sun week.

Railway cron schedule: 0 4 * * 1
(4:00 AM UTC every Monday = midnight Eastern; captures the Mon–Sun week that just ended.)
"""
import os
import json
import datetime
import urllib.request
import urllib.parse

# ── Tokens & IDs ────────────────────────────────────────────────────────────
AIRTABLE_TOKEN  = os.environ.get('AIRTABLE_TOKEN', '')
AIRTABLE_BASE   = os.environ.get('AIRTABLE_BASE', 'appA13jo4b3TIn4yT')
SHIPMENTS_TOKEN = os.environ.get('SHIPMENTS_TOKEN', '')
SNAPSHOTS_BASE  = os.environ.get('SHIPMENTS_BASE', 'app3xt0dghBWnHxdN')

PRODUCTION_TABLE = 'tbloztf6y79U60mqQ'   # Production Tasks (ops base)
SNAPSHOT_TABLE   = 'tblpfqQKS8cw4zVZN'   # Weekly Production Snapshots (Automated Data base)


# ── Airtable helpers ─────────────────────────────────────────────────────────
def airtable_get_all(base_id, table_id, token, filter_formula=None, fields=None):
    records = []
    offset = None
    while True:
        parts = []
        if filter_formula:
            parts.append(f"filterByFormula={urllib.parse.quote(filter_formula)}")
        if fields:
            for f in fields:
                parts.append(f"fields[]={urllib.parse.quote(f)}")
        if offset:
            parts.append(f"offset={urllib.parse.quote(offset)}")
        qs = '&'.join(parts)
        url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
        if qs:
            url += f"?{qs}"
        req = urllib.request.Request(url, headers={'Authorization': f'Bearer {token}'})
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read())
        records.extend(data.get('records', []))
        offset = data.get('offset')
        if not offset:
            break
    return records


def airtable_create(base_id, table_id, token, fields):
    url = f"https://api.airtable.com/v0/{base_id}/{table_id}"
    body = json.dumps({'fields': fields}).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'},
        method='POST'
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


# ── Main ─────────────────────────────────────────────────────────────────────
def main():
    today = datetime.date.today()  # Monday (UTC) = midnight Eastern Sunday night

    # Previous week: Mon–Sun
    # today is Monday; week_end = yesterday (Sunday), week_start = 6 days before that (Monday)
    week_end   = today - datetime.timedelta(days=1)   # Sunday
    week_start = week_end - datetime.timedelta(days=6) # Monday

    print(f"[{today}] Capturing production snapshot for {week_start} → {week_end}")

    # Week boundaries as ISO datetimes for Airtable filter
    # Start Time / End Time are datetime fields — filter by date range
    week_start_iso = f"{week_start}T00:00:00.000Z"
    week_end_iso   = f"{week_end}T23:59:59.999Z"

    # Fetch all records with Start Time in the week
    filter_formula = (
        f'AND('
        f'  IS_AFTER({{Start Time}}, "{week_start - datetime.timedelta(days=1)}"), '
        f'  IS_BEFORE({{Start Time}}, "{week_end + datetime.timedelta(days=1)}")'
        f')'
    )
    records = airtable_get_all(
        AIRTABLE_BASE, PRODUCTION_TABLE, AIRTABLE_TOKEN,
        filter_formula=filter_formula,
        fields=['Status', 'Start Time', 'End Time', 'Final Count', 'Production Type']
    )
    print(f"  Found {len(records)} production records")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    total_tasks = 0
    total_units = 0
    total_hours = 0.0

    for r in records:
        f = r.get('fields', {})
        status       = f.get('Status', '')
        start_time   = f.get('Start Time')
        end_time     = f.get('End Time')
        final_count  = f.get('Final Count', 0) or 0
        prod_type    = f.get('Production Type', '')

        # Hours: any record with both start + end (sanity: 1 min – 16 hrs)
        if start_time and end_time:
            start_dt = datetime.datetime.fromisoformat(start_time.replace('Z', '+00:00'))
            end_dt   = datetime.datetime.fromisoformat(end_time.replace('Z', '+00:00'))
            diff_hrs = (end_dt - start_dt).total_seconds() / 3600
            if (1/60) <= diff_hrs <= 16:
                total_hours += diff_hrs

        # Tasks + Units: Complete or Partial Complete, based on End Time date
        is_done = status in ('Complete', 'Partial Complete')
        if not is_done or not end_time:
            continue

        end_dt  = datetime.datetime.fromisoformat(end_time.replace('Z', '+00:00'))
        end_date = end_dt.date()
        if not (week_start <= end_date <= week_end):
            continue

        total_tasks += 1
        if final_count and prod_type == 'Product SKU':
            total_units += final_count

    # ── Write snapshot ────────────────────────────────────────────────────────
    snapshot_fields = {
        'Week End Date':   week_end.isoformat(),
        'Week Start Date': week_start.isoformat(),
        'Total Tasks':     total_tasks,
        'Total Units':     total_units,
        'Total Hours':     round(total_hours, 2),
    }

    result = airtable_create(SNAPSHOTS_BASE, SNAPSHOT_TABLE, SHIPMENTS_TOKEN, snapshot_fields)
    record_id = result.get('id', '?')
    print(f"  ✅ Snapshot written — record {record_id}")
    print(f"  Tasks: {total_tasks} | Units: {total_units} | Hours: {total_hours:.1f}h")


if __name__ == '__main__':
    main()
