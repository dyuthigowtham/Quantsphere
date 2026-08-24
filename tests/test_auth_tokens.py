import time

import jwt
import pytest

from app.services.auth_tokens import create_access_token, decode_access_token
from config.settings import settings


def test_create_and_decode_round_trips_to_the_same_user_id():
    token = create_access_token(42)
    assert decode_access_token(token) == 42


def test_decode_rejects_a_tampered_token():
    token = create_access_token(1)
    # Flip a character in the middle of the signature segment, not the very
    # last character — base64url's last character can encode "don't care"
    # padding bits that some decoders ignore, so tampering only the last
    # character can occasionally decode to the same bytes and flakily pass
    # signature verification anyway.
    signature = token.rsplit(".", 1)[1]
    mid = len(signature) // 2
    tampered_char = "A" if signature[mid] != "A" else "B"
    tampered_signature = signature[:mid] + tampered_char + signature[mid + 1 :]
    tampered = token.rsplit(".", 1)[0] + "." + tampered_signature
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(tampered)


def test_decode_rejects_an_expired_token():
    expired_payload = {"sub": "1", "exp": int(time.time()) - 10}
    expired_token = jwt.encode(expired_payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(expired_token)


def test_decode_rejects_a_token_signed_with_a_different_secret():
    wrong_secret_token = jwt.encode({"sub": "1", "exp": int(time.time()) + 60}, "a-completely-different-secret", algorithm=settings.jwt_algorithm)
    with pytest.raises(jwt.PyJWTError):
        decode_access_token(wrong_secret_token)
