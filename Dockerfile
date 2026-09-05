FROM ghcr.io/astral-sh/uv:0.8.17@sha256:e4644cb5bd56fdc2c5ea3ee0525d9d21eed1603bccd6a21f887a938be7e85be1 AS uv
FROM python:3.12-slim@sha256:78387bc3881b8273120a12ebe6c1ab22b018ccc2c9adf565ae1ac9b536e184ea

ENV INVEST_MODE=demo \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
COPY . /app
RUN uv sync --frozen --no-dev \
    && rm /usr/local/bin/uv

USER 65532:65532
CMD [".venv/bin/python", "-c", "import invest; print('invest-pipeline')"]
