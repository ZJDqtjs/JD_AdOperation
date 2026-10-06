#!/usr/bin/env bash
set -euo pipefail

: "${JD_DATA_DIR:=/data}"
: "${JD_AUTH_DIR:=/auth}"
: "${JD_CONFIG_DIR:=/config}"
: "${JD_WEB_PORT:=8000}"
: "${JD_WEB_HOST:=0.0.0.0}"

mkdir -p "$JD_DATA_DIR" "$JD_AUTH_DIR" "$JD_CONFIG_DIR"

# 首次启动：把示例配置铺到挂载目录，方便用户直接编辑
for f in accounts.example.json costs.example.json; do
  if [ -f "/app/config-default/$f" ] && [ ! -f "$JD_CONFIG_DIR/$f" ]; then
    cp "/app/config-default/$f" "$JD_CONFIG_DIR/$f"
  fi
done
# 成本表：/config 下没有就提示（可从宿主机拷进来，或直接改 config 目录）
if [ ! -f "${JD_EXCEL_PATH:-/config/计算roi公式.xlsx}" ]; then
  echo "[entrypoint][warn] 未找到成本表 ${JD_EXCEL_PATH:-/config/计算roi公式.xlsx}，" \
       "将使用内置示例成本（客单价100/成本70/运费5 → 保本ROI 4.00）。" \
       "建议把 计算roi公式.xlsx 放到挂载的 config 目录。"
fi

echo "[entrypoint] data=$JD_DATA_DIR auth=$JD_AUTH_DIR config=$JD_CONFIG_DIR tz=${TZ:-Asia/Shanghai}"
echo "[entrypoint] 调度 每天 ${JD_SCHEDULE_HOUR:-0}:${JD_SCHEDULE_MINUTE:-5}，回刷最近 ${JD_REFRESH_DAYS:-15} 天"

# 单 worker：调度器与扫码登录会话都在进程内
exec uvicorn jd_roi.webapp:app \
  --host "$JD_WEB_HOST" \
  --port "$JD_WEB_PORT" \
  --workers 1 \
  --proxy-headers \
  --timeout-keep-alive 75 \
  --log-level "${JD_LOG_LEVEL:-info}"
