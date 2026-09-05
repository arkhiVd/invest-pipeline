FROM python:3.12.3-slim@sha256:afc139a0a640942491ec481ad8dda10f2c5b753f5c969393b12480155fe15a63

ENV INVEST_MODE=demo \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY . /app
RUN pip install --no-cache-dir uv==0.8.17 \
    && uv sync --frozen --no-dev

USER 65532:65532
CMD [".venv/bin/python", "-c", "import invest; print('invest-pipeline demo image')"]
