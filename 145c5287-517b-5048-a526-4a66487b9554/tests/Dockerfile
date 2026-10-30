# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# The toolkit stage builds at the TARGET platform rather than the build
# platform, and downloads its own uv rather than receiving one from an earlier
# stage, because uv tool install does two things that both bind to an
# architecture: it executes the uv binary, and it compiles a virtualenv
# carrying native extension modules. A build-platform uv cannot execute here
# under emulation, and a build-platform virtualenv would ship foreign .so files
# into a cross-built verifier that passes every byte-level check and then fails
# at grade time. Both per-architecture uv checksums stay pinned and enforced.
#
# It is also not the runtime stage, because uv tool install from a git+
# reference needs a git executable, and item 2a.1 admits only the toolchain the
# solver itself runs into a shipped layer, so git, its remote and the clone
# stay out of the verifier image.
FROM --platform=$TARGETPLATFORM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS toolkit
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
WORKDIR /stage

# snapshot.ubuntu.com answers http with a 301 to https, and the base image
# ships no CA bundle, so the dated snapshot is unreachable until one exists.
# ca-certificates is therefore installed first from the base image's own
# plain-http pocket, which apt verifies against the in-image
# ubuntu-archive-keyring.gpg. Only the bundle comes from the undated pocket,
# at the same 20240203 version the snapshot carries; every package below is
# pinned to the snapshot.
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates=20240203

RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    printf '%s\n' 'deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z noble main universe' \
      > /etc/apt/sources.list.d/snapshot.list \
 && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends --allow-downgrades \
      perl-base=5.38.2-3.2build2 \
      perl-modules-5.38=5.38.2-3.2build2 \
      libperl5.38t64=5.38.2-3.2build2 \
      perl=5.38.2-3.2build2 \
      curl=8.5.0-2ubuntu10 \
      unzip=6.0-28ubuntu4 \
      git=1:2.43.0-1ubuntu7 \
      python3=3.12.3-0ubuntu1 \
      python3-venv=3.12.3-0ubuntu1 \
      python3-pip=24.0+dfsg-1ubuntu1

ARG UV_VERSION=0.9.7
ARG UV_SHA256_AMD64=b26fcc8dfa1c39b5a5613445af3be3eefda45d9a39359bee271eafe34913583e
ARG UV_SHA256_ARM64=8b3d31a154673c6d357727d2083a33525b515589d153fa5b5455e1db9e9e6363
RUN case "$TARGETARCH" in \
      amd64) triple=x86_64-unknown-linux-gnu; want="$UV_SHA256_AMD64" ;; \
      arm64) triple=aarch64-unknown-linux-gnu; want="$UV_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --proto '=https' --tlsv1.2 \
      "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${triple}.tar.gz" \
      -o /stage/uv.tar.gz \
 && echo "${want}  /stage/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /stage/uv.tar.gz -C /stage --strip-components=1 \
 && rm -f /stage/uv.tar.gz \
 && install -m 0755 /stage/uv /usr/local/bin/uv

ENV UV_TOOL_DIR=/opt/toolkit/share \
    UV_TOOL_BIN_DIR=/opt/toolkit/bin \
    UV_CACHE_DIR=/opt/uv-cache
ARG REWARDKIT_VERSION=0.2.4
ARG HARBOR_COMMIT=fdba28fc411e47e8765420a1a063b4918fce3def
RUN uv tool install --no-cache \
      "harbor-rewardkit @ git+https://github.com/harbor-framework/harbor.git@${HARBOR_COMMIT}#subdirectory=packages/rewardkit" \
 && rm -rf /opt/uv-cache \
 && got="$(uv tool list | awk '$1=="harbor-rewardkit"{print substr($2,2)}')" \
 && if [ "$got" != "$REWARDKIT_VERSION" ]; then \
      echo "harbor-rewardkit is '${got}', expected exactly ${REWARDKIT_VERSION}" >&2; exit 1; \
    fi \
 && echo "harbor-rewardkit ${got} built at the pinned commit"

