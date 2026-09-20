FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/freeze_thaw.db

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# 数据库目录交给非 root 用户；named volume 首次挂载会继承属主
RUN mkdir -p /data && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /data /app
USER appuser

EXPOSE 8000

# 容器启动即由应用初始化数据库（建表、索引）
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
