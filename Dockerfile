# 昨日酒馆 · 镜像
# 同一镜像可跑两种模式:
#   - 后台自动演化守护进程(默认 CMD):python -m town_tavern.daemon
#   - 交互式 CLI(docker compose run cli):python -m town_tavern.main
FROM python:3.12-slim

# 不写 .pyc、输出不缓冲(便于 docker logs 实时查看)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TOWN_TAVERN_DB=/data/town_tavern.db

WORKDIR /app

# 先装依赖(利用层缓存:依赖不变时不重装)
COPY town_tavern/requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r requirements.txt

# 再拷贝源码(含 data/ 种子文件;.env 由 .dockerignore 排除,运行时注入)
COPY town_tavern ./town_tavern

# 存档数据库落到可挂载的卷,保证容器重启不丢进度
VOLUME ["/data"]

# 默认以"后台自动演化守护进程"运行
CMD ["python", "-m", "town_tavern.daemon"]
