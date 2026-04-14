# Technical Reference – Thermal Camera ONVIF Server

## System overview

```
┌─────────────────────────────────────────────────────────────────┐
│  Raspberry Pi                                                   │
│                                                                 │
│  MI48 sensor ──SPI──► camera thread ──► _latest_jpeg           │
│    (80×62, 25 FPS)      (daemon)          (shared bytes)        │
│                                               │                 │
│                              ┌────────────────┤                 │
│                              ▼                ▼                 │
│                         HTTP :8000         ffmpeg               │
│                         (ThreadingTCPServer)  │                 │
│                              │      MJPEG → H.264 (libx264)     │
│                    ┌─────────┼────────┐       │                 │
│                    ▼         ▼        ▼       ▼                 │
│                 /stream  /snapshot  ONVIF  mediamtx :554        │
│                 MJPEG    JPEG       SOAP   /thermal (H.264)     │
└─────────────────────────────────────────────────────────────────┘
                    │                   │       │
              browsers/VLC           NVRs   VLC/NVRs
              (HTTP Basic)         (ONVIF)  (RTSP/H.264)
```

mediamtx pulls `/stream` via an internal ffmpeg process, transcodes MJPEG to H.264 (libx264, Baseline profile, 1500 kbps), and serves the result as RTSP on port 554. Authentication for RTSP is delegated back to the Python server via the `/rtsp_auth` callback endpoint.

---

## File layout

| File | Description |
|------|-------------|
| `onvif_thermal_server.py` | Single-file server – everything except mediamtx |
| `auth.json` | Credentials (single source of truth for both HTTP and RTSP) |
| `setup.sh` | Idempotent deployment script |
| `/etc/systemd/system/onvif-thermal.service` | Main server service |
| `/etc/systemd/system/mediamtx.service` | RTSP gateway service |
| `/etc/mediamtx/mediamtx.yml` | mediamtx configuration |
| `/var/log/onvif-thermal.log` | Persistent log file |

---

## Ports

| Port | Proto | Service | Purpose |
|------|-------|---------|---------|
| 8000 | TCP | onvif-thermal | MJPEG stream, snapshot, ONVIF SOAP, rtsp_auth callback |
| 554  | TCP | mediamtx | RTSP (standard port) |
| 5000 | UDP | mediamtx | RTP (RTSP media data) |
| 5001 | UDP | mediamtx | RTCP (RTSP timing/control) |

---

## Authentication

### HTTP endpoints (`/stream`, `/snapshot`, `/onvif/*`)

All HTTP endpoints use **HTTP Basic Auth**. Credentials are loaded from `auth.json` at startup.

### ONVIF SOAP endpoints

SOAP endpoints additionally accept **WS-Security UsernameToken** (the method used by Synology Surveillance Station and other NVRs). Both password types are supported:

- **PasswordText** – plaintext password in SOAP header
- **PasswordDigest** – `Base64(SHA-1(nonce_bytes + created_utf8 + password_utf8))`

`GetSystemDateAndTime` is intentionally unauthenticated (required by ONVIF spec so NVR clients can fetch server time to compute the digest nonce before authenticating).

Implementation: `_auth_ok_soap(body)` in `_Handler`.

### RTSP (`/thermal` via mediamtx)

mediamtx calls `POST /rtsp_auth` (HTTP, localhost only) before accepting each RTSP connection. The Python server validates credentials from `auth.json` and returns HTTP 200 (allow) or 401 (deny).

The internal ffmpeg publisher (localhost, `action=publish`) is always allowed without credentials.

Implementation: `_handle_rtsp_auth(body)` in `_Handler`, `authMethod: http` in `mediamtx.yml`.

### auth.json format

```json
{
  "username": {"password": "plaintext_password"}
}
```

Changes to `auth.json` require a service restart (`sudo systemctl restart onvif-thermal.service`).

---

## Image processing pipeline

Raw MI48 frames (80×62 float16, °C) go through six stages before encoding:

### Stage 1 – Spatial smoothing
5×5 Gaussian blur on the raw 80×62 array. Removes spatially-correlated pixel noise before it gets amplified by 8× upscaling.

### Stage 2 – Motion-adaptive temporal EMA
Per-pixel exponential moving average. Two alpha values:

| Condition | Alpha | Effect |
|-----------|-------|--------|
| `\|pixel_change\| < 0.8°C` | 0.12 | Noise suppression (~8-frame time constant) |
| `\|pixel_change\| ≥ 0.8°C` | 0.80 | Instant response to hands/people entering frame |

### Stage 3 – Percentile normalisation
Display range = 0.5th to 99.5th percentile of the smoothed frame (ensures small hot spots < 2% of frame area stay visible). Range itself is EMA-smoothed:

| Condition | Alpha |
|-----------|-------|
| Stable scene | 0.15 |
| Range jump > 2°C | 0.80 |

### Stage 4 – Colormap + upscale
`cv.COLORMAP_JET`, bicubic upscale to 640×480.

### Stage 5 – Colorbar
80px strip appended to the right. Contains JET gradient, 5 temperature tick labels, and a date/time stamp. Cached and only rebuilt when the temperature range shifts by more than 0.2°C.

### Stage 6 – JPEG encode
`cv.imencode('.jpg', frame, [IMWRITE_JPEG_QUALITY, 70])`. Result stored in `_latest_jpeg` under `_frame_lock`.

**Output dimensions:** 720×480 px (640 thermal + 80 colorbar)

### Temperature values
MI48 raw uint16 → °C via pysenxor: `raw / 10 + KELVIN_0` where `KELVIN_0 = −273.15`. Colorbar labels show actual °C.

---

## ONVIF implementation

### Supported operations

#### Device service (`/onvif/device_service`)

| Operation | Notes |
|-----------|-------|
| `GetSystemDateAndTime` | Unauthenticated (ONVIF spec requirement) |
| `GetDeviceInformation` | Manufacturer: Meridian Innovation, Model: MI48 |
| `GetCapabilities` | Device, Media, Events XAddrs |
| `GetServices` | Lists device, media, events service URLs |
| `GetScopes` | `type/video_encoder`, `type/thermal`, `hardware/MI48`, `name/ThermalCamera` |
| `GetHostname` | Returns `socket.gethostname()` |
| `GetNetworkInterfaces` | Returns current IP |
| `GetNTP` | Returns `FromDHCP: true` |
| `GetDNS` | Returns `FromDHCP: true` |

#### Media service (`/onvif/media_service`)

| Operation | Notes |
|-----------|-------|
| `GetProfiles` / `GetProfile` | Built-in profile `Profile1`; NVR-created profiles included |
| `GetVideoSources` | Token `VideoSource0`, 720×480, 25 FPS |
| `GetVideoSourceConfigurations` / `GetVideoSourceConfiguration` | Token `VSConfig` |
| `GetVideoEncoderConfigurations` / `GetVideoEncoderConfiguration` | Token `VEConfig`, H.264 Baseline, 1500 kbps, 25 fps |
| `GetVideoEncoderConfigurationOptions` | Advertises H.264 Baseline, 1–25 fps |
| `SetVideoEncoderConfiguration` / `SetVideoSourceConfiguration` | Accepted silently (pipeline not reconfigurable at runtime) |
| `AddVideoSourceConfiguration` / `RemoveVideoSourceConfiguration` | Accepted silently |
| `AddVideoEncoderConfiguration` / `RemoveVideoEncoderConfiguration` | Accepted silently |
| `CreateProfile` | Creates NVR-managed profile with unique token; stored in `_created_profiles` |
| `DeleteProfile` | Removes from `_created_profiles`; built-in `Profile1` cannot be deleted |
| `GetStreamUri` | Returns `rtsp://<ip>/thermal` |
| `GetSnapshotUri` | Returns `http://<ip>:8000/snapshot` |
| `GetAudioSources` | Empty response (no audio) |
| `GetAudioEncoderConfigurations` | Empty response (no audio) |
| `GetServiceCapabilities` | SnapshotUri, RTP_TCP, RTP_RTSP_TCP |

#### Events service (`/onvif/events_service`)

| Operation | Notes |
|-----------|-------|
| `GetEventProperties` | Describes `tns1:VideoSource/MotionAlarm` topic |
| `CreatePullPointSubscription` | Returns subscription URL |
| `PullMessages` | Drains motion event queue, returns `IsMotion` notifications |
| `Renew` | Extends subscription |
| `Unsubscribe` | Removes subscription |

