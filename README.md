# Thermal Camera ONVIF Server

Python ONVIF camera server for Raspberry Pi with Meridian Innovation MI48 thermal sensor (Bobcat uHAT). Reads frames via SPI, renders them as a coloured MJPEG stream with live temperature scale and timestamp, and exposes a full ONVIF interface so any NVR or VMS can discover and connect to it as an IP camera.

## Hardware

| Component | Detail |
|-----------|--------|
| Board | Raspberry Pi (tested on Pi 3/4) |
| Sensor | Meridian Innovation MI48 Bobcat – 80×62 pixels, up to 25.5 FPS |
| HAT | Meridian uHAT |
| Interface | SPI (frames) + I²C (control) |

### GPIO wiring

| Signal | BCM GPIO | RPi pin |
|--------|----------|---------|
| SPI CS_N | BCM 7 | 26 |
| DATA_READY | BCM 24 | 18 |
| RESET_N | BCM 23 | 16 |
| SPI CLK | BCM 11 | 23 |
| SPI MOSI | BCM 10 | 19 |
| SPI MISO | BCM 9 | 21 |
| I²C SDA | BCM 2 | 3 |
| I²C SCL | BCM 3 | 5 |

CS is driven manually via GPIO (`spi.no_cs = True`) — not by the SPI controller. Read sequence: assert CS → 100 µs delay → `mi48.read()` → 100 µs delay → deassert CS.

## Installation

```bash
# System packages (Raspberry Pi OS)
sudo apt install python3-numpy python3-opencv python3-smbus python3-spidev

# senxor driver (Meridian pysenxor)
pip install -e Thermal_Camera_Hat/pysenxor-master/
# or if already installed system-wide:
# /usr/local/lib/python3.9/dist-packages/pysenxor-*.egg/

# gpiozero (usually pre-installed on RPi OS)
pip install gpiozero

# Enable SPI and I2C in raspi-config if not already done
sudo raspi-config  # → Interface Options → SPI / I2C
```

## Credentials

Copy or edit `auth.json` in the project directory:

```json
{
  "admin": {"password": "admin"},
  "user":  {"password": "password"}
}
```

Default: `admin` / `admin`. Change before deployment.

## Running

```bash
# Foreground (development)
sudo python3 onvif_thermal_server.py

# As systemd service
sudo systemctl start   onvif-thermal.service
sudo systemctl stop    onvif-thermal.service
sudo systemctl restart onvif-thermal.service
sudo systemctl status  onvif-thermal.service
sudo journalctl -u onvif-thermal.service -f

# Log file
tail -f /var/log/onvif-thermal.log
```

The service is defined in `/etc/systemd/system/onvif-thermal.service` and runs as root (required for GPIO/SPI access). It restarts automatically on failure.

## Ports

| Port | Protocol | Service | Description |
|------|----------|---------|-------------|
| 8000 | TCP | onvif-thermal | MJPEG stream, JPEG snapshot, ONVIF SOAP |
| 554  | TCP | mediamtx | RTSP (standard port) |
| 5000 | UDP | mediamtx | RTP (RTSP media data) |
| 5001 | UDP | mediamtx | RTCP (RTSP control) |

No WS-Discovery (UDP 3702) – add the camera manually in your NVR.

## RTSP stream

The RTSP gateway runs as a separate service (`mediamtx`). It pulls the MJPEG stream internally from port 8000 via ffmpeg and serves it on the standard RTSP port 554. No transcoding — frames are passed through unchanged.

```
rtsp://<pi-ip>/thermal
rtsp://admin:admin@<pi-ip>/thermal   # if your client needs credentials
```

```bash
# VLC
vlc rtsp://<pi-ip>/thermal

# ffplay
ffplay rtsp://<pi-ip>/thermal

# Service control
sudo systemctl start   mediamtx.service
sudo systemctl stop    mediamtx.service
sudo systemctl restart mediamtx.service
sudo journalctl -u mediamtx.service -f
```

Config: `/etc/mediamtx/mediamtx.yml`

## Endpoints

All endpoints require HTTP Basic Auth.

| Path | Method | Description |
|------|--------|-------------|
| `/stream` | GET | MJPEG live stream |
| `/snapshot` | GET | Single JPEG frame |
| `/onvif/device_service` | POST | ONVIF Device SOAP |
| `/onvif/media_service` | POST | ONVIF Media SOAP |
| `/onvif/events_service` | POST | ONVIF Events / PullPoint SOAP |
| `/onvif/events` | GET | Motion status (XML, legacy) |

### Quick test

```bash
# Snapshot
curl -u admin:admin http://localhost:8000/snapshot -o snap.jpg

# Live stream (Ctrl-C to stop)
curl -u admin:admin http://localhost:8000/stream

# Motion event status
curl -u admin:admin http://localhost:8000/onvif/events
```

### VLC

```
http://admin:admin@<pi-ip>:8000/stream
```

### NVR / VMS (ONVIF)

Point your NVR at `http://<pi-ip>:8000/onvif/device_service`. The server advertises itself via ONVIF Device, Media, and Events (PullPoint) services. Motion alarms are delivered via `CreatePullPointSubscription` / `PullMessages`.

## Synology Surveillance Station

### Via ONVIF (empfohlen)

