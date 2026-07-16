"""
Password hashing helpers. Uses hashlib's built-in PBKDF2-HMAC (no extra
dependency needed) with a random per-user salt, 200k iterations.
"""
import hashlib
import secrets

_ITERATIONS = 200_000


def hash_password(plain_password: str) -> tuple[str, str]:
    """Returns (hashed_password_hex, salt_hex)."""
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return hashed, salt


def verify_password(plain_password: str, hashed_password: str, salt: str) -> bool:
    if not hashed_password or not salt:
        return False
    candidate = hashlib.pbkdf2_hmac(
        "sha256", plain_password.encode("utf-8"), bytes.fromhex(salt), _ITERATIONS
    ).hex()
    return secrets.compare_digest(candidate, hashed_password)
