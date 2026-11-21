# Build from the repo root: docker build -t my-bot .
# The match runs it with no network, a read-only root (writable /tmp), 1 CPU, 512 MB,
# and the map mounted read-only at /arena.
#
# Base image pinned by digest (not just tag) so the build cannot silently change.
# Digest = the OCI index for python:3.13.15-slim-bookworm (includes linux/amd64),
# read from registry-1.docker.io on 2026-11-22. The tag is kept for readability;
# when both are given the digest wins.
FROM python:3.13.15-slim-bookworm@sha256:2325bb286ec344af3e5898cc224b5844e2707ac6e26b1632516fd3edc84a5e26
WORKDIR /bot
COPY . .
ENTRYPOINT ["python3", "-u", "/bot/bot.py"]
