@echo off
chcp 65001 >nul
cd /d "%~dp0"
title OLX Telegram Bot
where python >nul 2>nul || (echo Python не найден. Установите Python 3.11+ с python.org, отметив "Add python.exe to PATH". & pause & exit /b 1)
if not exist .venv (
  echo Первый запуск: устанавливаю зависимости, это займёт минуту...
  python -m venv .venv || (pause & exit /b 1)
)
.venv\Scripts\python -m pip install -q --disable-pip-version-check -r requirements.txt || (pause & exit /b 1)
:loop
.venv\Scripts\python main.py
if errorlevel 2 (pause & exit /b 1)
echo Бот остановился, перезапуск через 10 секунд. Закройте окно, чтобы выключить бота.
timeout /t 10 >nul
goto loop
