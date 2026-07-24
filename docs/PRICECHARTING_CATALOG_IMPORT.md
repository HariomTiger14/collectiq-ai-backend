# PriceCharting Catalog Import

PackLox supports a local PriceCharting catalog seeded from the Legendary CSV download.

## What This Stores

The `pricecharting_catalog` table stores one row per PriceCharting product:

- PriceCharting product ID
- Product name
- Console/platform/category
- UPC/ASIN/ePID when present
- Release date when present
- Price fields as raw cents/pennies from PriceCharting
- Raw CSV payload for audit/debugging
- Normalized identity for matching

PriceCharting prices are pennies. Example: `3325` means `$33.25`.

## Setup

1. Open Supabase SQL editor for SIT.
2. Run `database/migrations/20260725_create_pricecharting_catalog.sql`.
3. Download the PriceCharting CSV from Subscription > API/Download.
4. Save the CSV locally.
5. Dry-run the import:

```bash
python scripts/import_pricecharting_catalog.py /path/to/pricecharting.csv --dry-run
```

6. Import into Supabase:

```bash
SUPABASE_URL="https://YOUR_PROJECT.supabase.co" \
SUPABASE_SERVICE_ROLE_KEY="YOUR_SERVICE_ROLE_KEY" \
python scripts/import_pricecharting_catalog.py /path/to/pricecharting.csv
```

## Import From Configured CSV URLs

If Render has these private CSV URL environment variables configured:

- `PRICECHARTING_CSV_VIDEO_GAMES_URL`
- `PRICECHARTING_CSV_POKEMON_URL`
- `PRICECHARTING_CSV_MAGIC_URL`
- `PRICECHARTING_CSV_YUGIOH_URL`
- `PRICECHARTING_CSV_ONE_PIECE_URL`

then import all configured CSV downloads in one run:

```bash
SUPABASE_URL="https://YOUR_PROJECT.supabase.co" \
SUPABASE_SERVICE_ROLE_KEY="YOUR_SERVICE_ROLE_KEY" \
PRICECHARTING_CSV_VIDEO_GAMES_URL="https://..." \
PRICECHARTING_CSV_POKEMON_URL="https://..." \
python scripts/import_pricecharting_catalog.py --from-env --dry-run
```

Remove `--dry-run` after the summary looks right:

```bash
python scripts/import_pricecharting_catalog.py --from-env
```

Do not commit the CSV URLs to GitHub. They may contain your private PriceCharting token.

## Frequency

PriceCharting CSV files are generated once every 24 hours, so PackLox should import at most once per day.

## Next Integration Step

After importing the CSV, wire analyzer pricing lookup order as:

1. PackLox `pricecharting_catalog` local match
2. PriceCharting API fallback for refresh/missing items
3. Shared pricing cache
4. Structured unavailable state
