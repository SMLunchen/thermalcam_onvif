# Konfigurationsdatei für die ONVIF-Thermal-Kamera

# Server-Konfiguration
SERVER_HOST = "0.0.0.0"  # Auf allen Interfaces lauschen
SERVER_PORT = 8000
RTSP_PORT = 8554
ONVIF_PORT = 8080

# Kamera-Konfiguration
CAMERA_WIDTH = 32
CAMERA_HEIGHT = 24
CAMERA_FPS = 10
INTERPOLATION_FACTOR = 10  # Vergrößert das Bild für bessere Darstellung

# Bewegungserkennung
MOTION_THRESHOLD = 2.0  # Temperaturänderung in Grad Celsius für Bewegungserkennung
MOTION_COOLDOWN = 5  # Sekunden zwischen Bewegungsereignissen

# Authentifizierung
AUTH_FILE = "users.json"
