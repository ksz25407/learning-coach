@echo off
REM Double-click to start the Learning Coach. Opens http://localhost:8501 in your browser.
REM Close this window (or press Ctrl+C in it) to stop the app.

cd /d "%~dp0"

if not exist ".venv\Scripts\activate.bat" (
    echo The virtual environment .venv was not found in %CD%.
    echo Set it up first, see README.md: python -m venv .venv, then pip install -r requirements.txt
    pause
    exit /b 1
)

call ".venv\Scripts\activate.bat"
streamlit run ui\app.py

REM Keep the window open if Streamlit stops with an error, so you can read it.
if errorlevel 1 pause
