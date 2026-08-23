FROM python:3.12-slim

WORKDIR /app

# Install deps first so this layer is cached across code-only changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Bind 0.0.0.0 (not the local-dev default 127.0.0.1) so Docker's port
# mapping can reach it — safe because main.py's serve command only ever
# serves frontend/, output/, API.md, and /api/*, and /api/* is gated by
# API_KEY when it's set. Port 80 is mapped in from outside at `docker run`
# time (see DEPLOY.md); the app itself listens on 8080 here.
ENV SERVE_HOST=0.0.0.0
ENV PORT=8080
EXPOSE 8080

CMD ["python", "main.py", "serve"]
