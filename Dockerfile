FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tradingbot ./tradingbot
COPY config.example.yaml config.trading212.example.yaml ./
# Mount config.yaml, .env, state/ and logs/ at runtime.
# The engine rewrites its state file on every poll, so a stale file means the bot is stuck.
HEALTHCHECK --interval=5m --timeout=10s --start-period=5m \
  CMD find /app/state -name '*.json' -mmin -15 | grep -q .
ENTRYPOINT ["python", "-m", "tradingbot"]
CMD ["paper"]
