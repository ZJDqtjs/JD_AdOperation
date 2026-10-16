@echo off
chcp 65001 >nul
setlocal

rem ============================================================
rem  Rebuild image jd-roi:latest inside WSL (distro: Arch-Linux).
rem  Build ONLY on purpose - it does NOT touch the running container,
rem  so your Windows-side service keeps working while you experiment.
rem  ASCII-only + chcp 65001 so UTF-8 output coming back from WSL renders.
rem ============================================================

set "DISTRO=Arch-Linux"
set "PROJ=/mnt/d/code/JD_AdOperation"
set "LOG=%PROJ%/tmp/docker_build.log"

title jd-roi image rebuild
echo ============================================================
echo   Rebuild image : jd-roi:latest
echo   distro        : %DISTRO%
echo   project       : %PROJ%
echo   build log     : %LOG%
echo ============================================================
echo.

echo [1/3] checking docker daemon ...
wsl.exe -d %DISTRO% -u root -e bash -lc "docker info >/dev/null 2>&1 || /usr/local/bin/start-docker.sh; if docker info >/dev/null 2>&1; then echo daemon: OK; else echo daemon: NOT READY; fi"
if errorlevel 1 goto fail

echo.
echo [2/3] building image ... pip + chromium download may take 10-30 min
wsl.exe -d %DISTRO% -u root -e bash -lc "cd %PROJ% && mkdir -p tmp && docker compose build 2>&1 | tee tmp/docker_build.log"
if errorlevel 1 goto fail

echo.
echo [3/3] smoke test - import the app inside the fresh image
wsl.exe -d %DISTRO% -u root -e bash -lc "docker run --rm --entrypoint python jd-roi:latest -c 'import httpx, jd_roi.webapp as w; print(w.__name__, httpx.__version__)'"
if errorlevel 1 goto fail

echo.
echo fresh image:
wsl.exe -d %DISTRO% -u root -e bash -lc "docker images jd-roi --format '{{.Repository}}:{{.Tag}}  {{.Size}}  created {{.CreatedSince}}'"

echo.
echo ------------------------------------------------------------
echo  BUILD DONE. The running container was NOT touched.
echo.
echo  When you are ready to go live:
echo   1) The compose mounts docker-data / docker-auth / docker-config
echo      do not exist yet and will be created EMPTY on first up.
echo      Empty docker-auth means no login session in the container.
echo      To reuse the existing login and the historical data, copy these FIRST:
echo        data\daily             to  docker-data\daily\    (historical data - migrate it, or the report starts empty)
echo        auth\browser_profile   to  docker-auth\main\     (one profile dir per account key)
echo        auth\browser_profile_b to  docker-auth\b\
echo        auth\jd_state.json     to  docker-auth\main\
echo        config\accounts.json   and the cost-table xlsx  to  docker-config\
echo      Do NOT copy docker-data\state.json - it is a runtime artifact.
echo      On overlapping dates fill ONLY the missing days, never overwrite:
echo      freshly scraped days carry more complete attribution.
echo   2) Then swap the container:
echo        wsl -d Arch-Linux -u root -- bash -lc "cd %PROJ% ^&^& docker compose up -d"
echo   3) Verify: http://127.0.0.1:8000  (/api/ai/jobs 200 = new code)
echo ------------------------------------------------------------
echo.
pause
exit /b 0

:fail
echo.
echo [FAILED] see build log: %LOG%
echo Common causes: pip timeout (check PIP_INDEX_URL in .env),
echo                no network, dockerd not running.
echo.
pause
exit /b 1
