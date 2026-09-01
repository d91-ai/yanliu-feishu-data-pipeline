FROM python:3.11-slim

WORKDIR /app
COPY structured_generate_service.py .
COPY legacy_generate_table.py .

ENV PYTHONUNBUFFERED=1
CMD ["python", "structured_generate_service.py", "serve"]
