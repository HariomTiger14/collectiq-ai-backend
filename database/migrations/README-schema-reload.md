# PostgREST schema reload

PostgREST caches the database schema. A table, column or RPC it has not
reloaded is invisible to it — the API returns 404 or 503 for something that
demonstrably exists in the database.

```sql
NOTIFY pgrst, 'reload schema';
```

**Run this by hand, at deploy time, whenever a change exposes something new to
the API.** Not as a line inside a migration file you have already applied —
production does not re-run those, so the statement never executes and the file
reads as though it did.

Observed 2026-09-10: `pricecharting_current_price` was live and fully
backfilled, and catalog detail still failed briefly, because PostgREST had not
reloaded. The table was not the problem; the cache was.

Migrations under this directory carry the statement for the benefit of a fresh
environment rebuilt from scratch. Those copies are annotated where they did not
run in production.
