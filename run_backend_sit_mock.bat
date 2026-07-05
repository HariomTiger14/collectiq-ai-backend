@echo off
setlocal
set "ROOT=%~dp0"
if not defined PYTHON_BIN set "PYTHON_BIN=py"

set "BACKEND_ENV=sit"
set "AI_PROVIDER=mock"
set "PRICING_PROVIDER=mock"
if not defined PORT set "PORT=8000"
if not defined CORS_ALLOWED_ORIGINS set "CORS_ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000"

cd /d "%ROOT%"
"%PYTHON_BIN%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT%
