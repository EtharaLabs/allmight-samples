# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
FROM --platform=$BUILDPLATFORM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS fetch
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
ARG TARGETARCH
WORKDIR /stage

# snapshot.ubuntu.com answers http with a 301 to https, and the base image
# ships no CA bundle, so the dated snapshot is unreachable until one exists.
# ca-certificates is therefore installed first from the base image's own
# plain-http pocket, which apt verifies against the in-image
# ubuntu-archive-keyring.gpg. Only the bundle comes from the undated pocket,
# at the same 20240203 version the snapshot carries; every task package below
# is pinned to the snapshot.
RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates=20240203

RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    printf '%s\n' 'deb https://snapshot.ubuntu.com/ubuntu/20261001T000000Z noble main universe' \
      > /etc/apt/sources.list.d/snapshot.list \
 && rm -f /etc/apt/sources.list /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      curl=8.5.0-2ubuntu10

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
 && rm -f /stage/uv.tar.gz

# [agent] declares network_mode = "no-network", so an installed agent cannot
# fetch its own CLI at setup time and must find it already in the image. The
# release is selected by explicit $TARGETARCH with its published checksum
# enforced, rather than by piping the upstream installer, because that
# installer picks its artifact from the architecture it is running on: in this
# build-platform stage it would ship a foreign self-contained ELF that passes
# every byte-level check and then fails to exec in the sandbox.
ARG CLAUDE_VERSION=2.1.314
ARG CLAUDE_SHA256_AMD64=4487042b4c4f5d88d4568f68ae98aae0b1ffae58864cf57fb868641bc5f84594
ARG CLAUDE_SHA256_ARM64=a66c0a8f5d0c87a439953d797985192bbcbaefbce2e0d7b2eaa8df30ef0f3ef2
RUN case "$TARGETARCH" in \
      amd64) platform=linux-x64; want="$CLAUDE_SHA256_AMD64" ;; \
      arm64) platform=linux-arm64; want="$CLAUDE_SHA256_ARM64" ;; \
      *) echo "unsupported TARGETARCH: $TARGETARCH" >&2; exit 1 ;; \
    esac \
 && curl -fsSL --proto '=https' --tlsv1.2 \
      "https://downloads.claude.ai/claude-code-releases/${CLAUDE_VERSION}/${platform}/claude" \
      -o /stage/claude \
 && echo "${want}  /stage/claude" | sha256sum -c - \
 && chmod 0755 /stage/claude

FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS runtime
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
LABEL org.opencontainers.image.source="https://github.com/EtharaOrion/allmight" \
      org.opencontainers.image.revision="fdb08142db5cfed3b127e12f5a8fd8069d357523" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.base.name="ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c"

RUN --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates=20240203

# postgresql-common pulls perl, and the dated snapshot is a release pocket with
# no noble-updates, so it carries perl 5.38.2-3.2build2 while the base image
# already ships the later security build of perl-base. perl Depends on an exact
# equal perl-base version, so the two disagree and apt reports held broken
# packages. The whole perl family is therefore pinned to the snapshot version
# and downgraded as one consistent set, which keeps every byte at the dated
# snapshot instead of mixing two pockets. Every version below was read from the
# snapshot Packages index and is identical on amd64 and arm64.
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
      locales=2.39-0ubuntu8 \
      python3=3.12.3-0ubuntu1 \
      python3-venv=3.12.3-0ubuntu1 \
      postgresql-16=16.2-1ubuntu4 \
      postgresql-client-16=16.2-1ubuntu4 \
      postgresql-client-common=257build1 \
      postgresql-common=257build1 \
      python3-uvicorn=0.27.1-1 \
      python3-starlette=0.31.1-1 \
      python3-h11=0.14.0-1 \
      python3-anyio=4.2.0-1 \
      python3-sniffio=1.3.0-2 \
      python3-idna=3.6-2 \
      python3-click=8.1.6-2 \
      python3-typing-extensions=4.10.0-1 \
      fonts-liberation=1:2.1.5-3

COPY --from=fetch /stage/uv /usr/local/bin/uv
COPY --from=fetch /stage/claude /usr/local/bin/claude

RUN groupadd --gid 10001 agent \
 && useradd --uid 10001 --gid 10001 --create-home --shell /bin/bash agent \
 && mkdir -p /app /run/postgresql \
 && chown 10001:10001 /app /run/postgresql

# The cluster lives under the agent identity so the solver can start it without
# any privilege grant: no sudo, no setuid binary, no root shell.
ENV PGDATA=/var/lib/ledgerline/pgdata \
    PGBIN=/usr/lib/postgresql/16/bin \
    PATH=/usr/lib/postgresql/16/bin:$PATH
RUN mkdir -p "$PGDATA" \
 && chown -R 10001:10001 /var/lib/ledgerline

# The corpus is root-owned and read-only to the solver, which holds uid 10001.
# inputs-unmodified is a red line, so the shipped question must not be writable
# by the identity whose answer is graded against it. Root ownership is what
# enforces that: against uid 10001 these directories grant r-x and the files
# r--, so it can neither rewrite a file nor unlink one. The directories keep
# the owner write bit rather than dropping to 0555 because the corpus is also
# an artifact, and `docker cp` recreates each directory with the mode it reads
# here: a 0555 receipts/ is recreated unwritable on the host and the copy then
# fails with EACCES on its first file, which silently delivers a partial
# corpus to the verifier and fails inputs_unmodified along with every
# criterion that recomputes an expected value from the corpus.
COPY --chown=0:0 --chmod=0444 inputs /app/inputs
COPY --chown=10001:10001 start-postgres.sh /usr/local/bin/start-postgres.sh
RUN chmod 0755 /usr/local/bin/start-postgres.sh \
 && chmod 0755 /app/inputs /app/inputs/receipts

USER 10001:10001
RUN "$PGBIN/initdb" --username=ledgerline --auth=trust --encoding=UTF8 --locale=C -D "$PGDATA" \
 && "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=localhost -p 5432" -w start \
 && "$PGBIN/createdb" --username=ledgerline --host=localhost --port=5432 ledgerline \
 && "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop

WORKDIR /app
ENTRYPOINT ["/usr/local/bin/start-postgres.sh"]
CMD ["/bin/bash", "-l"]
