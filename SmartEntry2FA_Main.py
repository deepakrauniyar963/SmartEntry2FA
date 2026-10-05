import cv2
import os
import sys
import threading
import time
from datetime import datetime
import numpy as np
import face_recognition
from enum import Enum, auto
import customtkinter as ctk
from PIL import Image
from tkinter import filedialog, messagebox, ttk
from tkcalendar import DateEntry
import pandas as pd
import shutil
from fpdf import FPDF
import re
import matplotlib
matplotlib.use("TkAgg")
import matplotlib.pyplot as plt
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

# Core application modules
from config import check_admin_password, LATE_TIME
from database import DatabaseManager
from hardware import ArduinoManager
from notifications import NotificationManager
from ai_core import FaceEngine

# Keep all runtime data (database, dataset, backups, .env) next to the app,
# whether it runs as a script or as a PyInstaller .exe.
APP_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
os.chdir(APP_DIR)

class AppMode(Enum):
    IDLE            = auto()
    REGISTER_FACE   = auto()
    REGISTER_FINGER = auto()
    RECOGNIZE       = auto()
    VERIFY_FINGER   = auto()

class SmartEntryApp(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("SmartEntry2FA: PRO Edition")
        self.geometry("1350x880")
        ctk.set_appearance_mode("dark")
        self.protocol("WM_DELETE_WINDOW", self.exit_app)

        # 1. Initialize core modules
        self.db = DatabaseManager()
        self.notifier = NotificationManager(self.log)
        self.ai = FaceEngine(self.log)

        # 2. Application state
        self.camera_running = False
        self.cap = None
        self.mode = AppMode.IDLE
        self.today_date = datetime.now().strftime("%Y-%m-%d")
        self.logged_today = set()
        self.temp_user_data = {}
        self.current_verifying_id = None
        self.capture_start_time = 0
        self._admin_fail_count = 0
        self._admin_locked_until = 0.0

        # 3. UI setup
        self.user_data = self.load_user_data()
        self.setup_ui()

        # 4. Start hardware (after the UI exists so logs are visible)
        self.hw = ArduinoManager(self.process_arduino_data, self.log)
        self.hw.connect()

    # ==========================================
    # Helper wrappers
    # ==========================================
    def _db(self, query, params=()):
        return self.db.fetch_all(query, params)

    def _db_one(self, query, params=()):
        return self.db.fetch_one(query, params)

    def _db_many(self, queries_params, commit=True):
        self.db.execute_many(queries_params, commit)

    def speak(self, text):
        self.notifier.speak(text)

    def send_attendance_email(self, email, name, t_str, status):
        self.notifier.send_email(email, name, t_str, status)

    def send_arduino_cmd(self, cmd_bytes):
        # cmd_bytes looks like b"VERIFY,1\n"; it is cleaned and forwarded to the hardware module
        cmd_str = cmd_bytes.decode().strip()
        parts = cmd_str.split(",")
        self.hw.send_cmd(parts[0], *parts[1:])

    def center_window(self, win, width, height):
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        win.geometry(f"{width}x{height}+{int(sw/2-width/2)}+{int(sh/2-height/2)}")

    def focus_existing_window(self, window_attr):
        if hasattr(self, window_attr):
            w = getattr(self, window_attr)
            if w and w.winfo_exists():
                w.deiconify(); w.lift(); w.focus_force()
                return True
        return False

    def load_user_data(self):
        u_map = {}
        try:
            rows = self._db("SELECT id, first_name, last_name, mobile, email FROM Users")
            for r in rows:
                u_map[int(r[0])] = {"name": f"{r[1]} {r[2]}", "mobile": r[3], "email": r[4]}
        except Exception as e:
            self.log(f"DB Load Error: {e}")
        return u_map

    # ==========================================
    # 🧠 CORE LOGIC (Camera & AI Integration)
    # ==========================================
    def verify_admin(self, callback):
        if self.focus_existing_window('auth_win'): return
        self.auth_win = ctk.CTkToplevel(self)
        auth_win = self.auth_win
        auth_win.title("Admin Lock")
        self.center_window(auth_win, 380, 230)
        auth_win.attributes("-topmost", True)

        ctk.CTkLabel(auth_win, text="Administrator Access Required", font=("Segoe UI", 16, "bold"), text_color="#ef4444").pack(pady=(20, 8))
        attempt_lbl = ctk.CTkLabel(auth_win, text="", text_color="#ef4444", font=("Segoe UI", 12))
        attempt_lbl.pack()
        pwd_entry = ctk.CTkEntry(auth_win, show="*", width=220, placeholder_text="Enter Admin Password")
        pwd_entry.pack(pady=8)

        def check_pwd():
            now = time.time()
            if self._admin_locked_until > now:
                remaining = int(self._admin_locked_until - now)
                attempt_lbl.configure(text=f"Locked! Try again in {remaining}s.")
                return

            if check_admin_password(pwd_entry.get()):
                self._admin_fail_count = 0
                auth_win.destroy()
                callback()
            else:
                self._admin_fail_count += 1
                rem = 5 - self._admin_fail_count
                if self._admin_fail_count >= 5:
                    self._admin_locked_until = now + 60.0
                    self._admin_fail_count = 0
                    attempt_lbl.configure(text="Too many attempts! Locked for 60 seconds.")
                else:
                    attempt_lbl.configure(text=f"Wrong password! {rem} attempt(s) left.")
                auth_win.attributes("-topmost", True)

        ctk.CTkButton(auth_win, text="Unlock", fg_color="#2563eb", command=check_pwd).pack(pady=10)
        pwd_entry.bind("<Return>", lambda e: check_pwd())

    def process_arduino_data(self, data: str):
        self.log(f"[ARDUINO]: {data}")

        if data == "ENROLL_SUCCESS" and self.mode == AppMode.REGISTER_FINGER:
            u = self.temp_user_data
            try:
                self._db_many([
                    ("INSERT INTO Users VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (u['id'], u['enroll'], u['first_name'], u['last_name'], u['father_name'],
                      u['department'], u['course'], u['mobile'], u['email'], u['address']))
                ])
                for f in ("encodings.pkl", "encodings.pkl.sig"):
                    if os.path.exists(f): os.remove(f)
                self.user_data = self.load_user_data()
                self.update_summary()
                self.speak(f"Registration successful for {u['first_name']}")
                self.mode = AppMode.IDLE
                self.attributes("-topmost", False)
                messagebox.showinfo("Success", "Student Registered Successfully!", parent=self)
            except Exception as e:
                self.log(f"Registration DB Error: {e}")
                self.mode = AppMode.IDLE
                self.attributes("-topmost", False)
                messagebox.showerror("Database Error", f"Failed to save student: {e}", parent=self)

        elif data == "ENROLL_FAIL" and self.mode == AppMode.REGISTER_FINGER:
            self.speak("Fingerprint failed.")
            self.attributes("-topmost", False)
            if messagebox.askyesno("Failed", "Fingerprint failed! Retry?", parent=self):
                self.speak("Place finger again.")
                self.hw.send_cmd("ENROLL", str(self.temp_user_data['id']))
            else:
                self.mode = AppMode.IDLE
                self.stop_camera()

        elif data == "VERIFY_SUCCESS" and self.mode == AppMode.VERIFY_FINGER:
            uid = self.current_verifying_id
            user_info = self._db_one("SELECT first_name, last_name, department, mobile, email FROM Users WHERE id=?", (uid,))
            if not user_info:
                self.mode = AppMode.RECOGNIZE
                return

            full_name = f"{user_info[0]} {user_info[1]}"
            dept, mobile, email = user_info[2], user_info[3], user_info[4]
            now = datetime.now()
            t_str = now.strftime("%H:%M:%S")
            date_str = now.strftime("%Y-%m-%d")

            try:
                late_time = datetime.strptime(LATE_TIME, "%H:%M:%S").time()
            except Exception:
                late_time = datetime.strptime("09:30:00", "%H:%M:%S").time()

            att_status = "Late" if now.time() > late_time else "Present"
            try:
                self._db_many([
                    ("INSERT INTO Attendance (id, name, department, date, time, status) VALUES (?, ?, ?, ?, ?, ?)",
                     (uid, full_name, dept, date_str, t_str, att_status))
                ])
                self.logged_today.add(str(uid))
                self.log(f"✅ {full_name} marked {att_status}")
                self.update_summary()
                self.speak(f"Welcome {full_name}, you are {att_status}.")

                msg = f"Alert: {full_name} marked {att_status} at {t_str}. 2FA Verified."
                self.hw.send_cmd("SMS", mobile, msg)

                if email and "@" in email:
                    self.send_attendance_email(email, full_name, t_str, att_status)
            except Exception as e:
                self.log(f"⚠️ Already marked or Error: {e}")
                self.speak(f"{full_name}, you are already marked present.")
            self.mode = AppMode.RECOGNIZE

        elif data == "VERIFY_FAIL" and self.mode == AppMode.VERIFY_FINGER:
            self.speak("Access Denied. Wrong Fingerprint.")
            self.log("🔴 2FA FAILED: Unauthorized Fingerprint!")
            self.mode = AppMode.RECOGNIZE

        elif data.startswith("HW_STATUS:"):
            if hasattr(self, 'hw_win') and self.hw_win.winfo_exists():
                status = data.split(":")[1]
                if status == "ALL_OK":
                    self.lbl_finger.configure(text="Fingerprint Sensor : OK", text_color="#10b981")
                    self.lbl_gsm.configure(text="GSM Module : OK", text_color="#10b981")
                    self.speak("All hardware components are working.")
                elif status == "FINGER_FAIL":
                    self.lbl_finger.configure(text="Fingerprint Sensor : FAILED", text_color="#ef4444")
                elif status == "GSM_FAIL":
                    self.lbl_gsm.configure(text="GSM Module : FAILED", text_color="#ef4444")

    def _init_camera(self, on_success_mode: AppMode):
        def init():
            cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
            if not cap.isOpened(): cap = cv2.VideoCapture(1, cv2.CAP_DSHOW)
            if not cap.isOpened():
                self.after(0, lambda: messagebox.showerror("Camera Error", "Webcam not found!", parent=self))
                self.after(0, lambda: setattr(self, 'mode', AppMode.IDLE))
                return
            if self.mode == AppMode.IDLE:
                cap.release()
                return
            self.cap = cap
            self.camera_running = True
            self.mode = on_success_mode
            self.after(0, self.update_frame)
        threading.Thread(target=init, daemon=True).start()

    def start_face_capture(self):
        self.stop_camera()
        uid = self.temp_user_data['id']
        self.user_path = f"dataset/user_{uid}"
        os.makedirs(self.user_path, exist_ok=True)
        self.speak("Please stand still for 3 seconds.")
        self.log("Initializing Camera...")
        self.capture_start_time = time.time()
        self.mode = AppMode.REGISTER_FACE
        self._init_camera(AppMode.REGISTER_FACE)

    def start_recognition(self):
        self.stop_camera()
        self.ai.reset_state()
        self.user_data = self.load_user_data()
        cache_file = "encodings.pkl"

        if os.path.exists(cache_file):
            try:
                data = self.ai.load_cache_verified(cache_file)
                self.ai.known_face_encodings = data["encodings"]
                self.ai.known_face_ids = data["ids"]
                self.log("⚡ Face Encodings loaded from verified cache!")
                self._start_camera_recognition_thread()
            except Exception as e:
                self.log(f"⚠️ Cache tampering detected or missing: {e}")
                self._build_cache_async(cache_file)
        else:
            self._build_cache_async(cache_file)

    def _build_cache_async(self, cache_file):
        self.video_label.configure(text="[ AI BUILDING DATASET CACHE... PLEASE WAIT ]", text_color="#eab308", image="")
        self.log("⏳ Scanning dataset (generating cache)...")

        def task():
            temp_encodings, temp_ids = [], []
            if os.path.exists("dataset"):
                for root, _, files in os.walk("dataset"):
                    for fname in files:
                        if fname.endswith(".jpg"):
                            try:
                                u_id = int(os.path.basename(root).split("_")[1])
                                img = face_recognition.load_image_file(os.path.join(root, fname))
                                encs = face_recognition.face_encodings(img)
                                if encs:
                                    temp_encodings.append(encs[0])
                                    temp_ids.append(u_id)
                            except Exception: pass

            self.ai.known_face_encodings = temp_encodings
            self.ai.known_face_ids = temp_ids
            try:
                self.ai.save_cache_with_sig(cache_file, {"encodings": temp_encodings, "ids": temp_ids})
                self.after(0, lambda: self.log("✅ Cache saved securely with HMAC signature."))
            except Exception as e:
                self.after(0, lambda err=e: self.log(f"Cache Save Error: {err}"))
            self.after(0, self._start_camera_recognition_thread)

        threading.Thread(target=task, daemon=True).start()

    def _start_camera_recognition_thread(self):
        self.logged_today.clear()
        rows = self._db("SELECT id FROM Attendance WHERE date=?", (self.today_date,))
        for r in rows: self.logged_today.add(str(r[0]))
        self.speak("System active. Please blink your eyes.")
        self.log("Starting recognition camera...")
        self.mode = AppMode.RECOGNIZE
        self._init_camera(AppMode.RECOGNIZE)

    def update_frame(self):
        if not self.camera_running or self.cap is None: return
        ret, frame = self.cap.read()
        if ret:
            frame = cv2.flip(frame, 1)

            if self.mode == AppMode.REGISTER_FACE:
                elapsed = time.time() - self.capture_start_time
                remaining = int(4 - elapsed)
                if remaining > 0:
                    cv2.putText(frame, f"READY... {remaining}", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 255, 255), 3)
                else:
                    small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
                    rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    locs = face_recognition.face_locations(rgb_small, model="hog")
                    if locs:
                        cv2.imwrite(f"{self.user_path}/0.jpg", frame)
                        self.stop_camera()
                        self.speak("Face saved. Place finger.")
                        self.hw.send_cmd("ENROLL", str(self.temp_user_data['id']))
                        self.mode = AppMode.REGISTER_FINGER
                        return
                    else:
                        cv2.putText(frame, "ALIGN FACE", (80, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 255), 3)

            elif self.mode == AppMode.REGISTER_FINGER:
                cv2.putText(frame, "PLACE FINGER ON SENSOR", (50, 400), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 255), 3)

            elif self.mode == AppMode.RECOGNIZE:
                small = cv2.resize(frame, (0, 0), fx=0.25, fy=0.25)
                rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)

                if not getattr(self, 'ai_processing', False):
                    self.ai_processing = True
                    # Background me AI process chalne do (Taki camera lagg na ho)
                    threading.Thread(target=self._run_ai, args=(rgb_small,), daemon=True).start()

                with self.ai.ai_lock:
                    locs = list(self.ai.last_known_locations)
                    ids = list(self.ai.last_known_ids)
                    names = list(self.ai.last_known_names)
                    local_blinked = self.ai.blinked

                for (top, right, bottom, left), u_id, name_ in zip(locs, ids, names):
                    top *= 4; right *= 4; bottom *= 4; left *= 4
                    if not local_blinked:
                        cv2.rectangle(frame, (left, top), (right, bottom), (0, 165, 255), 2)
                        cv2.putText(frame, "BLINK EYES", (left, top - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 165, 255), 2)
                    else:
                        color = (0, 255, 0) if name_ != "Unknown" else (0, 0, 255)
                        cv2.rectangle(frame, (left, top), (right, bottom), color, 2)
                        cv2.putText(frame, name_, (left, top - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.8, color, 2)
                        if u_id:
                            if str(u_id) in self.logged_today:
                                cv2.putText(frame, "ALREADY PRESENT", (left, bottom + 30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                            else:
                                self.speak("Place finger.")
                                self.current_verifying_id = u_id
                                self.hw.send_cmd("VERIFY", str(u_id))
                                self.mode = AppMode.VERIFY_FINGER
                                with self.ai.ai_lock:
                                    self.ai.blinked = False
                                break

            elif self.mode == AppMode.VERIFY_FINGER:
                cv2.putText(frame, "VERIFYING FINGERPRINT...", (80, 420), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 0), 2)

            tw = self.monitor_frame.winfo_width()
            th = self.monitor_frame.winfo_height()
            if tw > 10 and th > 10:
                frame_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                frame_resized = cv2.resize(frame_rgb, (tw, th))
                pil_image = Image.fromarray(frame_resized)
                if not hasattr(self, 'current_video_img'):
                    self.current_video_img = ctk.CTkImage(pil_image, size=(tw, th))
                    self.video_label.configure(image=self.current_video_img, text="")
                else:
                    self.current_video_img.configure(light_image=pil_image, dark_image=pil_image, size=(tw, th))
                    self.video_label.configure(image=self.current_video_img, text="")

        if self.camera_running:
            self.after(30, self.update_frame) # Fast FPS optimization

    def _run_ai(self, rgb_small):
        self.ai.process_frame(rgb_small, self.user_data)
        self.ai_processing = False

    def stop_camera(self):
        self.camera_running = False
        cap_temp = self.cap
        self.cap = None
        if cap_temp:
            threading.Thread(target=cap_temp.release, daemon=True).start()
        self.video_label.configure(image="", text="[ SECURITY CAMERA OFFLINE ]", text_color="#4b5563")
        self.mode = AppMode.IDLE

    def update_clock(self):
        now = datetime.now()
        current_time = now.strftime("%I:%M:%S %p")
        current_day = now.strftime("%A")
        current_date = now.strftime("%d %b %Y")
        iso_date = now.strftime("%Y-%m-%d")

        if hasattr(self, 'today_date') and self.today_date != iso_date and hasattr(self, 'logged_today'):
            self.today_date = iso_date
            self.logged_today.clear()
            self.update_summary()

        self.today_date = iso_date
        if hasattr(self, 'clock_label'):
            self.clock_label.configure(text=f"{current_time} | {current_day}\n{current_date}")
        self.after(1000, self.update_clock)

    def update_summary(self):
        try:
            tot = self._db_one("SELECT COUNT(*) FROM Users")[0]
            pres = self._db_one("SELECT COUNT(DISTINCT id) FROM Attendance WHERE date=?", (self.today_date,))[0]
            self.total_reg_lbl.configure(text=f"Total Registered: {tot}")
            self.present_today_lbl.configure(text=f"Present Today: {pres}")
        except Exception: pass

    def log(self, text: str):
        t = datetime.now().strftime("%H:%M:%S")
        def update_ui():
            self.log_box.configure(state="normal")
            self.log_box.insert("end", f"[{t}] > {text}\n")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.after(0, update_ui)

    def toggle_mute(self):
        self.notifier.muted = self.mute_var.get()
        if self.notifier.muted:
            self.mute_switch.configure(text="Muted")
            self.log("System Sound Muted.")
        else:
            self.mute_switch.configure(text="Sound On")
            self.log("System Sound Enabled.")

    def exit_app(self):
        self.app_running = False
        self.stop_camera()
        self.notifier.stop()
        self.hw.stop()
        self.destroy()

    # ------------------------------------------------------------------
    # REGISTRATION FORM
    # ------------------------------------------------------------------
    def open_registration_form(self):
        if self.focus_existing_window('reg_win'):
            return
        if self.mode != AppMode.IDLE:
            messagebox.showwarning("System Busy",
                "A registration or scanning task is already in progress. "
                "Please complete or cancel it first.", parent=self)
            return

        self.stop_camera()
        self.reg_win = ctk.CTkToplevel(self)
        reg_win = self.reg_win
        reg_win.title("Student Registration Form")
        self.center_window(reg_win, 550, 820)
        reg_win.attributes("-topmost", True)

        def on_reg_close():
            self.mode = AppMode.IDLE
            self.stop_camera()
            self.temp_user_data = {}
            reg_win.destroy()

        reg_win.protocol("WM_DELETE_WINDOW", on_reg_close)

        ctk.CTkLabel(reg_win, text="Student Registration Form",
                     font=("Segoe UI", 22, "bold")).pack(pady=15)
        sc = ctk.CTkScrollableFrame(reg_win, width=500, height=630)
        sc.pack(pady=5, padx=20, fill="both", expand=True)

        fields = {}
        labels = [
            "Finger Print ID*", "Enrollment Number (Unique)*", "First Name*",
            "Last Name*", "Father's Name*", "Department*", "Course*",
            "Mobile Number (+91...)*", "Email ID*", "Home Address*"
        ]
        for label in labels:
            ctk.CTkLabel(sc, text=label, font=("Segoe UI", 12)).pack(anchor="w", padx=10, pady=(10, 0))
            entry = ctk.CTkEntry(sc, width=450)
            entry.pack(pady=2, padx=10)
            fields[label] = entry

        def submit_data():
            reg_win.attributes("-topmost", False)
            for lbl, ent in fields.items():
                if not ent.get().strip():
                    messagebox.showerror("Error", f"All fields are compulsory!\nMissing: {lbl}", parent=reg_win)
                    reg_win.attributes("-topmost", True)
                    return

            uid     = fields["Finger Print ID*"].get().strip()
            enroll  = fields["Enrollment Number (Unique)*"].get().strip()
            fname   = fields["First Name*"].get().strip().title()
            lname   = fields["Last Name*"].get().strip().title()
            address_val = fields["Home Address*"].get().strip().title()

            if len(address_val) > 200:
                messagebox.showerror("Error", "Address too long (max 200 chars).", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return
            if not uid.isdigit():
                messagebox.showerror("Error", "Fingerprint ID must be a number!", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return
            if not (re.match(r"^[A-Za-z\s]+$", fname) and re.match(r"^[A-Za-z\s]+$", lname)):
                messagebox.showerror("Error", "Invalid name — only letters and spaces allowed.", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return

            mobile_val = fields["Mobile Number (+91...)*"].get().strip()
            email_val  = fields["Email ID*"].get().strip()

            if not (mobile_val.isdigit() and len(mobile_val) == 10):
                messagebox.showerror("Error", "Mobile must be exactly 10 digits.", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return

            dept_val   = fields["Department*"].get().strip().upper()
            course_val = fields["Course*"].get().strip().upper()
            if len(dept_val) > 100 or len(course_val) > 100:
                messagebox.showerror("Error", "Department/Course too long (max 100 chars).", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return

            email_regex = r"^[\w\.\-]+@[\w\.\-]+\.[a-zA-Z]{2,}$"
            if not re.match(email_regex, email_val):
                messagebox.showerror("Error", "Invalid email. Example: student@gmail.com", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return

            exists = self._db_one("SELECT id FROM Users WHERE id=? OR enroll=?", (int(uid), enroll.upper()))
            if exists:
                messagebox.showerror("Error", "ID or Enrollment Number already exists!", parent=reg_win)
                reg_win.attributes("-topmost", True)
                return

            self.temp_user_data = {
                "id":          int(uid),
                "enroll":      enroll.upper(),
                "first_name":  fname,
                "last_name":   lname,
                "father_name": fields["Father's Name*"].get().strip().title(),
                "department":  dept_val,
                "course":      course_val,
                "mobile":      mobile_val,
                "email":       email_val.lower(),
                "address":     address_val
            }
            reg_win.protocol("WM_DELETE_WINDOW", lambda: None)
            reg_win.destroy()
            self.start_face_capture()

        ctk.CTkButton(reg_win, text="Next: Capture Face & Finger →", fg_color="#2563eb", height=45, command=submit_data).pack(pady=15)

    # ------------------------------------------------------------------
    # ANALYTICS PANEL
    # ------------------------------------------------------------------
    def open_analytics_panel(self):
        if self.focus_existing_window('analytics_win'):
            return
        self.stop_camera()
        self.analytics_win = ctk.CTkToplevel(self)
        w = self.analytics_win
        w.title("Student Analytics Dashboard")
        self.center_window(w, 1200, 800)
        w.attributes("-topmost", True)

        search_frame = ctk.CTkFrame(w, fg_color="transparent")
        search_frame.pack(pady=15, fill="x", padx=40)

        search_entry = ctk.CTkEntry(search_frame, placeholder_text="Enter Enroll No, Name or ID...", width=600, height=45, font=("Segoe UI", 14))
        search_entry.pack(side="left", padx=10)

        content_frame = ctk.CTkFrame(w, fg_color="transparent")
        content_frame.pack(fill="both", expand=True, padx=40, pady=10)

        left_info = ctk.CTkFrame(content_frame, width=420, corner_radius=12)
        left_info.pack(side="left", fill="both", padx=(0, 10))
        left_info.pack_propagate(False)

        right_chart = ctk.CTkFrame(content_frame, corner_radius=12, fg_color="#1a1a1a")
        right_chart.pack(side="right", fill="both", expand=True)

        _current_canvas = [None]

        def search_student():
            w.attributes("-topmost", False)
            raw_query = search_entry.get().strip()
            if not raw_query: return

            for widget in left_info.winfo_children(): widget.destroy()
            for widget in right_chart.winfo_children(): widget.destroy()
            if _current_canvas[0] is not None:
                try: _current_canvas[0].get_tk_widget().destroy()
                except Exception: pass
                _current_canvas[0] = None

            safe_like = raw_query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            user = self._db_one("SELECT * FROM Users WHERE enroll=? OR first_name LIKE ? ESCAPE '\\' OR CAST(id AS TEXT)=?",
                                (raw_query, f"%{safe_like}%", raw_query))

            if not user:
                messagebox.showerror("Error", "No Student Found!", parent=w)
                w.attributes("-topmost", True)
                return

            uid, enroll, fname, lname, father, dept, course, mobile, email, address = user
            full_name = f"{fname} {lname}"

            att_rows = self._db("SELECT status, date, time FROM Attendance WHERE id=? ORDER BY date DESC", (uid,))
            all_dates = self._db("SELECT DISTINCT date FROM Attendance")

            work_days = 0
            for (d,) in all_dates:
                try:
                    if datetime.strptime(d, "%Y-%m-%d").weekday() != 6: work_days += 1
                except Exception: pass

            on_time = sum(1 for r in att_rows if r[0] == "Present")
            late = sum(1 for r in att_rows if r[0] == "Late")
            tot_present = on_time + late
            if work_days == 0: work_days = tot_present if tot_present > 0 else 1
            absent = max(0, work_days - tot_present)

            img_p = f"dataset/user_{uid}/0.jpg"
            if os.path.exists(img_p):
                img = ctk.CTkImage(Image.open(img_p), size=(120, 120))
                ctk.CTkLabel(left_info, image=img, text="").pack(pady=15)

            ctk.CTkLabel(left_info, text=full_name, font=("Segoe UI", 22, "bold"), text_color="#54cfdc").pack()
            ctk.CTkLabel(left_info, text=f"{course} | {dept}", font=("Segoe UI", 14)).pack(pady=2)
            ctk.CTkLabel(left_info, text=f"ID: {uid}  |  Enroll: {enroll}\nFather: {father}\nMobile: {mobile}\nEmail: {email}",
                         font=("Consolas", 13), justify="center").pack(pady=10)

            sf = ctk.CTkFrame(left_info, fg_color="transparent")
            sf.pack(pady=10, fill="x")
            ctk.CTkLabel(sf, text=f"Present: {on_time}", font=("Segoe UI", 14, "bold"), text_color="#16a34a").pack(side="left", expand=True)
            ctk.CTkLabel(sf, text=f"Late: {late}", font=("Segoe UI", 14, "bold"), text_color="#eab308").pack(side="left", expand=True)
            ctk.CTkLabel(sf, text=f"Absent: {absent}", font=("Segoe UI", 14, "bold"), text_color="#ef4444").pack(side="left", expand=True)

            ctk.CTkButton(left_info, text="Edit Profile", fg_color="#d97706", command=lambda: self.open_update_form(uid, w)).pack(pady=10, fill="x", padx=40)

            def download_report():
                p = filedialog.asksaveasfilename(defaultextension='.pdf', initialfile=f"Report_{enroll}.pdf", filetypes=[("PDF", "*.pdf")])
                if not p: return
                pdf = FPDF()
                pdf.add_page()
                pdf.set_font("Arial", 'B', 18)
                pdf.cell(190, 10, f"Student Report — {full_name}", ln=True, align='C')
                pdf.set_font("Arial", size=12)
                pdf.ln(8)
                pdf.cell(190, 8, f"Enroll: {enroll} | Dept: {dept} | ID: {uid}", ln=True)
                pdf.cell(190, 8, f"Working Days: {work_days} | On-Time: {on_time} | Late: {late} | Absent: {absent}", ln=True)
                pdf.ln(8)
                pdf.set_font("Arial", 'B', 12)
                pdf.cell(50, 10, "Date", border=1)
                pdf.cell(40, 10, "Time", border=1)
                pdf.cell(40, 10, "Status", border=1)
                pdf.ln()
                pdf.set_font("Arial", size=11)
                for r in att_rows:
                    pdf.cell(50, 10, r[1], border=1)
                    pdf.cell(40, 10, r[2], border=1)
                    pdf.cell(40, 10, r[0], border=1)
                    pdf.ln()
                pdf.output(p)
                messagebox.showinfo("Saved", "Report Downloaded!", parent=w)

            ctk.CTkButton(left_info, text="Download Full Report", fg_color="#2563eb", command=download_report).pack(pady=5, fill="x", padx=40)

            fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(7, 4), facecolor="#1a1a1a")
            raw_sizes  = [on_time, late, absent]
            raw_labels = ['On-Time', 'Late', 'Absent']
            raw_colors = ['#16a34a', '#eab308', '#dc2626']
            pie_sizes, pie_labels, pie_colors = [], [], []

            if tot_present > 0:
                for s, l, c in zip(raw_sizes, raw_labels, raw_colors):
                    if s > 0:
                        pie_sizes.append(s); pie_labels.append(l); pie_colors.append(c)
            else:
                pie_sizes, pie_labels, pie_colors = [1], ['Absent'], ['#dc2626']

            ax1.pie(pie_sizes, labels=pie_labels, autopct='%1.1f%%', startangle=90, colors=pie_colors, textprops={'color': 'white', 'weight': 'bold', 'size': 10})
            ax1.set_title("Attendance %", color="#54cfdc", fontsize=14, fontweight='bold', pad=10)

            months_map = {"01":"Jan","02":"Feb","03":"Mar","04":"Apr","05":"May","06":"Jun","07":"Jul","08":"Aug","09":"Sep","10":"Oct","11":"Nov","12":"Dec"}
            monthly_counts = {v: 0 for v in months_map.values()}
            ax2.set_facecolor("#1a1a1a")

            for row in att_rows:
                try:
                    m = row[1].split("-")[1]
                    if m in months_map: monthly_counts[months_map[m]] += 1
                except Exception: pass

            ax2.bar(list(monthly_counts.keys()), list(monthly_counts.values()), color="#2563eb", edgecolor="#3b82f6", width=0.6)
            ax2.set_title("Monthly Present History", color="#54cfdc", fontsize=14, fontweight='bold', pad=10)
            ax2.tick_params(colors='white', labelsize=10)
            ax2.set_xticklabels(list(monthly_counts.keys()), rotation=45, color="white")
            ax2.set_ylabel("Days Marked", color="white", fontsize=11)
            ax2.grid(axis='y', linestyle='--', alpha=0.2)
            for spine in ('top', 'right'): ax2.spines[spine].set_visible(False)
            for spine in ('bottom', 'left'): ax2.spines[spine].set_color('#555555')

            plt.tight_layout()
            canvas = FigureCanvasTkAgg(fig, master=right_chart)
            canvas.draw()
            canvas.get_tk_widget().pack(fill="both", expand=True, padx=15, pady=15)
            _current_canvas[0] = canvas
            plt.close(fig)
            w.attributes("-topmost", True)

        ctk.CTkButton(search_frame, text="Search", fg_color="#ff7a00", command=search_student).pack(side="left", padx=10)

    # ------------------------------------------------------------------
    # ATTENDANCE VIEWER
    # ------------------------------------------------------------------
    def view_attendance(self):
        if self.focus_existing_window('attendance_win'):
            return
        self.attendance_win = ctk.CTkToplevel(self)
        w = self.attendance_win
        w.title("Filtered Attendance Viewer")
        self.center_window(w, 1100, 700)
        w.configure(fg_color="#1e1e24")
        w.attributes("-topmost", True)

        top_frame = ctk.CTkFrame(w, fg_color="transparent")
        top_frame.pack(pady=10, fill="x", padx=20)

        ctk.CTkLabel(top_frame, text="From:").pack(side="left", padx=5)
        cal_from = DateEntry(top_frame, width=12, background='darkblue', foreground='white', borderwidth=2, date_pattern='dd-mm-yyyy')
        cal_from.pack(side="left", padx=5)
        ctk.CTkLabel(top_frame, text="To:").pack(side="left", padx=5)
        cal_to = DateEntry(top_frame, width=12, background='darkblue', foreground='white', borderwidth=2, date_pattern='dd-mm-yyyy')
        cal_to.pack(side="left", padx=5)
        ctk.CTkLabel(top_frame, text="Department:").pack(side="left", padx=(15, 5))
        dept_entry = ctk.CTkEntry(top_frame, width=120)
        dept_entry.pack(side="left", padx=5)

        style = ttk.Style(w)
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", borderwidth=0, font=("Segoe UI", 11))
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=("Segoe UI", 12, "bold"), borderwidth=0)
        style.map('Treeview', background=[('selected', '#2563eb')])

        tree = ttk.Treeview(w, columns=("ID", "Name", "Dept", "Date", "Time", "Status"), show="headings", height=15)
        for c in tree["columns"]:
            tree.heading(c, text=c)
            tree.column(c, anchor="center")
        tree.pack(fill="both", expand=True, padx=20, pady=10)

        def load_data():
            for item in tree.get_children(): tree.delete(item)
            dep_filter = dept_entry.get().strip()
            try:
                f_date_iso = datetime.strptime(cal_from.get(), "%d-%m-%Y").strftime("%Y-%m-%d")
                t_date_iso = datetime.strptime(cal_to.get(), "%d-%m-%Y").strftime("%Y-%m-%d")
            except Exception:
                messagebox.showerror("Error", "Invalid date format.", parent=w)
                return

            base_q = "SELECT id, name, department, date, time, status FROM Attendance WHERE date BETWEEN ? AND ?"
            params = [f_date_iso, t_date_iso]
            if dep_filter:
                base_q += " AND department LIKE ?"
                params.append(f"%{dep_filter}%")
            base_q += " ORDER BY date DESC, time DESC"

            try:
                rows = self._db(base_q, tuple(params))
                for r in rows:
                    display_row = list(r)
                    try: display_row[3] = datetime.strptime(r[3], "%Y-%m-%d").strftime("%d-%m-%Y")
                    except Exception: pass
                    tree.insert("", "end", values=display_row)
            except Exception as e:
                self.log(f"Table read error: {e}")

        ctk.CTkButton(top_frame, text="Apply Filter", command=load_data, fg_color="#2563eb").pack(side="left", padx=15)

        def bulk_delete():
            selected = tree.selection()
            if not selected: return
            w.attributes("-topmost", False)
            if messagebox.askyesno("Confirm", f"Delete {len(selected)} selected record(s)?", parent=w):
                queries = []
                for item in selected:
                    vals = tree.item(item, "values")
                    try: db_date = datetime.strptime(vals[3], "%d-%m-%Y").strftime("%Y-%m-%d")
                    except Exception: db_date = vals[3]
                    queries.append(("DELETE FROM Attendance WHERE id=? AND date=? AND time=?", (vals[0], db_date, vals[4])))
                    tree.delete(item)
                self._db_many(queries)
                self.update_summary()
            w.attributes("-topmost", True)

        def export_filtered(ext):
            items = tree.get_children()
            if not items: return
            data = [tree.item(i, "values") for i in items]
            w.attributes("-topmost", False)
            p = filedialog.asksaveasfilename(defaultextension=ext)
            if not p:
                w.attributes("-topmost", True)
                return
            try:
                if ext == '.xlsx':
                    df = pd.DataFrame(data, columns=list(tree["columns"]))
                    df.to_excel(p, index=False)
                elif ext == '.pdf':
                    pdf = FPDF()
                    pdf.add_page()
                    pdf.set_font("Arial", 'B', 14)
                    pdf.cell(190, 10, "Filtered Attendance Report", ln=True, align='C')
                    pdf.ln(5)
                    pdf.set_font("Arial", size=10)
                    for row in data:
                        pdf.cell(190, 8, f"ID:{row[0]} | {row[1]} | {row[2]} | {row[3]} | {row[4]} | {row[5]}", ln=True)
                    pdf.output(p)
                messagebox.showinfo("Saved", f"Exported to {ext}!", parent=w)
            except Exception as e:
                messagebox.showerror("Export Error", str(e), parent=w)
            w.attributes("-topmost", True)

        btn_frame = ctk.CTkFrame(w, fg_color="transparent")
        btn_frame.pack(pady=10, fill="x", padx=20)
        ctk.CTkLabel(btn_frame, text="*Hold CTRL/SHIFT and click rows to multi-select", text_color="gray").pack(side="left")
        ctk.CTkButton(btn_frame, text="Delete Selected", fg_color="#ef4444", command=bulk_delete).pack(side="left", padx=15)
        ctk.CTkButton(btn_frame, text="Export Excel", fg_color="#10b981", command=lambda: export_filtered('.xlsx')).pack(side="right", padx=5)
        ctk.CTkButton(btn_frame, text="Export PDF", fg_color="#e11d48", command=lambda: export_filtered('.pdf')).pack(side="right", padx=5)
        load_data()

    # ------------------------------------------------------------------
    # UPDATE FORM
    # ------------------------------------------------------------------
    def open_update_form(self, uid, parent_win):
        if self.focus_existing_window('update_win'): return
        parent_win.attributes("-topmost", False)
        res = self._db_one("SELECT id, enroll, first_name, last_name, father_name, department, course, mobile, email, address FROM Users WHERE id=?", (uid,))
        if not res: return

        self.update_win = ctk.CTkToplevel(self)
        up_win = self.update_win
        up_win.title("Edit Student")
        self.center_window(up_win, 550, 820)
        up_win.attributes("-topmost", True)

        sc = ctk.CTkScrollableFrame(up_win, width=500, height=700)
        sc.pack(pady=10, padx=20, fill="both", expand=True)

        labels = ["Enrollment Number", "First Name", "Last Name", "Father's Name", "Department", "Course", "Mobile Number", "Email ID", "Home Address"]
        fields = {}
        for i, label in enumerate(labels):
            ctk.CTkLabel(sc, text=label).pack(anchor="w", padx=10)
            entry = ctk.CTkEntry(sc, width=450)
            entry.insert(0, str(res[i + 1]) if res[i + 1] is not None else "")
            entry.pack(pady=5)
            fields[label] = entry

        def save_update():
            up_win.attributes("-topmost", False)
            vals = [e.get().strip() for e in fields.values()]
            for val in vals:
                if not val:
                    messagebox.showerror("Error", "All fields are compulsory!", parent=up_win)
                    up_win.attributes("-topmost", True)
                    return

            enroll_val, fname, lname, father_name, department, course, mobile_val, email_val, address_val = vals
            enroll_val, fname, lname, father_name = enroll_val.upper(), fname.title(), lname.title(), father_name.title()
            department, course, email_val, address_val = department.upper(), course.upper(), email_val.lower(), address_val.title()

            if len(address_val) > 200:
                messagebox.showerror("Error", "Address too long (max 200 chars).", parent=up_win)
                up_win.attributes("-topmost", True)
                return

            dup = self._db_one("SELECT id FROM Users WHERE enroll=? AND id!=?", (enroll_val, uid))
            if dup:
                messagebox.showerror("Error", "Enrollment exists for another student!", parent=up_win)
                up_win.attributes("-topmost", True)
                return

            if not all(re.match(r"^[A-Za-z\s]+$", n) for n in (fname, lname, father_name)):
                messagebox.showerror("Error", "Names can only contain letters and spaces.", parent=up_win)
                up_win.attributes("-topmost", True)
                return

            if not (mobile_val.isdigit() and len(mobile_val) == 10):
                messagebox.showerror("Error", "Mobile must be exactly 10 digits.", parent=up_win)
                up_win.attributes("-topmost", True)
                return

            if not re.match(r"^[\w\.\-]+@[\w\.\-]+\.[a-zA-Z]{2,}$", email_val):
                messagebox.showerror("Error", "Invalid email.", parent=up_win)
                up_win.attributes("-topmost", True)
                return

            try:
                full_name = f"{fname} {lname}"
                self._db_many([
                    ("UPDATE Users SET enroll=?, first_name=?, last_name=?, father_name=?, department=?, course=?, mobile=?, email=?, address=? WHERE id=?",
                     (enroll_val, fname, lname, father_name, department, course, mobile_val, email_val, address_val, uid)),
                    ("UPDATE Attendance SET name=?, department=? WHERE id=?", (full_name, department, uid))
                ])
                self.user_data = self.load_user_data()
                messagebox.showinfo("Success", "Student Updated!", parent=up_win)
                up_win.destroy()
                parent_win.destroy()
            except Exception as e:
                messagebox.showerror("Database Error", f"Failed to update: {e}", parent=up_win)
                up_win.attributes("-topmost", True)

        ctk.CTkButton(up_win, text="Save Changes", fg_color="#16a34a", command=save_update).pack(pady=15)

    # ------------------------------------------------------------------
    # USER MANAGER (PRO UI WITH SEARCH BAR)
    # ------------------------------------------------------------------
    def open_user_manager(self):
        if self.focus_existing_window('user_manager_win'): return
        self.log("Opening Student Management Panel...")
        self.user_manager_win = ctk.CTkToplevel(self)
        w = self.user_manager_win
        w.title("Manage Students")
        self.center_window(w, 1050, 680)
        w.attributes("-topmost", True)

        top_frame = ctk.CTkFrame(w, fg_color="transparent")
        top_frame.pack(pady=(20, 5), padx=25, fill="x")

        ctk.CTkLabel(top_frame, text="Registered Students", font=("Segoe UI", 24, "bold"), text_color="#ffffff").pack(side="left")

        search_var = ctk.StringVar()
        def perform_search(*args):
            query = search_var.get().strip()
            populate_list(query)

        search_btn = ctk.CTkButton(top_frame, text="Search", width=80, fg_color="#2563eb", hover_color="#1d4ed8", font=("Segoe UI", 13, "bold"), command=perform_search)
        search_btn.pack(side="right", padx=(10, 0))

        search_entry = ctk.CTkEntry(top_frame, textvariable=search_var, width=300, placeholder_text="Search by Name, Enroll or ID...", font=("Segoe UI", 13))
        search_entry.pack(side="right")
        search_entry.bind("<Return>", perform_search)

        sc = ctk.CTkScrollableFrame(w, width=950, height=520, fg_color="transparent")
        sc.pack(pady=10, padx=20, fill="both", expand=True)

        def load_row(u_id, u_enroll, u_fname, u_lname, u_dept, u_course, u_mobile, u_email):
            f = ctk.CTkFrame(sc, fg_color="#2b2b2b", corner_radius=12)
            f.pack(fill="x", pady=8, padx=5)
            full_name = f"{u_fname} {u_lname}"

            img_lbl = ctk.CTkLabel(f, text="⏳", width=60, height=60, corner_radius=8, fg_color="#1e1e1e")
            img_lbl.pack(side="left", padx=15, pady=10)

            info_frame = ctk.CTkFrame(f, fg_color="transparent")
            info_frame.pack(side="left", fill="both", expand=True, padx=10, pady=10)

            ctk.CTkLabel(info_frame, text=f"{full_name}  (ID: {u_id})", font=("Segoe UI", 18, "bold"), text_color="#54cfdc", anchor="w").pack(fill="x")
            ctk.CTkLabel(info_frame, text=f"🎓 {u_course} | {u_dept}   |   🆔 Enroll: {u_enroll}", font=("Segoe UI", 13), text_color="#d1d5db", anchor="w").pack(fill="x", pady=(2, 0))
            ctk.CTkLabel(info_frame, text=f"📱 +91 {u_mobile}   |   📧 {u_email}", font=("Segoe UI", 13), text_color="#9ca3af", anchor="w").pack(fill="x")

            btn_frame = ctk.CTkFrame(f, fg_color="transparent")
            btn_frame.pack(side="right", padx=15)

            ctk.CTkButton(btn_frame, text="Update", fg_color="#eab308", text_color="black", hover_color="#ca8a04", width=80, font=("Segoe UI", 13, "bold"),
                          command=lambda i=u_id: self.open_update_form(i, w)).pack(side="left", padx=5)
            ctk.CTkButton(btn_frame, text="Delete", fg_color="#ef4444", hover_color="#b91c1c", width=80, font=("Segoe UI", 13, "bold"),
                          command=lambda i=u_id, n=full_name: self.delete_user(i, n, w)).pack(side="left", padx=5)

            def load_img():
                img_p = f"dataset/user_{u_id}/0.jpg"
                if os.path.exists(img_p):
                    try:
                        img = Image.open(img_p).resize((60, 60))
                        ctk_img = ctk.CTkImage(img, size=(60, 60))
                        def attach():
                            img_lbl.configure(image=ctk_img, text="")
                            img_lbl.image = ctk_img
                        self.after(0, attach)
                    except Exception:
                        self.after(0, lambda: img_lbl.configure(text="❌"))
            threading.Thread(target=load_img, daemon=True).start()

        def populate_list(search_query=""):
            for widget in sc.winfo_children(): widget.destroy()
            if search_query:
                safe_query = f"%{search_query}%"
                rows = self._db("SELECT id, enroll, first_name, last_name, department, course, mobile, email FROM Users WHERE enroll LIKE ? OR first_name LIKE ? OR last_name LIKE ? OR CAST(id AS TEXT) LIKE ?",
                                (safe_query, safe_query, safe_query, safe_query))
            else:
                rows = self._db("SELECT id, enroll, first_name, last_name, department, course, mobile, email FROM Users")
            if not rows:
                ctk.CTkLabel(sc, text="No students found!", font=("Segoe UI", 16), text_color="#ef4444").pack(pady=20)
                return
            for row in rows: load_row(*row)
        populate_list()

    def delete_user(self, uid, name, win):
        win.attributes("-topmost", False)
        if messagebox.askyesno("Confirm", f"Delete {name}? This removes all face and fingerprint data.", parent=win):
            try:
                self._db_many([
                    ("DELETE FROM Users WHERE id=?", (uid,)),
                    ("DELETE FROM Attendance WHERE id=?", (uid,))
                ])
                if os.path.exists(f"dataset/user_{uid}"): shutil.rmtree(f"dataset/user_{uid}")
                for f in ("encodings.pkl", "encodings.pkl.sig"):
                    if os.path.exists(f): os.remove(f)
                self.user_data = self.load_user_data()
                self.update_summary()
                win.destroy()
                self.open_user_manager()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to delete: {e}", parent=win)
                win.attributes("-topmost", True)
        else:
            win.attributes("-topmost", True)

    # ------------------------------------------------------------------
    # BACKUP DASHBOARD
    # ------------------------------------------------------------------
    def open_backup_dashboard(self):
        if self.focus_existing_window('backup_win'): return
        self.backup_win = ctk.CTkToplevel(self)
        w = self.backup_win
        w.title("System Full Backup")
        self.center_window(w, 600, 420)
        w.configure(fg_color="#1e1e24")
        w.attributes("-topmost", True)

        ctk.CTkLabel(w, text="Full System Disaster Recovery", font=("Segoe UI", 18, "bold")).pack(pady=10)

        style = ttk.Style(w)
        style.theme_use("default")
        style.configure("Treeview", background="#2b2b2b", foreground="white", fieldbackground="#2b2b2b", borderwidth=0, font=("Segoe UI", 11))
        style.configure("Treeview.Heading", background="#1f538d", foreground="white", font=("Segoe UI", 12, "bold"), borderwidth=0)

        tree = ttk.Treeview(w, columns=("File", "Date", "Time"), show="headings")
        tree.heading("File", text="Backup File"); tree.heading("Date", text="Date"); tree.heading("Time", text="Time")
        tree.column("File", width=320, anchor="w"); tree.column("Date", width=120, anchor="center"); tree.column("Time", width=120, anchor="center")
        tree.pack(fill="both", expand=True, padx=20, pady=10)

        os.makedirs("Database_Backups", exist_ok=True)
        for f in reversed(os.listdir("Database_Backups")):
            if f.endswith(".zip"):
                try:
                    parts = f.replace("SmartEntry_FullBackup_", "").replace(".zip", "").split("_")
                    tree.insert("", "end", values=(f, parts[0], parts[1].replace("-", ":")))
                except Exception: pass

        def create_backup():
            w.attributes("-topmost", False)
            self.log("Creating Full System Backup... Please wait.")
            def do_backup():
                t_stamp = datetime.now().strftime("%d-%m-%Y_%H-%M-%S")
                temp_folder = f"temp_backup_{t_stamp}"
                try:
                    os.makedirs(temp_folder, exist_ok=True)
                    if os.path.exists(self.db.db_path): shutil.copy(self.db.db_path, temp_folder)
                    if os.path.exists("dataset"): shutil.copytree("dataset", os.path.join(temp_folder, "dataset"))
                    for cf in ("encodings.pkl", "encodings.pkl.sig"):
                        if os.path.exists(cf): shutil.copy(cf, temp_folder)
                    shutil.make_archive(f"Database_Backups/SmartEntry_FullBackup_{t_stamp}", 'zip', temp_folder)
                    shutil.rmtree(temp_folder)
                    self.after(0, lambda: self.log(f"✅ Backup: SmartEntry_FullBackup_{t_stamp}.zip"))
                    self.after(0, lambda: messagebox.showinfo("Success", "Backup Saved!", parent=self))
                except Exception as e:
                    self.after(0, lambda err=e: self.log(f"Backup Error: {err}"))
                self.after(0, lambda: (w.destroy(), self.open_backup_dashboard()))
            threading.Thread(target=do_backup, daemon=True).start()

        ctk.CTkButton(w, text="Create Full ZIP Backup Now", fg_color="#10b981", command=create_backup).pack(pady=10)

    # ------------------------------------------------------------------
    # HARDWARE DIAGNOSTICS
    # ------------------------------------------------------------------
    def open_hw_diagnostics(self):
        if self.focus_existing_window('hw_win'): return
        self.hw_win = ctk.CTkToplevel(self)
        self.hw_win.title("Hardware Diagnostics")
        self.center_window(self.hw_win, 450, 380)
        self.hw_win.configure(fg_color="#1e1e24")
        self.hw_win.attributes("-topmost", True)

        ctk.CTkLabel(self.hw_win, text="Hardware Diagnostics Tool", font=("Segoe UI", 20, "bold"), text_color="#54cfdc").pack(pady=15)

        ard_status = "Connected" if (self.hw.arduino and self.hw.arduino.is_open) else "Disconnected"
        ard_color  = "#10b981" if (self.hw.arduino and self.hw.arduino.is_open) else "#ef4444"

        self.lbl_ard = ctk.CTkLabel(self.hw_win, text=f"Arduino (COM) : {ard_status}", font=("Segoe UI", 16, "bold"), text_color=ard_color)
        self.lbl_ard.pack(pady=15)
        self.lbl_finger = ctk.CTkLabel(self.hw_win, text="Fingerprint Sensor : Waiting...", font=("Segoe UI", 16, "bold"))
        self.lbl_finger.pack(pady=15)
        self.lbl_gsm = ctk.CTkLabel(self.hw_win, text="GSM Module : Waiting...", font=("Segoe UI", 16, "bold"))
        self.lbl_gsm.pack(pady=15)

        def start_test():
            self.lbl_finger.configure(text="Fingerprint Sensor : Testing...", text_color="white")
            self.lbl_gsm.configure(text="GSM Module : Testing...", text_color="white")
            if self.hw.arduino and self.hw.arduino.is_open:
                self.log("Running Hardware Diagnostics...")
                self.send_arduino_cmd(b"CHECK_HW\n")
            else:
                self.lbl_finger.configure(text="Fingerprint Sensor : Error (No Arduino)", text_color="#ef4444")
                self.lbl_gsm.configure(text="GSM Module : Error (No Arduino)", text_color="#ef4444")

        ctk.CTkButton(self.hw_win, text="Run Full Test", fg_color="#2563eb", height=40, font=("Segoe UI", 14, "bold"), command=start_test).pack(pady=25)
        start_test()

    # ------------------------------------------------------------------
    # UI SETUP
    # ------------------------------------------------------------------
    def setup_ui(self):
        self.grid_rowconfigure(0, weight=1)
        self.grid_columnconfigure(0, weight=0, minsize=480)
        self.grid_columnconfigure(1, weight=1)

        self.sidebar = ctk.CTkFrame(self, corner_radius=0, fg_color="#121212")
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_columnconfigure(0, weight=1)

        brand_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand_frame.grid(row=0, column=0, pady=(25, 10))
        ctk.CTkLabel(brand_frame, text="SmartEntry2FA", font=("Segoe UI Black", 42), text_color="#54cfdc").pack()
        ctk.CTkLabel(brand_frame, text="SECURE ATTENDANCE", font=("Segoe UI", 14, "bold"), text_color="#54cfdc").pack()

        self.clock_label = ctk.CTkLabel(brand_frame, text="Loading...", font=("Consolas", 14, "bold"), text_color="#eab308")
        self.clock_label.pack(pady=(5, 0))

        stats_frame = ctk.CTkFrame(self.sidebar, fg_color="#1e1e1e", corner_radius=10, border_width=1, border_color="#333")
        stats_frame.grid(row=1, column=0, padx=35, pady=(0, 15), sticky="ew")
        self.total_reg_lbl = ctk.CTkLabel(stats_frame, text="Total Registered: 0")
        self.total_reg_lbl.pack(pady=(5, 0))
        self.present_today_lbl = ctk.CTkLabel(stats_frame, text="Present Today: 0", text_color="#10b981")
        self.present_today_lbl.pack(pady=(0, 5))

        btn_cfg = dict(font=("Segoe UI", 15, "bold"), height=42)
        ctk.CTkButton(self.sidebar, text="Register New Student", fg_color="#2563eb", command=self.open_registration_form, **btn_cfg).grid(row=2, column=0, padx=35, pady=6, sticky="ew")
        ctk.CTkButton(self.sidebar, text="Start Attendance Camera", fg_color="#16a34a", command=self.start_recognition, **btn_cfg).grid(row=3, column=0, padx=35, pady=6, sticky="ew")
        ctk.CTkButton(self.sidebar, text="Stop Camera", fg_color="#d97706", command=self.stop_camera, **btn_cfg).grid(row=4, column=0, padx=35, pady=6, sticky="ew")

        row_manage = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        row_manage.grid(row=5, column=0, padx=35, pady=4, sticky="ew")
        row_manage.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(row_manage, text="View Data", fg_color="#0ea5e9", font=("Segoe UI", 14, "bold"), height=40, command=lambda: self.verify_admin(self.view_attendance)).grid(row=0, column=0, padx=(0, 5), sticky="ew")
        ctk.CTkButton(row_manage, text="Users", fg_color="#64748b", font=("Segoe UI", 14, "bold"), height=40, command=lambda: self.verify_admin(self.open_user_manager)).grid(row=0, column=1, padx=(5, 0), sticky="ew")

        ctk.CTkButton(self.sidebar, text="Analytics Dashboard", fg_color="#ff7a00", command=lambda: self.verify_admin(self.open_analytics_panel), **btn_cfg).grid(row=6, column=0, padx=35, pady=6, sticky="ew")
        ctk.CTkButton(self.sidebar, text="System Recovery Backup", fg_color="#8b5cf6", command=lambda: self.verify_admin(self.open_backup_dashboard), **btn_cfg).grid(row=7, column=0, padx=35, pady=6, sticky="ew")
        ctk.CTkButton(self.sidebar, text="Hardware Diagnostics", fg_color="#4f46e5", command=self.open_hw_diagnostics, **btn_cfg).grid(row=8, column=0, padx=35, pady=6, sticky="ew")

        self.mute_var = ctk.BooleanVar(value=False)
        self.mute_switch = ctk.CTkSwitch(self.sidebar, text="Sound On", variable=self.mute_var, command=self.toggle_mute, font=("Segoe UI", 14, "bold"), fg_color="#10b981")
        self.mute_switch.grid(row=9, column=0, padx=35, pady=5, sticky="w")

        ctk.CTkButton(self.sidebar, text="Exit System", fg_color="#dc2626", font=("Segoe UI", 16, "bold"), height=45, command=self.exit_app).grid(row=10, column=0, padx=35, pady=(8, 15), sticky="ew")

        self.main_frame = ctk.CTkFrame(self, fg_color="#1e1e24")
        self.main_frame.grid(row=0, column=1, sticky="nsew", padx=20, pady=20)
        self.main_frame.grid_rowconfigure(0, weight=10)
        self.main_frame.grid_columnconfigure(0, weight=1)

        self.monitor_frame = ctk.CTkFrame(self.main_frame, fg_color="#0a0a0a")
        self.monitor_frame.grid(row=0, column=0, sticky="nsew", padx=10, pady=10)
        self.monitor_frame.grid_propagate(False)

        self.video_label = ctk.CTkLabel(self.monitor_frame, text="[ SECURITY CAMERA OFFLINE ]", font=("Segoe UI", 24, "bold"), text_color="#4b5563")
        self.video_label.pack(fill="both", expand=True)

        self.log_frame = ctk.CTkFrame(self.main_frame)
        self.log_frame.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))

        self.log_box = ctk.CTkTextbox(self.log_frame, font=("Consolas", 14), text_color="#10b981", height=90)
        self.log_box.pack(fill="both", expand=True, padx=10, pady=10)
        self.log_box.configure(state="disabled")

        self.log("SmartEntry2FA PRO Initialized.")
        self.update_summary()
        self.update_clock()
if __name__ == "__main__":
    app = SmartEntryApp()
    app.mainloop()
