@echo off
REM ===========================================================================
REM  One-time setup for the CAN bus reverse-engineering skills (Windows).
REM  Creates a local virtual environment in .venv and installs requirements.
REM  Safe to re-run; it will reuse / update the existing .venv.
REM ===========================================================================
setlocal
cd /d "%~dp0"

echo.
echo [1/3] Creating virtual environment in .venv ...
python -m venv .venv
if errorlevel 1 (
  echo.
  echo ERROR: could not create the virtual environment.
  echo        Make sure Python 3.10+ is installed and on your PATH ^(python --version^).
  exit /b 1
)

echo.
echo [2/3] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo [3/3] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERROR: dependency installation failed. See the messages above.
  exit /b 1
)

echo.
echo ===========================================================================
echo  Done. The environment is ready in .venv
echo  Open Claude Code in this folder and ask it to reverse engineer a CAN signal.
echo ===========================================================================
endlocal
