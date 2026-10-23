import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

_fernet = None


def _get() -> Fernet:
    """Created on first use, so importing this module works at build time (no env vars there)."""
    global _fernet
    if _fernet is None:
        key = os.environ.get("ENCRYPTION_KEY")
        if not key:
            raise RuntimeError("ENCRYPTION_KEY is not set in the environment.")
        _fernet = Fernet(key.strip().encode())
    return _fernet


def encrypt(text: str) -> str:
    return _get().encrypt(text.encode()).decode()


def decrypt(text: str) -> str:
    return _get().decrypt(text.encode()).decode()