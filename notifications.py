import threading
import queue
import pythoncom
import win32com.client
import smtplib
from email.mime.text import MIMEText
import time
import logging
from config import EMAIL_USER, EMAIL_PASS

logger = logging.getLogger("SmartEntry2FA.Notifications")

class NotificationManager:
    def __init__(self, log_callback):
        self.log_callback = log_callback
        # 🚨 FIX: maxsize=3 कर दिया ताकि सिस्टम हैंग न हो
        self.tts_queue = queue.Queue(maxsize=3)
        self.muted = False
        threading.Thread(target=self._tts_worker, daemon=True).start()

    def _tts_worker(self):
        """बैकग्राउंड में आवाज़ निकालने वाला थ्रेड"""
        pythoncom.CoInitialize()
        speaker = win32com.client.Dispatch("SAPI.SpVoice")
        while True:
            text = self.tts_queue.get()
            if text is None:
                break
            try:
                speaker.Speak(text)
            except Exception:
                pass
            self.tts_queue.task_done()

    def speak(self, text: str):
        if not self.muted:
            try:
                # अगर कतार (queue) फुल है, तो पुरानी फालतू आवाज़ें छोड़ देगा
                self.tts_queue.put_nowait(text)
            except queue.Full:
                pass

    def send_email(self, to_email, student_name, time_str, status):
        if not EMAIL_PASS:
            self.log_callback("⚠️ Email Error: EMAIL_PASS not found in .env!")
            return

        def send():
            for attempt in range(3):
                try:
                    clean_email = to_email.strip()
                    body = (f"Hello {student_name},\n\n"
                            f"Your attendance has been marked as [{status}] at {time_str} "
                            f"via SmartEntry2FA.\n\nSmartEntry2FA Admin")
                    msg = MIMEText(body)
                    msg['Subject'] = f'Attendance Confirmation — {status}'
                    msg['From'] = EMAIL_USER
                    msg['To'] = clean_email

                    # 🚨 FIX: SMTP_SSL Port 465 (फायरवॉल को बायपास करने के लिए)
                    server = smtplib.SMTP_SSL('smtp.gmail.com', 465, timeout=10)
                    server.login(EMAIL_USER, EMAIL_PASS)
                    server.send_message(msg)
                    server.quit()

                    self.log_callback(f"📧 Email sent to {student_name}.")
                    break
                except Exception as e:
                    # ईमेल पासवर्ड को लॉग में छपने से रोकने के लिए
                    safe_err = str(e).replace(EMAIL_PASS, "***") if EMAIL_PASS else str(e)
                    logger.error(f"Email attempt {attempt+1} failed: {safe_err}")
                    if attempt == 2:
                        self.log_callback(f"Email Error: {safe_err}")
                    time.sleep(2)

        threading.Thread(target=send, daemon=True).start()

    def stop(self):
        try:
            self.tts_queue.put_nowait(None)
        except Exception:
            pass
