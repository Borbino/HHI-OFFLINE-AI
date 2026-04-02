@echo off
chcp 65001 > nul
title HD현대중공업 기술교육원 LLM SYSTEM 구동기

echo ===================================================
echo  HD현대중공업 기술교육원 LLM SYSTEM 초기화 중...
echo ===================================================
echo.

echo [1/3] 기존 실행 중인 좀비 프로세스 정리 중...
FOR /F "tokens=5" %%a IN ('netstat -aon ^| findstr ":5000 " ^| findstr "LISTENING"') DO (
    echo 포트 5000 점유 프로세스(PID: %%a) 강제 종료...
    taskkill /F /PID %%a > nul 2>&1
)
FOR /F "tokens=5" %%a IN ('netstat -aon ^| findstr ":8000 " ^| findstr "LISTENING"') DO (
    echo 포트 8000 점유 프로세스(PID: %%a) 강제 종료...
    taskkill /F /PID %%a > nul 2>&1
)
echo 기존 프로세스 정리 완료.
echo.

echo [2/3] 가상환경 확인 및 필요 라이브러리 점검...
:: 가상환경이 있을 경우 아래 주석 해제 후 경로 지정
:: call .venv\Scripts\activate.bat
echo.

echo [3/3] 메인 서버 구동 시작...
echo ※ 주의: 이 검은색 창을 닫으면 LLM SYSTEM이 즉시 종료됩니다.
echo.
python app.py

pause
