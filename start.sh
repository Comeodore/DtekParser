#!/bin/bash

# echo "🧹 Cleaning up old X locks..."
# rm -f /tmp/.X99-lock
# rm -f /tmp/.X11-unix/X99

# echo "🖥️ Starting Xvfb..."
# Xvfb :99 -screen 0 1920x1080x24 &
# XVFB_PID=$!
# sleep 3

# if ! kill -0 $XVFB_PID 2>/dev/null; then
#     echo "❌ Xvfb failed to start"
#     exit 1
# fi

# export DISPLAY=:99
# echo "✅ Xvfb started on display :99"

# echo "🖼️ Starting fluxbox..."
# fluxbox &
# sleep 1

# echo "📡 Starting VNC server on port 5900..."
# x11vnc -display :99 -forever -nopw -shared -rfbport 5900 -rfbversion 3.8 -noxdamage -listen 0.0.0.0 &
# sleep 1

echo "🚀 Starting API server..."
exec uvicorn app.main:app --host 0.0.0.0 --port 9999
