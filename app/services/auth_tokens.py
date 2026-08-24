from datetime import datetime, timedelta, timezone

import jwt

from config.settings import settings


def create_access_token(user_id: int) -> str:
    """
    Purpose:    Issue a signed, time-limited bearer token for a logged-in user.
    Args:       user_id (int): The authenticated user's id.
    Returns:    str: The encoded JWT.
    Raises:     None.
    """
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=settings.jwt_expire_minutes)
    payload = {"sub": str(user_id), "exp": expires_at}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> int:
    """
    Purpose:    Validate a bearer token and recover the user id it was
                issued for.
    Args:       token (str): The raw bearer token from the Authorization header.
    Returns:    int: The user id.
    Raises:     jwt.PyJWTError: If the token is malformed, tampered with, or expired.
    """
    payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
    return int(payload["sub"])
