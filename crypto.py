import os
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
_f = Fernet(os.environ["ENCRYPTION_KEY"].encode())


def encrypt(text: str) -> str:
    return _f.encrypt(text.encode()).decode()


def decrypt(text: str) -> str:
    return _f.decrypt(text.encode()).decode()