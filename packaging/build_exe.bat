@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [1/3] Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 goto :fail
)

echo [2/3] Installing runtime deps and PyInstaller...
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt
if errorlevel 1 goto :fail
".venv\Scripts\python.exe" -m pip install -q -r requirements-dev.txt
if errorlevel 1 goto :fail

echo [3/3] Building EXE...
".venv\Scripts\pyinstaller.exe" "packaging\qingcheng-shouna.spec" --noconfirm --clean --distpath dist --workpath build
if errorlevel 1 goto :fail

echo.
echo Build OK: dist\qingcheng-shouna\ ... (see dist folder)
echo Output: dist
exit /b 0

:fail
echo.
echo Build FAILED. Check the error above.
exit /b 1
