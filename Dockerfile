FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && python -c "import httpx; print('httpx OK')"
COPY bot.py .
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=30s --start-period=60s --retries=3 \
  CMD pgrep -f "python bot.py" > /dev/null || exit 1
CMD ["python", "-u", "bot.py"]
