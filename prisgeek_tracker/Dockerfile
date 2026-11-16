FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PRISGEEK_DB_PATH=/data/prisgeek_tracker.db \
    HOST=0.0.0.0 \
    PORT=8050

WORKDIR /app

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY db.py prisgeek_client.py notifier.py watcher.py mcp_server.py dashboard.py addon_entrypoint.py /app/

EXPOSE 8050

CMD ["python", "/app/addon_entrypoint.py"]
