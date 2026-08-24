from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from config.settings import settings

_ENV_PATH = Path(".env")


def _persist_key_to_env(key: str) -> None:
    """
    Purpose:    Append a freshly generated MT5 encryption key to .env so
                previously-encrypted passwords stay decryptable across restarts.
    Args:       key (str): The Fernet key to persist.
    Returns:    None.
    Raises:     None. Silently skipped if .env doesn't exist (e.g. under tests) —
                the key still works for the lifetime of the current process.
    """
    if not _ENV_PATH.exists():
        return
    with _ENV_PATH.open("a", encoding="utf-8") as f:
        f.write(f"\nMT5_ENCRYPTION_KEY={key}\n")


def _get_fernet() -> Fernet:
    """
    Purpose:    Lazily provision the symmetric key used to encrypt MT5 account
                passwords at rest, generating and persisting one on first use.
    Args:       None.
    Returns:    Fernet: Keyed cipher for encrypt_password/decrypt_password.
    Raises:     None.
    """
    if not settings.mt5_encryption_key:
        key = Fernet.generate_key().decode("ascii")
        settings.mt5_encryption_key = key
        _persist_key_to_env(key)
    return Fernet(settings.mt5_encryption_key.encode("ascii"))


def encrypt_password(plaintext: str) -> str:
    """
    Purpose:    Encrypt an MT5 account password before storing it in the database.
    Args:       plaintext (str): The raw password as submitted by the user.
    Returns:    str: Encrypted, ASCII-safe ciphertext suitable for a text column.
    Raises:     None.
    """
    return _get_fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password(ciphertext: str) -> str:
    """
    Purpose:    Recover the plaintext MT5 account password to authenticate
                against the local terminal.
    Args:       ciphertext (str): Value previously produced by encrypt_password.
    Returns:    str: The original plaintext password.
    Raises:     ValueError: If the ciphertext is invalid or was encrypted with
                    a different key (e.g. MT5_ENCRYPTION_KEY was lost/rotated).
    """
    try:
        return _get_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt stored MT5 password — encryption key mismatch") from exc
