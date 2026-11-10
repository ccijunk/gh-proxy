#! /usr/bin/env bash
set -euo pipefail

# Get the listen port for Nginx, default to 80
USE_LISTEN_PORT=${LISTEN_PORT:-80}
USE_NGINX_WORKER_PROCESSES=${NGINX_WORKER_PROCESSES:-1}
USE_NGINX_WORKER_CONNECTIONS=${NGINX_WORKER_CONNECTIONS:-1024}

# 渲染 nginx 主配置，具体取值由部署清单的 env 决定
sed \
    -e "s/@LISTEN_PORT@/${USE_LISTEN_PORT}/g" \
    -e "s/@NGINX_WORKER_PROCESSES@/${USE_NGINX_WORKER_PROCESSES}/g" \
    -e "s/@NGINX_WORKER_CONNECTIONS@/${USE_NGINX_WORKER_CONNECTIONS}/g" \
    /app/nginx/nginx.conf > /etc/nginx/nginx.conf

exec "$@"
