#!/usr/bin/env bash
# setup.sh – Deploy thermal camera ONVIF server + RTSP gateway on Raspberry Pi.
#
# Run as root:  sudo bash setup.sh
# Idempotent:  safe to re-run.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MEDIAMTX_VERSION="v1.17.1"
MEDIAMTX_URL="https://github.com/bluenviron/mediamtx/releases/download/${MEDIAMTX_VERSION}/mediamtx_${MEDIAMTX_VERSION}_linux_arm64.tar.gz"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
info()  { echo "[INFO]  $*"; }
die()   { echo "[ERROR] $*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "Please run as root: sudo bash setup.sh"
[[ "$(uname -m)" == "aarch64" ]] || die "This script is for aarch64 (Raspberry Pi OS 64-bit)."

# ---------------------------------------------------------------------------
# 1. System packages
# ---------------------------------------------------------------------------
info "Installing system packages…"
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3-numpy \
    python3-opencv \
    python3-smbus \
    python3-spidev \
    python3-gpiozero \
    python3-pip \
    ffmpeg \
    curl

# ---------------------------------------------------------------------------
# 2. Python – senxor driver (Meridian pysenxor)
# ---------------------------------------------------------------------------
info "Installing pysenxor…"
if python3 -c "import senxor" 2>/dev/null; then
    info "  pysenxor already installed, skipping."
else
    pip3 install -e "${REPO_DIR}/Thermal_Camera_Hat/pysenxor-master/" --quiet
fi

# ---------------------------------------------------------------------------
# 3. Enable SPI and I2C; add spi0-0cs overlay for Waveshare Thermal Camera HAT
# ---------------------------------------------------------------------------
# The MI48 sensor uses manual GPIO chip-select (BCM7). The spi0-0cs overlay
# removes hardware CS control from SPI0 so the GPIO CS line doesn't conflict.
# Without it the SPI driver may fight the application over the CS pin.
info "Enabling SPI and I2C interfaces…"
REBOOT_REQUIRED=0

raspi-config nonint do_spi 0   2>/dev/null || true
raspi-config nonint do_i2c 0   2>/dev/null || true

BOOT_CFG=/boot/config.txt
[[ -f /boot/firmware/config.txt ]] && BOOT_CFG=/boot/firmware/config.txt

if ! grep -q "dtoverlay=spi0-0cs" "${BOOT_CFG}"; then
    info "Adding dtoverlay=spi0-0cs to ${BOOT_CFG}…"
    # Insert the overlay line directly after dtparam=spi=on
    sed -i '/^dtparam=spi=on/a dtoverlay=spi0-0cs' "${BOOT_CFG}"
    REBOOT_REQUIRED=1
fi

# ---------------------------------------------------------------------------
# 4. auth.json – create default credentials if missing
# ---------------------------------------------------------------------------
AUTH_FILE="${REPO_DIR}/auth.json"
if [[ ! -f "${AUTH_FILE}" ]]; then
    info "Creating default auth.json (admin/admin)…"
    cat > "${AUTH_FILE}" <<'EOF'
{
  "admin": {"password": "admin"},
  "user":  {"password": "password"}
}
EOF
    chmod 600 "${AUTH_FILE}"
    info "  !! Change the passwords in ${AUTH_FILE} before production use !!"
fi

# ---------------------------------------------------------------------------
# 5. Log file
# ---------------------------------------------------------------------------
touch /var/log/onvif-thermal.log
chmod 644 /var/log/onvif-thermal.log

# ---------------------------------------------------------------------------
# 6. onvif-thermal systemd service
# ---------------------------------------------------------------------------
info "Installing onvif-thermal.service…"
cat > /etc/systemd/system/onvif-thermal.service <<EOF
[Unit]
Description=ONVIF Thermal Camera Server (MI48)
After=network.target

[Service]
ExecStart=/usr/bin/python3 ${REPO_DIR}/onvif_thermal_server.py
WorkingDirectory=${REPO_DIR}
StandardOutput=journal
StandardError=journal
SyslogIdentifier=onvif-thermal
User=root
Group=root
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------------------
# 7. mediamtx – download if not present or wrong version
# ---------------------------------------------------------------------------
MEDIAMTX_BIN=/usr/local/bin/mediamtx
if [[ -x "${MEDIAMTX_BIN}" ]] && "${MEDIAMTX_BIN}" --version 2>&1 | grep -q "${MEDIAMTX_VERSION}"; then
    info "mediamtx ${MEDIAMTX_VERSION} already installed."
else
    info "Downloading mediamtx ${MEDIAMTX_VERSION} (arm64)…"
    TMP=$(mktemp -d)
    curl -L --progress-bar "${MEDIAMTX_URL}" | tar xz -C "${TMP}"
    install -m 755 "${TMP}/mediamtx" "${MEDIAMTX_BIN}"
    rm -rf "${TMP}"
fi

# ---------------------------------------------------------------------------
# 8. mediamtx config
# ---------------------------------------------------------------------------
info "Writing /etc/mediamtx/mediamtx.yml…"
mkdir -p /etc/mediamtx
cat > /etc/mediamtx/mediamtx.yml <<'EOF'
# mediamtx – RTSP gateway for onvif-thermal stream
#
# Architecture:
#   onvif-thermal (HTTP :8000/stream  MJPEG)
#     → ffmpeg (MJPEG → H.264 libx264, internal TCP)
#       → mediamtx (RTSP server :554, H.264)
#         → NVR / VLC clients
#
# Authentication is delegated to onvif-thermal via the /rtsp_auth endpoint.
# Credentials are always read from auth.json – single source of truth.

logLevel: info
logDestinations: [syslog]

# Standard RTSP port (requires root – service runs as root)
rtspAddress: :554
# RTP/RTCP UDP – shifted away from :8000 (our HTTP server)
rtpAddress:  :5000
rtcpAddress: :5001

# Disable unused protocols to save RAM/CPU
rtmp:   false
hls:    false
webrtc: false
srt:    false

# Delegate authentication to the onvif-thermal HTTP server.
# It validates credentials against auth.json (single source of truth).
# Localhost publish actions (internal ffmpeg) are always allowed.
authMethod: http
authHTTPAddress: http://127.0.0.1:8000/rtsp_auth

paths:
  thermal:
    runOnInit: >
      ffmpeg -loglevel error
      -i http://admin:admin@127.0.0.1:8000/stream
      -c:v libx264 -profile:v baseline -level:v 3.1
      -tune zerolatency -preset ultrafast
      -b:v 1500k -g 25 -keyint_min 25 -pix_fmt yuv420p -an
      -x264-params "slice-max-size=1300"
      -f rtsp -rtsp_transport tcp rtsp://127.0.0.1:$RTSP_PORT/$MTX_PATH
    runOnInitRestart: yes
EOF

# ---------------------------------------------------------------------------
# 9. mediamtx systemd service
# ---------------------------------------------------------------------------
info "Installing mediamtx.service…"
cat > /etc/systemd/system/mediamtx.service <<'EOF'
[Unit]
Description=mediamtx RTSP gateway (thermal camera)
After=network.target onvif-thermal.service
Wants=onvif-thermal.service

[Service]
ExecStart=/usr/local/bin/mediamtx /etc/mediamtx/mediamtx.yml
Restart=on-failure
RestartSec=5
User=root
Group=root
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mediamtx

[Install]
WantedBy=multi-user.target
EOF

# ---------------------------------------------------------------------------
# 10. Enable and start services
# ---------------------------------------------------------------------------
info "Enabling and starting services…"
systemctl daemon-reload
systemctl enable onvif-thermal.service mediamtx.service
systemctl restart onvif-thermal.service
systemctl restart mediamtx.service

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
IP=$(hostname -I | awk '{print $1}')
echo ""
echo "=========================================="
echo " Setup complete."
echo ""
echo " HTTP stream:   http://${IP}:8000/stream"
echo " RTSP stream:   rtsp://${IP}/thermal"
echo " ONVIF device:  http://${IP}:8000/onvif/device_service"
echo " Snapshot:      http://${IP}:8000/snapshot"
echo ""
echo " Default credentials: admin / admin"
echo " Change them in: ${AUTH_FILE}"
echo "=========================================="

if [[ "${REBOOT_REQUIRED}" -eq 1 ]]; then
    echo ""
    echo "!! REBOOT REQUIRED !!"
    echo "   config.txt was modified (dtoverlay=spi0-0cs added)."
    echo "   The thermal camera will not work until after a reboot."
    echo "   Run:  sudo reboot"
fi
