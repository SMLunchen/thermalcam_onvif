# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

A Python ONVIF thermal camera server for Raspberry Pi. It reads frames from a Meridian Innovation MI48 thermal sensor via SPI, exposes them as an MJPEG HTTP stream, and wraps the stream in an ONVIF-compliant SOAP/XML interface so standard NVR/VMS software can discover and connect to it as an IP camera.

## Single entry point

Everything lives in one file: `onvif_thermal_server.py`.

## Running

```bash
# Direct (foreground)
sudo python3 onvif_thermal_server.py

# As service
sudo systemctl restart onvif-thermal.service
sudo systemctl status  onvif-thermal.service
sudo journalctl -u onvif-thermal.service -f
```

The service is configured in `/etc/systemd/system/onvif-thermal.service` and runs as root (required for GPIO/SPI access).

## Endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/stream` | GET | MJPEG live stream (VLC, browsers) |
| `/snapshot` | GET | Single JPEG frame |
| `/onvif/device_service` | POST | ONVIF Device SOAP (GetDeviceInformation, GetCapabilities, GetSystemDateAndTime) |
| `/onvif/media_service` | POST | ONVIF Media SOAP (GetProfiles, GetStreamUri, GetSnapshotUri) |
| `/onvif/events` | GET | Motion event status (XML) |

All endpoints require HTTP Basic Auth. Credentials are in `auth.json` (same directory).

Default: `admin` / `admin`  — change this in `auth.json`.

**VLC:**
```
http://admin:admin@<pi-ip>:8000/stream
```

## Testing endpoints

```bash
curl -u admin:admin http://localhost:8000/snapshot -o snap.jpg
curl -u admin:admin http://localhost:8000/stream    # streams until Ctrl-C
curl -u admin:admin http://localhost:8000/onvif/events
```

## Architecture

`onvif_thermal_server.py` has three sections:

1. **Camera thread** (`_camera_loop`) — runs in a daemon thread. Initialises the MI48, then loops: wait for DATA_READY pin → assert CS → read frame → deassert CS → normalise/filter/colormap/resize → store as JPEG bytes in `_latest_jpeg`.

2. **HTTP handler** (`_Handler`) — serves all endpoints. Reads `_latest_jpeg` under `_frame_lock`. Auth is checked on every request via `_auth_ok()`.

3. **Main** — loads `auth.json`, starts camera thread, starts `ThreadingTCPServer` on port 8000.

## MI48 hardware wiring (Meridian uHAT)

| Signal | BCM GPIO | RPi pin |
|--------|----------|---------|
| SPI CS_N | BCM7 | 26 |
| DATA_READY | BCM24 | 18 |
| RESET_N | BCM23 | 16 |
| SPI CLK | BCM11 | 23 |
| SPI MOSI | BCM10 | 19 |
| SPI MISO | BCM9 | 21 |
| I2C SDA | BCM2 | 3 |
| I2C SCL | BCM3 | 5 |

CS is driven manually via GPIO (not by the SPI controller) — `spi.no_cs = True` in the code. The correct read sequence is: assert CS → small delay → `mi48.read()` → small delay → deassert CS. Getting this wrong causes CRC errors on every frame.

## Dependencies

`senxor` is installed system-wide: `/usr/local/lib/python3.9/dist-packages/pysenxor-*.egg/`  
The source is also in `Thermal_Camera_Hat/pysenxor-master/` (reference / reinstall with `pip install -e Thermal_Camera_Hat/pysenxor-master/`).

Other required packages: `numpy`, `opencv-python`, `smbus`, `spidev`, `gpiozero`

## Configuration

All tunable constants are at the top of `onvif_thermal_server.py`:

```python
PORT             = 8000
STREAM_RES       = (640, 480)
FRAME_RATE       = 9       # FPS
JPEG_QUALITY     = 85
COLORMAP         = cv.COLORMAP_JET
MOTION_THRESHOLD = 2.0     # °C
MOTION_MIN_PCT   = 5.0     # % of pixels that must change
```