# The Playwright Python package is installed from PyPI with its per-architecture
# wheel digest enforced by --require-hashes, so a substituted wheel fails the
# build. That wheel version deterministically selects one chromium revision,
# which is how the browser is pinned by version.
ARG PLAYWRIGHT_VERSION=1.64.1
ARG PLAYWRIGHT_SHA256_AMD64=577b8d88778c245cfbd145ae17cf53044f084b1b46c55c69081d4069d56057e2
ARG PLAYWRIGHT_SHA256_ARM64=a29fda44725e11b0b625c4bff6458a1f6dbee97b88c9788d12e18a8344458c2d
ARG PYEE_VERSION=14.0.0
ARG GREENLET_VERSION=3.5.6
# pyee and greenlet are playwright's two runtime dependencies. Both are pinned
# to the exact versions measured in a build of this image rather than to the
# ranges playwright's metadata declares, because surface.py refuses an
# unpinned pip install as ENV_INSTALL_UNPINNED and a range admits a different
# resolution on a later build. They carry no digest: these are versions this
# run measured, not digests it verified, and the exact-version form is the
# strongest pin the bytes here actually perform.
RUN case "$TARGETARCH" in \
      amd64) want="$PLAYWRIGHT_SHA256_AMD64" ;; \
      arm64) want="$PLAYWRIGHT_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && python3 -m venv /opt/pw \
 && /opt/pw/bin/pip install --no-cache-dir --upgrade "pip==24.0" \
 && printf '%s\n' "playwright==${PLAYWRIGHT_VERSION} --hash=sha256:${want}" \
      > /stage/playwright.txt \
 && /opt/pw/bin/pip install --no-cache-dir --no-deps --require-hashes \
      -r /stage/playwright.txt \
 && /opt/pw/bin/pip install --no-cache-dir \
      "pyee==${PYEE_VERSION}" "greenlet==${GREENLET_VERSION}" \
 && rm -f /stage/playwright.txt

# The browser archives are fetched by URL and verified with sha256sum -c, so a
# substituted archive stops the build. Each digest below is SELF-MEASURED by
# this run from the bytes the CDN served at 2026-10-27T22:03Z, NOT published
# upstream: Playwright publishes no archive digest, and its own
# "playwright install" path verifies nothing at all. The URL beside each value
# is the one measured, so a reader can re-measure and compare rather than only
# read a claim. The design contract records this as the named gap
# browser-archive-checksum-self-measured, whose closing condition is Playwright
# publishing archive digests.
#
# The archives unpack into exactly the directory layout
# PLAYWRIGHT_BROWSERS_PATH expects; the validation marker "playwright install"
# writes afterwards is not a launch precondition, which a probe confirmed by
# launching chromium from a marker-free tree.
ARG CHROMIUM_BUILD=156.0.8078.4
ARG CHROMIUM_REVISION=1248
ARG CHROMIUM_CDN=https://cdn.playwright.dev/builds/cft
# chrome-linux64.zip, measured 2026-10-27T22:03Z
ARG CHROME_SHA256_AMD64=43fd31766c7533a02dda2ded5ad50c2f861cd5400be7509c0abad71129f79d79
# chrome-linux-arm64.zip, measured 2026-10-27T22:03Z
ARG CHROME_SHA256_ARM64=910a12fff6170202195f72ef3d353801c7063d301ab80b1f51b77d791fda60ce
# chrome-headless-shell-linux64.zip, measured 2026-10-27T22:03Z
ARG SHELL_SHA256_AMD64=c3e50596aef52c58f8fda3c6b3411c33d34c88b177b3ed1c2243359fd3b8ba20
# chrome-headless-shell-linux-arm64.zip, measured 2026-10-27T22:03Z
ARG SHELL_SHA256_ARM64=81ab9d4d3c301dc5fdfb6c541b9e6632b16fc819a6a854460f40a76fe2b2c476
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright
RUN case "$TARGETARCH" in \
      amd64) slug=linux64; chrome="$CHROME_SHA256_AMD64"; shell="$SHELL_SHA256_AMD64" ;; \
      arm64) slug=linux-arm64; chrome="$CHROME_SHA256_ARM64"; shell="$SHELL_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && mkdir -p "/opt/playwright/chromium-${CHROMIUM_REVISION}" \
             "/opt/playwright/chromium_headless_shell-${CHROMIUM_REVISION}" \
 && curl -fsSL --proto '=https' --tlsv1.2 \
      "${CHROMIUM_CDN}/${CHROMIUM_BUILD}/${slug}/chrome-${slug}.zip" -o /stage/chrome.zip \
 && echo "${chrome}  /stage/chrome.zip" | sha256sum -c - \
 && unzip -q /stage/chrome.zip -d "/opt/playwright/chromium-${CHROMIUM_REVISION}" \
 && curl -fsSL --proto '=https' --tlsv1.2 \
      "${CHROMIUM_CDN}/${CHROMIUM_BUILD}/${slug}/chrome-headless-shell-${slug}.zip" \
      -o /stage/shell.zip \
 && echo "${shell}  /stage/shell.zip" | sha256sum -c - \
 && unzip -q /stage/shell.zip \
      -d "/opt/playwright/chromium_headless_shell-${CHROMIUM_REVISION}" \
 && rm -f /stage/chrome.zip /stage/shell.zip \
 && /opt/pw/bin/python -c "import pathlib, sys; from playwright.sync_api import sync_playwright; p=sync_playwright().start(); path=pathlib.Path(p.chromium.executable_path); p.stop(); sys.exit(0 if path.is_file() else 1)" \
 && echo "chromium ${CHROMIUM_BUILD} (revision ${CHROMIUM_REVISION}) verified in place"

