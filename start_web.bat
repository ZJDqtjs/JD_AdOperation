@echo off
REM ============================================================
REM  京东广告运营分析服务 —— 稳定启动脚本（Windows）
REM  双击即可运行；会固化时区，避免 TZ=UTC 污染导致抓取日期错一天。
REM  监听 0.0.0.0：同一局域网的其它设备也能访问（http://<本机IP>:8011/）。
REM ============================================================
chcp 65001 >nul
setlocal enabledelayedexpansion

REM 关键：显式指定业务时区（代码层亦有双保险，这里是第一道防线）
set TZ=Asia/Shanghai
set PYTHONUTF8=1

cd /d "%~dp0"

REM 监听所有网卡（0.0.0.0）。若仍写 127.0.0.1，则只有本机能访问
start "JD-AdOperation-Web" /b cmd /c ".venv\Scripts\python.exe -m uvicorn jd_roi.webapp:app --host 0.0.0.0 --port 8011 >> logs_web.log 2>&1"

REM 取本机对外网卡的 IPv4（DHCP 获取的那个即局域网地址），用于显示访问地址
set LANIP=
for /f "delims=" %%i in ('powershell -NoProfile -Command "@(Get-NetIPAddress -AddressFamily IPv4 -PrefixOrigin Dhcp -ErrorAction SilentlyContinue)[0].IPAddress"') do set LANIP=%%i

echo.
echo   本机访问  ：http://127.0.0.1:8011/
if defined LANIP (
    echo   局域网访问：http://!LANIP!:8011/
) else (
    echo   局域网访问：http://^<本机IPv4^>:8011/  （用 ipconfig 查看本机 IP）
)
echo   日志文件  ：%~dp0logs_web.log
echo   停止服务  ：运行 stop_web.bat
echo.
echo   提示：局域网内若打不开，需放行 Windows 防火墙 TCP 8011 入站。
echo.
timeout /t 4 >nul
endlocal
