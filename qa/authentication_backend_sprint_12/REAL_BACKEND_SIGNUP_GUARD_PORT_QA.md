# Authentication Backend Sprint 12 - Real Backend Signup Guard Port QA

## Backend Identity

- Repository inspected: `C:\Users\hario\Desktop\projects\collectiq_ai_backend`
- Assessment: likely the real PackLox/CollectIQ SIT backend.
- Evidence:
  - Dedicated FastAPI backend repo.
  - `render.yaml` defines `collectiq-ai-backend-sit`.
  - `render.yaml` sets `PUBLIC_API_URL` to `https://api-sit.packlox.com`.
  - `docs/BACKEND_SIT_DEPLOYMENT.md` says to deploy `HariomTiger14/collectiq-ai-backend` as the Render Web Service.

## Endpoint Status

- Before this port, `POST /auth/signup-start` was not present in this backend.
- Added:
  - `app/routers/auth.py`
  - `app/services/auth/signup_start_guard.py`
  - `app/services/auth/__init__.py`
- Registered `auth.router` in `app/main.py`.

## Contract

- Fresh email:
  - Supabase Admin lookup returns no matching confirmed user.
  - Backend returns `safeForAccountCreation: true`.
- Confirmed existing email:
  - Supabase Admin lookup returns a matching user with confirmation timestamp.
  - Backend returns `safeForAccountCreation: false`.
  - Response does not include account-existence wording, user ID, token, or secret.
- Unconfirmed existing email:
  - Matching user with unset confirmation fields is allowed.
  - Backend returns `safeForAccountCreation: true`.
- Config/Supabase failure:
  - Backend returns HTTP `503` with retryable safe error.

## Secret Handling

- Uses backend-only `SUPABASE_SERVICE_ROLE_KEY`.
- No service-role/admin key is added to Flutter.
- No secret values were printed or committed.
- Tests use `test-placeholder-key` only.

## Deployment Notes

- Render deployment already has placeholders for:
  - `SUPABASE_URL`
  - `SUPABASE_SERVICE_ROLE_KEY`
  - `SUPABASE_ANON_KEY`
- After deploy, Flutter SIT should call:
  - `{API_BASE_URL}/auth/signup-start`
- For `api-sit.packlox.com`, verify:
  - `GET https://api-sit.packlox.com/health`
  - `POST https://api-sit.packlox.com/auth/signup-start`

## Validation Status

- Backend tests added:
  - `tests/test_auth_signup_start_guard.py`
- Local backend test execution depends on a Python runner being available on this machine.
