-- The mobile app becomes the third error source alongside 'api' and
-- 'cron': its global error hooks (FlutterError.onError /
-- PlatformDispatcher.onError / runZonedGuarded, already wired in
-- main.dart) report through POST /api/ops/client-errors into this same
-- table, so app-side failures appear in the admin portal's one error
-- feed instead of only in the Firebase console.
ALTER TABLE public.ops_error_events
    DROP CONSTRAINT IF EXISTS ops_error_events_source_check;
ALTER TABLE public.ops_error_events
    ADD CONSTRAINT ops_error_events_source_check
    CHECK (source IN ('api', 'cron', 'app'));
