# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS node

FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS fetch
ARG TARGETOS
ARG TARGETARCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
COPY uv-requirements.lock /build/uv-requirements.lock
RUN pip install --no-deps --require-hashes -r /build/uv-requirements.lock \
    && case "${TARGETOS}/${TARGETARCH}" in \
         linux/amd64) platform=x86_64-manylinux_2_36 ;; \
         linux/arm64) platform=aarch64-manylinux_2_36 ;; \
         *) echo "unsupported target ${TARGETOS}/${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && uv pip install --no-deps --require-hashes --only-binary=:all: \
         --python /usr/local/bin/python3.12 --python-platform="$platform" \
         --target /build/uv -r /build/uv-requirements.lock \
    && mv /build/uv/bin /build/uv-bin

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ARG VCS_REF=unset
ARG SOURCE_DATE_EPOCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 PYTHONDONTWRITEBYTECODE=1
# Debian packages come from the archive snapshot of 2026-10-25. build-essential and libpq-dev let the
# agent compile native packages into the workspace during its own phase.
RUN printf '%s\n' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261025T202701Z bookworm main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian/20261025T202701Z bookworm-updates main' \
      'deb [check-valid-until=no] https://snapshot.debian.org/archive/debian-security/20261025T235307Z bookworm-security main' \
      > /etc/apt/sources.list \
    && rm -f /etc/apt/sources.list.d/debian.sources \
    && printf 'Acquire::Retries "5";\nAcquire::http::Timeout "60";\nAcquire::https::Timeout "60";\n' > /etc/apt/apt.conf.d/80-forge \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
      ca-certificates=20250419~deb12u2 \
      curl=7.88.1-10+deb12u15 \
      git=1:2.39.5-0+deb12u3 \
      procps=2:4.0.2-3 \
      postgresql-15=15.19-0+deb12u1 \
      postgresql-client-15=15.19-0+deb12u1 \
      postgresql-common=248+deb12u1 \
      postgresql-client-common=248+deb12u1 \
      libpq-dev=15.19-0+deb12u1 \
      build-essential=12.9 \
    && pg_dropcluster 15 main \
    && find /etc/ssl/certs -maxdepth 1 -lname ssl-cert-snakeoil.pem -delete \
    && rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
    && rm -rf /var/log/apt/* /var/log/dpkg.log /var/log/alternatives.log /var/cache/ldconfig/aux-cache \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=fetch /build/uv/ /usr/local/lib/python3.12/site-packages/
COPY --from=fetch /build/uv-bin/ /usr/local/bin/
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && node --version && npm --version && python3 --version && uv --version \
    && rm -rf /tmp/node-compile-cache \
    && groupadd --gid 1000 agent && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash agent \
    && groupadd --gid 1001 svc && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash svc \
    && mkdir -p /app /etc/shiftbook /var/lib/svc /run/svc /logs/agent \
    && chown 1000:1000 /app /logs/agent \
    && chown 1001:1001 /var/lib/svc /run/svc \
    && chmod 755 /etc/shiftbook /run/svc
COPY --chmod=755 services /opt/svc/services
ENV PATH=/usr/local/bin:/usr/bin:/bin:/usr/lib/postgresql/15/bin \
    LANG=C.UTF-8 \
    PORT=8080 \
    SLUG=shiftbook
LABEL org.opencontainers.image.source="ethara/shiftbook-payroll" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm"
WORKDIR /app
USER root
ENTRYPOINT ["/opt/svc/services/entrypoint.sh"]
CMD ["sleep", "infinity"]
