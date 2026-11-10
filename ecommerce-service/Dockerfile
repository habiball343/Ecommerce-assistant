# Python 3.12 轻量镜像
FROM docker.1ms.run/library/python:3.12-slim

WORKDIR /app

# 安装 uv
RUN pip install uv -i https://mirrors.aliyun.com/pypi/simple/

ENV UV_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/

# 先复制依赖清单，利用分层缓存
COPY pyproject.toml uv.lock ./

# 仅安装生产依赖，并严格按 lock 文件安装
RUN uv sync --frozen --no-dev

# 再复制业务代码
COPY . .

EXPOSE 8001

CMD ["uv", "run", "uvicorn", "ecommerce_service.main:app", "--host", "0.0.0.0", "--port", "8001"]
