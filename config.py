import os
import sys
import hmac
import hashlib
import secrets
from dotenv import load_dotenv

# Folder that holds the .env file: next to the .exe when frozen, next to this file otherwise
BASE_DIR = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
    else os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.path.join(BASE_DIR, ".env")

load_dotenv(ENV_PATH)


def _ensure_cache_key() -> str:
    """Create a random HMAC key for the face-encoding cache on first run (never ship a default key)."""
    key = os.getenv("CACHE_HMAC_KEY")
    if key:
        return key
    key = secrets.token_hex(32)
    try:
        with open(ENV_PATH, "a", encoding="utf-8") as f:
            f.write(f"\nCACHE_HMAC_KEY={key}\n")
    except OSError:
        pass  # read-only folder: key is used for this session only
    os.environ["CACHE_HMAC_KEY"] = key
    return key


# Email settings (Gmail App Password goes in .env, never in code)
EMAIL_USER = os.getenv("EMAIL_USER", "")
EMAIL_PASS = os.getenv("EMAIL_PASS")

# Security settings
ADMIN_PASS_HASH = os.getenv("ADMIN_PASS_HASH", "__NO_CREDENTIAL__")
LATE_TIME = os.getenv("LATE_TIME", "09:30:00")

# HMAC key used to sign the face-encoding cache (encodings.pkl)
CACHE_KEY = _ensure_cache_key().encode("utf-8")


def hash_password(plain: str) -> str:
    """Hash the admin password with PBKDF2-HMAC-SHA256 (260,000 iterations)."""
    dk = hashlib.pbkdf2_hmac("sha256", plain.encode(), b"SmartEntry2FA_salt_v1", 260000)
    return dk.hex()


def check_admin_password(plain_pwd: str) -> bool:
    """Return True if the given password matches the stored admin hash."""
    if ADMIN_PASS_HASH == "__NO_CREDENTIAL__":
        return False
    return hmac.compare_digest(hash_password(plain_pwd), ADMIN_PASS_HASH)
