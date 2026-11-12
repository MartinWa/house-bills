# syntax=docker/dockerfile:1
FROM node:24-bookworm-slim@sha256:01d566e8d94d8c619591d78652babfccd3c6992a3dffa9ed5ab39e11cf6c70e9 AS browser-node
FROM ghcr.io/astral-sh/uv:0.13.8@sha256:5cc1805e2426484abca4ad46ea783bab281fdb225fd82d8ce6d66a81254bac85 AS uv-bin

FROM python:3.14.10-slim-bookworm@sha256:3fe1a0b6c2d6b2ebb572906cdabeca75aeb558c0c17db5ffefbcf8094871c32b AS os-base
RUN apt-get update && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/*

FROM os-base AS sqlite-build
RUN apt-get update && apt-get install -y --no-install-recommends build-essential ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ADD --checksum=sha256:81f5be397049b0cae1b167f2225af7646fc0f82e4a9b3c48c9ea3a533e21d77a \
    https://www.sqlite.org/2026/sqlite-autoconf-3510300.tar.gz /tmp/sqlite.tar.gz
RUN tar -xzf /tmp/sqlite.tar.gz -C /tmp \
    && cd /tmp/sqlite-autoconf-3510300 \
    && ./configure --prefix=/opt/sqlite --enable-threadsafe --disable-static \
    && make -j2 && make install

FROM os-base AS dependency-exports
COPY --from=uv-bin /uv /bin/uv
WORKDIR /build
COPY pyproject.toml uv.lock ./
COPY tools/runtime_pins.py ./runtime_pins.py
RUN uv export --locked --no-dev --no-emit-project --format requirements-txt \
        --output-file requirements.txt > /dev/null \
    && uv export --locked --only-group dev --no-emit-project --format requirements-txt \
        --output-file requirements-dev.txt > /dev/null \
    && python runtime_pins.py uv.lock runtime-pins.json

FROM os-base AS python-build
COPY --from=dependency-exports /build/requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes \
    --prefix=/opt/python -r /tmp/requirements.txt

FROM os-base AS runtime-base
# The pdfplumber PDFium wheel needs the C++ runtime, not a native image-decoder build.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/opt/sqlite/lib
COPY --from=sqlite-build /opt/sqlite/lib /opt/sqlite/lib
COPY --from=python-build /opt/python /usr/local
COPY --from=dependency-exports /build/runtime-pins.json /opt/house-bills/runtime-pins.json
COPY tools/check_runtime.py /usr/local/bin/house-bills-check-runtime.py
RUN python /usr/local/bin/house-bills-check-runtime.py
WORKDIR /app

FROM runtime-base AS development
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && git config --system --add safe.directory /app \
    && rm -rf /var/lib/apt/lists/*
COPY --from=dependency-exports /build/requirements-dev.txt /opt/requirements-dev.txt
RUN python -m venv --system-site-packages /state/venv \
    && /state/venv/bin/python -m pip install --no-cache-dir --require-hashes --no-deps \
       -r /opt/requirements-dev.txt \
    && /state/venv/bin/python /usr/local/bin/house-bills-check-runtime.py \
    && mv /state/venv /opt/venv-seed \
    && tar -C /opt/venv-seed -cf - . | sha256sum > /opt/venv-seed.sha256 \
    && useradd --uid 1000 --create-home developer \
    && mkdir -p /state/tmp && chown -R 1000:1000 /state
ENV PATH=/state/venv/bin:$PATH \
    HOUSE_BILLS_DATA_DIR=/state/development HOUSE_BILLS_STATE_DIR=/state \
    TMPDIR=/state/tmp RUFF_CACHE_DIR=/state/ruff-cache \
    PYTEST_ADDOPTS="-o cache_dir=/state/pytest-cache" PIP_REQUIRE_VIRTUALENV=true
COPY tools/dev_entrypoint.py /usr/local/bin/house-bills-dev.py
COPY . /app
USER 1000:1000
ENTRYPOINT ["/usr/local/bin/python", "/usr/local/bin/house-bills-dev.py"]
CMD ["python", "-m", "tools.dev_server"]

# The browser toolchain is isolated from both application image targets.
FROM development AS browser
USER root
COPY --from=browser-node /usr/local/bin/node /usr/local/bin/node
COPY --from=browser-node /usr/local/lib/node_modules/npm /usr/local/lib/node_modules/npm
COPY package.json package-lock.json /opt/browser/
ENV NODE_PATH=/opt/browser/node_modules PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN cd /opt/browser \
    && node /usr/local/lib/node_modules/npm/bin/npm-cli.js ci --ignore-scripts \
    && node /opt/browser/node_modules/playwright/cli.js install --with-deps chromium \
    && chmod -R a+rX /opt/browser /opt/playwright \
    && rm -rf /var/lib/apt/lists/* /root/.npm
USER 1000:1000
CMD ["python", "-m", "tools.run_browser"]

# A plain build produces the production image.
FROM runtime-base AS production
ARG HOUSE_BILLS_VERSION=development
ENV HOUSE_BILLS_DATA_DIR=/data DJANGO_SETTINGS_MODULE=house_bills.settings.production \
    HOUSE_BILLS_VERSION=$HOUSE_BILLS_VERSION
LABEL org.opencontainers.image.licenses="PolyForm-Noncommercial-1.0.0"
RUN python -m pip uninstall --yes pip \
    && mkdir -p /data && chown 1000:1000 /data
COPY manage.py /app/manage.py
COPY house_bills /app/house_bills
COPY bills /app/bills
COPY LICENSE NOTICE /app/
COPY tools/__init__.py tools/check_production.py tools/check_recovery.py tools/check_http.py tools/synthetic_pdf.py /app/tools/
RUN HOUSE_BILLS_SECRET_KEY=synthetic-build-only-key-never-for-runtime-000000000000000000 \
    HOUSE_BILLS_ALLOWED_HOSTS=build.invalid python manage.py collectstatic --noinput
USER 1000:1000
RUN HOUSE_BILLS_SECRET_KEY=synthetic-build-only-key-never-for-runtime-000000000000000000 \
    HOUSE_BILLS_ALLOWED_HOSTS=build.invalid \
    python -c "from house_bills.wsgi import application; assert application"
CMD ["python", "-m", "house_bills.server"]
