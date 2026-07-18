# Local Telegram Bot API server — raises file size limits to 2GB.
# Deploy this as a SEPARATE Render Web Service (Docker environment),
# then point your bot's LOCAL_BOT_API_URL env var at this service's URL.

FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y \
    make git zlib1g-dev libssl-dev gperf cmake g++ \
    && rm -rf /var/lib/apt/lists/*

RUN git clone --recursive https://github.com/tdlib/telegram-bot-api.git /src \
    && cd /src \
    && mkdir build && cd build \
    && cmake -DCMAKE_BUILD_TYPE=Release .. \
    && cmake --build . --target install -j$(nproc)

FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y ca-certificates && rm -rf /var/lib/apt/lists/*
COPY --from=builder /usr/local/bin/telegram-bot-api /usr/local/bin/telegram-bot-api

EXPOSE 8081

# api-id and api-hash are passed as env vars at runtime (see render.yaml / Render dashboard)
CMD telegram-bot-api --api-id=${TELEGRAM_API_ID} --api-hash=${TELEGRAM_API_HASH} \
    --http-port=8081 --dir=/data --temp-dir=/data/temp --local
