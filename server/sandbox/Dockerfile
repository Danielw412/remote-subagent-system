# rsa sandbox image: every task runs in a fresh, disposable container from this image.
# Add whatever toolchains your repositories need, then run `rsa server build`.
FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=2.1.3

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_update_notifier=false \
    npm_config_fund=false

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git curl wget jq ripgrep fd-find less procps psmisc file \
      unzip zip xz-utils iptables iproute2 \
      python3 python3-pip python3-venv python-is-python3 \
      build-essential pkg-config \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@opencode/cli@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && opencode --version

COPY opencode.json /root/.config/opencode/opencode.json
COPY entrypoint.sh /usr/local/bin/rsa-entrypoint
RUN chmod 0755 /usr/local/bin/rsa-entrypoint

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/rsa-entrypoint"]
CMD ["task"]
