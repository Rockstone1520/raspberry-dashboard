FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# gcc solo por si psutil no tiene wheel para tu arquitectura (armv7)
RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc python3-dev curl \
 && rm -rf /var/lib/apt/lists/*

COPY app/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && apt-get purge -y gcc python3-dev && apt-get autoremove -y

COPY app/ .

EXPOSE 8088

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
  CMD curl -fs http://localhost:8088/healthz || exit 1

# 1 worker: el recolector vive en un hilo del proceso; varios hilos para las peticiones
CMD ["gunicorn", "-w", "1", "--threads", "4", "-b", "0.0.0.0:8088", "main:app"]
