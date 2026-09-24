FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY tradingbot ./tradingbot
# Mount config.yaml, .env, state/ and logs/ at runtime.
ENTRYPOINT ["python", "-m", "tradingbot"]
CMD ["paper"]
