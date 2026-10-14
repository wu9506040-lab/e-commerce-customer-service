@echo off
REM ============================================================
REM XianyuDaemon Windows 服务卸载脚本
REM
REM 用法（管理员 CMD）：
REM   scripts\xianyu\uninstall_service.bat
REM ============================================================

setlocal

set SERVICE_NAME=XianyuDaemon

where nssm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] NSSM 未找到
    exit /b 1
)

echo [INFO] 停止服务 %SERVICE_NAME%...
nssm stop %SERVICE_NAME%
timeout /t 3 /nobreak >nul

echo [INFO] 卸载服务 %SERVICE_NAME%...
nssm remove %SERVICE_NAME% confirm

echo [OK] XianyuDaemon 已卸载

endlocal
