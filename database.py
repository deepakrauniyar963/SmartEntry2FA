import sqlite3
import threading
import logging
import os

logger = logging.getLogger("SmartEntry2FA.DB")

class DatabaseManager:
    def __init__(self, db_name="smartentry_master.db"):
        self.db_path = os.path.join(os.getcwd(), db_name)
        self.lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        """टेबल्स बनाता है अगर वो पहले से नहीं हैं"""
        try:
            with self.lock:
                conn = sqlite3.connect(self.db_path, check_same_thread=False)
                cur = conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA foreign_keys = ON")

                cur.execute('''CREATE TABLE IF NOT EXISTS Users (
                    id INTEGER PRIMARY KEY,
                    enroll TEXT UNIQUE,
                    first_name TEXT,
                    last_name TEXT,
                    father_name TEXT,
                    department TEXT,
                    course TEXT,
                    mobile TEXT,
                    email TEXT,
                    address TEXT
                )''')

                cur.execute('''CREATE TABLE IF NOT EXISTS Attendance (
                    id INTEGER REFERENCES Users(id) ON DELETE CASCADE,
                    name TEXT,
                    department TEXT,
                    date TEXT,
                    time TEXT,
                    status TEXT
                )''')

                cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_attendance_unique ON Attendance(id, date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_att_date ON Attendance(date)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_att_id ON Attendance(id)")
                cur.execute("CREATE INDEX IF NOT EXISTS idx_users_enroll ON Users(enroll)")

                conn.commit()
                conn.close()
        except Exception as e:
            logger.critical(f"Fatal DB Error: {e}")

    def fetch_all(self, query: str, params: tuple = ()):
        """सारा डेटा लाने के लिए (Select All)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.cursor()
            cur.execute(query, params)
            res = cur.fetchall()
            conn.close()
            return res

    def fetch_one(self, query: str, params: tuple = ()):
        """सिर्फ एक रिकॉर्ड लाने के लिए (Select One)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.cursor()
            cur.execute(query, params)
            res = cur.fetchone()
            conn.close()
            return res

    def execute_many(self, queries_params: list, commit: bool = True):
        """इंसर्ट या डिलीट करने के लिए (Insert/Update/Delete)"""
        with self.lock:
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            cur = conn.cursor()
            try:
                for q, p in queries_params:
                    cur.execute(q, p)
                if commit:
                    conn.commit()
            except Exception as e:
                conn.rollback()
                raise e
            finally:
                conn.close()
