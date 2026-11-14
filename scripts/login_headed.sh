#!/usr/bin/env bash
# ============================================================
# 有头登录（WSL2 + WSLg）：打开一个真实可见的浏览器窗口，用于过滑块 / 短信验证
#
# 背景：线上容器是无头浏览器（JD_HEADLESS=1）且没有 X 显示，
#       /login 页勾选「显示浏览器窗口」也弹不出窗口，账号密码登录必然失败
#       （Playwright 报 "launched a headed browser without having a XServer"）。
# 做法：另起一个一次性容器，把 WSLg 的 X11 socket 挂进去，窗口直接出现在 Windows 桌面；
#       它和线上共用 docker-auth / docker-config / docker-data，
#       登录成功后 Profile 就写在线上的那一份里，不需要再拷贝。
#
# 用法：
#   ./scripts/login_headed.sh                 # 主账号 main
#   ./scripts/login_headed.sh b               # 指定账号 key（见 config/accounts.json）
#   JD_LOGIN_WAIT_SEC=600 ./scripts/login_headed.sh main   # 给更多时间（默认 300 秒）
#
# 说明：
#   - 窗口是 Chrome，可以扫码，也可以在登录页输账号密码并拖动滑块；
#   - 登录成功后脚本自己关窗口，页面 /login 刷新一下就是「已登录」；
#   - 如果这时线上保活正好在用同一个 Profile，会提示 Profile 被占用，重跑一次即可。
# ============================================================
set -euo pipefail

ACCOUNT="${1:-main}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${JD_IMAGE:-jd-roi:latest}"
AUTH_DIR="${JD_AUTH_DIR_HOST:-/opt/jd-roi/docker-auth}"
CFG_DIR="${JD_CONFIG_DIR_HOST:-/opt/jd-roi/docker-config}"
DATA_DIR="${JD_DATA_DIR_HOST:-/opt/jd-roi/docker-data}"
DISPLAY="${DISPLAY:-:0}"
SCREEN="${DISPLAY#:}"
SCREEN="${SCREEN%%.*}"

if [ ! -S "/tmp/.X11-unix/X${SCREEN}" ]; then
  echo "[error] 找不到 X 显示 ${DISPLAY}（/tmp/.X11-unix/X${SCREEN} 不存在）。" >&2
  echo "        WSL2 需要 WSLg（Windows 11 或 Win10 22H2+ 自带）；" >&2
  echo "        或在 Windows 装 VcXsrv，并把 DISPLAY 指过去后重跑。" >&2
  exit 1
fi
for d in "$AUTH_DIR" "$CFG_DIR" "$DATA_DIR"; do
  if [ ! -d "$d" ]; then
    echo "[error] 目录不存在: $d（可用 JD_AUTH_DIR_HOST / JD_CONFIG_DIR_HOST / JD_DATA_DIR_HOST 覆盖）" >&2
    exit 1
  fi
done
if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "[error] 找不到镜像 $IMG（可用 JD_IMAGE=... 指定）" >&2
  exit 1
fi

echo "[info] 账号=$ACCOUNT  镜像=$IMG  显示=$DISPLAY"
echo "[info] 浏览器窗口会出现在 Windows 桌面；完成后自动关闭。"
echo

TTY=""
[ -t 0 ] && TTY="-it"

docker run --rm $TTY \
  --name "jd-login-${ACCOUNT}" \
  -e "DISPLAY=${DISPLAY}" \
  -e "JD_HEADLESS=0" \
  -e "JD_LOGIN_WAIT_SEC=${JD_LOGIN_WAIT_SEC:-300}" \
  -v /tmp/.X11-unix:/tmp/.X11-unix \
  -v "$ROOT/jd_roi:/app/jd_roi" \
  -v "$AUTH_DIR:/auth" \
  -v "$CFG_DIR:/config" \
  -v "$DATA_DIR:/data" \
  --entrypoint python \
  "$IMG" -m jd_roi.login "--account=${ACCOUNT}"
