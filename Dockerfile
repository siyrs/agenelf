# Agenelf 运行镜像
# 说明：代码不打入镜像，运行时通过 docker-compose 挂载：
#   - app-fork/（只读，容器实际运行的代码）
#   - app-tmp/（可写，agent 自我迭代暂存区）
#   - scripts/（只读，底线脚本，agent 只能触发）
#   - data/ logs/ workspace/（可写运行时数据）
FROM python:3.12-slim

WORKDIR /agenelf

# 先仅拷贝依赖清单并安装，充分利用 Docker 层缓存：
# 只要 requirements.txt 不变，代码变动不会触发依赖重装。
COPY app/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    # 启动命令依赖 uvicorn；api.py 由独立实现提供，这里确保服务器本身可用
    && pip install --no-cache-dir "uvicorn>=0.30"

# 以非 root 用户运行，缩小容器内破坏面
RUN useradd --create-home --uid 1000 agenelf \
    && chown -R agenelf:agenelf /agenelf
USER agenelf

# 启动 HTTP API（api.py 位于运行时挂载的 /agenelf/app-fork 下）
CMD ["python", "-m", "uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "/agenelf/app-fork"]
