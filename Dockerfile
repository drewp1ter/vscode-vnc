# syntax=docker/dockerfile:1.7

FROM ubuntu:24.04

ARG DEBIAN_FRONTEND=noninteractive
ARG USERNAME=ubuntu
ARG USER_UID=1000
ARG USER_GID=1000

ENV LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY_NUM=1 \
    VNC_GEOMETRY=1920x1080 \
    VNC_DEPTH=24 \
    PATH="/home/${USERNAME}/.local/bin:${PATH}" \
    CLAUDE_CONFIG_DIR="/home/${USERNAME}/.config/claude"

# XFCE, TigerVNC and basic development/desktop utilities.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xfce4-session \
        ca-certificates \
        curl \
        dbus-x11 \
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
    xfsettingsd &
    xfwm4 --replace &
    xfdesktop &

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
        "/home/${USERNAME}/.config/Code" \
        "/home/${USERNAME}/.vscode" \
        "/home/${USERNAME}/.claude" \
        "/home/${USERNAME}/.config/claude"

USER ${USERNAME}
RUN curl -fsSL https://claude.ai/install.sh | bash
WORKDIR /workspace

# DISPLAY_NUM=1 corresponds to TCP port 5901.
EXPOSE 5901

VOLUME ["/workspace"]

CMD ["/usr/local/bin/start-vnc"]