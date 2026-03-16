FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=60s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import os; assert os.environ.get('TELEGRAM_BOT_TOKEN')" || exit 1
CMD ["python", "bot.py"]