1. **Surveillance Station öffnen** → IP-Kamera → Hinzufügen
2. **Methode**: "Kamera suchen" überspringen → **Manuell hinzufügen**
3. Felder ausfüllen:

   | Feld | Wert |
   |------|------|
   | Marke | ONVIF |
   | Modell | ONVIF Kamera |
   | Protokoll | HTTP |
   | IP-Adresse | `<pi-ip>` |
   | Port | `8000` |
   | Benutzername | `admin` |
   | Kennwort | `admin` |

4. **Verbindung testen** → Übernehmen.

Surveillance Station ruft dann selbst Stream-URL und Snapshot-URL über ONVIF ab.

---

### Via RTSP (alternativ, falls ONVIF-Erkennung fehlschlägt)

1. **Manuell hinzufügen** wie oben, aber:

   | Feld | Wert |
   |------|------|
   | Marke | Benutzerdefiniert (RTSP) |
   | Primärer Stream | `rtsp://<pi-ip>/thermal` |
   | Protokoll | RTSP |
   | Port | `554` |
   | Benutzername | *(leer)* |
   | Kennwort | *(leer)* |

   > mediamtx benötigt keine Authentifizierung für den RTSP-Zugriff (nur der HTTP-Port 8000 ist passwortgeschützt).

2. Codec auf **MJPEG** stellen, falls die Kamera nicht automatisch erkannt wird.

---

### Bewegungserkennung in Surveillance Station

Die Bewegungserkennung kann auf zwei Arten erfolgen:

- **Kameraseitig (ONVIF Events):** Surveillance Station abonniert `tns1:VideoSource/MotionAlarm` via PullPoint. Alarm wird ausgelöst wenn >5 % der Pixel sich um >2°C ändern. Einstellung: Kamera → Bewegungserkennung → **Kameraseitig**.
- **Surveillance Station intern:** Surveillance Station analysiert den Stream selbst per Bilddifferenz. Für thermische Bilder empfehlen sich höhere Empfindlichkeitsstufen, da der JET-Colormap Temperaturdifferenzen stark verstärkt.

## Frame layout

```
┌────────────────────┬──────────────────┐
│                    │ ▓ 28.3°C         │
│   640×480 thermal  │ ▒                │
│   (JET colormap)   │ ░ 20.1°C         │
│                    │                  │
│                    │   14.04.26       │
│                    │   14:51:47       │
│                    │                  │
│                    │ ░ 12.3°C         │
└────────────────────┴──────────────────┘
         640 px              80 px
```

Total output: 720×480 px. The 80 px colorbar strip contains the JET gradient, 5 temperature tick labels, and a date/time stamp.

## Configuration

All tunable constants are at the top of `onvif_thermal_server.py`:

```python
PORT             = 8000
STREAM_RES       = (640, 480)   # output resolution
FRAME_RATE       = 25           # FPS (MI48 max 25.5)
JPEG_QUALITY     = 70
COLORMAP         = cv.COLORMAP_JET
MOTION_THRESHOLD = 2.0          # °C per-pixel change for motion detection
MOTION_MIN_PCT   = 5.0          # % of pixels that must change to trigger motion
COLORBAR_W       = 80           # colorbar strip width in pixels
COLORBAR_TICKS   = 5            # number of temperature labels on scale
```

### Image processing pipeline

1. **Spatial smoothing** – 5×5 Gaussian on the raw 80×62 array (before upscaling).
2. **Temporal EMA** – per-pixel exponential moving average. Stable pixels use α=0.12 (noise suppression); pixels that change more than 0.8°C in one frame switch to α=0.80 (instant response to hands/people entering the scene).
3. **Percentile normalisation** – display range is set to the 0.5th–99.5th percentile so small hot spots (<2% of frame area) are always visible on the scale. Range itself is also EMA-smoothed (α=0.15 normally, α=0.80 when a scene change is detected).
4. **Colormap + upscale** – JET colormap, bilinear upscale to `STREAM_RES`.
5. **Colorbar** – temperature scale appended as a separate strip (cached, rebuilt only when range shifts >0.2°C).
6. **Timestamp** – date and time drawn inside the colorbar strip between the 4th and 5th tick labels.

## Architecture

Everything is in one file: `onvif_thermal_server.py`.

- **`_camera_loop`** – daemon thread. Initialises MI48, then loops: wait for DATA_READY → assert CS → read frame → deassert CS → run pipeline → JPEG-encode → store in `_latest_jpeg`.
- **`_Handler`** – `BaseHTTPRequestHandler` subclass. Serves all HTTP and SOAP endpoints. Auth checked on every request.
- **`_Server`** – `ThreadingTCPServer` with `TCP_NODELAY` set on every connection (prevents MJPEG frame buffering).
- **Main** – loads `auth.json`, starts camera thread, starts server on `PORT`.

## Temperature values

The MI48 returns raw uint16 values. `pysenxor` converts them to °C: `raw / 10 + KELVIN_0` where `KELVIN_0 = −273.15`. The colorbar labels reflect the actual scene temperature range in °C.

## ONVIF events (motion)

Motion is detected when more than 5% of pixels change by more than 2°C between frames. State changes (on/off) are pushed to an internal queue and delivered to subscribing NVRs via the ONVIF PullPoint mechanism (`tns1:VideoSource/MotionAlarm`).

Supported ONVIF operations:
- **Device**: `GetDeviceInformation`, `GetCapabilities`, `GetSystemDateAndTime`
- **Media**: `GetProfiles`, `GetStreamUri`, `GetSnapshotUri`
- **Events**: `GetEventProperties`, `CreatePullPointSubscription`, `PullMessages`, `Renew`, `Unsubscribe`
