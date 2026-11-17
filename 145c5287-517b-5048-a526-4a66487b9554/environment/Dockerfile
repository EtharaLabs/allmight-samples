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

FROM ubuntu:24.04@sha256:24737cef17f4e22b1977df7d9a05e77c4e28b7da5b8911f8d8ccda136bf3573c AS runtime
SHELL ["/bin/bash", "-o", "pipefail", "-c"]
LABEL org.opencontainers.image.source="ethara/145c5287-517b-5048-a526-4a66487b9554" \
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

# The corpus is root-owned and read-only by mode. That is a speed bump, not the
# red line: the image ends on root (see below), so the solver can clear the bit.
# inputs-unmodified is enforced instead by comparing every byte under inputs/
# against digests recorded in the verifier's fixtures, which this image never
# holds, so a rewritten corpus is detected rather than rewarded.
COPY --chown=0:0 --chmod=0444 inputs /app/inputs
COPY --chown=10001:10001 start-postgres.sh /usr/local/bin/start-postgres.sh
RUN chmod 0755 /usr/local/bin/start-postgres.sh \
 && chmod 0755 /app/inputs /app/inputs/receipts

# initdb and the server both refuse uid 0, so the cluster is built under the
# agent identity -- by dropping inside this layer with setpriv rather than by
# switching USER, which leaves the image on a single USER root like every other
# task in this set. Ending non-root is what the sandbox penalises: it can stall
# at startup before it ever reports. The entrypoint drops to uid 10001 the same
# way for the server, so the database still never runs privileged. setpriv comes
# from util-linux in the base image, so this adds no dependency.
RUN setpriv --reuid=10001 --regid=10001 --init-groups bash -c '"$PGBIN/initdb" --username=ledgerline --auth=trust --encoding=UTF8 --locale=C -D "$PGDATA" && "$PGBIN/pg_ctl" -D "$PGDATA" -o "-c listen_addresses=localhost -p 5432" -w start && "$PGBIN/createdb" --username=ledgerline --host=localhost --port=5432 ledgerline && "$PGBIN/pg_ctl" -D "$PGDATA" -m fast -w stop'

WORKDIR /app
USER root
# The entrypoint ends in exec "$@", so this command is pid 1 and the container
# lives exactly as long as it does. A login shell reads end-of-input the moment
# it starts without a terminal attached and exits, taking the container with it
# before anything can attach; a runner that substitutes its own keepalive command
# hides that, one that honours the image does not. sleep infinity never exits.
ENTRYPOINT ["/usr/local/bin/start-postgres.sh"]
CMD ["sleep", "infinity"]
