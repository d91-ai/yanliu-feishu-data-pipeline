FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r /app/requirements.txt
COPY feishu_drive_to_bitable.py /app/
COPY version_retention.json /app/data/version_retention.json

CMD ["python", "/app/feishu_drive_to_bitable.py", "--help"]
