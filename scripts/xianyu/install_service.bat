@echo off
REM ============================================================
REM 闲鱼自动化 daemon Windows 服务安装脚本
REM
REM 前置：安装 NSSM（nssm.cc），把 nssm.exe 路径加到 PATH
REM
REM 用法（管理员 CMD）：
REM   scripts\xianyu\install_service.bat
REM ============================================================

setlocal enabledelayedexpansion

set SERVICE_NAME=XianyuDaemon
set SERVICE_DISPLAY=闲鱼自动化客服 Daemon (智能客服 V16.1)
set SERVICE_DESC=智能客服 24h 闲鱼 IM 渠道自动回复（MCP + AI Agent）
set SCRIPT_DIR=%~dp0
set PROJECT_ROOT=%SCRIPT_DIR%..\..
set PYTHON_EXE=python
set LOG_DIR=%PROJECT_ROOT%\logs

REM 检查 nssm
where nssm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] NSSM 未找到。请下载 https://nssm.cc 并把 nssm.exe 加到 PATH
    exit /b 1
)

REM 检查 Python
where %PYTHON_EXE% >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 未找到。请装 Python 3.11+ 并加到 PATH
    exit /b 1
)

REM 检查 .env.dev / .env.prod
if not exist "%PROJECT_ROOT%\deploy\.env.dev" (
    echo [WARN] deploy\.env.dev 不存在，daemon 可能跑不起来
)

REM 卸载旧的（如有）
nssm stop %SERVICE_NAME% >nul 2>&1
nssm remove %SERVICE_NAME% confirm >nul 2>&1

REM 注册服务
echo [INFO] 注册 Windows 服务: %SERVICE_NAME%
nssm install %SERVICE_NAME% %PYTHON_EXE% "%PROJECT_ROOT%\scripts\run_xianyu_daemon.py"
nssm set %SERVICE_NAME% DisplayName %SERVICE_DISPLAY%
nssm set %SERVICE_NAME% Description %SERVICE_DESC%
nssm set %SERVICE_NAME% AppDirectory %PROJECT_ROOT%
nssm set %SERVICE_NAME% AppStdout %LOG_DIR%\xianyu_daemon.out.log
nssm set %SERVICE_NAME% AppStderr %LOG_DIR%\xianyu_daemon.err.log
nssm set %SERVICE_NAME% AppRotateFiles 1
nssm set %SERVICE_NAME% AppRotateBytes 10485760
nssm set %SERVICE_NAME% Start SERVICE_AUTO_START
nssm set %SERVICE_NAME% AppExit 0 0
nssm set %SERVICE_NAME% AppRestartDelay 5000
nssm set %SERVICE_NAME% AppStdoutCreationDisposition 4
nssm set %SERVICE_NAME% AppStderrCreationDisposition 4

REM 环境变量（覆盖系统环境）
nssm set %SERVICE_NAME% AppEnvironmentExtra ^
    "ENABLE_XIANYU_TOOLS=true" ^
    "APP_ENV=prod" ^
    "PYTHONIOENCODING=utf-8" ^
    "XIANYU_CHAT_API_URL=http://localhost:8000/api/chat" ^
    "XIANYU_MIN_DELAY=30" ^
    "XIANYU_MAX_DELAY=180" ^
    "XIANYU_DEBOUNCE_WINDOW=30"

echo [INFO] 启动服务...
nssm start %SERVICE_NAME%
if errorlevel 1 (
    echo [ERROR] 服务启动失败，请看日志：%LOG_DIR%\xianyu_daemon.err.log
    exit /b 1
)

echo.
echo [OK] XianyuDaemon 服务已安装并启动
echo.
echo 常用命令：
echo   查看状态:  nssm status %SERVICE_NAME%
echo   停止:      nssm stop %SERVICE_NAME%
echo   重启:      nssm restart %SERVICE_NAME%
echo   卸载:      scripts\xianyu\uninstall_service.bat
echo   看日志:    type %LOG_DIR%\xianyu_daemon.log
echo   健康检查:  curl http://localhost:8000/admin/xianyu/health
echo.

endlocal
