FROM python:3.13.7-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
COPY packages/python-platform /transport
COPY services/ai-core/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt /transport && useradd --uid 10001 --create-home app
COPY services/ai-core .
USER app
EXPOSE 8080
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080", "--timeout-keep-alive", "5", "--limit-concurrency", "100", "--no-access-log"]
