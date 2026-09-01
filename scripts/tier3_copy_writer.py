"""COPY-based catalog writer for the tier-3 rotation.

Why this exists: the shared PostgREST writer costs one HTTP round trip per
sub-batch, and Supabase is in ap-northeast-1 while this runs from a laptop.
Measured 2026-09-01, that path sustains ~100 rows/sec and a 25-set batch
(~15,000 rows) spends ~150s writing against ~30s fetching -- so ~83% of a
tier-3 run is round trips, not work. The same box COPYs 20,000 rows in
3.9s (~5,200 rows/sec) straight into Postgres through the same pooler.

The win is structural rather than a tuning knob: ~375 round trips per batch
become one COPY plus a handful of set-based statements inside ONE
transaction. That also removes the 57014 statement timeouts the REST path
hits under contention with the hourly tier-1 job, since there is no longer a
long series of independent statements racing it, and it makes the write
atomic -- the REST path's close-then-insert history pair cannot be, which is
why it needs its careful ordering comment.

Deliberately NOT wired into write_catalog_rows(): that function is shared by
~15 scripts, and this needs a direct DATABASE_URL connection (psycopg) that
Render's cron path does not have. This is an additive fast path for tier-3,
selected explicitly, leaving every other caller untouched.

Semantics are copied from SupabaseCatalogClient, not reinvented:
  * the catalog upsert gate is catalog.content_hash
  * the history gate is the CURRENT history row's change_hash (a different
    row, so the two are compared separately even though to_catalog_row()
    currently derives both from catalog_history_change_hash)
  * a price observation is written only for a brand-new item or one whose
    PRICES changed -- metadata-only versions write none
  * observed_at is the provider download timestamp, which is also the
    (pricecharting_id, observed_at) idempotency key
"""

from __future__ import annotations

import io
import json
from typing import Any

import psycopg

from scripts.import_pricecharting_catalog import (
    CATALOG_COLUMNS,
    PRICE_OBSERVATION_COLUMNS,
)

_PRICE_COLS = ", ".join(PRICE_OBSERVATION_COLUMNS)


