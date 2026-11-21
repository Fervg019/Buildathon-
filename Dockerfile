# Build from the repo root: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB,
# and the map mounted read-only at /arena.
FROM python:3.13.15-slim-bookworm
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
