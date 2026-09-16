# syntax=docker/dockerfile:1
#
# Minecraft Bedrock Dedicated Server (BDS) — Railway deployment
# Target version: 1.20.0.01 (must match the Android Bedrock client version)
#
# Two-stage build:
#   1) "fetch"  — downloads + unpacks the official BDS zip
#   2) runtime  — small Ubuntu 20.04 image with just what bedrock_server needs
#
# Ubuntu 20.04 is used deliberately (not a newer LTS) because this generation
# of bedrock_server binaries links against libssl.so.1.1, which Ubuntu 20.04
# ships by default and Ubuntu 22.04+/24.04 do not.

########################################
# Stage 1: Fetch Bedrock Dedicated Server
########################################
FROM ubuntu:20.04 AS fetch

ARG BDS_VERSION=1.20.0.01
# This is Microsoft/Mojang's standard predictable download pattern. It is
# ONLY guaranteed to work for whatever build is currently "latest". Mojang
# does not promise old builds stay reachable at this URL forever — see
# README.md -> "نصب دستی فایل رسمی BDS" if this 404s for 1.20.0.01.
ARG BDS_DOWNLOAD_URL=https://minecraft.azureedge.net/bin-linux/bedrock-server-${BDS_VERSION}.zip

RUN apt-get update && \
    apt-get install -y --no-install-recommends curl ca-certificates unzip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/bds

RUN set -eu; \
    echo "Attempting to download BDS ${BDS_VERSION} from:"; \
    echo "  ${BDS_DOWNLOAD_URL}"; \
    if curl -fSL --retry 3 --retry-delay 2 -o /tmp/bedrock-server.zip "${BDS_DOWNLOAD_URL}"; then \
        echo "Download OK."; \
    else \
        echo; \
        echo "############################################################"; \
        echo "BUILD FAILED: could not download BDS ${BDS_VERSION} from the"; \
        echo "predictable Mojang CDN URL above. This almost always means"; \
        echo "that exact old build is no longer published at that address"; \
        echo "(Mojang generally only keeps the current build linked)."; \
        echo; \
        echo "This is NOT a link this Dockerfile can guess its way around."; \
        echo "See README.md section 'نصب دستی فایل رسمی BDS' for exactly"; \
        echo "how to supply the official file yourself, then rebuild with:"; \
        echo "  --build-arg BDS_DOWNLOAD_URL=https://<your-hosted-copy>.zip"; \
        echo "############################################################"; \
        exit 1; \
    fi; \
    unzip -q /tmp/bedrock-server.zip -d /opt/bds; \
    rm -f /tmp/bedrock-server.zip; \
    chmod +x /opt/bds/bedrock_server

########################################
# Stage 2: Runtime image
########################################
FROM ubuntu:20.04

ARG BDS_VERSION=1.20.0.01
ENV BDS_VERSION=${BDS_VERSION} \
    DEBIAN_FRONTEND=noninteractive

# libssl1.1 + libcurl4: runtime dependencies of bedrock_server itself.
# python3/pip: runtime for the admin panel (panel/app.py, Flask).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        libssl1.1 libcurl4 ca-certificates python3 python3-pip && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Admin panel dependencies (installed separately so this layer is cached
# even when app.py changes). Flask is required — if it fails to install the
# build should fail loudly. psutil is only used for the optional CPU/RAM
# widget on the dashboard (panel/app.py already falls back gracefully if
# it's missing), so it's installed best-effort and never breaks the build.
COPY panel/requirements.txt /app/panel/requirements.txt
RUN pip3 install --no-cache-dir Flask==3.0.3 && \
    (pip3 install --no-cache-dir psutil==6.0.0 || echo "psutil install failed — dashboard will skip CPU/RAM stats, everything else still works")

# Clean baked-in copy of the engine (binary + stock packs). start.sh copies
# this into the persistent /data volume — never overwriting world data or
# user-edited config files.
COPY --from=fetch /opt/bds /app/bds-install
COPY server.properties /app/defaults/server.properties
COPY panel/app.py /app/panel/app.py
COPY start.sh /app/start.sh

RUN chmod +x /app/start.sh

EXPOSE 19132/udp
EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/app/start.sh"]
