FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FASTEMBED_CACHE_PATH=/models \
    EMBEDDING_MODEL=BAAI/bge-small-zh-v1.5

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

# 从 FastEmbed 官方模型包下载量化 BGE，避免构建时依赖 Hugging Face Xet 传输。
# 运行期以 local_files_only 加载，不依赖公网或 API Key。
RUN python -c "import tarfile, urllib.request; url='https://storage.googleapis.com/qdrant-fastembed/fast-bge-small-zh-v1.5.tar.gz'; archive='/tmp/bge.tar.gz'; urllib.request.urlretrieve(url, archive); tarfile.open(archive).extractall('/models')"

COPY app.py .
RUN useradd -m -u 1000 embedding && chown -R embedding:embedding /app /models
USER embedding

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health')"

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
