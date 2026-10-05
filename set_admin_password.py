"""Set (or change) the admin password used by SmartEntry2FA.

Usage:  python set_admin_password.py
The password is hashed (PBKDF2-HMAC-SHA256) and only the hash is written to .env.
"""
import getpass
import os
import sys

from config import ENV_PATH, hash_password


def write_env_value(key: str, value: str, path: str = ENV_PATH) -> None:
    """Create or update KEY=value in the .env file, keeping all other lines."""
    lines = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    new_line = f"{key}={value}"
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = new_line
            break
    else:
        lines.append(new_line)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def main() -> int:
    pwd = getpass.getpass("New admin password: ")
    if len(pwd) < 8:
        print("Password must be at least 8 characters.")
        return 1
    if pwd != getpass.getpass("Confirm password: "):
        print("Passwords do not match.")
        return 1
    write_env_value("ADMIN_PASS_HASH", hash_password(pwd))
    print(f"Done. Admin password hash saved to {ENV_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
