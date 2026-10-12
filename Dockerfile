FROM python:3.10-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Весь код целиком (список исключений — в .dockerignore): новые модули не надо прописывать по одному.
COPY . .

# data/ (база, источники, планы) и videos/ (скачанные видео) живут в томах, не в образе.
ENV DATA_DIR=/app/data VIDEO_DIR=/app/videos MOTHER_API_HOST=0.0.0.0
VOLUME ["/app/data", "/app/videos"]
EXPOSE 8787

CMD ["python", "bot.py"]