class CopyCatalogWriter:
    """Writes catalog rows via COPY + a single server-side merge."""

    def __init__(self, database_url: str, *, statement_timeout_ms: int = 120_000) -> None:
        if not database_url:
            raise SystemExit("DATABASE_URL is required for the COPY writer.")
        self.database_url = database_url
        self.statement_timeout_ms = statement_timeout_ms
        # One connection for the whole run. Establishing it costs a TLS
        # handshake to ap-northeast-1 -- ~2s of a ~10s batch when paid per
        # batch, which is pure overhead on a path whose entire point is
        # removing round trips.
        self._conn: psycopg.Connection | None = None
        self.catalog_write_stats: dict[str, int] = {
            "written": 0,
            "skippedUnchanged": 0,
            "failed": 0,
        }
        self.price_history_stats: dict[str, int] = {
            "attempted": 0,
            "inserted": 0,
            "duplicateSkipped": 0,
            "failed": 0,
        }

    @staticmethod
    def _encode(value: Any) -> Any:
        # raw_payload is jsonb; psycopg would otherwise adapt a dict to a
        # Postgres record type rather than JSON.
        if isinstance(value, (dict, list)):
            return json.dumps(value)
        return value

    def write(self, catalog_rows: list[dict[str, Any]]) -> bool:
        """Returns True when the batch is durable. One transaction: either
        the whole batch lands or none of it does, so a failure leaves
        nothing half-applied for the retry to reason about."""
        if not catalog_rows:
            return True

        cols = list(CATALOG_COLUMNS)
        try:
            conn = self._connection()
            if True:
                with conn.cursor() as cur:
                    cur.execute(f"set local statement_timeout = {self.statement_timeout_ms}")
                    cur.execute(
                        "create temp table _tier3_staging "
                        "(like public.pricecharting_catalog including defaults) "
                        "on commit drop"
                    )

                    copy_sql = (
                        f"copy _tier3_staging ({', '.join(cols)}) from stdin"
                    )
                    with cur.copy(copy_sql) as copy:
                        for row in catalog_rows:
                            copy.write_row([self._encode(row.get(c)) for c in cols])

                    # Deduplicate within the batch itself: one CSV can carry
                    # the same pricecharting_id twice, and ON CONFLICT cannot
                    # act on a row the same statement touches twice.
                    cur.execute(
                        "create temp table _tier3_batch on commit drop as "
                        "select distinct on (pricecharting_id) * from _tier3_staging "
                        "order by pricecharting_id"
                    )

                    # Rows whose catalog content actually differs.
                    cur.execute(
                        "create temp table _tier3_changed on commit drop as "
                        "select s.* from _tier3_batch s "
                        "left join public.pricecharting_catalog c using (pricecharting_id) "
                        "where c.pricecharting_id is null "
                        "   or c.content_hash is distinct from s.content_hash"
                    )
                    cur.execute("select count(*) from _tier3_changed")
                    changed = int(cur.fetchone()[0])
                    skipped = len(catalog_rows) - changed

                    # Rows needing a new history VERSION, gated on the
                    # current history row rather than the catalog row.
                    cur.execute(
                        "create temp table _tier3_hist on commit drop as "
                        "select s.*, h.history_id as prev_history_id, "
                        f"       ({ ' or '.join(f'h.{c} is distinct from s.{c}' for c in PRICE_OBSERVATION_COLUMNS) } "
                        "        or h.currency is distinct from s.currency) as prices_changed "
                        "  from _tier3_batch s "
                        "  left join public.pricecharting_catalog_history h "
                        "    on h.pricecharting_id = s.pricecharting_id and h.is_current "
                        " where h.history_id is null "
                        "    or h.change_hash is distinct from s.content_hash"
                    )

                    # Price observation first, matching the REST path's order.
                    cur.execute(
                        "insert into public.pricecharting_price_history "
                        f"(pricecharting_id, observed_at, {_PRICE_COLS}, currency, source_file) "
                        "select pricecharting_id, source_downloaded_at, "
                        f"       {_PRICE_COLS}, coalesce(currency, 'USD'), source_file "
                        "  from _tier3_hist "
                        " where prev_history_id is null or prices_changed "
                        "on conflict (pricecharting_id, observed_at) do nothing"
                    )
                    observations = cur.rowcount

                    cur.execute(
                        "update public.pricecharting_catalog_history h "
                        "   set valid_to = s.source_downloaded_at, is_current = false "
                        "  from _tier3_hist s "
                        " where h.history_id = s.prev_history_id"
                    )

                    cur.execute(
                        "insert into public.pricecharting_catalog_history ("
                        "  pricecharting_id, product_name, console_name, category, upc, "
                        "  asin, epid, release_date, loose_price_cents, cib_price_cents, "
                        "  new_price_cents, graded_price_cents, box_only_price_cents, "
                        "  manual_only_price_cents, currency, product_url, "
                        "  normalized_identity, raw_payload, source_file, "
                        "  source_downloaded_at, valid_from, valid_to, is_current, change_hash) "
                        "select pricecharting_id, product_name, console_name, category, upc, "
                        "  asin, epid, release_date, loose_price_cents, cib_price_cents, "
                        "  new_price_cents, graded_price_cents, box_only_price_cents, "
                        "  manual_only_price_cents, coalesce(currency, 'USD'), product_url, "
                        "  normalized_identity, raw_payload, source_file, "
                        "  source_downloaded_at, source_downloaded_at, null, true, content_hash "
                        "from _tier3_hist"
                    )
                    versions = cur.rowcount

                    cur.execute(
                        f"insert into public.pricecharting_catalog ({', '.join(cols)}) "
                        f"select {', '.join(cols)} from _tier3_changed "
                        "on conflict (pricecharting_id) do update set "
                        + ", ".join(
                            f"{c} = excluded.{c}"
                            for c in cols
                            if c != "pricecharting_id"
                        )
                        + ", updated_at = now()"
                    )
                conn.commit()
        except Exception as exc:  # noqa: BLE001 -- mirrors write_catalog_rows
            # A failed transaction leaves the connection in an aborted state;
            # drop it so the next batch starts clean rather than inheriting it.
            self.close()
            self.catalog_write_stats["failed"] += len(catalog_rows)
            print(
                f"  COPY write failed for this batch, will retry next cycle: {exc}",
                flush=True,
            )
            return False

        self.catalog_write_stats["written"] += changed
        self.catalog_write_stats["skippedUnchanged"] += max(0, skipped)
        self.price_history_stats["attempted"] += observations
        self.price_history_stats["inserted"] += observations
        print(
            f"  COPY: {len(catalog_rows)} rows -> {changed} changed, "
            f"{max(0, skipped)} unchanged, {versions} history versions, "
            f"{observations} price observations.",
            flush=True,
        )
        return True

    def _connection(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = psycopg.connect(self.database_url, autocommit=False)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:  # noqa: BLE001 -- closing must never raise
                pass
            self._conn = None
