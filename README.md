# Thermal Camera ONVIF Server

Python ONVIF IP-camera server for Raspberry Pi with the Meridian Innovation MI48 thermal sensor (Bobcat µHAT). Reads frames via SPI, renders them as a colour-mapped MJPEG stream with a live temperature scale, and exposes a full ONVIF interface so any NVR or VMS can discover and connect to it like a regular IP camera.

A separate `mediamtx` service transcodes the MJPEG stream to H.264 and serves it over standard RTSP (port 554) for NVRs that require it.

---

## Features

- **Full ONVIF profile** – Device, Media, and Events services over SOAP/HTTP
- **WS-Security UsernameToken** – PasswordDigest and PasswordText (Synology-compatible)
- **MJPEG HTTP stream** – direct access via browser, VLC, or any HTTP client
- **H.264 RTSP stream** – via mediamtx on standard port 554, tested with Synology Surveillance Station
- **Motion detection** – ONVIF PullPoint events (`tns1:VideoSource/MotionAlarm`) based on per-pixel temperature change
- **Thermal image pipeline** – Gaussian spatial smoothing → motion-adaptive temporal EMA → percentile normalisation → JET colormap → 640×480 upscale
- **Live temperature scale** – 80 px colorbar strip with 5 tick labels and date/time stamp, cached and updated only when the scene range shifts
- **Single-file server** – everything in `onvif_thermal_server.py`, easy to audit and deploy
- **systemd services** – auto-start and auto-restart for both the Python server and the RTSP gateway

---

## Hardware

| Component | Detail |
|-----------|--------|
| Board | Raspberry Pi (tested on Pi 3 / 4, 64-bit OS) |
| Sensor | Meridian Innovation MI48 – 80×62 pixels, up to 25.5 FPS |
| HAT | Meridian Bobcat µHAT |
| Interface | SPI (frames) + I²C (control) |

### GPIO wiring

| Signal | BCM GPIO | RPi pin |
|--------|----------|---------|
| SPI CS_N | 7 | 26 |
| DATA_READY | 24 | 18 |
| RESET_N | 23 | 16 |
| SPI CLK | 11 | 23 |
| SPI MOSI | 10 | 19 |
| SPI MISO | 9 | 21 |
| I²C SDA | 2 | 3 |
| I²C SCL | 3 | 5 |

CS is driven manually via GPIO (`spi.no_cs = True`). Read sequence: assert CS → 100 µs → `mi48.read()` → 100 µs → deassert CS.

---

## Architecture

```
MI48 sensor (SPI)
      │
      ▼
camera thread          ─────────────────────────────────
  Gaussian smooth                                       │
  Temporal EMA                                         │  ffmpeg
  Percentile norm      → _latest_jpeg (JPEG, 720×480)  │  MJPEG → H.264
  JET colormap                │                        │  libx264 baseline
  Colorbar + timestamp        │                        │  1500 kbps, 25 fps
                              │                        │  (internal TCP)
                        HTTP :8000                     │
                    ┌─────────┼──────┐           mediamtx :554
                    ▼         ▼      ▼                 │
                 /stream  /snapshot  ONVIF SOAP    /thermal (RTSP/H.264)
                  MJPEG    JPEG    device_service        │
                              media_service         ─────────────────────
                              events_service        NVR / VLC / ffplay
                    │              │
                 browsers      Synology SS
                  VLC          Blue Iris
                               Milestone …
```

---

## Installation

```bash
git clone <this-repo> /home/pi/thermalcam_onvif
cd /home/pi/thermalcam_onvif
sudo bash setup.sh
```

`setup.sh` is idempotent – safe to re-run after updates. It:
1. Installs system packages (`python3-numpy`, `python3-opencv`, `ffmpeg`, …)
2. Installs the `pysenxor` driver
3. Enables SPI and I²C via `raspi-config`
4. Creates default `auth.json` if absent
5. Downloads mediamtx v1.17.1 (arm64)
6. Writes `/etc/mediamtx/mediamtx.yml`
7. Installs and starts both systemd services

### Manual dependency install (without setup.sh)

```bash
sudo apt install python3-numpy python3-opencv python3-smbus python3-spidev python3-gpiozero ffmpeg
pip3 install -e Thermal_Camera_Hat/pysenxor-master/
sudo raspi-config   # → Interface Options → SPI + I²C
```

---

## Credentials

`auth.json` in the project directory (single source of truth for HTTP, ONVIF, and RTSP):

```json
{
  "admin": {"password": "admin"},
  "user":  {"password": "password"}
}
```

**Change before deployment.** Changes require a service restart:
```bash
sudo systemctl restart onvif-thermal.service
```

---

## Running

```bash
# Foreground (development / debugging)
sudo python3 onvif_thermal_server.py

# Service management
sudo systemctl start   onvif-thermal.service
sudo systemctl stop    onvif-thermal.service
sudo systemctl restart onvif-thermal.service
sudo systemctl status  onvif-thermal.service
sudo journalctl -u onvif-thermal.service -f

# RTSP gateway
sudo systemctl restart mediamtx.service
sudo journalctl -u mediamtx.service -f

# Log file
tail -f /var/log/onvif-thermal.log
```

---

## Ports

| Port | Protocol | Service | Description |
|------|----------|---------|-------------|
| 8000 | TCP | onvif-thermal | MJPEG stream, JPEG snapshot, ONVIF SOAP, rtsp_auth |
| 554  | TCP | mediamtx | RTSP H.264 (standard port) |
| 5000 | UDP | mediamtx | RTP (RTSP media data) |
| 5001 | UDP | mediamtx | RTCP (RTSP timing/control) |

No WS-Discovery (UDP 3702) – add the camera manually in your NVR using the IP and port 8000.

