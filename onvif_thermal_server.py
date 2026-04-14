#!/usr/bin/env python3
"""
ONVIF Thermal Camera Server – Meridian MI48 on Raspberry Pi.

Endpoints
---------
GET  /stream                  MJPEG live stream  (VLC, browsers, NVRs)
GET  /snapshot                Single JPEG frame
POST /onvif/device_service    ONVIF Device service (SOAP)
POST /onvif/media_service     ONVIF Media service (SOAP)
POST /onvif/events_service    ONVIF Events / PullPoint (SOAP)
GET  /onvif/events            Motion event status (XML, legacy)

Credentials: auth.json  (same directory as this file)
"""

import base64
import hashlib
import http.server
import json
import logging
import os
import queue
import re
import signal
import socket
import socketserver
import threading
import time
import uuid
from datetime import datetime, timedelta

import cv2 as cv
import numpy as np
from gpiozero import DigitalInputDevice, DigitalOutputDevice
from smbus import SMBus
from spidev import SpiDev

from senxor.interfaces import I2C_Interface, SPI_Interface
from senxor.mi48 import DATA_READY, MI48
from senxor.utils import data_to_frame

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PORT             = 8000
AUTH_FILE        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'auth.json')
STREAM_RES       = (640, 480)   # output resolution (width, height)
FRAME_RATE       = 25           # FPS – MI48 Bobcat max is 25.5; use 25
JPEG_QUALITY     = 70   # thermal imagery tolerates lower JPEG quality well
COLORMAP         = cv.COLORMAP_JET
MOTION_THRESHOLD = 2.0          # °C per-pixel change to count as motion
MOTION_MIN_PCT   = 5.0          # % of pixels that must change

# MI48 hardware wiring (Meridian uHAT on RPi)
I2C_CHANNEL    = 1
I2C_ADDR       = 0x40
SPI_BUS        = 0
SPI_DEVICE     = 0
SPI_MODE       = 0b00
SPI_SPEED_HZ   = 31_200_000
SPI_XFER_BYTES = 160
SPI_CS_DELAY   = 0.0001         # seconds, before/after CS assert/deassert
GPIO_CS_N      = "BCM7"         # active-low chip select
GPIO_DATA_RDY  = "BCM24"        # data-ready input
GPIO_RESET_N   = "BCM23"        # active-low reset

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('/var/log/onvif-thermal.log'),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger('onvif-thermal')

# ---------------------------------------------------------------------------
# Shared state (camera thread → HTTP handlers)
# ---------------------------------------------------------------------------
_frame_lock      = threading.Lock()
_latest_jpeg     = None   # bytes, set by processor thread
_frame_seq       = 0      # incremented on every new frame; lets stream handlers detect changes
_motion_active   = False
_motion_event_id = None   # str uuid
_auth            = {}     # loaded from auth.json
_created_profiles: dict = {}  # token → name  (profiles created by NVR via CreateProfile)

# Pipeline: SPI reader thread → _raw_queue → processor thread
_raw_queue = queue.Queue(maxsize=1)  # maxsize=1 – always process the latest frame

# ONVIF PullPoint event queue (motion on/off events for NVR subscribers)
_pullpoint_lock   = threading.Lock()
_pullpoint_events = []   # list of (utc_iso_str, is_motion_bool) waiting to be polled

# ---------------------------------------------------------------------------
# Image processing pipeline
# ---------------------------------------------------------------------------
#
# Stage 1 – spatial smoothing on the raw 80×62 sensor array.
#   A 5×5 Gaussian at native resolution blurs spatially-correlated
#   pixel noise before it gets amplified by 8× upscaling.
#
# Stage 2 – per-pixel temporal EMA (α=0.08 → ~12-frame / 500 ms constant).
#   Removes remaining temporal noise without introducing visible lag on
#   slow-moving thermal objects.
#
# Stage 3 – percentile normalization (2nd/98th) with slow EMA on the range
#   (α=0.02 → ~2 s). Prevents single hot pixels from crushing the colour
#   scale and stops the range from jumping frame-to-frame.
#
# Stage 4 – colourbar: a 60 px strip is appended to the right of the frame
#   showing the full JET gradient with temperature labels (no overlay).

_PIXEL_ALPHA       = 0.12   # stable-scene noise reduction  (~8-frame time constant)
_PIXEL_ALPHA_FAST  = 0.80   # fast-change pixels (hand, person) – 2-frame response
_MOTION_THRESH_C   = 0.8    # °C per-pixel change that triggers fast-alpha path
_NORM_ALPHA        = 0.15   # slow norm adaptation (stable scene, noise suppression)
_NORM_ALPHA_FAST   = 0.80   # fast norm adaptation (triggered when range jumps > 2°C)
_NORM_THRESH_C     = 2.0    # °C range-jump threshold to switch to fast norm alpha
_smooth_raw   = None   # per-pixel EMA accumulator (float64)
_norm_lo      = None   # smoothed low  end of display range (°C or raw units)
_norm_hi      = None   # smoothed high end of display range

# Colorbar cache – rebuilt only when temperature range changes by >0.2°C
_cached_bar       = None
_cached_bar_lo    = None
_cached_bar_hi    = None
_COLORBAR_REBUILD = 0.2   # °C change threshold to trigger rebuild

COLORBAR_W     = 80    # total width of the appended strip
COLORBAR_GRAD  = 16   # width of the colour gradient bar itself
COLORBAR_TICKS = 5    # number of labelled temperature ticks


def _build_colorbar(height: int, lo: float, hi: float) -> np.ndarray:
    """Return a (height × COLORBAR_W × 3) BGR strip with JET gradient + labels.

    Gradient is built with numpy (no Python loop), then labels are drawn.
    Layout: [4px pad][16px gradient][4px gap][labels]
    """
    bar = np.full((height, COLORBAR_W, 3), 30, dtype=np.uint8)

    gx0 = 4
    gx1 = gx0 + COLORBAR_GRAD

    # build gradient column via numpy, then broadcast to strip width
    vals = np.linspace(255, 0, height, dtype=np.uint8).reshape(height, 1, 1)
    gradient_col = cv.applyColorMap(vals, COLORMAP)          # (H,1,3)
    bar[:, gx0:gx1] = gradient_col                           # broadcast to strip

    # tick marks + labels (only 5 iterations – negligible cost)
    font = cv.FONT_HERSHEY_SIMPLEX
    lx   = gx1 + 4
    for i in range(COLORBAR_TICKS):
        frac  = i / (COLORBAR_TICKS - 1)
        temp  = hi - frac * (hi - lo)
        y     = int(frac * (height - 1))
        cv.line(bar, (gx1, y), (gx1 + 3, y), (200, 200, 200), 1)
        ty = max(min(y + 4, height - 4), 8)
        cv.putText(bar, f"{temp:.1f}", (lx, ty),
                   font, 0.33, (220, 220, 220), 1, cv.LINE_AA)
    return bar


