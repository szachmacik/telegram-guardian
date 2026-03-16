FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV PYTHONUNBUFFERED=1
# Healthcheck: verify python works (container stays up even without token)
HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
  CMD python -c "import httpx; print('ok')" || exit 1
CMD ["python", "-u", "bot.py"]
