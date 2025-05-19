import json
import base64
from config import AUTH_FILE

class Authentication:
    def __init__(self):
        self.users = {}
        self.load_users()
    
    def load_users(self):
        try:
            with open(AUTH_FILE, 'r') as f:
                self.users = json.load(f)
            print(f"Benutzer aus {AUTH_FILE} geladen")
        except FileNotFoundError:
            print(f"Warnung: {AUTH_FILE} nicht gefunden. Erstelle Standard-Benutzer.")
            self.users = {"admin": "admin123"}
            self.save_users()
    
    def save_users(self):
        with open(AUTH_FILE, 'w') as f:
            json.dump(self.users, f, indent=2)
    
    def verify_credentials(self, auth_header):
        if not auth_header:
            return False
        
        try:
            auth_type, auth_info = auth_header.split(' ', 1)
            if auth_type.lower() != 'basic':
                return False
            
            auth_decoded = base64.b64decode(auth_info).decode('utf-8')
            username, password = auth_decoded.split(':', 1)
            
            return username in self.users and self.users[username] == password
        except Exception as e:
            print(f"Authentifizierungsfehler: {e}")
            return False
    
    def add_user(self, username, password):
        self.users[username] = password
        self.save_users()
    
    def remove_user(self, username):
        if username in self.users:
            del self.users[username]
            self.save_users()
