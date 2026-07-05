@echo off
setlocal
set "ROOT=%~dp0"
if not defined PYTHON_BIN set "PYTHON_BIN=py"

if not defined OPENAI_API_KEY (
  echo [CollectIQ Backend SIT] OPENAI_API_KEY is required for AI_PROVIDER=openai.
  echo Set OPENAI_API_KEY in this terminal or in your deployment provider secrets.
  exit /b 1
)

set "BACKEND_ENV=sit"
set "AI_PROVIDER=openai"
if not defined PRICING_PROVIDER set "PRICING_PROVIDER=mock"
if not defined PORT set "PORT=8000"
if not defined CORS_ALLOWED_ORIGINS set "CORS_ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000"

cd /d "%ROOT%"
"%PYTHON_BIN%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
