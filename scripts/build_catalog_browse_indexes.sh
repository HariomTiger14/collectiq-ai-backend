#!/usr/bin/env bash
#
# Builds the index that backs category browse in Discover.
#
# CREATE INDEX CONCURRENTLY cannot run inside a transaction, so the normal
# migration runner cannot apply it. It also takes long enough on a 16GB
# table that the things below stop being optional:
#
#   * statement_timeout must be 0. The pooled connection string in .env
#     inherits a timeout that will kill the build partway through.
#   * TCP keepalives must be short. A killed build is how the first attempt
#     at this failed: Supavisor dropped an idle-looking client and Postgres
#     cancelled the build with no error text at all.
#   * A cancelled build leaves an INVALID index behind. It is inert -- the
#     planner ignores it, so nothing breaks -- but CREATE INDEX ... IF NOT
#     EXISTS then sees the name as taken and skips forever. This script
#     drops an invalid leftover before retrying, which makes reruns safe:
#     re-run it as many times as it takes.
#
# Safe to run against a live database. CONCURRENTLY does not block reads or
# writes, so the repricing crons keep running; it just takes longer than a
# plain build (two table scans instead of one).
#
# Usage:
#   scripts/build_catalog_browse_indexes.sh                # build
#   scripts/build_catalog_browse_indexes.sh --status       # report only
#   DATABASE_URL=... scripts/build_catalog_browse_indexes.sh
#
# Prefer a DIRECT connection over the pooler if you have one -- the pooler
# is what dropped the first attempt.

set -uo pipefail

INDEX_NAME="pricecharting_catalog_browse_price_idx"
TABLE_NAME="public.pricecharting_catalog"
MIGRATION="database/migrations/20260826_add_pricecharting_catalog_browse_price_index.sql"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT" || exit 1

if [[ -z "${DATABASE_URL:-}" ]]; then
    if [[ -f .env ]]; then
        DATABASE_URL="$(grep -m1 '^DATABASE_URL=' .env | cut -d= -f2- | tr -d '"'"'"'')"
    fi
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
    echo "DATABASE_URL is not set and .env has no DATABASE_URL line." >&2
    exit 1
fi
if ! command -v psql >/dev/null 2>&1; then
    echo "psql not found on PATH. Install the postgresql client." >&2
    exit 1
fi

export PGCONNECT_TIMEOUT=30

# Split the password out of the connection string and pass it through the
# environment instead. A DSN handed to psql as an argument is visible in
# the process list to every user on the machine -- `ps` prints it in clear
# -- and this build runs long enough for that to matter. PGPASSWORD is not
# in argv, so it does not show up.
#
# The keepalive parameters are appended here too: without them the pooler
# drops the connection mid-build and cancels it with no error text.
eval "$(python3 - "$DATABASE_URL" <<'PYEOF'
import shlex, sys
from urllib.parse import urlsplit, urlunsplit, quote, unquote

parts = urlsplit(sys.argv[1])
# unquote: urlsplit leaves percent-escapes encoded, but PGPASSWORD must be
# the literal password. strip: the libpq URI parser skips stray whitespace
# around the password field, so a URL that connects fine can still carry a
# leading space that would break PGPASSWORD verbatim -- this .env does.
# (No apostrophes in these comments: the macOS bash 3.2 parser counts
# quotes naively inside command substitution and one here breaks the
# whole script.)
password = unquote(parts.password or "").strip()
host = parts.hostname or ""
if parts.port:
    host = f"{host}:{parts.port}"
user = quote(parts.username or "", safe="")
netloc = user + "@" + host if parts.username else host
keepalives = "keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=6"
query = f"{parts.query}&{keepalives}" if parts.query else keepalives
dsn = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
print(f"DSN={shlex.quote(dsn)}")
print(f"export PGPASSWORD={shlex.quote(password)}")
PYEOF
)"
if [[ -z "${DSN:-}" ]]; then
    echo "Could not parse DATABASE_URL." >&2
    exit 1
fi

q() { psql "$DSN" -X -q -t -A -c "$1" 2>/dev/null; }

