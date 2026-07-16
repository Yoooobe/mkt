# 4unik.mail — imagem para Cloudflare Containers.
# Contexto de build = raiz do repo (ver deploy/worker/wrangler.jsonc: image_build_context).
# Estado persistente (dashboard.db) vive no R2 via Litestream: restore no boot +
# replicação contínua (disco do container é efêmero).
#
# Layout dentro da imagem (espelha o layout local, exigido por db.py/engine.py):
#   /app/gws-4unik/dashboard/   ← código + dashboard.db restaurado (db.DB_PATH)
#   /app/gws-4unik/proposals/   ← templates/estado do monitor (db.PROPOSALS)
#   /app/gws_accounts/          ← perfis gws por remetente (engine.ACCOUNTS_DIR =
#                                 GWS_DIR.parent/"gws_accounts", ou seja, IRMÃO do repo)
FROM python:3.12-slim

ARG LITESTREAM_VERSION=0.5.14

ENV PYTHONUNBUFFERED=1 \
    # keyring de arquivo: mesmo backend usado localmente pelos perfis multi-conta
    GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl nodejs npm \
    && rm -rf /var/lib/apt/lists/*

# Litestream (release oficial). TARGETARCH é preenchido pelo BuildKit
# (Cloudflare Containers roda linux/amd64).
ARG TARGETARCH=amd64
RUN case "${TARGETARCH}" in \
        amd64) LS_ARCH=x86_64 ;; \
        arm64) LS_ARCH=arm64 ;; \
        *) echo "arquitetura não suportada: ${TARGETARCH}" && exit 1 ;; \
    esac \
    && curl -fsSL "https://github.com/benbjohnson/litestream/releases/download/v${LITESTREAM_VERSION}/litestream-${LITESTREAM_VERSION}-linux-${LS_ARCH}.tar.gz" \
       | tar -xz -C /usr/local/bin litestream \
    && litestream version

# CLI gws = pacote npm @googleworkspace/cli (shim node que baixa o binário da
# plataforma). Rodar `gws --version` no build baixa o binário linux de uma vez,
# em vez de no primeiro request do container.
RUN npm install -g @googleworkspace/cli \
    && gws --version

WORKDIR /app/gws-4unik

COPY dashboard/requirements.txt dashboard/requirements.txt
RUN pip install --no-cache-dir -r dashboard/requirements.txt

COPY dashboard/ dashboard/
COPY proposals/ proposals/

# Cinto e suspensório: nada de dados/segredos locais na imagem, mesmo que o
# .dockerignore falhe (o estado real vem do R2 no boot).
RUN rm -f dashboard/*.db dashboard/*.db-shm dashboard/*.db-wal dashboard/.env \
          proposals/monitor_config.env proposals/sent_log.txt \
    && rm -rf dashboard/logs dashboard/out proposals/monitor_state proposals/drafts \
    && mkdir -p dashboard/logs /app/gws_accounts

COPY deploy/litestream.yml /etc/litestream.yml
COPY deploy/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