def _process_frame(raw: np.ndarray):
    """
    Full pipeline: raw sensor array → (coloured frame, current lo/hi temps).
    Returns (bgr_with_colorbar, norm_lo, norm_hi).
    """
    global _smooth_raw, _norm_lo, _norm_hi

    # stage 1: spatial smoothing on native 80×62 array
    smoothed_spatial = cv.GaussianBlur(raw.astype(np.float32), (5, 5), 0)

    # stage 2: motion-adaptive per-pixel temporal EMA
    #   - stable pixels  → low alpha  (noise removal)
    #   - fast-changing  → high alpha (instant response to hand / person entering)
    if _smooth_raw is None:
        _smooth_raw = smoothed_spatial.astype(np.float64)
    else:
        diff = smoothed_spatial.astype(np.float64) - _smooth_raw
        alpha_map = np.where(np.abs(diff) > _MOTION_THRESH_C,
                             _PIXEL_ALPHA_FAST, _PIXEL_ALPHA)
        _smooth_raw += alpha_map * diff

    # stage 3: percentile normalisation with motion-adaptive EMA on range
    #   - stable scene  → slow alpha (suppresses norm-range flicker from noise)
    #   - scene changes → fast alpha (hand/person: instant contrast re-normalise)
    # Use 0.5/99.5 so small hot spots (< 2 % of frame) are not clipped off the scale
    lo = float(np.percentile(_smooth_raw,  0.5))
    hi = float(np.percentile(_smooth_raw, 99.5))
    if _norm_lo is None:
        _norm_lo, _norm_hi = lo, hi
    else:
        norm_alpha = (_NORM_ALPHA_FAST
                      if abs(lo - _norm_lo) > _NORM_THRESH_C
                         or abs(hi - _norm_hi) > _NORM_THRESH_C
                      else _NORM_ALPHA)
        _norm_lo += norm_alpha * (lo - _norm_lo)
        _norm_hi += norm_alpha * (hi - _norm_hi)

    span   = max(_norm_hi - _norm_lo, 0.1)
    normed = np.clip((_smooth_raw - _norm_lo) / span, 0.0, 1.0)
    img8u  = (normed * 255).astype(np.uint8)

    # colormap + upscale
    colored = cv.applyColorMap(img8u, COLORMAP)
    frame   = cv.resize(colored, STREAM_RES, interpolation=cv.INTER_CUBIC)

    # stage 4: append colorbar – cached, rebuilt only when range shifts >0.2°C
    global _cached_bar, _cached_bar_lo, _cached_bar_hi
    if (_cached_bar is None
            or abs(_norm_lo - _cached_bar_lo) > _COLORBAR_REBUILD
            or abs(_norm_hi - _cached_bar_hi) > _COLORBAR_REBUILD):
        _cached_bar    = _build_colorbar(STREAM_RES[1], _norm_lo, _norm_hi)
        _cached_bar_lo = _norm_lo
        _cached_bar_hi = _norm_hi

    frame = np.concatenate([frame, _cached_bar], axis=1)

    # stage 5: timestamp in the colorbar strip – placed between tick 3 (y≈364)
    # and tick 4 (y≈476) so it never overlaps temperature labels.
    # cx aligns with tick labels (gx0=4, COLORBAR_GRAD=16, gap=4 → lx=24 within bar).
    now = datetime.now()
    cx  = STREAM_RES[0] + COLORBAR_GRAD + 8   # = 664, aligned with tick labels
    cv.putText(frame, now.strftime('%d.%m.%y'),
               (cx, 415),
               cv.FONT_HERSHEY_SIMPLEX, 0.33, (170, 170, 170), 1, cv.LINE_AA)
    cv.putText(frame, now.strftime('%H:%M:%S'),
               (cx, 432),
               cv.FONT_HERSHEY_SIMPLEX, 0.40, (210, 210, 210), 1, cv.LINE_AA)

    return frame


def _load_auth() -> None:
    global _auth
    try:
        with open(AUTH_FILE) as f:
            _auth = json.load(f)
        log.info("Loaded %d users from %s", len(_auth), AUTH_FILE)
    except Exception as exc:
        log.warning("Cannot load %s (%s) – using built-in default", AUTH_FILE, exc)
        _auth = {"admin": {"password": "admin", "role": "admin"}}


# ---------------------------------------------------------------------------
# MI48 reset handler
# ---------------------------------------------------------------------------
class _MI48Reset:
    def __init__(self, pin, assert_s: float = 0.000035, deassert_s: float = 0.050):
        self.pin = pin
        self.assert_s = assert_s
        self.deassert_s = deassert_s

    def __call__(self) -> None:
        log.info("Resetting MI48…")
        self.pin.on()
        time.sleep(self.assert_s)
        self.pin.off()
        time.sleep(self.deassert_s)
        log.info("Reset done.")


# ---------------------------------------------------------------------------
# Motion detection (on raw float temperature data)
# ---------------------------------------------------------------------------
def _detect_motion(current, prev) -> bool:
    if prev is None:
        return False
    pct = np.sum(np.abs(current.astype(float) - prev.astype(float)) > MOTION_THRESHOLD)
    return (pct / current.size * 100) > MOTION_MIN_PCT


# ---------------------------------------------------------------------------
# Camera thread – single loop (SPI read + process in one thread)
# ---------------------------------------------------------------------------