# tests/judge.toml names claude-code as the judged carrier, so Reward Kit
# shells out to this binary rather than calling the messages endpoint. The two
# are not interchangeable against a subscription credential: the same
# sk-ant-oat token this CLI accepts is refused by the plain endpoint with a
# rate_limit_error, which was measured rather than assumed. It is fetched in
# this stage because the stage already builds at the target platform and
# carries curl, while the runtime stage has neither curl nor a network route.
ARG CLAUDE_VERSION=2.1.314
ARG CLAUDE_SHA256_AMD64=4487042b4c4f5d88d4568f68ae98aae0b1ffae58864cf57fb868641bc5f84594
ARG CLAUDE_SHA256_ARM64=a66c0a8f5d0c87a439953d797985192bbcbaefbce2e0d7b2eaa8df30ef0f3ef2
RUN case "$TARGETARCH" in \
      amd64) platform=linux-x64; want="$CLAUDE_SHA256_AMD64" ;; \
      arm64) platform=linux-arm64; want="$CLAUDE_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && mkdir -p /opt/claude \
 && curl -fsSL --proto '=https' --tlsv1.2 \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/${platform}/claude" \
      -o /opt/claude/claude \
 && echo "${want}  /opt/claude/claude" | sha256sum -c - \
 && chmod 0755 /opt/claude/claude \
 && got="$(HOME=/root /opt/claude/claude --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" \
 && if [ "$got" != "$CLAUDE_VERSION" ]; then \
      echo "claude is '${got}', expected exactly ${CLAUDE_VERSION}" >&2; exit 1; \
    fi \
 && echo "claude ${got} verified for the judged dimension"

FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS runtime
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
LABEL org.opencontainers.image.source="https://github.com/EtharaOrion/allmight" \
      org.opencontainers.image.revision="fdb08142db5cfed3b127e12f5a8fd8069d357523" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c"

# No Postgres client: the verifier runs in a separate offline container that
# cannot reach the agent's database, which is why the graded carrier for the
# reconciliation table is the declared /app/out/snapshot.sql artifact. Every
# non-browser checker in checks.py reads files with the standard library alone,
# so a database client here would be attack surface with no caller.
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates=20240203

# Three groups are added to the runtime stage, each required at grade time
# rather than at build time. The server packages run the application itself:
# tests/test.sh execs the /app/start.sh deliverable here, because Harbor starts
# the separate verifier in its own compose project after stopping the agent's,
# so the image that grades the rendered surface is also the image that serves
# it. The font packages back document.fonts.check, which resolves against
# fonts the image ships. The chromium shared-library closure is what the
# browser links at launch. Every version is the candidate the dated snapshot
# carries, identical on amd64 and arm64.
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    printf '%s\n' 'deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z noble main universe' \
      > /etc/apt/sources.list.d/snapshot.list \
 && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      python3=3.12.3-0ubuntu1 \
      python3-venv=3.12.3-0ubuntu1 \
      python3-uvicorn=0.27.1-1 \
      python3-starlette=0.31.1-1 \
      python3-h11=0.14.0-1 \
      python3-anyio=4.2.0-1 \
      python3-sniffio=1.3.0-2 \
      python3-idna=3.6-2 \
      python3-click=8.1.6-2 \
      python3-typing-extensions=4.10.0-1 \
      fonts-liberation=1:2.1.5-3 \
      fonts-dejavu-core=2.37-8 \
      libnss3=2:3.98-1build1 \
      libatk1.0-0t64=2.52.0-1build1 \
      libatk-bridge2.0-0t64=2.52.0-1build1 \
      libcups2t64=2.4.7-1.2ubuntu7 \
      libdrm2=2.4.120-2build1 \
      libxkbcommon0=1.6.0-1build1 \
      libxcomposite1=1:0.4.5-1build3 \
      libxdamage1=1:1.1.6-1build1 \
      libxfixes3=1:6.0.0-2build1 \
      libxrandr2=2:1.5.2-2build1 \
      libgbm1=24.0.5-1ubuntu1 \
      libpango-1.0-0=1.52.1+ds-1build1 \
      libcairo2=1.18.0-3build1 \
      libasound2t64=1.2.11-1build2

