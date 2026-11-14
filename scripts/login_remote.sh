#!/usr/bin/env bash
# ============================================================
# 远程登录（无桌面 / SSH 进 WSL）：在浏览器里看浏览器，过滑块 / 短信验证
#
# 适用：你不是 Windows 主机本机，WSLg 弹的窗口你看不到。
# 做法：起一个带虚拟屏(Xvfb) + x11vnc + noVNC 的一次性容器，把画面转成网页，
#       你在自己的浏览器里打开就能看到那个 Chromium 并直接拖滑块。
#       登录态写回同一份 docker-auth，线上立即复用，不需要拷贝。
#
# 用法：
#   ./scripts/login_remote.sh                 # 主账号 main，网页端口 6080
#   ./scripts/login_remote.sh b               # 其他账号 key
#   VNC_PASS=mysecret ./scripts/login_remote.sh main    # 指定 VNC 密码
#   VNC_PORT=6081 ./scripts/login_remote.sh main        # 换端口（6080 被占用时）
#   JD_LOGIN_WAIT_SEC=1200 ./scripts/login_remote.sh main   # 给更多时间（默认 900 秒）
#
# 前置：镜像里要有 xvfb/x11vnc/novnc（一次性构建，约 1 分钟）
#   docker build -f docker/Dockerfile.vnc -t jd-roi:vnc .
# ============================================================
set -euo pipefail

ACCOUNT="${1:-main}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMG="${JD_VNC_IMAGE:-jd-roi:vnc}"
AUTH_DIR="${JD_AUTH_DIR_HOST:-/opt/jd-roi/docker-auth}"
CFG_DIR="${JD_CONFIG_DIR_HOST:-/opt/jd-roi/docker-config}"
DATA_DIR="${JD_DATA_DIR_HOST:-/opt/jd-roi/docker-data}"
VNC_PORT="${VNC_PORT:-6080}"
VNC_PASS="${VNC_PASS:-$(head -c 6 /dev/urandom | od -An -tx1 | tr -d ' \n')}"
WAIT_SEC="${JD_LOGIN_WAIT_SEC:-900}"

if ! docker image inspect "$IMG" >/dev/null 2>&1; then
  echo "[error] 缺少镜像 $IMG，先构建：" >&2
  echo "        cd $ROOT && docker build -f docker/Dockerfile.vnc -t jd-roi:vnc ." >&2
  exit 1
fi
for d in "$AUTH_DIR" "$CFG_DIR" "$DATA_DIR"; do
  [ -d "$d" ] || { echo "[error] 目录不存在: $d" >&2; exit 1; }
done
if docker ps --format '{{.Names}}' | grep -qx "jd-login-vnc"; then
  echo "[error] 已有 jd-login-vnc 在跑，先 docker rm -f jd-login-vnc" >&2
  exit 1
fi

IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "[info] 账号=$ACCOUNT  端口=$VNC_PORT  VNC 密码=$VNC_PASS  等待=${WAIT_SEC}s"
echo
echo "  在你自己电脑的浏览器里打开（和访问 8011 用的同一台机器地址）："
echo "    http://${IP:-<这台机器的IP>}:${VNC_PORT}/vnc.html?autoconnect=1&resize=scale"
echo
echo "  连上后输入上面的 VNC 密码，就能看到登录页：扫码 / 输账号密码 / 拖滑块。"
echo "  登录成功后容器自动退出，刷新 /login 即为已登录。"
echo

TTY=""
[ -t 0 ] && TTY="-it"

docker run --rm $TTY \
  --name jd-login-vnc \
  -p "${VNC_PORT}:6080" \
  -e "DISPLAY=:99" \
  -e "JD_HEADLESS=0" \
  -e "JD_LOGIN_WAIT_SEC=${WAIT_SEC}" \
  -v "$ROOT/jd_roi:/app/jd_roi" \
  -v "$AUTH_DIR:/auth" \
  -v "$CFG_DIR:/config" \
  -v "$DATA_DIR:/data" \
  --entrypoint sh \
  "$IMG" -c '
    set -e
    Xvfb :99 -screen 0 1600x1000x24 -nolisten tcp &
    for i in $(seq 1 40); do xdpyinfo >/dev/null 2>&1 && break; sleep 0.25; done
    x11vnc -display :99 -rfbport 5900 -forever -shared -noxdamage -passwd "'"$VNC_PASS"'" -quiet &
    websockify --web=/usr/share/novnc 6080 127.0.0.1:5900 &
    sleep 1
    python -m jd_roi.login --account='"$ACCOUNT"'
  '
