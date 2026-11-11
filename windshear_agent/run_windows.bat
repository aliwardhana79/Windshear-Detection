@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install Python 3.11 from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup, then run this file again.
    pause
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the virtual environment...
    py -3.11 -m venv .venv 2>nul || py -3 -m venv .venv
    if errorlevel 1 (
        echo Could not create the virtual environment. Install Python 3.11 and try again.
        pause
        exit /b 1
    )
)

echo Installing requirements. The first run can take a few minutes...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo Installation failed. Check the messages above.
    pause
    exit /b 1
)

echo Starting Windshear Agent at http://localhost:8501 ...
".venv\Scripts\python.exe" -m streamlit run streamlit_app.py

pause