COPY --from=toolkit /usr/local/bin/uv /usr/local/bin/uv
COPY --from=toolkit /opt/toolkit /opt/toolkit
COPY --from=toolkit /opt/pw /opt/pw
COPY --from=toolkit /opt/playwright /opt/playwright
COPY --from=toolkit /opt/claude /opt/claude

# The toolkit and the browser arrive already built for this image's own
# architecture, so the verifier fetches nothing at verify time and runs offline
# at both its baseline and its phase. Each exact-version assertion is repeated
# here against the copied tree, because the toolkit-stage check proves what was
# built and this one proves what shipped.
ENV UV_NO_SYNC=1 \
    UV_TOOL_DIR=/opt/toolkit/share \
    UV_TOOL_BIN_DIR=/opt/toolkit/bin \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright \
    PATH=/opt/toolkit/bin:/opt/pw/bin:/opt/claude:$PATH
ARG REWARDKIT_VERSION=0.2.4
ARG PLAYWRIGHT_VERSION=1.64.1
ARG CLAUDE_VERSION=2.1.314
RUN command -v rewardkit >/dev/null \
 && cc="$(HOME=/root claude --version | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -1)" \
 && if [ "$cc" != "$CLAUDE_VERSION" ]; then \
      echo "claude is '${cc}', expected exactly ${CLAUDE_VERSION}" >&2; exit 1; \
    fi \
 && got="$(uv tool list | awk '$1=="harbor-rewardkit"{print substr($2,2)}')" \
 && if [ "$got" != "$REWARDKIT_VERSION" ]; then \
      echo "harbor-rewardkit is '${got}', expected exactly ${REWARDKIT_VERSION}" >&2; exit 1; \
    fi \
 && pw="$(/opt/pw/bin/python -c 'import importlib.metadata as m; print(m.version("playwright"))')" \
 && if [ "$pw" != "$PLAYWRIGHT_VERSION" ]; then \
      echo "playwright is '${pw}', expected exactly ${PLAYWRIGHT_VERSION}" >&2; exit 1; \
    fi \
 && echo "harbor-rewardkit ${got} and playwright ${pw} pinned in image"

RUN groupadd --gid 10002 verifier \
 && useradd --uid 10002 --gid 10002 --create-home --shell /bin/bash verifier \
 && mkdir -p /tests /logs/verifier \
 && chown -R 10002:10002 /tests /logs

COPY --chown=10002:10002 . /tests/

WORKDIR /tests
USER 10002:10002

# Reward Kit runs each criteria file in its own process, and checks.py imports
# playwright from the venv rather than from the system interpreter, so the venv
# site-packages must be on the path of whichever python Reward Kit starts.
ENV PYTHONPATH=/opt/pw/lib/python3.12/site-packages
# No ENTRYPOINT. Harbor runs a declared verifier image prebuilt and starts it
# through its own compose template, whose main service carries
# command = [ "sh", "-c", "sleep infinity" ]. A compose command replaces CMD
# and never ENTRYPOINT, so a non-neutral ENTRYPOINT here would receive "sh" as
# a script path, read the ELF, and refuse it with exit 126 before the verifier
# could start at all. Harbor never reads this CMD either: it builds the
# verifier command itself from the test script path, chmods it as root, and
# runs it through docker compose exec, which bypasses ENTRYPOINT entirely. The
# CMD below is therefore the standalone-run convenience only, kept in exec form
# per item 2a.1, and it names the interpreter explicitly because test.sh ships
# without an execute bit.
CMD ["/bin/bash", "/tests/test.sh"]
