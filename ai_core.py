import face_recognition
import cv2
import numpy as np
import pickle
import hmac
import hashlib
import os
import math
import threading
import logging
from config import CACHE_KEY

logger = logging.getLogger("SmartEntry2FA.AI")

def compute_ear(eye):
    """आँखें कितनी खुली हैं (EAR - Eye Aspect Ratio) यह नापता है"""
    def dist(p1, p2):
        return math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    A = dist(eye[1], eye[5])
    B = dist(eye[2], eye[4])
    C = dist(eye[0], eye[3])
    return (A + B) / (2.0 * C) if C != 0 else 0

class FaceEngine:
    def __init__(self, log_callback):
        self.log_callback = log_callback
        self.known_face_encodings = []
        self.known_face_ids = []
        self.ai_lock = threading.Lock()
        self.reset_state()

    def reset_state(self):
        """जब कैमरा फ्रेम से चेहरा हट जाए तो सब कुछ रिसेट कर दो"""
        with self.ai_lock:
            self.blinked = False
            self.blink_counter = 0
            self.last_known_locations = []
            self.last_known_ids = []
            self.last_known_names = []
            self.last_log_id = None

    def save_cache_with_sig(self, cache_file: str, data: dict):
        """चेहरों का डेटा सेव करते वक़्त उसमें एक सीक्रेट (HMAC) लॉक लगाता है"""
        raw = pickle.dumps(data)
        sig = hmac.new(CACHE_KEY, raw, hashlib.sha256).hexdigest()
        with open(cache_file, "wb") as f:
            f.write(raw)
        with open(cache_file + ".sig", "w") as f:
            f.write(sig)

    def load_cache_verified(self, cache_file: str):
        """चेहरों का डेटा लोड करने से पहले चेक करता है कि किसी ने फाइल से छेड़छाड़ तो नहीं की"""
        sig_file = cache_file + ".sig"
        if not os.path.exists(sig_file):
            raise ValueError("Cache signature file missing — possible tampering.")
        with open(cache_file, "rb") as f:
            raw = f.read()
        with open(sig_file) as f:
            stored_sig = f.read().strip()
        expected = hmac.new(CACHE_KEY, raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, stored_sig):
            raise ValueError("Cache HMAC mismatch — cache may be tampered or corrupt.")
        return pickle.loads(raw)

    def process_frame(self, rgb_small, user_data):
        """कैमरा की हर फोटो को चेक करता है (चेहरा, ब्लिंक, और पहचान)"""
        try:
            face_locations = face_recognition.face_locations(rgb_small, model="hog")
            if not face_locations:
                self.reset_state()
                return

            local_blinked = self.blinked
            local_blink_counter = self.blink_counter

            # ब्लिंक चेक कर रहे हैं (Liveness Detection)
            if not local_blinked:
                landmarks = face_recognition.face_landmarks(rgb_small, face_locations)
                for marks in landmarks:
                    if 'left_eye' in marks and 'right_eye' in marks:
                        ear = (compute_ear(marks['left_eye']) + compute_ear(marks['right_eye'])) / 2.0
                        if ear < 0.22:
                            local_blink_counter += 1
                        else:
                            if local_blink_counter >= 1:
                                local_blinked = True
                                self.log_callback("Real Human Detected (Blink Verified)!")
                            local_blink_counter = 0
                    break

            temp_ids, temp_names = [], []

            # अगर असली इंसान है (पलक झपकाई है), तभी चेहरा मैच करो
            if local_blinked:
                face_encodings = face_recognition.face_encodings(rgb_small, face_locations)
                for enc in face_encodings:
                    id_ = None
                    name_ = "Unknown"
                    if self.known_face_encodings:
                        # 🚨 FIX: Tolerance 0.45 कर दिया ताकि अटेंडेंस फ़ास्ट लगे
                        matches = face_recognition.compare_faces(self.known_face_encodings, enc, tolerance=0.45)
                        dists = face_recognition.face_distance(self.known_face_encodings, enc)
                        if len(dists) > 0 and matches[np.argmin(dists)]:
                            id_ = self.known_face_ids[np.argmin(dists)]
                            if id_ in user_data:
                                name_ = user_data[id_]['name']
                    temp_ids.append(id_)
                    temp_names.append(name_)

            # थ्रेड-सेफ तरीके से डेटा अपडेट करो
            with self.ai_lock:
                self.blinked = local_blinked
                self.blink_counter = local_blink_counter
                self.last_known_locations = face_locations
                self.last_known_ids = temp_ids
                self.last_known_names = temp_names

        except Exception as e:
            logger.error(f"AI Pipeline Error: {e}")
            self.log_callback(f"AI Pipeline Error: {e}")
