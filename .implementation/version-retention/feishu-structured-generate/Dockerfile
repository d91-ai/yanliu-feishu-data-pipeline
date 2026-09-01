FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY structured_generate_service.py /app/
COPY skill_contract.py /app/

EXPOSE 8790
CMD ["python", "/app/structured_generate_service.py", "doctor"]
