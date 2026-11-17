# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e

FROM node:22-bookworm-slim@sha256:de170b39439eb0e5a3444ad7d60b015614a381e10fecae2e30ec9e735122f38d AS node

# Build stage. It runs on the build host's own platform and fetches, for the target platform,
# every Python wheel against its hash-locked manifest and the three Playwright browser archives
# against the sha256 pinned beside each, so only checked, installed files reach the image.
FROM --platform=$BUILDPLATFORM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS fetch
ARG TARGETOS
ARG TARGETARCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
COPY uv-requirements.lock requirements.lock /build/
# surface_docker.unpinned_packages reads separate option values as packages; this line
# installs only from the --require-hashes lock file above.
RUN pip install --no-deps --require-hashes -r /build/uv-requirements.lock \
    && case "${TARGETOS}/${TARGETARCH}" in \
         linux/amd64) platform=x86_64-manylinux_2_36 ;; \
         linux/arm64) platform=aarch64-manylinux_2_36 ;; \
         *) echo "unsupported target ${TARGETOS}/${TARGETARCH}" >&2; exit 1 ;; \
       esac \
    && uv pip install --require-hashes --only-binary=:all: \
         --python /usr/local/bin/python3.12 --python-platform="$platform" \
         --target /build/site -r /build/uv-requirements.lock -r /build/requirements.lock \
    && mv /build/site/bin /build/site-bin
RUN python3 - <<'EOF'
# Playwright 1.64.1 names these archives for Chromium revision 1248 (Chrome for Testing
# 156.0.8078.4), its headless shell, and ffmpeg 1013. Playwright publishes no checksums, so
# each sha256 below is trust-on-first-use: the task author fetched every archive once and
# pinned the digest beside its URL with the fetch time.
import hashlib, os, urllib.request, zipfile
ARCHIVES = {
    "amd64": [
        # https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-linux64.zip
        # sha256 43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79, fetched 2026-10-27T14:29:56Z
        ("chromium-1248", "https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-linux64.zip", "43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79"),
        # https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip
        # sha256 c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20, fetched 2026-10-27T14:30:05Z
        ("chromium_headless_shell-1248", "https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux64/chrome-headless-shell-linux64.zip", "c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20"),
        # https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1013/ffmpeg-linux.zip
        # sha256 ee986434d158f74fb5d9c5ff1092026ceedb63f324fe90b5be89460926b7b679, fetched 2026-10-27T14:30:07Z
        ("ffmpeg-1013", "https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1013/ffmpeg-linux.zip", "ee986434d158f74fb5d9c5ff1092026ceedb63f324fe90b5be89460926b7b679"),
    ],
    "arm64": [
        # https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux-arm64/chrome-linux-arm64.zip
        # sha256 910a12fff6170202195f72ef3d353801c7063d301ab80b1f51b77d791fda60ce, fetched 2026-10-27T14:30:22Z
        ("chromium-1248", "https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux-arm64/chrome-linux-arm64.zip", "910a12fff6170202195f72ef3d353801c7063d301ab80b1f51b77d791fda60ce"),
        # https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux-arm64/chrome-headless-shell-linux-arm64.zip
        # sha256 81ab9d4d3c301dc5fdfb6c541b9e6632b16fc819a6a854460f40a76fe2b2c476, fetched 2026-10-27T14:30:31Z
        ("chromium_headless_shell-1248", "https://cdn.playwright.dev/builds/cft/156.0.8078.4/linux-arm64/chrome-headless-shell-linux-arm64.zip", "81ab9d4d3c301dc5fdfb6c541b9e6632b16fc819a6a854460f40a76fe2b2c476"),
        # https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1013/ffmpeg-linux-arm64.zip
        # sha256 457360e803a7d8d5f8f7f5ecf562b2d338f6b4f7f86339d0c31938a451e8878b, fetched 2026-10-27T14:30:33Z
        ("ffmpeg-1013", "https://cdn.playwright.dev/dbazure/download/playwright/builds/ffmpeg/1013/ffmpeg-linux-arm64.zip", "457360e803a7d8d5f8f7f5ecf562b2d338f6b4f7f86339d0c31938a451e8878b"),
    ],
}
for name, url, digest in ARCHIVES[os.environ["TARGETARCH"]]:
    path = "/build/" + url.rsplit("/", 1)[1]
    urllib.request.urlretrieve(url, path)
    sha = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    if sha.hexdigest() != digest:
        raise SystemExit(f"{url}: sha256 {sha.hexdigest()} is not the pinned {digest}")
    target = "/build/playwright/" + name
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            written = archive.extract(info, target)
            mode = (info.external_attr >> 16) & 0o777
            if mode and not info.is_dir():
                os.chmod(written, mode)
    open(target + "/INSTALLATION_COMPLETE", "w").close()
    os.remove(path)
EOF

FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70 AS runtime
ARG VCS_REF=unset
# A RUN step sees SOURCE_DATE_EPOCH only when the stage declares it. Declared here, it makes
# adduser and useradd date their /etc/shadow entries at the build epoch, not the build day.
ARG SOURCE_DATE_EPOCH
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG DEBIAN_FRONTEND=noninteractive
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# Debian packages come from the archive snapshot of 2026-10-25. The second group is the exact
# set Playwright 1.64.1 installs for Chromium on Debian 12, pinned at the versions it resolved.
# No compiler ships: the submission runs as built (instruction R13.3), and every wheel is binary.
# The package scripts also create a default PostgreSQL cluster, a snakeoil private key and
# certificate, logs and an ldconfig cache stamped at build time, and font caches keyed to the
# build-time directory times, which the epoch-clamped layer never matches. Nothing here uses
# them, since services/up.sh initializes its own cluster, so the same layer removes them.
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
      libpq5=15.19-0+deb12u1 \
      xvfb=2:21.1.7-3+deb12u13 \
      fonts-noto-color-emoji=2.042-0+deb12u1 \
      fonts-unifont=1:15.0.01-2 \
      libfontconfig1=2.14.1-4 \
      libfreetype6=2.12.1+dfsg-5+deb12u4 \
      xfonts-scalable=1:1.0.3-1.3 \
      fonts-liberation=1:1.07.4-11 \
      fonts-ipafont-gothic=00303-23 \
      fonts-wqy-zenhei=0.9.45-8 \
      fonts-tlwg-loma-otf=1:0.7.3-1 \
      fonts-freefont-ttf=20120503-10 \
      libasound2=1.2.8-1+b1 \
      libatk-bridge2.0-0=2.46.0-5 \
      libatk1.0-0=2.46.0-5 \
      libatspi2.0-0=2.46.0-5 \
      libcairo2=1.16.0-7 \
      libcups2=2.4.2-3+deb12u9 \
      libdbus-1-3=1.14.10-1~deb12u1 \
      libdrm2=2.4.114-1+b1 \
      libgbm1=22.3.6-1+deb12u2 \
      libglib2.0-0=2.74.6-2+deb12u9 \
      libnspr4=2:4.35-1 \
      libnss3=2:3.87.1-1+deb12u4 \
      libpango-1.0-0=1.50.12+ds-1 \
      libx11-6=2:1.8.4-2+deb12u2 \
      libxcb1=1.15-1 \
      libxcomposite1=1:0.4.5-1 \
      libxdamage1=1:1.1.6-1 \
      libxext6=2:1.3.4-1+b1 \
      libxfixes3=1:6.0.0-2 \
      libxkbcommon0=1.5.0-1 \
      libxrandr2=2:1.5.2-2+b1 \
    && pg_dropcluster 15 main \
    && find /etc/ssl/certs -maxdepth 1 -lname ssl-cert-snakeoil.pem -delete \
    && rm -f /etc/ssl/private/ssl-cert-snakeoil.key /etc/ssl/certs/ssl-cert-snakeoil.pem \
    && rm -rf /var/log/apt/* /var/log/dpkg.log /var/log/alternatives.log /var/log/fontconfig.log \
         /var/cache/ldconfig/aux-cache /var/cache/fontconfig/*.cache-* \
    && rm -rf /var/lib/apt/lists/*

COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
COPY --from=fetch /build/site/ /usr/local/lib/python3.12/site-packages/
COPY --from=fetch /build/site-bin/ /usr/local/bin/
COPY --from=fetch /build/playwright/ /opt/playwright/
# The install below finds every archive already in place and only validates its host
# dependencies; the download host points nowhere, so any attempt to fetch fails the build.
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    && ln -s ../lib/node_modules/corepack/dist/corepack.js /usr/local/bin/corepack \
    && uv --version && LITELLM_LOCAL_MODEL_COST_MAP=True rewardkit --help > /dev/null \
    && PLAYWRIGHT_DOWNLOAD_HOST=http://127.0.0.1:9 playwright install chromium \
    && groupadd --gid 1000 app && useradd --uid 1000 --gid 1000 --create-home --shell /bin/bash app \
    && groupadd --gid 1001 svc && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash svc \
    && mkdir -p /app /etc/physio /var/lib/svc /run/svc /logs/verifier /logs/artifacts \
    && chown 1000:1000 /app \
    && chown 1001:1001 /var/lib/svc /run/svc \
    && chmod 755 /etc/physio /run/svc /opt/playwright
# The grader is readable by root and the services identity alone; the submission, uid 1000,
# reads none of it.
COPY --chown=0:1001 --chmod=750 . /tests
ENV PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:/usr/lib/postgresql/15/bin \
    LANG=C.UTF-8
LABEL org.opencontainers.image.source="ethara/harborview-physio" \
      org.opencontainers.image.revision="${VCS_REF}" \
      org.opencontainers.image.licenses="LicenseRef-Proprietary" \
      org.opencontainers.image.base.name="docker.io/library/python:3.12-slim-bookworm" \
      org.opencontainers.image.base.digest="sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70"
WORKDIR /tests
USER 1001:1001
CMD ["sleep", "infinity"]
