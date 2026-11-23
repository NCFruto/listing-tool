import os
import json
import csv
import re
from datetime import datetime

script_dir = os.path.dirname(os.path.abspath(__file__))
base_dir = os.path.dirname(script_dir)
listings_dir = os.path.join(base_dir, 'listings')
csv_path = os.path.join(base_dir, 'pipeline_status.csv')

fieldnames = ['listing_id', 'scraped_date', 'address', 'client_agency',
              'content_status', 'ad_brief_status', 'posted_fb_ig', 'ad_launched', 'notes']

# Read existing CSV, strip example rows
existing_ids = set()
existing_rows = []

if os.path.exists(csv_path):
    with open(csv_path, 'r', newline='', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row['listing_id'].startswith('example'):
                continue
            existing_ids.add(row['listing_id'])
            existing_rows.append(row)

# Find new listings
new_rows = []
if os.path.exists(listings_dir):
    for listing_id in sorted(os.listdir(listings_dir)):
        listing_path = os.path.join(listings_dir, listing_id)
        if not os.path.isdir(listing_path):
            continue
        if listing_id in existing_ids:
            continue

        json_path = os.path.join(listing_path, 'listing.json')
        address = ''
        client_agency = ''
        scraped_date = datetime.fromtimestamp(os.path.getmtime(listing_path)).strftime('%Y-%m-%d')

        if os.path.exists(json_path):
            with open(json_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            address = data.get('address', '')
            agency_name = data.get('agency_name') or ''
            client_agency = re.sub(r'[^a-z0-9]+', '_', agency_name.lower()).strip('_') if agency_name else ''

        new_rows.append({
            'listing_id': listing_id,
            'scraped_date': scraped_date,
            'address': address,
            'client_agency': client_agency,
            'content_status': 'not_started',
            'ad_brief_status': 'not_started',
            'posted_fb_ig': 'no',
            'ad_launched': 'no',
            'notes': ''
        })

all_rows = existing_rows + new_rows
with open(csv_path, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(all_rows)

print(f"\nDone! Added {len(new_rows)} new listings, removed example row.")
for row in new_rows:
    print(f"  + {row['listing_id']}: {row['address']}")
input("\nPress Enter to close...")
