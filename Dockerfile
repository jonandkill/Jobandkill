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

CMD ["python", "-m", "jobandkill", "serve"]
