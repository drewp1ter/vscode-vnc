#!/bin/bash

# Set up VNC password
export VNC_PASSWORD="password"
echo $VNC_PASSWORD | vncpasswd -f > ~/.vnc/passwd
chmod 600 ~/.vnc/passwd

# Start VNC server
vncserver :1 -geometry 1920x1080 -depth 24 -rfbport 5901 -localhost no &

# Wait a bit for VNC to start
sleep 2

# Start XFCE desktop environment in background
startxfce4 &

# Start VSCode in the background
code &

# Keep the container running
wait