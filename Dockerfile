FROM python:3.12-slim

WORKDIR /app

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends wget procps supervisor && \
    rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY antygravity_bot.py .

# Supervisor config — oba boty w jednym kontenerze
RUN mkdir -p /var/log/supervisor
COPY supervisord.conf /etc/supervisor/conf.d/bots.conf

ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD pgrep -f "python.*bot.py" > /dev/null || exit 1

CMD ["/usr/bin/supervisord", "-n", "-c", "/etc/supervisor/supervisord.conf"]
