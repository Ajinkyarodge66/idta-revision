FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PORT=8080
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app app
COPY migrate.py .
RUN useradd -r -u 10001 api && chown -R api /srv
USER api
# 2 worker processes per container; Cloud Run adds containers automatically as traffic grows
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --workers ${WORKERS:-2} --proxy-headers --forwarded-allow-ips='*' --no-server-header
