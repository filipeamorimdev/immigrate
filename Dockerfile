FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DATA_DIR=/data \
    LIBRARY_ROOT=/nas \
    HOME=/data \
    TMPDIR=/data/tmp

WORKDIR /srv
COPY requirements.lock pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir -r requirements.lock \
    && pip install --no-cache-dir --no-deps .

# /data is open to any user because compose runs the container as IMMIGRATE_UID,
# which is often not 1000. A new volume copies this mode.
RUN useradd --uid 1000 --no-create-home immigrate \
    && mkdir -p /data /nas \
    && chown immigrate /data \
    && chmod 1777 /data
USER immigrate

VOLUME /data
EXPOSE 8088
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s \
    CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8088/healthz', timeout=4)"
CMD ["immigrate"]
