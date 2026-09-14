FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    JOBNKILL_ENV=production \
    JOBNKILL_HOST=0.0.0.0 \
    JOBNKILL_PORT=8787

WORKDIR /app

RUN groupadd --system jobandkill && useradd --system --gid jobandkill --home-dir /app jobandkill

COPY requirements.txt requirements-production.txt ./
RUN python -m pip install --upgrade pip && python -m pip install -r requirements-production.txt

COPY jobandkill ./jobandkill
COPY web ./web
COPY README.md ./

RUN chown -R jobandkill:jobandkill /app
USER jobandkill

EXPOSE 8787
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import json,os,urllib.request; port=os.environ.get('PORT',os.environ.get('JOBNKILL_PORT','8787')); assert json.load(urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=3))['status']=='ok'"

CMD ["sh", "-c", "exec gunicorn 'jobandkill.wsgi:create_app()' --preload --bind 0.0.0.0:${PORT:-8787} --worker-class sync --workers 2 --threads 1 --timeout 30 --graceful-timeout 25 --max-requests 1000 --max-requests-jitter 100 --limit-request-line 4094 --limit-request-fields 50 --limit-request-field_size 8190 --logger-class jobandkill.gunicorn_logging.RedactingLogger"]
