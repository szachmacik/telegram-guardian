FROM python:3.12-slim
WORKDIR /app
# Install wget for healthcheck + curl for debugging
RUN apt-get update -qq && apt-get install -y --no-install-recommends wget procps && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV PYTHONUNBUFFERED=1
# Healthcheck: bot process is running (telegram bots don't serve HTTP)
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
  CMD pgrep -f "python.*bot.py" > /dev/null || exit 1
CMD ["python", "-u", "bot.py"]
