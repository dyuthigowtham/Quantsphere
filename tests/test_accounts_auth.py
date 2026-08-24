from app.services.accounts import _hash_password, verify_password


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
