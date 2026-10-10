"""Generate and encrypt Windows VPS credentials."""
import os
import secrets
import string
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken
from dotenv import load_dotenv
from django.views.decorators.debug import sensitive_variables


@sensitive_variables()
def _cipher():
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
    key = os.getenv("WINDOWS_PASSWORD_ENCRYPTION_KEY", "").strip()
    try:
        return Fernet(key.encode("ascii"))
    except (ValueError, UnicodeError):
        raise RuntimeError("Windows password encryption key is missing or invalid.") from None


@sensitive_variables()
def generate_encrypted_password():
    cipher = _cipher()
    alphabet = string.ascii_letters + string.digits + "!@#%+-_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(24))
        if (any(c.islower() for c in password)
                and any(c.isupper() for c in password)
                and any(c.isdigit() for c in password)
                and any(c in "!@#%+-_" for c in password)):
            return cipher.encrypt(password.encode("utf-8")).decode("ascii")


@sensitive_variables()
def decrypt_windows_password(encrypted):
    if not encrypted:
        raise RuntimeError("Windows credentials have not been generated.")
    try:
        return _cipher().decrypt(encrypted.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeError):
        raise RuntimeError("Windows credentials could not be decrypted.") from None
