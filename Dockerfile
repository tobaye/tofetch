FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends aria2 ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY server.py aria2.conf ./
COPY static ./static

ENV TOFETCH_PORT=8077 \
    TOFETCH_DOWNLOAD_DIR=/downloads \
    TOFETCH_DATA_DIR=/data

VOLUME ["/downloads", "/data"]
EXPOSE 8077

CMD ["python3", "server.py"]
