# 京东广告运营分析服务
# 基镜像用 python:3.12-slim，再装 playwright 依赖 —— 浏览器版本与 pip 包版本天然一致，
# 不用去猜 mcr.microsoft.com/playwright 的 tag 是否已发布。
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Shanghai \
    JD_APP_DIR=/app \
    JD_DATA_DIR=/data \
    JD_AUTH_DIR=/auth \
    JD_CONFIG_DIR=/config \
    JD_EXCEL_PATH=/config/计算roi公式.xlsx \
    JD_WEB_HOST=0.0.0.0 \
    JD_WEB_PORT=8000 \
    JD_SCHEDULE_HOUR=0 \
    JD_SCHEDULE_MINUTE=5 \
    JD_REFRESH_DAYS=15 \
    JD_MAX_BACKFILL_DAYS=120 \
    JD_HEADLESS=1 \
    JD_ENABLE_SCHEDULER=1 \
    JD_RUN_ON_START=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt /app/requirements.txt

# PyPI 官方源在国内网络下极慢，pip 会 ReadTimeoutError 直接导致构建失败。
# 默认用官方源；国内换成镜像源即可，例如：
#   docker build --build-arg PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ .
ARG PIP_INDEX_URL=https://pypi.org/simple
RUN pip install --no-cache-dir --index-url "$PIP_INDEX_URL" --retries 5 --timeout 60 \
        -r /app/requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/* /root/.cache/pip

COPY jd_roi /app/jd_roi
COPY config /app/config-default
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh && mkdir -p /data /auth /config

EXPOSE 8000
VOLUME ["/data", "/auth", "/config"]

HEALTHCHECK --interval=60s --timeout=8s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5).status==200 else 1)"

ENTRYPOINT ["/app/entrypoint.sh"]
