@echo off
cd /d "%~dp0"
echo.
echo  [1] PC에서 로컬 실행 (python auto_edit.py)
echo  [2] 웹앱 실행 - 브라우저에서 열림 (streamlit run app.py)
echo.
set /p choice=번호를 입력하세요 (1 or 2):
if "%choice%"=="1" python auto_edit.py
if "%choice%"=="2" streamlit run app.py
pause
