@echo off
REM ============================================================
REM  京东广告运营分析服务 —— 停止脚本（Windows）
REM  关闭所有监听 8011 端口的进程。
REM ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

set FOUND=0
for /f "tokens=5" %%p in ('netstat -ano ^| findstr ":8011" ^| findstr "LISTENING"') do (
    echo 停止 PID %%p ...
    taskkill /F /PID %%p >nul 2>&1
    set FOUND=1
)

if "!FOUND!"=="0" (
    echo 没有正在监听 8011 的进程。
) else (
    echo 已停止。
)

timeout /t 2 >nul
endlocal
