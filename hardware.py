import serial
import serial.tools.list_ports
import threading
import time
import logging

logger = logging.getLogger("SmartEntry2FA.Hardware")

class ArduinoManager:
    def __init__(self, data_callback, log_callback):
        self.arduino = None
        self.running = True
        self.data_callback = data_callback  # जब भी Arduino से डेटा आएगा, यह फंक्शन चलेगा
        self.log_callback = log_callback    # UI में लॉग दिखाने के लिए

    def connect(self):
        self.log_callback("Searching for Hardware...")

        def hw_setup():
            while self.running:
                ports = serial.tools.list_ports.comports()
                connected = False
                for port in ports:
                    desc = port.description or ""
                    if any(k in desc for k in ("USB", "CH340", "Arduino")):
                        try:
                            self.arduino = serial.Serial(port.device, 9600, timeout=1)
                            self.arduino.setDTR(False)
                            time.sleep(0.5)
                            self.arduino.setDTR(True)
                            time.sleep(2)
                            self.log_callback(f"✅ Hardware connected on {port.device}")
                            connected = True
                            break
                        except Exception as e:
                            self.log_callback(f"Serial Connection Error: {e}")

                if connected:
                    threading.Thread(target=self._serial_worker, daemon=True).start()
                    break  # Retry loop से बाहर आ जाओ
                else:
                    self.log_callback("⚠️ Arduino not found. Retrying in 5s...")
                    time.sleep(5)

        threading.Thread(target=hw_setup, daemon=True).start()

    def _serial_worker(self):
        """बैकग्राउंड में हमेशा Arduino का डेटा सुनता रहेगा"""
        while self.running:
            if self.arduino and self.arduino.is_open:
                try:
                    if self.arduino.in_waiting > 0:
                        data = self.arduino.readline().decode('utf-8', errors='ignore').strip()
                        if data:
                            self.data_callback(data)
                except serial.SerialException as e:
                    self.log_callback(f"Device Disconnected: {e}")
                    if self.arduino:
                        self.arduino.close()
                    self.arduino = None
                    self.connect()  # डिसकनेक्ट होने पर वापस कनेक्ट करने की कोशिश
                    break
                except Exception:
                    time.sleep(0.5)
            else:
                break
            time.sleep(0.05)

    def send_cmd(self, cmd_type: str, *args):
        """Arduino को सेफ तरीके से कमांड भेजने के लिए"""
        if self.arduino and self.arduino.is_open:
            # कॉमा (,) और नई लाइन को हटा रहे हैं ताकि हैकिंग/इंजेक्शन न हो सके
            safe_args = [str(a).replace(",", "").replace("\n", "").replace("\r", "") for a in args]
            cmd_str = cmd_type + "," + ",".join(safe_args) + "\n"
            try:
                self.arduino.write(cmd_str.encode())
            except Exception as e:
                self.log_callback(f"⚠️ Hardware Comms Error: {e}")

    def stop(self):
        self.running = False
        if self.arduino and self.arduino.is_open:
            self.arduino.close()
