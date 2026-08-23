FROM python:3.12-slim

WORKDIR /app

# Install deps first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind 0.0.0.0 (not the local-dev default 127.0.0.1) so the Caddy
# container (see docker-compose.yml / Caddyfile) can reach it over the
# compose network — safe because main.py's serve command only ever
# serves frontend/, output/, API.md, and /api/*, and /api/* is gated by
# API_KEY when it's set. Caddy is what's actually reachable from outside,
# on 80/443; this container is never directly exposed to the host.
ENV SERVE_HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py", "serve"]
