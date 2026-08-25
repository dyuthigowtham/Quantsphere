from app.services.accounts import _hash_password, _hash_token, verify_password


def test_hash_and_verify_round_trip_with_correct_password():
    hashed = _hash_password("correct-horse-battery-staple")
    assert verify_password("correct-horse-battery-staple", hashed) is True


def test_verify_fails_on_wrong_password():
    hashed = _hash_password("correct-horse-battery-staple")
    assert verify_password("wrong-password", hashed) is False


def test_hash_is_salted_so_two_hashes_of_the_same_password_differ():
    first = _hash_password("same-password")
    second = _hash_password("same-password")
    assert first != second
    assert verify_password("same-password", first) is True
    assert verify_password("same-password", second) is True


def test_hash_token_is_deterministic_for_lookups():
    # Unlike passwords, reset tokens are hashed with a fast, deterministic
    # digest (not bcrypt) precisely so a DB lookup by hash works at all.
    token = "some-random-reset-token"
    assert _hash_token(token) == _hash_token(token)


def test_hash_token_differs_for_different_tokens():
    assert _hash_token("token-a") != _hash_token("token-b")
