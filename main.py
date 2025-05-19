import time
import signal
import sys
from thermal_camera import ThermalCamera
from onvif_server import ONVIFServer
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import threading
from auth import Authentication
from config import SERVER_HOST, SERVER_PORT

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    pass

class StreamHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.auth = Authentication()
        self.camera = None
        super().__init__(*args, **kwargs)
    
    def set_camera(self, camera):
        self.camera = camera
    
    def do_GET(self):
        # Überprüfe Authentifizierung
        if not self.auth.verify_credentials(self.headers.get('Authorization')):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Thermal Camera"')
            self.end_headers()
            return
        
        if self.path == '/snapshot':
            self.send_response(200)
            self.send_header('Content-Type', 'image/jpeg')
            self.end_headers()
            self.wfile.write(self.camera.get_jpeg())
        elif self.path == '/stream':
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.end_headers()
            try:
                while True:
                    jpeg = self.camera.get_jpeg()
                    self.wfile.write(b'--frame\r\n')
                    self.wfile.write(b'Content-Type: image/jpeg\r\n')
                    self.wfile.write(f'Content-Length: {len(jpeg)}\r\n\r\n'.encode())
                    self.wfile.write(jpeg)
                    self.wfile.write(b'\r\n')
                    time.sleep(1/self.camera.fps)
            except Exception as e:
                print(f"Stream unterbrochen: {e}")
        elif self.path == '/status':
            status = {
                "running": True,
                "motion_detected": self.camera.is_motion_detected(),
                "temperature_min": float(self.camera.raw_data.min()) if self.camera.raw_data is not None else 0,
                "temperature_max": float(self.camera.raw_data.max()) if self.camera.raw_data is not None else 0,
                "temperature_avg": float(self.camera.raw_data.mean()) if self.camera.raw_data is not None else 0
            }
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(str(status).encode())
        else:
            self.send_error(404)

class StreamServer:
    def __init__(self, camera):
        self.camera = camera
        self.server = None
        self.running = False
    
    def start(self):
        self.running = True
        server_address = (SERVER_HOST, SERVER_PORT)
        self.server = ThreadedHTTPServer(server_address, StreamHandler)
        
        # Setze die Kamera für den Handler
        self.server.RequestHandlerClass.camera = self.camera
        
        print(f"Stream-Server gestartet auf {SERVER_HOST}:{SERVER_PORT}")
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
    
    def stop(self):
        if self.server:
            self.running = False
            self.server.shutdown()
            self.server.server_close()
            print("Stream-Server gestoppt")

def signal_handler(sig, frame):
    print('Programm wird beendet...')
    if 'stream_server' in globals() and stream_server is not None:
        stream_server.stop()
    if 'onvif_server' in globals() and onvif_server is not None:
        onvif_server.stop()
    if 'camera' in globals() and camera is not None:
        camera.stop()
    sys.exit(0)

if __name__ == "__main__":
    # Signal-Handler für sauberes Beenden
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        # Initialisiere die Kamera
        camera = ThermalCamera()
        camera.start()
        print("Thermal Camera gestartet")
        
        # Starte den Stream-Server
        stream_server = StreamServer(camera)
        stream_server.start()
        
        # Starte den ONVIF-Server
        onvif_server = ONVIFServer(camera)
        onvif_server.start()
        
        # Halte das Programm am Laufen
        print("Server laufen. Drücke STRG+C zum Beenden.")
        while True:
            time.sleep(1)
    
    except Exception as e:
        print(f"Fehler: {e}")
        if 'stream_server' in locals() and stream_server is not None:
            stream_server.stop()
        if 'onvif_server' in locals() and onvif_server is not None:
            onvif_server.stop()
        if 'camera' in locals() and camera is not None:
            camera.stop()