def _camera_loop() -> None:
    global _latest_jpeg, _motion_active, _motion_event_id

    log.info("Initialising MI48…")
    try:
        i2c = I2C_Interface(SMBus(I2C_CHANNEL), I2C_ADDR)

        spi_dev = SpiDev(SPI_BUS, SPI_DEVICE)
        spi = SPI_Interface(spi_dev, xfer_size=SPI_XFER_BYTES)
        spi.device.mode          = SPI_MODE
        spi.device.max_speed_hz  = SPI_SPEED_HZ
        spi.device.bits_per_word = 8
        spi.device.lsbfirst      = False
        spi.cshigh = True
        spi.no_cs  = True

        cs_n       = DigitalOutputDevice(GPIO_CS_N,    active_high=False, initial_value=False)
        data_ready = DigitalInputDevice( GPIO_DATA_RDY, pull_up=False)
        reset_n    = DigitalOutputDevice(GPIO_RESET_N,  active_high=False, initial_value=True)

        mi48 = MI48(
            [i2c, spi],
            data_ready=data_ready,
            reset_handler=_MI48Reset(pin=reset_n),
        )

        log.info("Camera: %s", mi48.get_camera_info())
        mi48.set_fps(FRAME_RATE)

        if int(mi48.fw_version[0]) >= 2:
            mi48.enable_filter(f1=True, f2=True, f3=False)
            mi48.set_offset_corr(0.0)

        mi48.start(stream=True, with_header=True)
        log.info("MI48 streaming at %d FPS.", FRAME_RATE)

    except Exception as exc:
        log.error("Camera init failed: %s", exc)
        return

    prev_raw     = None
    fps_count    = 0
    fps_t0       = time.monotonic()
    temp_log_t0  = time.monotonic()

    try:
        while True:
            if hasattr(mi48, 'data_ready'):
                mi48.data_ready.wait_for_active()
            else:
                while not (mi48.get_status() & DATA_READY):
                    time.sleep(0.01)

            cs_n.on()
            time.sleep(SPI_CS_DELAY)
            data, _ = mi48.read()
            time.sleep(SPI_CS_DELAY)
            cs_n.off()

            if data is None:
                log.error("None data from MI48 – stopping camera thread.")
                break
            if mi48.crc_error:
                log.debug("CRC error, skipping frame.")
                continue

            raw = data_to_frame(data, mi48.fpa_shape)

            if time.monotonic() - temp_log_t0 >= 5.0:
                log.info("Sensor raw: min=%.1f°C  max=%.1f°C  mean=%.1f°C",
                         float(raw.min()), float(raw.max()), float(raw.mean()))
                temp_log_t0 = time.monotonic()

            motion_now = _detect_motion(raw, prev_raw)
            if motion_now and not _motion_active:
                _motion_active   = True
                _motion_event_id = str(uuid.uuid4())
                log.info("Motion detected – event %s", _motion_event_id)
                _push_motion_event(True)
            elif not motion_now and _motion_active:
                _motion_active = False
                log.info("Motion ended.")
                _push_motion_event(False)
            prev_raw = raw.copy()

            frame = _process_frame(raw)
            ok, buf = cv.imencode('.jpg', frame, [cv.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
            if ok:
                with _frame_lock:
                    global _frame_seq
                    _latest_jpeg = buf.tobytes()
                    _frame_seq  += 1
                fps_count += 1
                elapsed = time.monotonic() - fps_t0
                if elapsed >= 10.0:
                    log.info("Camera: %.1f FPS (target %d)", fps_count / elapsed, FRAME_RATE)
                    fps_count = 0
                    fps_t0    = time.monotonic()

    except Exception as exc:
        log.error("Camera loop error: %s", exc)
    finally:
        try:
            mi48.stop(poll_timeout=0.25, stop_timeout=1.2)
        except Exception:
            pass
        log.info("Camera thread exited.")


def _push_motion_event(is_motion: bool) -> None:
    """Append a motion state-change to the ONVIF PullPoint queue."""
    ts = datetime.utcnow().isoformat(timespec='seconds') + 'Z'
    with _pullpoint_lock:
        _pullpoint_events.append((ts, is_motion))
        if len(_pullpoint_events) > 100:
            del _pullpoint_events[:-100]


# ---------------------------------------------------------------------------
# HTTP request handler
# ---------------------------------------------------------------------------
def _get_ip() -> str:
    try:
        return socket.gethostbyname(socket.gethostname())
    except Exception:
        return '127.0.0.1'


class _Handler(http.server.BaseHTTPRequestHandler):

    def log_message(self, fmt, *args) -> None:  # silence per-request stdout spam
        log.debug("%s – " + fmt, self.address_string(), *args)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def _send_401(self) -> None:
        body = b'Authentication required'
        self.send_response(401)
        self.send_header('WWW-Authenticate', 'Basic realm="Thermal Camera"')
        self.send_header('Content-Type', 'text/plain')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _auth_ok(self) -> bool:
        """HTTP Basic Auth – used for stream/snapshot/events."""
        hdr = self.headers.get('Authorization', '')
        if not hdr.lower().startswith('basic '):
            self._send_401()
            return False
        try:
            user, pw = base64.b64decode(hdr.split(' ', 1)[1]).decode().split(':', 1)
            if _auth.get(user, {}).get('password') == pw:
                return True
        except Exception:
            pass
        self._send_401()
        return False

    def _auth_ok_soap(self, body: str) -> bool:
        """Accept HTTP Basic Auth OR ONVIF WS-Security UsernameToken (PasswordText/PasswordDigest).

        ONVIF clients (e.g. Synology Surveillance Station) embed credentials in the
        SOAP Security header rather than using HTTP Basic Auth.  PasswordDigest is
        computed as Base64(SHA-1(nonce_bytes + created_utf8 + password_utf8)).
        """
        # --- 1. HTTP Basic Auth (curl, simple clients) ---
        hdr = self.headers.get('Authorization', '')
        if hdr.lower().startswith('basic '):
            try:
                user, pw = base64.b64decode(hdr.split(' ', 1)[1]).decode().split(':', 1)
                if _auth.get(user, {}).get('password') == pw:
                    return True
            except Exception:
                pass

        # --- 2. WS-Security UsernameToken ---
        m_user = re.search(r'<[^:>\s]*:?Username[^>]*>([^<]+)</', body)
        if not m_user:
            self._send_401()
            return False
        username  = m_user.group(1).strip()
        stored_pw = _auth.get(username, {}).get('password')
        if stored_pw is None:
            self._send_401()
            return False

        m_pw = re.search(r'<[^:>\s]*:?Password\b([^>]*)>([^<]+)</', body)
        if not m_pw:
            self._send_401()
            return False
        pw_attrs = m_pw.group(1)
        pw_value = m_pw.group(2).strip()

        if 'PasswordDigest' in pw_attrs:
            m_nonce   = re.search(r'<[^:>\s]*:?Nonce\b[^>]*>([^<]+)</',   body)
            m_created = re.search(r'<[^:>\s]*:?Created\b[^>]*>([^<]+)</', body)
            if not m_nonce or not m_created:
                self._send_401()
                return False
            try:
                nonce_b  = base64.b64decode(m_nonce.group(1).strip())
                created_b = m_created.group(1).strip().encode()
                expected  = base64.b64encode(
                    hashlib.sha1(nonce_b + created_b + stored_pw.encode()).digest()
                ).decode()
                if expected == pw_value:
                    return True
            except Exception:
                pass
        else:
            # PasswordText
            if pw_value == stored_pw:
                return True

        self._send_401()
        return False

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def do_GET(self) -> None:
        if not self._auth_ok():
            return
        path = self.path.split('?')[0]
        if path == '/stream':
            self._handle_stream()
        elif path == '/snapshot':
            self._handle_snapshot()
        elif path == '/onvif/events':
            self._handle_events()
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        path   = self.path.split('?')[0]
        length = int(self.headers.get('Content-Length', 0))
        body   = self.rfile.read(length).decode('utf-8', errors='replace')

        # mediamtx RTSP auth callback – localhost only, no HTTP auth wrapper needed
        if path == '/rtsp_auth':
            self._handle_rtsp_auth(body)
            return

        is_onvif = any(x in path for x in ('/device_service', '/media_service', '/events_service'))

        # ONVIF spec requires GetSystemDateAndTime to be accessible without auth
        # so NVR clients can fetch server time to compute WS-Security digest nonces.
        if is_onvif and 'GetSystemDateAndTime' in body:
            self._soap_device(body)
            return

        if is_onvif:
            if not self._auth_ok_soap(body):
                return
        elif not self._auth_ok():
            return

        if is_onvif:
            # Extract action name from inside the SOAP Body element
            m = re.search(r'<(?:[^:>\s]+:)?Body[^>]*>\s*<(?:[^:>\s]+:)?(\w+)', body)
            action_name = m.group(1) if m else '?'
            log.info("SOAP %-14s %-40s [%s]", path.split('/')[-1], action_name,
                     self.client_address[0])

        if '/device_service' in path:
            self._soap_device(body)
        elif '/media_service' in path:
            self._soap_media(body)
        elif '/events_service' in path:
            self._soap_events(body)
        else:
            self.send_error(404)

    # ------------------------------------------------------------------
    # mediamtx RTSP auth callback
    # ------------------------------------------------------------------

    def _handle_rtsp_auth(self, body: str) -> None:
        """Called by mediamtx (HTTP auth backend) to validate RTSP clients.

        mediamtx POSTs JSON: {"user":"...","password":"...","ip":"...","action":"read|publish",...}
        We return HTTP 200 to allow or 401 to deny.
        Publish actions from localhost (internal ffmpeg) are always allowed.
        """
        try:
            data   = json.loads(body)
            action = data.get('action', '')
            ip     = data.get('ip', '')
            user   = data.get('user', '')
            pw     = data.get('password', '')
        except Exception:
            self.send_response(400)
            self.end_headers()
            return

        # Internal ffmpeg publisher – allow without credentials
        if action == 'publish' and ip in ('127.0.0.1', '::1'):
            self.send_response(200)
            self.end_headers()
            return

        # External clients – validate against auth.json
        if _auth.get(user, {}).get('password') == pw:
            self.send_response(200)
            self.end_headers()
        else:
            self.send_response(401)
            self.end_headers()

    # ------------------------------------------------------------------
    # Stream / snapshot
    # ------------------------------------------------------------------

    def _handle_snapshot(self) -> None:
        with _frame_lock:
            frame = _latest_jpeg
        if frame is None:
            body = b'Camera not ready'
            self.send_response(503)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(200)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(len(frame)))
        self.end_headers()
        self.wfile.write(frame)

    def _handle_stream(self) -> None:
        self.send_response(200)
        self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
        self.end_headers()
        last_seq = -1
        try:
            while True:
                with _frame_lock:
                    seq   = _frame_seq
                    frame = _latest_jpeg
                if frame is None or seq == last_seq:
                    time.sleep(0.02)   # 50 Hz poll – yields CPU while waiting for camera
                    continue
                last_seq = seq
                self.wfile.write(
                    b'--frame\r\n'
                    b'Content-Type: image/jpeg\r\n'
                    + f'Content-Length: {len(frame)}\r\n\r\n'.encode()
                    + frame
                    + b'\r\n'
                )
                self.wfile.flush()
        except Exception:
            pass  # client disconnected

    # ------------------------------------------------------------------
    # ONVIF events (simple GET endpoint, no subscription needed)
    # ------------------------------------------------------------------

    def _handle_events(self) -> None:
        ts   = datetime.utcnow().isoformat() + 'Z'
        body = (
            f'<?xml version="1.0" encoding="UTF-8"?>\n'
            f'<SOAP-ENV:Envelope'
            f' xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"'
            f' xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"'
            f' xmlns:tt="http://www.onvif.org/ver10/schema">\n'
            f'  <SOAP-ENV:Body>\n'
            f'    <wsnt:NotificationMessage>\n'
            f'      <wsnt:Topic>tns1:VideoSource/MotionAlarm</wsnt:Topic>\n'
            f'      <wsnt:Message>\n'
            f'        <tt:Message UtcTime="{ts}">\n'
            f'          <tt:Data>\n'
            f'            <tt:SimpleItem Name="State"'
            f' Value="{str(_motion_active).lower()}"/>\n'
            f'          </tt:Data>\n'
            f'        </tt:Message>\n'
            f'      </wsnt:Message>\n'
            f'    </wsnt:NotificationMessage>\n'
            f'  </SOAP-ENV:Body>\n'
            f'</SOAP-ENV:Envelope>'
        ).encode()
        self._write_response(200, 'application/xml', body)

    # ------------------------------------------------------------------
    # ONVIF SOAP helpers
    # ------------------------------------------------------------------

    def _write_response(self, status: int, ctype: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _soap_ok(self, xml: str) -> None:
        self._write_response(200, 'application/soap+xml', xml.encode())

    def _soap_events(self, body: str) -> None:
        """ONVIF Events service – PullPoint subscription for motion alarms."""
        ip  = _get_ip()
        now = datetime.utcnow()
        now_s   = now.isoformat(timespec='seconds') + 'Z'
        term_s  = (now + timedelta(hours=1)).isoformat(timespec='seconds') + 'Z'
        sub_url = f'http://{ip}:{PORT}/onvif/events_service'

        if 'GetEventProperties' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                   xmlns:tt="http://www.onvif.org/ver10/schema"
                   xmlns:tns1="http://www.onvif.org/ver10/topics">
  <SOAP-ENV:Body>
    <tev:GetEventPropertiesResponse>
      <tev:TopicNamespaceLocation>http://www.onvif.org/onvif/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
      <wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>
      <wstop:TopicSet xmlns:wstop="http://docs.oasis-open.org/wsn/t-1">
        <tns1:VideoSource>
          <tns1:MotionAlarm wstop:topic="true">
            <tt:MessageDescription IsProperty="true">
              <tt:Source>
                <tt:SimpleItemDescription Name="VideoSourceConfigurationToken" Type="tt:ReferenceToken"/>
              </tt:Source>
              <tt:Data>
                <tt:SimpleItemDescription Name="IsMotion" Type="xsd:boolean"/>
              </tt:Data>
            </tt:MessageDescription>
          </tns1:MotionAlarm>
        </tns1:VideoSource>
      </wstop:TopicSet>
      <tev:MessageContentFilterDialectSupport>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialectSupport>
    </tev:GetEventPropertiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'CreatePullPointSubscription' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                   xmlns:wsa="http://www.w3.org/2005/08/addressing">
  <SOAP-ENV:Body>
    <tev:CreatePullPointSubscriptionResponse>
      <tev:SubscriptionReference>
        <wsa:Address>{sub_url}</wsa:Address>
      </tev:SubscriptionReference>
      <wsnt:CurrentTime>{now_s}</wsnt:CurrentTime>
      <wsnt:TerminationTime>{term_s}</wsnt:TerminationTime>
    </tev:CreatePullPointSubscriptionResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'PullMessages' in body:
            with _pullpoint_lock:
                events = list(_pullpoint_events)
                _pullpoint_events.clear()

            notifications = ''
            for ts, is_motion in events:
                notifications += f'''      <wsnt:NotificationMessage>
        <wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet"
          >tns1:VideoSource/MotionAlarm</wsnt:Topic>
        <wsnt:Message>
          <tt:Message xmlns:tt="http://www.onvif.org/ver10/schema"
                      UtcTime="{ts}" PropertyOperation="Changed">
            <tt:Source>
              <tt:SimpleItem Name="VideoSourceConfigurationToken" Value="VideoSource0"/>
            </tt:Source>
            <tt:Data>
              <tt:SimpleItem Name="IsMotion" Value="{str(is_motion).lower()}"/>
            </tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>\n'''

            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <SOAP-ENV:Body>
    <tev:PullMessagesResponse>
      <tev:CurrentTime>{now_s}</tev:CurrentTime>
      <tev:TerminationTime>{term_s}</tev:TerminationTime>
{notifications}    </tev:PullMessagesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'Renew' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <SOAP-ENV:Body>
    <wsnt:RenewResponse>
      <wsnt:TerminationTime>{term_s}</wsnt:TerminationTime>
      <wsnt:CurrentTime>{now_s}</wsnt:CurrentTime>
    </wsnt:RenewResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'Unsubscribe' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <SOAP-ENV:Body><wsnt:UnsubscribeResponse/></SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        else:
            self._soap_fault("Unsupported events action")

    def _soap_fault(self, reason: str) -> None:
        self._write_response(500, 'application/soap+xml', (
            f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">'
            f'<SOAP-ENV:Body><SOAP-ENV:Fault>'
            f'<SOAP-ENV:Code><SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value></SOAP-ENV:Code>'
            f'<SOAP-ENV:Reason><SOAP-ENV:Text xml:lang="en">{reason}</SOAP-ENV:Text></SOAP-ENV:Reason>'
            f'</SOAP-ENV:Fault></SOAP-ENV:Body></SOAP-ENV:Envelope>'
        ).encode())

    def _soap_device(self, body: str) -> None:
        ip = _get_ip()

        if 'GetDeviceInformation' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <SOAP-ENV:Body>
    <tds:GetDeviceInformationResponse>
      <tds:Manufacturer>Meridian Innovation</tds:Manufacturer>
      <tds:Model>MI48</tds:Model>
      <tds:FirmwareVersion>1.0</tds:FirmwareVersion>
      <tds:SerialNumber>RPi-MI48-001</tds:SerialNumber>
      <tds:HardwareId>RPi-Thermal</tds:HardwareId>
    </tds:GetDeviceInformationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetCapabilities' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetCapabilitiesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <Capabilities>
        <tt:Device>
          <tt:XAddr>http://{ip}:{PORT}/onvif/device_service</tt:XAddr>
          <tt:Network>
            <tt:IPFilter>false</tt:IPFilter>
            <tt:ZeroConfiguration>false</tt:ZeroConfiguration>
            <tt:IPVersion6>false</tt:IPVersion6>
            <tt:DynDNS>false</tt:DynDNS>
          </tt:Network>
          <tt:System>
            <tt:DiscoveryResolve>false</tt:DiscoveryResolve>
            <tt:DiscoveryBye>false</tt:DiscoveryBye>
            <tt:RemoteDiscovery>false</tt:RemoteDiscovery>
            <tt:SystemBackup>false</tt:SystemBackup>
            <tt:SystemLogging>false</tt:SystemLogging>
            <tt:FirmwareUpgrade>false</tt:FirmwareUpgrade>
          </tt:System>
        </tt:Device>
        <tt:Events>
          <tt:XAddr>http://{ip}:{PORT}/onvif/events_service</tt:XAddr>
          <tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>
          <tt:WSPullPointSupport>true</tt:WSPullPointSupport>
        </tt:Events>
        <tt:Media>
          <tt:XAddr>http://{ip}:{PORT}/onvif/media_service</tt:XAddr>
          <tt:StreamingCapabilities>
            <tt:RTPMulticast>false</tt:RTPMulticast>
            <tt:RTP_TCP>true</tt:RTP_TCP>
            <tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>
          </tt:StreamingCapabilities>
        </tt:Media>
      </Capabilities>
    </GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetSystemDateAndTime' in body:
            now = datetime.utcnow()
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <SystemDateAndTime>
        <tt:DateTimeType>NTP</tt:DateTimeType>
        <tt:DaylightSavings>false</tt:DaylightSavings>
        <tt:UTCDateTime>
          <tt:Time>
            <tt:Hour>{now.hour}</tt:Hour>
            <tt:Minute>{now.minute}</tt:Minute>
            <tt:Second>{now.second}</tt:Second>
          </tt:Time>
          <tt:Date>
            <tt:Year>{now.year}</tt:Year>
            <tt:Month>{now.month}</tt:Month>
            <tt:Day>{now.day}</tt:Day>
          </tt:Date>
        </tt:UTCDateTime>
      </SystemDateAndTime>
    </GetSystemDateAndTimeResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetScopes' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetScopesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/video_encoder</tt:ScopeItem></Scopes>
      <Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/type/thermal</tt:ScopeItem></Scopes>
      <Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/hardware/MI48</tt:ScopeItem></Scopes>
      <Scopes><tt:ScopeDef>Fixed</tt:ScopeDef><tt:ScopeItem>onvif://www.onvif.org/name/ThermalCamera</tt:ScopeItem></Scopes>
    </GetScopesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetServices' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <tds:GetServicesResponse>
      <tds:Service>
        <tds:Namespace>http://www.onvif.org/ver10/device/wsdl</tds:Namespace>
        <tds:XAddr>http://{ip}:{PORT}/onvif/device_service</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>http://www.onvif.org/ver10/media/wsdl</tds:Namespace>
        <tds:XAddr>http://{ip}:{PORT}/onvif/media_service</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
      <tds:Service>
        <tds:Namespace>http://www.onvif.org/ver10/events/wsdl</tds:Namespace>
        <tds:XAddr>http://{ip}:{PORT}/onvif/events_service</tds:XAddr>
        <tds:Version><tt:Major>2</tt:Major><tt:Minor>0</tt:Minor></tds:Version>
      </tds:Service>
    </tds:GetServicesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetHostname' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetHostnameResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <HostnameInformation>
        <tt:FromDHCP>true</tt:FromDHCP>
        <tt:Name>{socket.gethostname()}</tt:Name>
      </HostnameInformation>
    </GetHostnameResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetNetworkInterfaces' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetNetworkInterfacesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <NetworkInterfaces token="eth0">
        <tt:Enabled>true</tt:Enabled>
        <tt:IPv4>
          <tt:Enabled>true</tt:Enabled>
          <tt:Config>
            <tt:DHCP>true</tt:DHCP>
            <tt:Manual>
              <tt:Address>{ip}</tt:Address>
              <tt:PrefixLength>24</tt:PrefixLength>
            </tt:Manual>
          </tt:Config>
        </tt:IPv4>
      </NetworkInterfaces>
    </GetNetworkInterfacesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetNTP' in body:
            self._soap_ok('''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetNTPResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <NTPInformation>
        <tt:FromDHCP>true</tt:FromDHCP>
      </NTPInformation>
    </GetNTPResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetDNS' in body:
            self._soap_ok('''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetDNSResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <DNSInformation>
        <tt:FromDHCP>true</tt:FromDHCP>
      </DNSInformation>
    </GetDNSResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetNetworkProtocols' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Body>
    <GetNetworkProtocolsResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <NetworkProtocols>
        <tt:Name>HTTP</tt:Name>
        <tt:Enabled>true</tt:Enabled>
        <tt:Port>{PORT}</tt:Port>
      </NetworkProtocols>
      <NetworkProtocols>
        <tt:Name>RTSP</tt:Name>
        <tt:Enabled>true</tt:Enabled>
        <tt:Port>554</tt:Port>
      </NetworkProtocols>
    </GetNetworkProtocolsResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetRelayOutputs' in body:
            # No relay outputs – return empty list
            self._soap_ok('''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
  <SOAP-ENV:Body>
    <GetRelayOutputsResponse xmlns="http://www.onvif.org/ver10/device/wsdl"/>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'SetNTP' in body or 'SetDNS' in body or 'SetNetworkProtocols' in body \
                or 'SetHostname' in body or 'SetNetworkInterfaces' in body:
            # Accept all network config writes as no-op
            m = re.search(r'<(?:[^:>\s]+:)?Body[^>]*>\s*<(?:[^:>\s]+:)?(\w+)', body)
            tag = (m.group(1) if m else 'Set') + 'Response'
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">'
                          f'<SOAP-ENV:Body><{tag} xmlns="http://www.onvif.org/ver10/device/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        else:
            action = re.search(r'<(?:[^:>\s]+:)?Body[^>]*>\s*<(?:[^:>\s]+:)?(\w+)', body)
            log.warning("SOAP device: unhandled action %s from %s",
                        action.group(1) if action else '?', self.client_address[0])
            self._soap_fault("Unsupported device action")

    def _soap_media(self, body: str) -> None:
        ip  = _get_ip()
        w   = STREAM_RES[0] + COLORBAR_W   # actual output width including colorbar
        h   = STREAM_RES[1]

        ns = 'xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" xmlns:tt="http://www.onvif.org/ver10/schema"'

        # Inner content of a VideoSourceConfiguration (used in both Profile and standalone calls)
        vsc_inner = f'''<tt:Name>VideoSource</tt:Name>
          <tt:UseCount>0</tt:UseCount>
          <tt:SourceToken>VideoSource0</tt:SourceToken>
          <tt:Bounds height="{h}" width="{w}" y="0" x="0"/>'''

        # Inner content of a VideoEncoderConfiguration.
        # H.264 – RTSP stream is transcoded to H.264 via RPi hardware encoder (h264_v4l2m2m).
        vec_inner = f'''<tt:Name>VideoEncoder</tt:Name>
          <tt:UseCount>0</tt:UseCount>
          <tt:Encoding>H264</tt:Encoding>
          <tt:Resolution>
            <tt:Width>{w}</tt:Width>
            <tt:Height>{h}</tt:Height>
          </tt:Resolution>
          <tt:Quality>70</tt:Quality>
          <tt:RateControl>
            <tt:FrameRateLimit>{FRAME_RATE}</tt:FrameRateLimit>
            <tt:EncodingInterval>1</tt:EncodingInterval>
            <tt:BitrateLimit>1500</tt:BitrateLimit>
          </tt:RateControl>
          <tt:H264>
            <tt:GovLength>25</tt:GovLength>
            <tt:H264Profile>Baseline</tt:H264Profile>
          </tt:H264>
          <tt:Multicast>
            <tt:Address><tt:Type>IPv4</tt:Type><tt:IPv4Address>0.0.0.0</tt:IPv4Address></tt:Address>
            <tt:Port>0</tt:Port><tt:TTL>0</tt:TTL><tt:AutoStart>false</tt:AutoStart>
          </tt:Multicast>
          <tt:SessionTimeout>PT60S</tt:SessionTimeout>'''

        # GetProfiles / GetProfile
        # Inside tt:Profile, child elements are tt:VideoSourceConfiguration / tt:VideoEncoderConfiguration
        # fixed="true" omitted – some NVRs refuse to use fixed profiles and loop trying to create new ones
        if 'GetProfile' in body:
            tag = 'GetProfilesResponse' if 'GetProfiles' in body else 'GetProfileResponse'
            # Profile1 is our built-in configured profile.
            # _created_profiles holds profiles created via CreateProfile (e.g. "SynoProfile").
            # They are returned as fully-configured profiles so the NVR can use them for GetStreamUri.
            profile1_xml = f'''      <Profiles token="Profile1">
        <tt:Name>ThermalProfile</tt:Name>
        <tt:VideoSourceConfiguration token="VSConfig">{vsc_inner}</tt:VideoSourceConfiguration>
        <tt:VideoEncoderConfiguration token="VEConfig">{vec_inner}</tt:VideoEncoderConfiguration>
      </Profiles>'''
            extra_profiles_xml = ''.join(
                f'''      <Profiles token="{tok}">
        <tt:Name>{name}</tt:Name>
        <tt:VideoSourceConfiguration token="VSConfig">{vsc_inner}</tt:VideoSourceConfiguration>
        <tt:VideoEncoderConfiguration token="VEConfig">{vec_inner}</tt:VideoEncoderConfiguration>
      </Profiles>'''
                for tok, name in _created_profiles.items()
            )
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <{tag} xmlns="http://www.onvif.org/ver10/media/wsdl">
{profile1_xml}
{extra_profiles_xml}
    </{tag}>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetVideoSources' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoSourcesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <VideoSources token="VideoSource0">
        <tt:Framerate>{FRAME_RATE}</tt:Framerate>
        <tt:Resolution><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:Resolution>
      </VideoSources>
    </GetVideoSourcesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetVideoSourceConfiguration' in body:
            # List: child element = Configurations (ONVIF WSDL name for GetVideoSourceConfigurationsResponse)
            # Single: child element = VideoSourceConfiguration
            if 'GetVideoSourceConfigurations' in body:
                self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoSourceConfigurationsResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Configurations token="VSConfig">{vsc_inner}</Configurations>
    </GetVideoSourceConfigurationsResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')
            else:
                self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoSourceConfigurationResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <VideoSourceConfiguration token="VSConfig">{vsc_inner}</VideoSourceConfiguration>
    </GetVideoSourceConfigurationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetVideoEncoderConfiguration' in body:
            # List: child element = Configurations
            # Single: child element = VideoEncoderConfiguration
            if 'GetVideoEncoderConfigurations' in body:
                self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoEncoderConfigurationsResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Configurations token="VEConfig">{vec_inner}</Configurations>
    </GetVideoEncoderConfigurationsResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')
            else:
                # Extract the requested token.  An empty token (NVR polling for a "free" encoder
                # config to add to a profile) should return NoEntity so the NVR will call
                # AddVideoEncoderConfiguration instead of looping.
                _vec_tok_m = re.search(r'<ConfigurationToken[^>]*>([^<]*)</ConfigurationToken>', body)
                _vec_tok = (_vec_tok_m.group(1).strip() if _vec_tok_m else '')
                if not _vec_tok:
                    # Empty token – signal that no free VEConfig is available
                    self._soap_fault("NoEntity")
                else:
                    self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoEncoderConfigurationResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <VideoEncoderConfiguration token="VEConfig">{vec_inner}</VideoEncoderConfiguration>
    </GetVideoEncoderConfigurationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetStreamUri' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetStreamUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <MediaUri>
        <tt:Uri>rtsp://{ip}/thermal</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </MediaUri>
    </GetStreamUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetSnapshotUri' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetSnapshotUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <MediaUri>
        <tt:Uri>http://{ip}:{PORT}/snapshot</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </MediaUri>
    </GetSnapshotUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetVideoSourceConfigurationOptions' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoSourceConfigurationOptionsResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Options>
        <tt:BoundsRange>
          <tt:XRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:XRange>
          <tt:YRange><tt:Min>0</tt:Min><tt:Max>0</tt:Max></tt:YRange>
          <tt:WidthRange><tt:Min>{w}</tt:Min><tt:Max>{w}</tt:Max></tt:WidthRange>
          <tt:HeightRange><tt:Min>{h}</tt:Min><tt:Max>{h}</tt:Max></tt:HeightRange>
        </tt:BoundsRange>
        <tt:VideoSourceTokensAvailable>VideoSource0</tt:VideoSourceTokensAvailable>
      </Options>
    </GetVideoSourceConfigurationOptionsResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetVideoEncoderConfigurationOptions' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetVideoEncoderConfigurationOptionsResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Options>
        <tt:QualityRange><tt:Min>0</tt:Min><tt:Max>100</tt:Max></tt:QualityRange>
        <tt:H264>
          <tt:ResolutionsAvailable><tt:Width>{w}</tt:Width><tt:Height>{h}</tt:Height></tt:ResolutionsAvailable>
          <tt:GovLengthRange><tt:Min>1</tt:Min><tt:Max>100</tt:Max></tt:GovLengthRange>
          <tt:FrameRateRange><tt:Min>1</tt:Min><tt:Max>{FRAME_RATE}</tt:Max></tt:FrameRateRange>
          <tt:EncodingIntervalRange><tt:Min>1</tt:Min><tt:Max>1</tt:Max></tt:EncodingIntervalRange>
          <tt:H264ProfilesSupported>Baseline</tt:H264ProfilesSupported>
        </tt:H264>
      </Options>
    </GetVideoEncoderConfigurationOptionsResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetGuaranteedNumberOfVideoEncoderInstances' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetGuaranteedNumberOfVideoEncoderInstancesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <TotalNumber>1</TotalNumber>
      <JPEG>0</JPEG>
      <H264>1</H264>
    </GetGuaranteedNumberOfVideoEncoderInstancesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'GetAudioSources' in body or 'GetAudioEncoderConfiguration' in body \
                or 'GetAudioEncoderConfigurationOptions' in body or 'GetAudioOutputs' in body:
            # No audio – return empty response for all audio-related calls
            m = re.search(r'<(?:[^:>\s]+:)?Body[^>]*>\s*<(?:[^:>\s]+:)?(\w+)', body)
            tag = (m.group(1) if m else 'GetAudioSources') + 'Response'
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope {ns}><SOAP-ENV:Body>'
                          f'<{tag} xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        elif 'GetServiceCapabilities' in body:
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <GetServiceCapabilitiesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Capabilities SnapshotUri="true" Rotation="false" VideoSourceMode="false" OSD="false">
        <ProfileCapabilities MaximumNumberOfProfiles="10"/>
        <StreamingCapabilities RTPMulticast="false" RTP_TCP="true" RTP_RTSP_TCP="true" NonAggregateControl="false"/>
      </Capabilities>
    </GetServiceCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'CreateProfile' in body:
            # Return an empty profile whose token matches the requested Name.
            # Synology (and other NVRs) create their own profiles (e.g. "SynoProfile") and then
            # add VSConfig/VEConfig to them via AddVideo*Configuration. Returning our own
            # pre-configured Profile1 here confuses Synology because it sees VEConfig already
            # assigned (UseCount>0) and can't proceed with its setup flow.
            # Any token that appears in GetStreamUri will still return our RTSP URL.
            _profile_name_m = re.search(r'<Name[^>]*>([^<]+)</Name>', body)
            _profile_name = _profile_name_m.group(1).strip() if _profile_name_m else 'NewProfile'
            _base_tok  = re.sub(r'[^A-Za-z0-9_\-]', '', _profile_name) or 'NewProfile'
            # Generate a unique token: SynoProfile, SynoProfile1, SynoProfile2, …
            _profile_tok = _base_tok
            _counter = 0
            while _profile_tok in _created_profiles or _profile_tok == 'Profile1':
                _counter += 1
                _profile_tok = f'{_base_tok}{_counter}'
            # Register in _created_profiles so GetProfiles includes this profile as fully configured.
            # Returning it with VSConfig+VEConfig already inside means the NVR sees it as a
            # ready-to-use streaming profile and can proceed directly to GetStreamUri.
            _created_profiles[_profile_tok] = _profile_name
            log.info("CreateProfile: name=%s token=%s", _profile_name, _profile_tok)
            self._soap_ok(f'''<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope {ns}>
  <SOAP-ENV:Body>
    <CreateProfileResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Profile token="{_profile_tok}">
        <tt:Name>{_profile_name}</tt:Name>
        <tt:VideoSourceConfiguration token="VSConfig">{vsc_inner}</tt:VideoSourceConfiguration>
        <tt:VideoEncoderConfiguration token="VEConfig">{vec_inner}</tt:VideoEncoderConfiguration>
      </Profile>
    </CreateProfileResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>''')

        elif 'AddVideoSourceConfiguration' in body or 'RemoveVideoSourceConfiguration' in body:
            # Fixed config – accept silently (no-op)
            tag = 'AddVideoSourceConfigurationResponse' if 'Add' in body else 'RemoveVideoSourceConfigurationResponse'
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope {ns}><SOAP-ENV:Body>'
                          f'<{tag} xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        elif 'AddVideoEncoderConfiguration' in body or 'RemoveVideoEncoderConfiguration' in body:
            # Fixed config – accept silently (no-op)
            tag = 'AddVideoEncoderConfigurationResponse' if 'Add' in body else 'RemoveVideoEncoderConfigurationResponse'
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope {ns}><SOAP-ENV:Body>'
                          f'<{tag} xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        elif 'SetVideoEncoderConfiguration' in body or 'SetVideoSourceConfiguration' in body:
            # Accept config changes silently – our pipeline is not reconfigurable at runtime
            tag = ('SetVideoEncoderConfigurationResponse' if 'Encoder' in body
                   else 'SetVideoSourceConfigurationResponse')
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope {ns}><SOAP-ENV:Body>'
                          f'<{tag} xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        elif 'DeleteProfile' in body:
            # Remove from created profiles if present; built-in Profile1 is never actually deleted.
            _dp_tok_m = re.search(r'<ProfileToken[^>]*>([^<]+)</ProfileToken>', body)
            if _dp_tok_m:
                _dp_tok = _dp_tok_m.group(1).strip()
                _created_profiles.pop(_dp_tok, None)
            self._soap_ok(f'<?xml version="1.0" encoding="UTF-8"?>'
                          f'<SOAP-ENV:Envelope {ns}><SOAP-ENV:Body>'
                          f'<DeleteProfileResponse xmlns="http://www.onvif.org/ver10/media/wsdl"/>'
                          f'</SOAP-ENV:Body></SOAP-ENV:Envelope>')

        else:
            action = re.search(r'<(?:[^:>\s]+:)?Body[^>]*>\s*<(?:[^:>\s]+:)?(\w+)', body)
            log.warning("SOAP media: unhandled action %s from %s",
                        action.group(1) if action else '?', self.client_address[0])
            self._soap_fault("Unsupported media action")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
def main() -> None:
    _load_auth()

    cam_thread = threading.Thread(target=_camera_loop, name='camera', daemon=True)
    cam_thread.start()

    # allow_reuse_address + TCP_NODELAY must be class attributes (set before bind())
    class _Server(socketserver.ThreadingTCPServer):
        allow_reuse_address = True

        def get_request(self):
            conn, addr = super().get_request()
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            return conn, addr

    server = _Server(('', PORT), _Handler)

    def _on_signal(sig, _frame) -> None:
        log.info("Signal %d received – shutting down.", sig)
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGINT,  _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    ip = _get_ip()
    log.info("=" * 60)
    log.info("ONVIF Thermal Camera Server")
    log.info("  Stream:   http://%s:%d/stream", ip, PORT)
    log.info("  Snapshot: http://%s:%d/snapshot", ip, PORT)
    log.info("  ONVIF:    http://%s:%d/onvif/device_service", ip, PORT)
    log.info("=" * 60)

    try:
        server.serve_forever()
    finally:
        server.server_close()
        log.info("Server stopped.")


if __name__ == '__main__':
    main()
