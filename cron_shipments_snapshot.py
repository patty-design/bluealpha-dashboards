"""
Weekly shipments snapshot cron script.
Captures total orders, components, and per-category breakdown for the previous Sun–Sat week.

Railway cron schedule: 0 4 * * 0
(4:00 AM UTC every Sunday = midnight Eastern; captures the week that just ended Sat night.)
"""
import os
import json
import datetime
import urllib.request
import urllib.parse

# ── Tokens & IDs ────────────────────────────────────────────────────────────
SHIPMENTS_TOKEN = os.environ.get('SHIPMENTS_TOKEN', '')
SHIPMENTS_BASE  = os.environ.get('SHIPMENTS_BASE', 'app3xt0dghBWnHxdN')
AIRTABLE_TOKEN  = os.environ.get('AIRTABLE_TOKEN', '')
AIRTABLE_BASE   = os.environ.get('AIRTABLE_BASE', 'appA13jo4b3TIn4yT')

SHIPMENTS_TABLE = 'tblrfthvKPOSmx0S6'
SKUS_TABLE      = 'tbljngm75r4Km2XIN'
SNAPSHOT_TABLE  = 'tbllq1huCZdrhxlWJ'

CATEGORIES = [
    'Battle Belts', 'EDC Belts', 'Duty Belts',
    'Accessories', 'Support Accessories', 'Apparel', 'Merch', 'Contract'
]


# ── Airtable helpers ─────────────────────────────────────────────────────────
def airtable_get_all(base_id, table_id, token, filter_formula=None, fields=None):
    """Fetch all records from an Airtable table (handles pagination)."""
    records = []
    offset = None
    while True:
        params = {}
        if filter_formula:
            params['filterByFormula'] = filter_formula
        if fields:
            for f in fields:
                params.setdefault('fields[]', [])
                params['fields[]'].append(f)
        if offset:
            params['offset'] = offset

        # urllib doesn't handle list params well — build manually
        parts = []
        for k, v in params.items():
            if isinstance(v, list):
                for item in v:
                    parts.append(f"fields[]={urllib.parse.quote(item)}")
            else:
                parts.append(f"{k}={urllib.parse.quote(str(v))}")
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
    """Create a single record in Airtable."""
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
    today = datetime.date.today()  # Sunday (UTC) = midnight Eastern Saturday night

    # Previous week: Sun–Sat
    # today is Sunday; week_end = yesterday (Saturday), week_start = 6 days before that
    week_end   = today - datetime.timedelta(days=1)   # Saturday
    week_start = week_end - datetime.timedelta(days=6) # Sunday

    print(f"[{today}] Capturing shipments snapshot for {week_start} → {week_end}")

    # ── 1. Fetch SKU → Category map from ops base ────────────────────────────
    sku_records = airtable_get_all(
        AIRTABLE_BASE, SKUS_TABLE, AIRTABLE_TOKEN,
        fields=['SKU ID', 'Category']
    )
    sku_map = {}
    for r in sku_records:
        f = r.get('fields', {})
        sku_id = f.get('SKU ID')
        category = f.get('Category')
        if sku_id and category:
            sku_map[sku_id] = category
    print(f"  Loaded {len(sku_map)} SKUs")

    # ── 2. Fetch shipments for the week ──────────────────────────────────────
    filter_formula = (
        f'AND('
        f'  IS_AFTER({{Date}}, "{week_start - datetime.timedelta(days=1)}"), '
        f'  IS_BEFORE({{Date}}, "{week_end + datetime.timedelta(days=1)}")'
        f')'
    )
    shipment_records = airtable_get_all(
        SHIPMENTS_BASE, SHIPMENTS_TABLE, SHIPMENTS_TOKEN,
        filter_formula=filter_formula,
        fields=['Date', 'Total Components', 'SKUs', 'SKU Quantities']
    )
    print(f"  Found {len(shipment_records)} shipments")

    # ── 3. Aggregate ─────────────────────────────────────────────────────────
    total_orders     = len(shipment_records)
    total_components = 0
    cat_counts       = {cat: 0 for cat in CATEGORIES}

    for r in shipment_records:
        f = r.get('fields', {})

        skus_text = f.get('SKUs', '')
        qtys_text = f.get('SKU Quantities', '')
        skus = [s.strip() for s in skus_text.split(',') if s.strip()]
        qtys = []
        for q in qtys_text.split(','):
            try:
                qtys.append(int(q.strip()))
            except ValueError:
                qtys.append(1)

        # Sum quantities as total components (Make doesn't populate Total Components field)
        total_components += sum(qtys) if qtys else len(skus)

        for i, sku in enumerate(skus):
            category = sku_map.get(sku)
            if category and category in cat_counts:
                qty = qtys[i] if i < len(qtys) else 1
                cat_counts[category] += qty

    # ── 4. Write snapshot ────────────────────────────────────────────────────
    snapshot_fields = {
        'Week End Date':   week_end.isoformat(),
        'Week Start Date': week_start.isoformat(),
        'Total Orders':    total_orders,
        'Total Components': total_components,
    }
    for cat in CATEGORIES:
        snapshot_fields[cat] = cat_counts[cat]

    result = airtable_create(SHIPMENTS_BASE, SNAPSHOT_TABLE, SHIPMENTS_TOKEN, snapshot_fields)
    record_id = result.get('id', '?')
    print(f"  ✅ Snapshot written — record {record_id}")
    print(f"  Orders: {total_orders} | Components: {total_components}")
    for cat in CATEGORIES:
        print(f"  {cat}: {cat_counts[cat]}")


if __name__ == '__main__':
    main()