report_status() {
    local row
    row="$(q "select coalesce((select case when not i.indisvalid then 'INVALID'
                                          when not i.indisready then 'NOT READY'
                                          else 'VALID' end
                               from pg_index i
                               where i.indexrelid = to_regclass('$INDEX_NAME')), 'MISSING')")"
    local size
    size="$(q "select coalesce(pg_size_pretty(pg_relation_size(to_regclass('$INDEX_NAME'))), '-')")"
    echo "  $INDEX_NAME: ${row:-UNKNOWN} (${size:-unknown})"
}

# Live progress, polled from a SECOND connection -- the building session is
# blocked inside CREATE INDEX and cannot report on itself.
watch_progress() {
    local build_pid="$1"
    while kill -0 "$build_pid" 2>/dev/null; do
        sleep 30
        kill -0 "$build_pid" 2>/dev/null || break
        local line
        line="$(q "select phase || '  ' ||
                          case when blocks_total > 0
                               then round(100.0 * blocks_done / blocks_total, 1) || '% of blocks'
                               when tuples_total > 0
                               then round(100.0 * tuples_done / tuples_total, 1) || '% of tuples'
                               else 'starting' end
                   from pg_stat_progress_create_index limit 1")"
        [[ -n "$line" ]] && echo "  [$(date +%H:%M:%S)] $line"
    done
}

echo "Index status before:"
report_status

if [[ "${1:-}" == "--status" ]]; then
    exit 0
fi

# A cancelled CONCURRENTLY build leaves an invalid index that IF NOT EXISTS
# would happily skip over, so the retry would silently do nothing.
invalid="$(q "select 1 from pg_index
              where indexrelid = to_regclass('$INDEX_NAME') and not indisvalid")"
if [[ "$invalid" == "1" ]]; then
    echo "Dropping invalid leftover from a previous cancelled build..."
    psql "$DSN" -X -q -c "drop index concurrently if exists $INDEX_NAME;" || {
        echo "Could not drop the invalid index. Drop it by hand, then re-run." >&2
        exit 1
    }
fi

valid="$(q "select 1 from pg_index
            where indexrelid = to_regclass('$INDEX_NAME') and indisvalid")"
if [[ "$valid" == "1" ]]; then
    echo "Already built. Nothing to do."
    exit 0
fi

echo
echo "Building $INDEX_NAME on $TABLE_NAME."
echo "This scans the table twice and takes a while; reads and writes keep working."
echo "If it is interrupted, just run this script again."
echo

# The timeouts are set IN-SESSION, not via PGOPTIONS: Supabase's pooler does
# not pass the `options` startup parameter through, so a PGOPTIONS-based
# statement_timeout=0 silently keeps the server default -- which is exactly
# how a previous run of this script was cancelled at 68%% of the first table
# scan. SET survives across -c/-f boundaries within one psql session, and
# each statement still autocommits individually, which CONCURRENTLY needs.
psql "$DSN" -X -q -v ON_ERROR_STOP=1 \
    -c "set statement_timeout = 0" \
    -c "set lock_timeout = 0" \
    -c "set idle_in_transaction_session_timeout = 0" \
    -f "$MIGRATION" &
build_pid=$!
watch_progress "$build_pid" &
watch_pid=$!
wait "$build_pid"
build_rc=$?
kill "$watch_pid" 2>/dev/null
wait "$watch_pid" 2>/dev/null

echo
echo "Index status after:"
report_status

if [[ $build_rc -ne 0 ]]; then
    echo
    echo "Build did not finish (exit $build_rc). Re-run this script -- it drops the" >&2
    echo "partial index and starts over." >&2
    exit $build_rc
fi

final="$(q "select 1 from pg_index
            where indexrelid = to_regclass('$INDEX_NAME') and indisvalid")"
if [[ "$final" != "1" ]]; then
    echo
    echo "psql exited cleanly but the index is not valid -- the build was cancelled" >&2
    echo "server-side. Re-run this script." >&2
    exit 1
fi

echo
echo "Done. Verify browse uses it:"
echo "  explain (analyze) select * from search_pricecharting_catalog('', 20, 5000, 200, array['Yugioh'], null, null, null);"
echo "Expect an Index Only Scan on $INDEX_NAME, not a Bitmap Heap Scan."
