FROM guysoft/uwsgi-nginx:python3.7

LABEL maintainer="hunshcn <hunsh.cn@gmail.com>"

RUN pip install flask requests

COPY ./app /app
WORKDIR /app

# Make /app/* available to be imported by Python globally to better support several use cases like Alembic migrations.
ENV PYTHONPATH=/app

# 并发容量默认值（见 opensourceways/ascend-ci-deployment#2463）：
# 基础镜像默认 16 个 uwsgi 进程、单 nginx worker（1024 连接），一个大的
# git-upload-pack 会占住 worker 上百秒，并发 clone 时小请求排队甚至 502。
# 进程数按线上单 worker 实测约 26MB 内存估算（32 × 26MB ≈ 830MB），
# 部署方需保证 2Gi 以上内存限额；镜像内 nginx 配置由 entrypoint 按下面
# 两个 NGINX_* 变量生成，uwsgi 通过 UWSGI_* 环境变量覆盖 ini 默认值。
ENV NGINX_WORKER_PROCESSES=2 \
    NGINX_WORKER_CONNECTIONS=8192 \
    UWSGI_PROCESSES=32

# Move the base entrypoint to reuse it
RUN mv /entrypoint.sh /uwsgi-nginx-entrypoint.sh
# Copy the entrypoint that will generate Nginx additional configs
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]

# Run the start script provided by the parent image tiangolo/uwsgi-nginx.
# It will check for an /app/prestart.sh script (e.g. for migrations)
# And then will start Supervisor, which in turn will start Nginx and uWSGI

EXPOSE 80

CMD ["/start.sh"]
