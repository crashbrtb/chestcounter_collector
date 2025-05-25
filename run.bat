REM **Create log file**
for /f "tokens=2 delims==" %%a in ('wmic OS Get LocalDateTime /VALUE ^| findstr LocalDateTime') do set "datahora=%%a"
set "data=%data:~0,4%-%data:~4,2%-%data:~6,2%"
set "logfile=C:\chestcounter\execution_logs\log_%datahora:~0,4%-%datahora:~4,2%-%datahora:~6,2%_%datahora:~8,2%-%datahora:~10,2%-%datahora:~12,2%.txt"
REM **Create log file**

echo **Entering relative path** >> "%logfile%"
cd \chestcounter

call C:\chestcounter\venv\Scripts\activate.bat >> "%logfile%"

echo **Starting script opentb** >> "%logfile%"

py C:\chestcounter\opentb.py >> "%logfile%"

echo **Starting script counter.py** >> "%logfile%"

py C:\chestcounter\counter.py >> "%logfile%" 

echo **Starting script closetb** >> "%logfile%"

py C:\chestcounter\closetb.py >> "%logfile%"