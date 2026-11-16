@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo [setup] Creating virtual environment...
    python -m venv venv || (echo Python 3.11+ is required on PATH & exit /b 1)
)

echo [setup] Installing dependencies...
"venv\Scripts\python.exe" -m pip install -q --disable-pip-version-check -r requirements.txt || exit /b 1

if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [setup] Created .env from .env.example - set API_KEY before uploading invoices.
)

echo [run] Starting server on http://0.0.0.0:8000
"venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000
endlocal
