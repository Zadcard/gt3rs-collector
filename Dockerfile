FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY collect.py daemon.py ./
RUN useradd --system --uid 10001 collector
USER collector
ENV PYTHONUNBUFFERED=1
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s CMD python -c "import json,time; from pathlib import Path; assert time.time()-json.loads(Path('/tmp/club-collector-health.json').read_text())['heartbeat']<65"
CMD ["python", "daemon.py"]
