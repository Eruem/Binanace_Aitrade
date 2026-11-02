# 运行镜像:交易循环(loop)/ 只读监控面板(dashboard)。
# .env 不打包(.dockerignore 排除);密钥全部经 compose env_file 运行时注入。
# 镜像级安全默认 = Paper:裸 docker run 无 .env 时绝不触碰真实资金;
# compose 服务经 env_file 覆盖为 live(环境变量优先级高于镜像 ENV)。
FROM python:3.14-slim

# PYTHONUTF8:容器内中文/JSON 输出统一 UTF-8;
# TZ:日志时间戳与日亏损跨日边界与宿主机本地时区一致
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    TZ=Asia/Shanghai \
    TRADING_MODE=paper \
    ENABLE_LIVE_TRADING=false \
    ENABLE_BINANCE_TESTNET=false

RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime \
    && echo $TZ > /etc/timezone

WORKDIR /app

RUN useradd --create-home --uid 10001 appuser

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .
RUN chown -R appuser:appuser /app
USER appuser

# 默认常驻循环;compose 中各服务用 command 覆盖
CMD ["python", "scripts/run_agent.py", "--loop", "--interval", "900"]
