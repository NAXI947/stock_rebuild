ARG PYTHON_BASE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM ${PYTHON_BASE}

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Asia/Shanghai \
    HOME=/app/tmp/system \
    TMPDIR=/app/tmp/system
WORKDIR /app
COPY requirements-stage2.lock ./
RUN pip install --disable-pip-version-check --no-cache-dir --only-binary=:all: --require-hashes -r requirements-stage2.lock \
    && groupadd --gid 10001 stock-v4 \
    && useradd --uid 10001 --gid 10001 --no-create-home --home-dir /app/tmp/system stock-v4 \
    && install -d -m 0700 -o 10001 -g 10001 /app/tmp/system /app/tmp/api /app/tmp/worker \
    && install -d -m 0755 /app/data /app/release-control

COPY modules ./modules
COPY shared_kernel ./shared_kernel
COPY web/index.html web/app.js web/style.css web/data-updates.js web/data-updates.css web/template-layout.js web/template-layout.css ./web/
COPY tests/fixtures/stage3_manual_v14.json ./tests/fixtures/

USER 10001:10001
EXPOSE 8000
CMD ["python", "-m", "modules.strategy_hermes.runtime", "api"]