### Motion detection

Triggered when > 5% of pixels change by more than 2°C between consecutive raw frames (before pipeline smoothing). State changes (on/off) are pushed to `_pullpoint_events` queue (max 100 entries). NVRs poll via `PullMessages`.

Tunable constants: `MOTION_THRESHOLD = 2.0` °C, `MOTION_MIN_PCT = 5.0` %.

### WS-Discovery

Not implemented. Add cameras manually in NVR software using IP and port 8000.

---

## RTSP gateway (mediamtx)

```
onvif-thermal :8000/stream  (HTTP MJPEG, 720×480)
        │
        │  ffmpeg
        │    -c:v libx264 -profile:v baseline -level:v 3.1
        │    -tune zerolatency -preset ultrafast
        │    -b:v 1500k -g 25 -pix_fmt yuv420p
        │    -f rtsp -rtsp_transport tcp
        ▼
mediamtx internal RTSP publisher  (TCP, localhost)
        │
        ▼
RTSP :554/thermal  (H.264 Constrained Baseline, RTP/AVP)
```

mediamtx launches ffmpeg via `runOnInit` and restarts it automatically if it exits. The internal ffmpeg→mediamtx link uses TCP (`-rtsp_transport tcp`) to avoid UDP MTU issues on the loopback path. Authentication is delegated to the Python server's `/rtsp_auth` endpoint (see Authentication section).

Stream parameters:
- Codec: H.264 Constrained Baseline, Level 3.1
- Resolution: 720×480 (640 px thermal + 80 px colorbar)
- Bitrate: 1500 kbps CBR
- Frame rate: 25 fps (matches MI48 sensor rate)
- GOP length: 25 frames (1-second keyframe interval)
- packetization-mode: 1 (Non-Interleaved / FU-A for NALUs > MTU)

Config: `/etc/mediamtx/mediamtx.yml`

---

## Hardware interface

### SPI read sequence

The MI48 requires manual CS control (`spi.no_cs = True`):

```
assert CS_N (BCM7, active-low)
  wait 100 µs
  mi48.read()        # SPI transfer via spidev
  wait 100 µs
deassert CS_N
```

The `DATA_READY` pin (BCM24) goes high when a new frame is available. The camera loop calls `data_ready.wait_for_active()` to block until then.

Incorrect CS timing causes CRC errors on every frame.

### GPIO pin map

| Signal | BCM | RPi header |
|--------|-----|-----------|
| SPI CS_N | 7 | 26 |
| DATA_READY | 24 | 18 |
| RESET_N | 23 | 16 |
| SPI CLK | 11 | 23 |
| SPI MOSI | 10 | 19 |
| SPI MISO | 9 | 21 |
| I²C SDA | 2 | 3 |
| I²C SCL | 3 | 5 |

### SPI parameters

| Parameter | Value |
|-----------|-------|
| Bus/Device | 0/0 |
| Mode | 0b00 |
| Speed | 31.2 MHz |
| Transfer size | 160 bytes |
| CS control | Manual via GPIO |

---

## Threading model

| Thread | Name | Purpose |
|--------|------|---------|
| Main | `MainThread` | Starts server, handles signals |
| Camera | `camera` | SPI reads + full image pipeline + JPEG encode |
| Per-HTTP-request | (ThreadingTCPServer) | One thread per client connection |

The camera thread writes `_latest_jpeg` under `_frame_lock`. HTTP handler threads read it under the same lock. No queue between them – stream handlers poll with `seq` counter and 20ms sleep when no new frame is available.

`TCP_NODELAY` is set on every connection to prevent MJPEG frames from being batched by Nagle's algorithm.

---

## Configuration reference

All constants in `onvif_thermal_server.py`:

| Constant | Default | Description |
|----------|---------|-------------|
| `PORT` | 8000 | HTTP server port |
| `STREAM_RES` | (640, 480) | Output resolution before colorbar |
| `FRAME_RATE` | 25 | Target FPS (MI48 max 25.5) |
| `JPEG_QUALITY` | 70 | JPEG compression quality |
| `COLORMAP` | COLORMAP_JET | OpenCV colormap |
| `MOTION_THRESHOLD` | 2.0 | °C per-pixel change to count as motion |
| `MOTION_MIN_PCT` | 5.0 | % of pixels that must change to trigger motion |
| `COLORBAR_W` | 80 | Colorbar strip width in pixels |
| `COLORBAR_TICKS` | 5 | Number of temperature labels on scale |
| `_PIXEL_ALPHA` | 0.12 | Temporal EMA alpha for stable pixels |
| `_PIXEL_ALPHA_FAST` | 0.80 | Temporal EMA alpha for changing pixels |
| `_MOTION_THRESH_C` | 0.8 | °C change per pixel to switch to fast alpha |
| `_NORM_ALPHA` | 0.15 | Normalisation range EMA alpha (stable) |
| `_NORM_ALPHA_FAST` | 0.80 | Normalisation range EMA alpha (scene change) |
| `_NORM_THRESH_C` | 2.0 | °C range jump to switch to fast norm alpha |
| `_COLORBAR_REBUILD` | 0.2 | °C range change to trigger colorbar rebuild |

---

## Synology Surveillance Station compatibility

Tested and working with Synology Surveillance Station. Three non-obvious behaviours required specific implementation choices:

### 1. `GetVideoEncoderConfiguration` with empty token

Synology polls `GetVideoEncoderConfiguration` with an empty `<ConfigurationToken/>` element (5 consecutive calls) to look for an "unassigned" VEC before creating profiles. If any VEC is returned, Synology repeats the check indefinitely instead of proceeding to `GetStreamUri`.

**Fix:** Return a `ter:NoEntity` SOAP fault when the token is empty. Synology interprets this as "no free VEC available" and moves on to `SetVideoEncoderConfiguration` → `GetStreamUri`.

```python
_vec_tok = re.search(r'<ConfigurationToken[^>]*>([^<]*)</ConfigurationToken>', body)
if not _vec_tok or not _vec_tok.group(1).strip():
    self._soap_fault("NoEntity")
```

### 2. `CreateProfile` token uniqueness

Synology calls `CreateProfile` with the same name ("SynoProfile") three times and expects three different tokens. Returning the same token causes Synology to overwrite its own profile state.

**Fix:** The server auto-generates unique tokens via a counter suffix (`SynoProfile`, `SynoProfile1`, `SynoProfile2`). All created profiles are stored in the module-level `_created_profiles: dict` (token → name) and included in subsequent `GetProfiles` responses. The dict resets on service restart — Synology re-creates its profiles on each connection.

### 3. Internal ffmpeg → mediamtx uses TCP

Without `-rtsp_transport tcp`, ffmpeg sends RTP over UDP to mediamtx. For large H.264 NALUs (IDR frames), the resulting RTP packets can exceed mediamtx's 1440-byte threshold, forcing mediamtx to remux them via FU-A fragmentation. This remux can introduce framing errors visible as `received unexpected interleaved frame` and `connection reset by peer` from Synology's RTSP client.

**Fix:** `-rtsp_transport tcp` in the mediamtx.yml `runOnInit` command. The loopback TCP path has no MTU concern; mediamtx receives complete packets and re-packetises for downstream clients without corruption.

---

## Known limitations

- **No WS-Discovery (UDP 3702):** Cameras must be added manually to NVR software using IP and port 8000.
- **Runtime profiles are in-memory only:** Profiles created by the NVR via `CreateProfile` are stored in the module-level `_created_profiles` dict and are lost on service restart. The NVR re-creates them on reconnect.
- **No substream / secondary profile:** Only one video source and one VEC are supported. NVRs that require a dedicated low-resolution substream profile will not find one.
- **No PTZ:** Not applicable for a fixed thermal sensor.
- **No audio:** Empty responses for all audio ONVIF operations.
- **RTSP credentials in mediamtx config:** The ffmpeg `runOnInit` command contains HTTP credentials in plaintext in `/etc/mediamtx/mediamtx.yml`. Acceptable on a single-purpose device with localhost-only traffic.
- **auth.json restart required:** Credential changes take effect only after `sudo systemctl restart onvif-thermal.service`.
