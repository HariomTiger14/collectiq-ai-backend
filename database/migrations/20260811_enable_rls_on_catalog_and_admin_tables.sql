-- Enable row level security on the six tables that never had it.
--
-- Every other table in the project either carries explicit RLS policies
-- (user-owned tables) or is RLS-enabled with zero policies (service-role-only
-- tables such as pricecharting_catalog_history). These six were created
-- without ENABLE ROW LEVEL SECURITY at all, so their exposure to the anon and
-- authenticated PostgREST roles depended entirely on the project's default
-- grants:
--
--   * pricecharting_catalog   — catalog reads go through the backend API
--                               (service role); no client reads it directly.
--   * kicksdb_catalog         — same: backend crons/services only.
--   * admin_import_jobs       — admin-portal ops data, backend-only.
--   * admin_notes             — internal admin annotations, backend-only.
--   * admin_audit_events      — admin action audit log, backend-only.
--   * scan_analysis_events    — scan telemetry / failure queue, backend-only.
--
-- Enabling RLS with no policies makes them service-role-only, which matches
-- how every caller already accesses them:
--   * the FastAPI backend and all cron jobs use SUPABASE_SERVICE_ROLE_KEY
--     (service role bypasses RLS entirely);
--   * the mobile app never touches these tables (verified against the app's
--     complete Supabase call map);
--   * the admin portal only talks to the backend API, never to PostgREST.
-- Foreign-key validation (portfolio_items.pricecharting_id ->
-- pricecharting_catalog) is performed by the system and is not subject to
-- RLS, so item linking is unaffected.

alter table public.pricecharting_catalog enable row level security;
alter table public.kicksdb_catalog enable row level security;
alter table public.admin_import_jobs enable row level security;
alter table public.admin_notes enable row level security;
alter table public.admin_audit_events enable row level security;
alter table public.scan_analysis_events enable row level security;
