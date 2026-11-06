@echo off
REM Starts the IRBS Data Hub on this computer.
REM Open http://localhost:8050 in a browser. To let colleagues on the campus
REM network use it, change HOST to 0.0.0.0 and share http://<this-computer-name>:8050
cd /d "%~dp0"
set HOST=127.0.0.1
set PORT=8050
python -c "import flask, waitress, pyodbc, pandas" 2>nul || pip install -r requirements.txt
start "" http://localhost:%PORT%
python app.py
pause
