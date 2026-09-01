#!/bin/zsh
# Daily tier-3 rotation from a laptop, for as long as sportscardspro's
# Cloudflare rules block /price-guide/download-custom from Render's IPs.
#
#   ./scripts/tier3_daily.sh          # 1,000 sets (~2 hours)
#   ./scripts/tier3_daily.sh 2500     # 2,500 sets (~5 hours)
#   ./scripts/tier3_daily.sh status   # progress only, no fetching
#
# No state of its own: pricecharting_set_registry.tier3_refreshed_at IS the
# queue position. The rotation orders oldest-refreshed-first and stamps each
# batch as its write lands, so stopping this at any point -- Ctrl-C, laptop
# sleep, a dead network -- costs at most the in-flight batch. Run it again
# and it continues from exactly where it stopped.
#
# Sizing: measured 2026-09-01 at ~8.2 sets/min, so sets/60/8.2 ~= hours. The
# full large-set half is 17,691 sets, i.e. ~18 days at the default. Render
# cycled it every 2-3 days; this is the cost of running it by hand.

set -e
cd "$(dirname "$0")/.."

SETS=${1:-1000}
BATCH=25

eval "$(.venv/bin/python -c "
from dotenv import dotenv_values
import shlex
v = dotenv_values('.env')
missing = [k for k in ('PRICECHARTING_API_KEY','SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY') if not v.get(k)]
if missing:
    raise SystemExit('Missing in .env: ' + ', '.join(missing))
for k in ('PRICECHARTING_API_KEY','SUPABASE_URL','SUPABASE_SERVICE_ROLE_KEY','DATABASE_URL'):
    if v.get(k):
        print(f'export {k}={shlex.quote(v[k])}')
")"

show_status() {
  psql "$DATABASE_URL" -q -c \
    "select state, sets, newest_refresh from public.tier3_rotation_status order by sets desc;"
}

if [[ "$SETS" == "status" ]]; then
  show_status
  exit 0
fi

# max-requests is HTTP requests; sets covered = max-requests x batch-size.
REQUESTS=$(( (SETS + BATCH - 1) / BATCH ))

echo "=== before ==="
show_status
echo "Rotating up to $SETS set(s) ($REQUESTS requests x $BATCH). Keep the lid OPEN and stay on power."
echo

# caffeinate blocks IDLE sleep only -- closing the lid still sleeps on Apple
# Silicon. The run is resumable, so that is an interruption, not a loss.
caffeinate -is .venv/bin/python -m scripts.refresh_sportscardspro_rotation \
  --api-token "$PRICECHARTING_API_KEY" \
  --max-requests "$REQUESTS" \
  --batch-size "$BATCH" \
  --catalog-batch-size 200 2>&1 \
  | sed -E "s/${PRICECHARTING_API_KEY}/[REDACTED]/g"

echo
echo "=== after ==="
show_status
