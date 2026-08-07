@echo off
setlocal
chcp 65001 >nul
title 财务系统
pushd "%~dp0"

if defined FINANCE_SYSTEM_PYTHON (
    "%FINANCE_SYSTEM_PYTHON%" "%~dp0run_finance.py" %*
    goto :finished
)

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0run_finance.py" %*
    goto :finished
)

where py >nul 2>nul
if not errorlevel 1 (
    py -3.11 "%~dp0run_finance.py" %*
    goto :finished
)

where python >nul 2>nul
if not errorlevel 1 (
    python "%~dp0run_finance.py" %*
    goto :finished
)

echo [错误] 未找到 Python 3.11。请安装 Python 3.11，或设置 FINANCE_SYSTEM_PYTHON。
set "_EXIT=1"
goto :failed

:finished
set "_EXIT=%ERRORLEVEL%"
if "%_EXIT%"=="0" goto :done

:failed
echo.
echo 财务系统启动失败，错误码：%_EXIT%
pause

:done
popd
exit /b %_EXIT%

