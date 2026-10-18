# Bisect web app: FastAPI backend + static Next.js UI in one image.
# The app launches sandbox containers through the HOST Docker daemon (sibling containers),
# so run it with the docker socket mounted and BISECT_DATA_DIR bind-mounted at the SAME
# path on host and container (snapshots are bind-mounted into sandboxes by host path).
FROM node:20-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
# Static Docker CLI only; the daemon is the host's (socket mounted at runtime).
COPY --from=docker:27-cli /usr/local/bin/docker /usr/local/bin/docker
WORKDIR /app
COPY backend/ backend/
RUN pip install --no-cache-dir ./backend
COPY sandbox/ sandbox/
COPY seeds/ seeds/
COPY --from=web /web/out web/out
ENV BISECT_DATA_DIR=/var/lib/bisect PYTHONUNBUFFERED=1
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "bisect_agent.server:app", "--host", "0.0.0.0", "--port", "8000", "--app-dir", "backend"]
