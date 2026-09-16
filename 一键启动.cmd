@echo off
rem 一键启动：检查四项后台服务，缺哪个补哪个。
rem Python 解释器按以下顺序查找，避免写死本机路径：
rem   1) 环境变量 PYTHON
rem   2) py launcher (py -3)
rem   3) PATH 中的 python
chcp 65001 >nul
title WeChat Auto-Reply - Start All Services
cd /d "%~dp0"

set "PYEXE=%PYTHON%"
if not defined PYEXE (
  py -3 -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYEXE=py -3"
)
if not defined PYEXE (
  python -c "import sys" >nul 2>&1
  if not errorlevel 1 set "PYEXE=python"
)
if not defined PYEXE (
  echo.
  echo [ERROR] Python not found.
  echo         Install Python 3.11+ and make sure it is on PATH,
  echo         or set PYTHON=C:\path\to\python.exe before running this.
  echo.
  pause
  exit /b 1
)

echo.
echo ============ Starting all WeChat auto-reply services ============
echo.
%PYEXE% -X utf8 "health_check.py" --fix
echo.
pause
