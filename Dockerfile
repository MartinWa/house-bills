# syntax=docker/dockerfile:1
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

FROM os-base AS python-build
COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --no-cache-dir --require-hashes \
    --prefix=/opt/python -r /tmp/requirements.txt

FROM os-base AS runtime-base
# The pdfplumber PDFium wheel needs the C++ runtime, not a native image-decoder build.
RUN apt-get update && apt-get install -y --no-install-recommends libstdc++6 \
    && rm -rf /var/lib/apt/lists/*
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 LD_LIBRARY_PATH=/opt/sqlite/lib
COPY --from=sqlite-build /opt/sqlite/lib /opt/sqlite/lib
COPY --from=python-build /opt/python /usr/local
COPY tools/check_runtime.py /usr/local/bin/house-bills-check-runtime.py
RUN python /usr/local/bin/house-bills-check-runtime.py
WORKDIR /app

FROM runtime-base AS development
COPY requirements-dev.txt /opt/requirements-dev.txt
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

# A plain build produces the production image.
FROM runtime-base AS production
ENV HOUSE_BILLS_DATA_DIR=/data DJANGO_SETTINGS_MODULE=house_bills.settings.production
RUN python -m pip uninstall --yes pip \
    && mkdir -p /data && chown 1000:1000 /data
COPY manage.py /app/manage.py
COPY house_bills /app/house_bills
COPY bills /app/bills
COPY tools/__init__.py tools/check_production.py tools/check_http.py tools/synthetic_pdf.py /app/tools/
RUN HOUSE_BILLS_SECRET_KEY=synthetic-build-only-key-never-for-runtime-000000000000000000 \
    HOUSE_BILLS_ALLOWED_HOSTS=build.invalid python manage.py collectstatic --noinput
USER 1000:1000
RUN HOUSE_BILLS_SECRET_KEY=synthetic-build-only-key-never-for-runtime-000000000000000000 \
    HOUSE_BILLS_ALLOWED_HOSTS=build.invalid \
    python -c "from house_bills.wsgi import application; assert application"
CMD ["python", "-m", "house_bills.server"]
