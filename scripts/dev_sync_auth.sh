#!/usr/bin/env bash
# ============================================================
# 把线上容器的登录态同步一份到本地 dev 环境（docker-dev/auth）
#
# 用途：dev 容器里 JD_KEEPALIVE=0，Profile 是"冷冻副本"，
#       sdtoken 约 30 分钟就会失效；需要抓数据前跑一次本脚本，
#       从线上（保活一直在刷新的正本）重新取一份。
#
# 用法：
#   ./scripts/dev_sync_auth.sh              # 同步所有账号
#   ./scripts/dev_sync_auth.sh main         # 只同步主账号
#
# 注意：
#   - 只读线上目录，不改动线上任何文件
#   - 剔除 Chromium 缓存（Code Cache / Cache 等）后约 20MB；不剔除则有 700MB+
#   - 若 dev 容器正在抓取，拷到的 Profile 可能不完整，脚本会提示
# ============================================================
set -euo pipefail

SRC="${JD_PROD_AUTH:-/opt/jd-roi/docker-auth}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DST="${JD_DEV_AUTH:-$REPO_ROOT/docker-dev/auth}"

if [ ! -d "$SRC" ]; then
  echo "[error] 找不到线上登录态目录: $SRC" >&2
  exit 1
fi

# 账号：参数优先，默认 main + b（按线上实际存在的目录过滤）
if [ "$#" -gt 0 ]; then
  ACCOUNTS=("$@")
else
  ACCOUNTS=(main b)
fi
KEYS=()
for a in "${ACCOUNTS[@]}"; do
  [ -d "$SRC/$a" ] && KEYS+=("$a") || echo "[skip] 线上没有账号目录: $a"
done
if [ "${#KEYS[@]}" -eq 0 ]; then
  echo "[error] 没有可同步的账号" >&2
  exit 1
fi

# 容器在跑时提醒（不阻塞：只读拷贝，不影响线上）
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx 'jd-roi-dev'; then
  echo "[warn] jd-roi-dev 正在运行；若此刻正在抓取或保活，拷到的 Profile 可能不完整"
fi

mkdir -p "$DST"

echo "[sync] $SRC -> $DST"
echo "[sync] 账号: ${KEYS[*]}"

tar -C "$SRC" \
  --exclude='*/GrShaderCache' \
  --exclude='*/ShaderCache' \
  --exclude='*/GPUCache' \
  --exclude='*/GPUPersistentCache' \
  --exclude='*/DawnGraphiteCache' \
  --exclude='*/DawnWebGPUCache' \
  --exclude='*/Code Cache' \
  --exclude='*/Cache' \
  --exclude='*/BrowserMetrics' \
  --exclude='*/Crashpad' \
  --exclude='*/Singleton*' \
  --exclude='*/Service Worker/CacheStorage' \
  -cf - "${KEYS[@]}" jd_state.json jd_state_b.json 2>/dev/null \
  | tar -C "$DST" -xf -

echo "[sync] 完成，当前大小:"
du -sh "$DST"/* 2>/dev/null || true
echo
echo "下一步：登录态是新拷进来的，dev 里可直接验证（不会重启 uvicorn，热重载不影响）"
echo "宿主机没装 curl，用 python3，或直接在浏览器打开 http://127.0.0.1:8012/login 看状态："
echo "  python3 -c \"import json,urllib.request as u;print(json.loads(u.urlopen(u.Request('http://127.0.0.1:8012/api/login/check?account=main&force=1',method='POST'),timeout=60).read()))\""
echo
echo "若提示未登录，说明线上那份也过期了：打开 http://127.0.0.1:8011/login 重新扫码。"
