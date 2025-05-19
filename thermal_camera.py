import time
import numpy as np
import cv2
from threading import Thread, Lock
from config import CAMERA_WIDTH, CAMERA_HEIGHT, CAMERA_FPS, INTERPOLATION_FACTOR, MOTION_THRESHOLD

try:
    import board
    import busio
    import adafruit_mlx90640
except ImportError:
    print("Warnung: Adafruit MLX90640 Bibliothek nicht gefunden. Verwende Dummy-Kamera.")
    USE_DUMMY = True
else:
    USE_DUMMY = False

class ThermalCamera:
    def __init__(self):
        self.width = CAMERA_WIDTH
        self.height = CAMERA_HEIGHT
        self.fps = CAMERA_FPS
        self.interpolation_factor = INTERPOLATION_FACTOR
        self.frame = None
        self.raw_data = None
        self.previous_raw_data = None
        self.lock = Lock()
        self.running = False
        self.motion_detected = False
        self.motion_threshold = MOTION_THRESHOLD
        
        if not USE_DUMMY:
            # Initialisiere den I2C-Bus und den MLX90640-Sensor
            self.i2c = busio.I2C(board.SCL, board.SDA, frequency=400000)
            self.mlx = adafruit_mlx90640.MLX90640(self.i2c)
            self.mlx.refresh_rate = adafruit_mlx90640.RefreshRate.REFRESH_4_HZ
        
    def start(self):
        self.running = True
        self.thread = Thread(target=self._update_frame)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self):
        self.running = False
        if hasattr(self, 'thread'):
            self.thread.join()
    
    def _update_frame(self):
        while self.running:
            try:
                if USE_DUMMY:
                    # Erzeuge Dummy-Daten für Tests
                    self.raw_data = np.random.uniform(20.0, 30.0, size=(self.height, self.width))
                    # Füge einen wärmeren Bereich hinzu, der sich bewegt
                    x = int((time.time() % 5) * (self.width / 5))
                    y = int((time.time() % 5) * (self.height / 5))
                    self.raw_data[max(0, y-2):min(self.height, y+2), max(0, x-2):min(self.width, x+2)] += 5.0
                else:
                    # Lese Daten vom MLX90640-Sensor
                    frame = [0] * 768
                    self.mlx.getFrame(frame)
                    self.raw_data = np.array(frame).reshape(24, 32)
                
                # Bewegungserkennung
                if self.previous_raw_data is not None:
                    diff = np.abs(self.raw_data - self.previous_raw_data)
                    if np.max(diff) > self.motion_threshold:
                        self.motion_detected = True
                    else:
                        self.motion_detected = False
                
                self.previous_raw_data = self.raw_data.copy()
                
                # Normalisiere die Daten für die Visualisierung
                normalized = self._normalize_frame(self.raw_data)
                
                # Interpoliere das Bild auf eine größere Größe
                resized = cv2.resize(normalized, 
                                    (self.width * self.interpolation_factor, 
                                     self.height * self.interpolation_factor), 
                                    interpolation=cv2.INTER_CUBIC)
                
                # Erzeuge ein Farbbild mit Heatmap
                colored = cv2.applyColorMap(resized, cv2.COLORMAP_JET)
                
                with self.lock:
                    self.frame = colored
                
                time.sleep(1.0 / self.fps)
            
            except Exception as e:
                print(f"Fehler beim Aktualisieren des Frames: {e}")
                time.sleep(1.0)
    
    def _normalize_frame(self, frame):
        """Normalisiert die Temperaturdaten auf 0-255 für die Bilddarstellung"""
        min_temp = np.min(frame)
        max_temp = np.max(frame)
        
        if max_temp == min_temp:
            normalized = np.zeros_like(frame, dtype=np.uint8)
        else:
            normalized = ((frame - min_temp) / (max_temp - min_temp) * 255).astype(np.uint8)
        
        return normalized
    
    def get_frame(self):
        """Gibt das aktuelle Frame zurück"""
        with self.lock:
            if self.frame is None:
                # Erzeuge ein leeres Bild, wenn noch kein Frame vorhanden ist
                empty = np.zeros((self.height * self.interpolation_factor, 
                                  self.width * self.interpolation_factor, 3), 
                                 dtype=np.uint8)
                return empty
            return self.frame.copy()
    
    def get_jpeg(self):
        """Gibt das aktuelle Frame als JPEG-Daten zurück"""
        frame = self.get_frame()
        _, jpeg = cv2.imencode('.jpg', frame)
        return jpeg.tobytes()
    
    def is_motion_detected(self):
        """Gibt zurück, ob eine Bewegung erkannt wurde"""
        return self.motion_detected
