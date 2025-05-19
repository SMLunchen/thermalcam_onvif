import os
import time
import socket
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from socketserver import ThreadingMixIn
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
import uuid

from auth import Authentication
from thermal_camera import ThermalCamera
from config import SERVER_HOST, SERVER_PORT, ONVIF_PORT, RTSP_PORT, MOTION_COOLDOWN

# ONVIF Namespace-Definitionen
ONVIF_NAMESPACES = {
    'SOAP-ENV': 'http://www.w3.org/2003/05/soap-envelope',
    'wsa': 'http://www.w3.org/2005/08/addressing',
    'tds': 'http://www.onvif.org/ver10/device/wsdl',
    'trt': 'http://www.onvif.org/ver10/media/wsdl',
    'tev': 'http://www.onvif.org/ver10/events/wsdl',
    'timg': 'http://www.onvif.org/ver20/imaging/wsdl',
    'tt': 'http://www.onvif.org/ver10/schema',
    'wsnt': 'http://docs.oasis-open.org/wsn/b-2',
    'wstop': 'http://docs.oasis-open.org/wsn/t-1',
    'tns1': 'http://www.onvif.org/ver10/topics',
}

class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    """Handle requests in a separate thread."""
    pass

class ONVIFHandler(BaseHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        self.auth = Authentication()
        self.camera = None
        super().__init__(*args, **kwargs)
    
    def set_camera(self, camera):
        self.camera = camera
    
    def do_POST(self):
        # Überprüfe Authentifizierung
        if not self.auth.verify_credentials(self.headers.get('Authorization')):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="ONVIF Thermal Camera"')
            self.end_headers()
            return
        
        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length).decode('utf-8')
        
        try:
            # Parse XML request
            root = ET.fromstring(post_data)
            
            # Extrahiere den Aktionsnamen aus dem Body
            body = root.find('.//{http://www.w3.org/2003/05/soap-envelope}Body')
            if body is None:
                self.send_error(400, "Invalid SOAP request")
                return
            
            action = None
            for child in body:
                action = child.tag.split('}')[-1]
                break
            
            if action is None:
                self.send_error(400, "No action specified")
                return
            
            # Verarbeite die ONVIF-Anfrage
            response = self.process_onvif_request(action, root)
            
            self.send_response(200)
            self.send_header('Content-Type', 'application/soap+xml')
            self.end_headers()
            self.wfile.write(response.encode('utf-8'))
            
        except Exception as e:
            print(f"Fehler bei der Verarbeitung der ONVIF-Anfrage: {e}")
            self.send_error(500, str(e))
    
    def do_GET(self):
        # Überprüfe Authentifizierung
        if not self.auth.verify_credentials(self.headers.get('Authorization')):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="ONVIF Thermal Camera"')
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
        else:
            self.send_error(404)
    
    def process_onvif_request(self, action, root):
        """Verarbeitet ONVIF-Anfragen und generiert entsprechende Antworten"""
        
        # Device Management Service
        if action == 'GetDeviceInformation':
            return self.get_device_information()
        elif action == 'GetCapabilities':
            return self.get_capabilities()
        elif action == 'GetServiceCapabilities':
            return self.get_service_capabilities()
        elif action == 'GetSystemDateAndTime':
            return self.get_system_date_and_time()
        
        # Media Service
        elif action == 'GetProfiles':
            return self.get_profiles()
        elif action == 'GetStreamUri':
            return self.get_stream_uri()
        elif action == 'GetSnapshotUri':
            return self.get_snapshot_uri()
        
        # Events Service
        elif action == 'GetEventProperties':
            return self.get_event_properties()
        elif action == 'CreatePullPointSubscription':
            return self.create_pull_point_subscription()
        elif action == 'PullMessages':
            return self.pull_messages()
        
        # Fallback für nicht implementierte Aktionen
        else:
            return self.create_soap_fault(f"Action '{action}' not implemented")
    
    def get_device_information(self):
        """Gibt Geräteinformationen zurück"""
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <tds:GetDeviceInformationResponse>
      <tds:Manufacturer>Raspberry Pi</tds:Manufacturer>
      <tds:Model>Waveshare Thermal Camera</tds:Model>
      <tds:FirmwareVersion>1.0</tds:FirmwareVersion>
      <tds:SerialNumber>{hex(uuid.getnode())}</tds:SerialNumber>
      <tds:HardwareId>RPi-Thermal-ONVIF</tds:HardwareId>
    </tds:GetDeviceInformationResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_capabilities(self):
        """Gibt die Fähigkeiten des Geräts zurück"""
        server_ip = socket.gethostbyname(socket.gethostname())
        
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <GetCapabilitiesResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <Capabilities>
        <tt:Device>
          <tt:XAddr>http://{server_ip}:{ONVIF_PORT}/onvif/device_service</tt:XAddr>
        </tt:Device>
        <tt:Events>
          <tt:XAddr>http://{server_ip}:{ONVIF_PORT}/onvif/event_service</tt:XAddr>
          <tt:WSSubscriptionPolicySupport>false</tt:WSSubscriptionPolicySupport>
          <tt:WSPullPointSupport>true</tt:WSPullPointSupport>
          <tt:WSPausableSubscriptionManagerInterfaceSupport>false</tt:WSPausableSubscriptionManagerInterfaceSupport>
        </tt:Events>
        <tt:Media>
          <tt:XAddr>http://{server_ip}:{ONVIF_PORT}/onvif/media_service</tt:XAddr>
          <tt:StreamingCapabilities>
            <tt:RTPMulticast>false</tt:RTPMulticast>
            <tt:RTP_TCP>true</tt:RTP_TCP>
            <tt:RTP_RTSP_TCP>true</tt:RTP_RTSP_TCP>
          </tt:StreamingCapabilities>
        </tt:Media>
      </Capabilities>
    </GetCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_service_capabilities(self):
        """Gibt die Service-Fähigkeiten zurück"""
        soap_response = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tds="http://www.onvif.org/ver10/device/wsdl">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <tds:GetServiceCapabilitiesResponse>
      <tds:Capabilities>
        <tds:Network IPFilter="false" ZeroConfiguration="false" IPVersion6="false" DynDNS="false" DHCPv6="false"/>
        <tds:Security TLS1.0="false" TLS1.1="false" TLS1.2="false" OnboardKeyGeneration="false" AccessPolicyConfig="false" DefaultAccessPolicy="false" Dot1X="false" RemoteUserHandling="false" X.509Token="false" SAMLToken="false" KerberosToken="false" UsernameToken="true" HttpDigest="false" RELToken="false"/>
        <tds:System DiscoveryResolve="false" DiscoveryBye="false" RemoteDiscovery="false" SystemBackup="false" SystemLogging="false" FirmwareUpgrade="false" HttpFirmwareUpgrade="false" HttpSystemBackup="false" HttpSystemLogging="false" HttpSupportInformation="false"/>
      </tds:Capabilities>
    </tds:GetServiceCapabilitiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_system_date_and_time(self):
        """Gibt das aktuelle Systemdatum und die Uhrzeit zurück"""
        now = datetime.now()
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <GetSystemDateAndTimeResponse xmlns="http://www.onvif.org/ver10/device/wsdl">
      <SystemDateAndTime>
        <tt:DateTimeType>NTP</tt:DateTimeType>
        <tt:DaylightSavings>false</tt:DaylightSavings>
        <tt:TimeZone>
          <tt:TZ>UTC</tt:TZ>
        </tt:TimeZone>
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
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_profiles(self):
        """Gibt die verfügbaren Medienprofile zurück"""
        server_ip = socket.gethostbyname(socket.gethostname())
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <GetProfilesResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <Profiles fixed="true" token="ThermalProfile">
        <tt:Name>ThermalCameraProfile</tt:Name>
        <tt:VideoSourceConfiguration token="VideoSourceToken">
          <tt:Name>ThermalCameraSource</tt:Name>
          <tt:UseCount>1</tt:UseCount>
          <tt:SourceToken>VideoSource</tt:SourceToken>
          <tt:Bounds height="{self.camera.height * self.camera.interpolation_factor}" width="{self.camera.width * self.camera.interpolation_factor}" y="0" x="0"/>
        </tt:VideoSourceConfiguration>
        <tt:VideoEncoderConfiguration token="VideoEncoderToken">
          <tt:Name>ThermalCameraEncoder</tt:Name>
          <tt:UseCount>1</tt:UseCount>
          <tt:Encoding>JPEG</tt:Encoding>
          <tt:Resolution>
            <tt:Width>{self.camera.width * self.camera.interpolation_factor}</tt:Width>
            <tt:Height>{self.camera.height * self.camera.interpolation_factor}</tt:Height>
          </tt:Resolution>
          <tt:Quality>80</tt:Quality>
          <tt:RateControl>
            <tt:FrameRateLimit>{self.camera.fps}</tt:FrameRateLimit>
            <tt:EncodingInterval>1</tt:EncodingInterval>
            <tt:BitrateLimit>500</tt:BitrateLimit>
          </tt:RateControl>
        </tt:VideoEncoderConfiguration>
        <tt:PTZConfiguration token="PTZToken">
          <tt:Name>No PTZ</tt:Name>
          <tt:UseCount>1</tt:UseCount>
          <tt:NodeToken>PTZNodeToken</tt:NodeToken>
          <tt:DefaultAbsolutePantTiltPositionSpace>http://www.onvif.org/ver10/tptz/PanTiltSpaces/PositionGenericSpace</tt:DefaultAbsolutePantTiltPositionSpace>
          <tt:DefaultAbsoluteZoomPositionSpace>http://www.onvif.org/ver10/tptz/ZoomSpaces/PositionGenericSpace</tt:DefaultAbsoluteZoomPositionSpace>
          <tt:DefaultPTZSpeed>
            <tt:PanTilt x="0" y="0" space="http://www.onvif.org/ver10/tptz/PanTiltSpaces/GenericSpeedSpace"/>
            <tt:Zoom x="0" space="http://www.onvif.org/ver10/tptz/ZoomSpaces/ZoomGenericSpeedSpace"/>
          </tt:DefaultPTZSpeed>
          <tt:DefaultPTZTimeout>PT1S</tt:DefaultPTZTimeout>
        </tt:PTZConfiguration>
      </Profiles>
    </GetProfilesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_stream_uri(self):
        """Gibt die URI für den Videostream zurück"""
        server_ip = socket.gethostbyname(socket.gethostname())
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <GetStreamUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <MediaUri>
        <tt:Uri>http://{server_ip}:{SERVER_PORT}/stream</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </MediaUri>
    </GetStreamUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_snapshot_uri(self):
        """Gibt die URI für Schnappschüsse zurück"""
        server_ip = socket.gethostbyname(socket.gethostname())
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <GetSnapshotUriResponse xmlns="http://www.onvif.org/ver10/media/wsdl">
      <MediaUri>
        <tt:Uri>http://{server_ip}:{SERVER_PORT}/snapshot</tt:Uri>
        <tt:InvalidAfterConnect>false</tt:InvalidAfterConnect>
        <tt:InvalidAfterReboot>false</tt:InvalidAfterReboot>
        <tt:Timeout>PT60S</tt:Timeout>
      </MediaUri>
    </GetSnapshotUriResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def get_event_properties(self):
        """Gibt die Eigenschaften der unterstützten Ereignisse zurück"""
        soap_response = """<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                   xmlns:wstop="http://docs.oasis-open.org/wsn/t-1"
                   xmlns:tns1="http://www.onvif.org/ver10/topics"
                   xmlns:tt="http://www.onvif.org/ver10/schema">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <tev:GetEventPropertiesResponse>
      <tev:TopicNamespaceLocation>http://www.onvif.org/ver10/topics/topicns.xml</tev:TopicNamespaceLocation>
      <wsnt:FixedTopicSet>true</wsnt:FixedTopicSet>
      <wstop:TopicSet>
        <tns1:RuleEngine>
          <tns1:MotionDetector>
            <tns1:Motion wstop:topic="true">
              <tt:MessageDescription IsProperty="true">
                <tt:Source>
                  <tt:SimpleItemDescription Name="VideoSource" Type="tt:ReferenceToken"/>
                </tt:Source>
                <tt:Data>
                  <tt:SimpleItemDescription Name="State" Type="xs:boolean"/>
                </tt:Data>
              </tt:MessageDescription>
            </tns1:Motion>
          </tns1:MotionDetector>
        </tns1:RuleEngine>
      </wstop:TopicSet>
      <tev:MessageContentFilterDialect>http://www.onvif.org/ver10/tev/messageContentFilter/ItemFilter</tev:MessageContentFilterDialect>
      <tev:MessageContentSchemaLocation>http://www.onvif.org/ver10/schema/onvif.xsd</tev:MessageContentSchemaLocation>
    </tev:GetEventPropertiesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def create_pull_point_subscription(self):
        """Erstellt einen PullPoint-Subscription für Ereignisse"""
        subscription_id = str(uuid.uuid4())
        current_time = datetime.utcnow()
        termination_time = current_time + timedelta(minutes=30)
        
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <tev:CreatePullPointSubscriptionResponse>
      <tev:SubscriptionReference>
        <wsa:Address>http://www.example.org/subscription/{subscription_id}</wsa:Address>
      </tev:SubscriptionReference>
      <wsnt:CurrentTime>{current_time.isoformat()}</wsnt:CurrentTime>
      <wsnt:TerminationTime>{termination_time.isoformat()}</wsnt:TerminationTime>
    </tev:CreatePullPointSubscriptionResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def pull_messages(self):
        """Gibt Ereignisnachrichten zurück, wenn verfügbar"""
        current_time = datetime.utcnow()
        
        # Überprüfe, ob eine Bewegung erkannt wurde
        if self.camera and self.camera.is_motion_detected():
            motion_event = f"""<wsnt:NotificationMessage>
        <wsnt:Topic Dialect="http://www.onvif.org/ver10/tev/topicExpression/ConcreteSet">tns1:RuleEngine/MotionDetector/Motion</wsnt:Topic>
        <wsnt:Message>
          <tt:Message UtcTime="{current_time.isoformat()}" PropertyOperation="Changed">
            <tt:Source>
              <tt:SimpleItem Name="VideoSource" Value="VideoSource"/>
            </tt:Source>
            <tt:Data>
              <tt:SimpleItem Name="State" Value="true"/>
            </tt:Data>
          </tt:Message>
        </wsnt:Message>
      </wsnt:NotificationMessage>"""
        else:
            motion_event = ""
        
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope" 
                   xmlns:wsa="http://www.w3.org/2005/08/addressing" 
                   xmlns:tev="http://www.onvif.org/ver10/events/wsdl"
                   xmlns:wsnt="http://docs.oasis-open.org/wsn/b-2"
                   xmlns:tt="http://www.onvif.org/ver10/schema"
                   xmlns:tns1="http://www.onvif.org/ver10/topics">
  <SOAP-ENV:Header/>
  <SOAP-ENV:Body>
    <tev:PullMessagesResponse>
      <tev:CurrentTime>{current_time.isoformat()}</tev:CurrentTime>
      <tev:TerminationTime>{(current_time + timedelta(minutes=30)).isoformat()}</tev:TerminationTime>
      {motion_event}
    </tev:PullMessagesResponse>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response
    
    def create_soap_fault(self, reason):
        """Erstellt eine SOAP-Fault-Antwort"""
        soap_response = f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://www.w3.org/2003/05/soap-envelope">
  <SOAP-ENV:Body>
    <SOAP-ENV:Fault>
      <SOAP-ENV:Code>
        <SOAP-ENV:Value>SOAP-ENV:Sender</SOAP-ENV:Value>
      </SOAP-ENV:Code>
      <SOAP-ENV:Reason>
        <SOAP-ENV:Text xml:lang="en">{reason}</SOAP-ENV:Text>
      </SOAP-ENV:Reason>
    </SOAP-ENV:Fault>
  </SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""
        return soap_response

class ONVIFServer:
    def __init__(self, camera):
        self.camera = camera
        self.server = None
        self.running = False
    
    def start(self):
        self.running = True
        server_address = (SERVER_HOST, ONVIF_PORT)
        self.server = ThreadedHTTPServer(server_address, ONVIFHandler)
        
        # Setze die Kamera für den Handler
        self.server.RequestHandlerClass.camera = self.camera
        
        print(f"ONVIF-Server gestartet auf {SERVER_HOST}:{ONVIF_PORT}")
        self.server_thread = threading.Thread(target=self.server.serve_forever)
        self.server_thread.daemon = True
        self.server_thread.start()
    
    def stop(self):
        if self.server:
            self.running = False
            self.server.shutdown()
            self.server.server_close()
            print("ONVIF-Server gestoppt")
