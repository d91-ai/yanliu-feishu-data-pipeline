FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY feishu_gateway.py skill_adapter.py source_contract_adapter.py minute_sanitize_service.py /app/

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/data/state \
    && chown -R appuser:appuser /app/data

USER appuser
EXPOSE 8791
CMD ["python", "/app/minute_sanitize_service.py", "doctor"]
