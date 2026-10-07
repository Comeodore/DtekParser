#!/usr/bin/env bash
# Pass Incapsula's hCaptcha by hand, for when the log says "Incapsula shows an hCaptcha".
# A visible Chromium (same image, IP and user agent as the service) runs on a
# VNC display that listens on the server's localhost only.
#
#   on the server:  cd /opt/dtek-service && tools/solve_captcha.sh krem
#   on the Mac:     ssh -p 2222 -N -L 5901:127.0.0.1:5900 root@192.168.0.185
#                   open vnc://localhost:5901        (password: dtek)
#
# Once the schedule page loads, the cookies go to data/state-<source>.json and
# the service's next browser context (it makes one on every failure) uses them.
set -euo pipefail
cd "$(dirname "$0")/.."
SOURCE="${1:?usage: tools/solve_captcha.sh kem|krem}"

docker run --rm --name dtek-captcha --network host \
  -e VNC_PASSWORD="${VNC_PASSWORD:-dtek}" \
  -v "$PWD/tools:/tools:ro" -v "$PWD/data:/app/data" \
  dtek-service:latest bash -c '
    set -e
    apt-get update -qq >/dev/null && apt-get install -y -qq --no-install-recommends xvfb x11vnc >/dev/null
    Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
    sleep 1
    x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -passwd "$VNC_PASSWORD" \
      -rfbversion 3.8 -noxdamage -quiet -bg -o /tmp/x11vnc.log
    DISPLAY=:99 PYTHONPATH=/app python /tools/solve_captcha.py "$0"' "$SOURCE"