---

## Endpoints

All HTTP endpoints require Basic Auth.

| Path | Method | Description |
|------|--------|-------------|
| `/stream` | GET | MJPEG live stream |
| `/snapshot` | GET | Single JPEG frame |
| `/onvif/device_service` | POST | ONVIF Device SOAP |
| `/onvif/media_service` | POST | ONVIF Media SOAP |
| `/onvif/events_service` | POST | ONVIF Events / PullPoint SOAP |
| `/onvif/events` | GET | Motion status (XML) |

### Quick test

```bash
# Snapshot
curl -u admin:admin http://localhost:8000/snapshot -o snap.jpg

# Live stream (Ctrl-C to stop)
curl -u admin:admin http://localhost:8000/stream

# RTSP stream
ffplay rtsp://admin:admin@localhost/thermal
vlc    rtsp://admin:admin@<pi-ip>/thermal
```

---

## Frame layout

```
┌────────────────────┬──────────────────┐
│                    │ ▓ 34.1°C         │
│   640×480 thermal  │ ▒                │
│   (JET colormap)   │ ░ 26.3°C         │
│                    │                  │
│                    │   14.04.26       │
│                    │   23:07:21       │
│                    │                  │
│                    │ ░ 18.7°C         │
└────────────────────┴──────────────────┘
         640 px              80 px
```

Total output: **720×480 px**. The 80 px colorbar shows the JET gradient, 5 temperature tick labels, and a date/time stamp. The range is set to the 0.5th–99.5th percentile of the current frame so small hot spots always appear on the scale.

---

## NVR / VMS integration

### Generic ONVIF

Point your NVR at `http://<pi-ip>:8000/onvif/device_service`. The server advertises ONVIF Device, Media, and Events services. Motion alarms are delivered via `CreatePullPointSubscription` / `PullMessages`.

Supported ONVIF operations: see [TECHREF.md](TECHREF.md).

### RTSP (direct, without ONVIF)

```
rtsp://admin:admin@<pi-ip>/thermal
```

H.264 Constrained Baseline, 720×480, 25 fps, 1500 kbps.

---

## Synology Surveillance Station

### Via ONVIF (empfohlen)

Der Server unterstützt vollständig **WS-Security UsernameToken** (PasswordDigest und PasswordText) – die Authentifizierungsmethode, die Synology standardmäßig verwendet.

1. **Surveillance Station** → IP-Kamera → Hinzufügen
2. "Kamera suchen" überspringen → **Manuell hinzufügen**
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

Surveillance Station ruft Stream-URL und Snapshot-URL automatisch über ONVIF ab und verbindet dann über RTSP (H.264).

> `GetSystemDateAndTime` ist gemäß ONVIF-Spezifikation ohne Authentifizierung erreichbar – das ist korrekt und wird von Synology benötigt, um den Digest-Nonce zu berechnen.

---

### Via RTSP (Fallback)

Falls die ONVIF-Erkennung fehlschlägt:

| Feld | Wert |
|------|------|
| Marke | Benutzerdefiniert (RTSP) |
| Protokoll | RTSP |
| IP-Adresse | `<pi-ip>` |
| Port | `554` |
| Primärer Stream | `rtsp://<pi-ip>/thermal` |
| Benutzername | `admin` |
| Kennwort | `admin` |

---

### Bewegungserkennung in Surveillance Station

- **Kameraseitig (ONVIF Events):** Surveillance Station abonniert `tns1:VideoSource/MotionAlarm` via PullPoint. Alarm wenn > 5 % der Pixel sich um > 2 °C ändern. Einstellung: Kamera → Bewegungserkennung → **Kameraseitig**.
- **Surveillance Station intern:** Surveillance Station analysiert den Stream selbst. Für thermische Bilder empfehlen sich höhere Empfindlichkeitsstufen, da die JET-Colormap Temperaturdifferenzen stark verstärkt.

---

## Configuration

All tunable constants at the top of `onvif_thermal_server.py`:

```python
PORT             = 8000
STREAM_RES       = (640, 480)   # output resolution (before colorbar)
FRAME_RATE       = 25           # FPS (MI48 max 25.5)
JPEG_QUALITY     = 85
COLORMAP         = cv.COLORMAP_JET
MOTION_THRESHOLD = 2.0          # °C per-pixel change threshold
MOTION_MIN_PCT   = 5.0          # % of pixels that must change to trigger
COLORBAR_W       = 80           # colorbar strip width (px)
COLORBAR_TICKS   = 5            # temperature labels on scale
```

RTSP transcoding parameters (libx264, H.264 Baseline) are in `/etc/mediamtx/mediamtx.yml`.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---------|-------------|-----|
| Camera thread: CRC errors every frame | CS timing wrong or SPI wiring issue | Check GPIO wiring, especially CS_N on BCM7 |
| HTTP 401 on all endpoints | Wrong credentials | Check `auth.json`, restart service |
| NVR stuck on "Activating" | Leftover ONVIF session state | Remove camera from NVR and re-add |
| RTSP stream connects then drops | mediamtx not running | `sudo systemctl restart mediamtx.service` |
| `av_interleaved_write_frame: Broken pipe` in mediamtx log | onvif-thermal restarted while mediamtx was running | Normal – mediamtx/ffmpeg restarts automatically |

**Logs:**
```bash
sudo journalctl -u onvif-thermal.service -f
sudo journalctl -u mediamtx.service -f
tail -f /var/log/onvif-thermal.log
```

---

## Technical reference

Full technical documentation (architecture, ONVIF operation table, authentication flows, image pipeline, threading model, hardware SPI timing, Synology compatibility notes): **[TECHREF.md](TECHREF.md)**
