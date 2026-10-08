FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install --with-deps chromium
COPY bot.py .
# Memory/state lives here. On Railway, attach a Volume at /data to keep it across deploys.
RUN mkdir -p /data/.openjarvis
ENV OPENJARVIS_HOME=/data/.openjarvis
CMD ["python", "bot.py"]
