# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG USERNAME=ubuntu
ARG USER_UID=1000
ARG USER_GID=1000
ARG USER_PASSWORD=secret12

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY_NUM=1 \
    VNC_GEOMETRY=1920x1080 \
    VNC_DEPTH=24 \
    PATH="/home/${USERNAME}/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR="/home/${USERNAME}/.config/claude" \
    QWEN_CONFIG_DIR="/home/${USERNAME}/.qwen"

# XFCE, TigerVNC and basic development/desktop utilities.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xfce4-session \
        ca-certificates \
        curl \
        dbus-x11 \
        build-essential \
        git \
        gnupg \
        openssh-client \
        sudo \
        tigervnc-standalone-server \
        tigervnc-tools \
        wget \
        x11-xserver-utils \
        xauth \
        xdg-utils \
        xfce4 \
        xfce4-terminal \
        xfconf \
        xfce4-panel \
        xfce4-settings \
        xfdesktop4 \
        xfwm4 \
        greybird-gtk-theme \
        terminator \
        xxd \
        gdb \
        strace \
        ltrace \
        nano \
        libwrap0-dev \
        libpam0g-dev \
        libpcap-dev \
        zlib1g-dev \
    && rm -rf /var/lib/apt/lists/*

# Official Microsoft repository for VS Code.
RUN wget -qO- https://packages.microsoft.com/keys/microsoft.asc \
        | gpg --dearmor -o /usr/share/keyrings/microsoft.gpg \
    && printf '%s\n' \
        'Types: deb' \
        'URIs: https://packages.microsoft.com/repos/code' \
        'Suites: stable' \
        'Components: main' \
        'Architectures: amd64 arm64 armhf' \
        'Signed-By: /usr/share/keyrings/microsoft.gpg' \
        > /etc/apt/sources.list.d/vscode.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends code \
    && rm -rf /var/lib/apt/lists/*

# Go from the official tarball. Leave GO_VERSION empty to use the latest
# stable release, or pin it, e.g. --build-arg GO_VERSION=go1.25.1.
ARG TARGETARCH
ARG GO_VERSION=
RUN go_version="${GO_VERSION:-$(curl -fsSL 'https://go.dev/VERSION?m=text' | head -n1)}" \
    && curl -fsSL "https://go.dev/dl/${go_version}.linux-${TARGETARCH:-amd64}.tar.gz" \
        | tar -xz -C /usr/local
ENV PATH="/usr/local/go/bin:/home/${USERNAME}/go/bin:${PATH}"

RUN install -d -o "${USERNAME}" -g "${USERNAME}" /workspace

# Programs launched when the VNC desktop starts. vncconfig provides clipboard
# synchronization, while VS Code opens /workspace automatically.
RUN install -d -o "${USERNAME}" -g "${USERNAME}" "/home/${USERNAME}/.vnc" \
    && cat > "/home/${USERNAME}/.vnc/xstartup" <<'EOF'
#!/bin/sh

unset SESSION_MANAGER
unset DBUS_SESSION_BUS_ADDRESS

export XDG_CONFIG_DIRS=/etc/xdg
export XDG_RUNTIME_DIR="/tmp/runtime-${USER}"

mkdir -p "${XDG_RUNTIME_DIR}"
chmod 0700 "${XDG_RUNTIME_DIR}"

rm -rf "${HOME}/.cache/sessions"

vncconfig -nowin &

exec dbus-run-session -- sh -c '
    theme_marker="$HOME/.config/xfce4/.dark-theme-configured"
    if [ ! -e "$theme_marker" ]; then
        xfconf-query --create -c xsettings -p /Net/ThemeName -t string -s Greybird-dark &&
        xfconf-query --create -c xfwm4 -p /general/theme -t string -s Greybird-dark &&
        touch "$theme_marker"
    fi

    startxfce4 &

    sleep 3

    code \
        --no-sandbox \
        --disable-gpu \
        --disable-software-rasterizer \
        --disable-dev-shm-usage \
        --password-store=basic \
        --new-window \
        /workspace &

    wait
'
EOF
RUN chmod 0755 "/home/${USERNAME}/.vnc/xstartup" \
    && chown "${USERNAME}:${USERNAME}" "/home/${USERNAME}/.vnc/xstartup"

RUN cat > /usr/local/bin/start-vnc <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

: "${VNC_PASSWORD:?Set VNC_PASSWORD when starting the container}"

opencode_config_dir="${HOME}/.config/opencode"
mkdir -p "${opencode_config_dir}"
if [[ ! -f "${opencode_config_dir}/opencode.json" ]]; then
        cat > "${opencode_config_dir}/opencode.json" <<'OPENCODE_CONFIG'
{
    "$schema": "https://opencode.ai/config.json",
    "model": "ollama/local",
    "small_model": "ollama/local",
    "provider": {
        "ollama": {
            "npm": "@ai-sdk/openai-compatible",
            "name": "Ollama (local)",
            "options": {
                "baseURL": "http://host.docker.internal:11434/v1"
            },
            "models": {
                "local": {
                    "id": "{env:OLLAMA_MODEL}",
                    "name": "{env:OLLAMA_MODEL}",
                    "limit": {
                        "context": 131072,
                        "output": 16384
                    }
                }
            }
        }
    }
}
OPENCODE_CONFIG
fi

ssh_dir="${HOME}/.ssh"
mkdir -p "${ssh_dir}"
chmod 0700 "${ssh_dir}"
if [[ ! -f "${ssh_dir}/id_ed25519" ]]; then
    ssh-keygen -q -t ed25519 -N '' -C 'github-vscode-vnc' -f "${ssh_dir}/id_ed25519"
    printf 'Generated GitHub SSH public key:\n'
    cat "${ssh_dir}/id_ed25519.pub"
fi

display=":${DISPLAY_NUM}"
vnc_dir="${HOME}/.vnc"
mkdir -p "${vnc_dir}"

# The classic VNC password format uses at most the first eight characters.
printf '%s\n' "${VNC_PASSWORD}" | vncpasswd -f > "${vnc_dir}/passwd"
chmod 0600 "${vnc_dir}/passwd"

# Remove files left by an uncleanly stopped container.
rm -f "/tmp/.X${DISPLAY_NUM}-lock" "/tmp/.X11-unix/X${DISPLAY_NUM}"

exec tigervncserver "${display}" \
    -fg \
    -localhost no \
    -geometry "${VNC_GEOMETRY}" \
    -depth "${VNC_DEPTH}" \
    -SecurityTypes VncAuth \
    -PasswordFile "${vnc_dir}/passwd" \
    -AcceptCutText=1 \
    -SendCutText=1 \
    -AlwaysShared
EOF
RUN chmod 0755 /usr/local/bin/start-vnc

RUN install -d -o "${USERNAME}" -g "${USERNAME}" \
        "/home/${USERNAME}/.config" \
        "/home/${USERNAME}/.config/Code" \
        "/home/${USERNAME}/.config/xfce4" \
        "/home/${USERNAME}/.ssh" \
        "/home/${USERNAME}/.vscode" \
        "/home/${USERNAME}/.claude" \
        "/home/${USERNAME}/.config/claude" \
        "/home/${USERNAME}/.config/opencode" \
        "/home/${USERNAME}/.qwen"
RUN printf '%s:%s\n' "${USERNAME}" "${USER_PASSWORD}" | chpasswd
RUN printf '%s ALL=(ALL) NOPASSWD:ALL\n' "${USERNAME}" > "/etc/sudoers.d/${USERNAME}" \
    && chmod 0440 "/etc/sudoers.d/${USERNAME}" \
    && visudo -cf "/etc/sudoers.d/${USERNAME}"

USER ${USERNAME}
RUN /bin/bash -c "curl -o- https://raw.githubusercontent.com/nvm-sh/nvm/v0.40.8/install.sh | bash \ 
    && source ${HOME}/.nvm/nvm.sh \
    && nvm install 24 \
    && npm install --global opencode-ai \
    && npm install --global @qwen-code/qwen-code"

RUN curl -fsSL https://claude.ai/install.sh | bash    
RUN curl -fsSL https://vast.ai/install.sh | bash
WORKDIR /workspace

# DISPLAY_NUM=1 corresponds to TCP port 5901.
EXPOSE 5901

VOLUME ["/workspace"]

CMD ["/usr/local/bin/start-vnc"]