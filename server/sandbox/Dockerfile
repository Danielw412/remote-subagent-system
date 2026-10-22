# rsa sandbox image: every task runs in a fresh, disposable container from this image.
# Add whatever toolchains your repositories need, then run `rsa server build`.
FROM eclipse-temurin:21-jdk-jammy AS java
FROM node:22-bookworm-slim

ARG OPENCODE_VERSION=2.1.3
ARG REA_VERSION=6.7.0
ARG GHIDRA_VERSION=12.1.4
ARG GHIDRA_DATE=20260921
ARG GHIDRA_SHA256=ddac49f903da9d5bac833e5cc79395098b9c33cfd3279be5f31bd00387d2d4db
ARG CMAKE_VERSION=4.4.4
ARG CMAKE_SHA256_X86_64=e5bb807f7728cb60cd8b27ebc97a2edb469b68655f21e844a600c3575b76f5bb
ARG CMAKE_SHA256_AARCH64=a1b6cc63636a0e55c63257cf3315a8a5f129e42fade25db1afea4ff8ab06f25e

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    PIP_BREAK_SYSTEM_PACKAGES=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    npm_config_update_notifier=false \
    npm_config_fund=false \
    JAVA_HOME=/opt/java/openjdk \
    GHIDRA_INSTALL_DIR=/opt/ghidra \
    GHIDRA_HEADLESS_MAXMEM=2G \
    REA_ANALYSIS_PROVIDER=ghidra \
    OPENCODE_CONFIG=/etc/rsa/reverse-engineering.json

# Use a full JDK from an older glibc base so it also runs on Debian bookworm.
COPY --from=java /opt/java/openjdk /opt/java/openjdk
ENV PATH="/opt/java/openjdk/bin:/opt/ghidra/support:${PATH}"

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates git curl wget jq ripgrep fd-find less procps psmisc file \
      unzip zip xz-utils iptables iproute2 \
      python3 python3-pip python3-venv python-is-python3 \
      build-essential pkg-config ninja-build \
 && rm -rf /var/lib/apt/lists/*

RUN npm install -g "@opencode/cli@${OPENCODE_VERSION}" \
 && npm cache clean --force \
 && opencode --version

# Verify the release archive before extracting it. Pin the supported 12.1 series;
# newer Ghidra versions are not necessarily admitted by the REA provider.
RUN curl -fL --retry 3 -o /tmp/ghidra.zip \
      "https://github.com/NationalSecurityAgency/ghidra/releases/download/Ghidra_${GHIDRA_VERSION}_build/ghidra_${GHIDRA_VERSION}_PUBLIC_${GHIDRA_DATE}.zip" \
 && echo "${GHIDRA_SHA256}  /tmp/ghidra.zip" | sha256sum -c - \
 && unzip -q /tmp/ghidra.zip -d /opt \
 && mv "/opt/ghidra_${GHIDRA_VERSION}_PUBLIC" /opt/ghidra \
 && rm /tmp/ghidra.zip \
 && java -version && javac -version

# No npx downloads at runtime. Ship REA's matching instructions and references
# alongside its CLI/MCP server in OpenCode's global skill directory.
RUN npm install -g "rea-agents@${REA_VERSION}" \
 && npm cache clean --force \
 && mkdir -p /root/.config/opencode/skills \
 && cp -R "$(npm root -g)/rea-agents/skills/reverse-engineer-anything" \
      /root/.config/opencode/skills/ \
 && rea --version

# Kitware's release build: Debian's CMake (3.25) is older than many current projects
# require. Verify the archive, then drop the GUIs and HTML docs (agents use --help-*).
RUN case "$(dpkg --print-architecture)" in \
      amd64) arch=x86_64 sha="${CMAKE_SHA256_X86_64}" ;; \
      arm64) arch=aarch64 sha="${CMAKE_SHA256_AARCH64}" ;; \
      *) echo "no pinned CMake build for $(dpkg --print-architecture)" >&2; exit 1 ;; \
    esac \
 && curl -fL --retry 3 -o /tmp/cmake.tar.gz \
      "https://github.com/Kitware/CMake/releases/download/v${CMAKE_VERSION}/cmake-${CMAKE_VERSION}-linux-${arch}.tar.gz" \
 && echo "${sha}  /tmp/cmake.tar.gz" | sha256sum -c - \
 && mkdir /opt/cmake \
 && tar -xzf /tmp/cmake.tar.gz -C /opt/cmake --strip-components=1 \
 && rm -rf /tmp/cmake.tar.gz /opt/cmake/doc /opt/cmake/man /opt/cmake/bin/cmake-gui /opt/cmake/bin/ccmake \
 && ln -s /opt/cmake/bin/cmake /opt/cmake/bin/ctest /opt/cmake/bin/cpack /usr/local/bin/ \
 && cmake --version && ninja --version

COPY reverse-engineering.json /etc/rsa/reverse-engineering.json
COPY reverse-engineering.md /etc/rsa/reverse-engineering.md
COPY reverse-engineering-selftest.py /etc/rsa/reverse-engineering-selftest.py
COPY cmake.md /etc/rsa/cmake.md
COPY cmake-selftest.sh /etc/rsa/cmake-selftest.sh
COPY opencode.json /root/.config/opencode/opencode.json
COPY opencode-server.json /etc/rsa/opencode-server.json
COPY entrypoint.sh /usr/local/bin/rsa-entrypoint
COPY rsa-ws /usr/local/bin/rsa-ws
COPY rsa-ignore-build-trees /usr/local/bin/rsa-ignore-build-trees
RUN chmod 0755 /usr/local/bin/rsa-entrypoint /usr/local/bin/rsa-ws /usr/local/bin/rsa-ignore-build-trees

WORKDIR /workspace
ENTRYPOINT ["/usr/local/bin/rsa-entrypoint"]
CMD ["task"]
