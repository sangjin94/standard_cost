@echo off
chcp 65001 > nul
cd /d "%~dp0"
echo 표준물류단가 산정 서버를 시작합니다...
echo 브라우저에서 http://localhost:5001 접속
start "" /min cmd /c "timeout /t 3 >/dev/null & start http://localhost:5001"
where py >/dev/null 2>nul
if %errorlevel%==0 ( py app.py ) else ( python app.py )
pause
