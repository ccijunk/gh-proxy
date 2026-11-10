FROM python:3.12-slim

LABEL maintainer="hunshcn <hunsh.cn@gmail.com>"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app

# nginx 做入口并负责慢客户端缓冲，supervisor 同时管理 nginx 和应用进程
RUN apt-get update \
    && apt-get install -y --no-install-recommends nginx supervisor \
    && rm -rf /var/lib/apt/lists/* \
    && pip install "aiohttp>=3.10" "uvloop>=0.20" \
    && mkdir -p /var/log/nginx

COPY ./app /app
COPY ./nginx/nginx.conf /app/nginx/nginx.conf
COPY ./supervisord.conf /etc/supervisor/supervisord.conf

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

WORKDIR /app

EXPOSE 80

ENTRYPOINT ["/entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/supervisor/supervisord.conf"]
