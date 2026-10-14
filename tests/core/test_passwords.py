"""Password hashing and the password rules (TASK-023, AUTH-S3, AUTH-S4)."""

import pytest

from app.core import security
from app.core.errors import BusinessRuleError
from app.core.security import check_password_rules, hash_password, verify_password


def test_a_hash_is_bcrypt_and_never_the_password_itself():
    password_hash = hash_password("river-stone-42")

    assert password_hash.startswith("$2b$")
    assert "river-stone-42" not in password_hash


def test_the_same_password_gives_a_different_hash_each_time():
    # a random salt, so two people with the same password do not share a hash
    assert hash_password("river-stone-42") != hash_password("river-stone-42")


def test_the_right_password_verifies():
    assert verify_password("river-stone-42", hash_password("river-stone-42")) is True


def test_a_wrong_password_does_not_verify():
    password_hash = hash_password("river-stone-42")

    assert verify_password("river-stone-43", password_hash) is False
    assert verify_password("RIVER-STONE-42", password_hash) is False
    assert verify_password("", password_hash) is False


def test_production_hashes_use_twelve_rounds():
    # the tests swap in a faster context (see conftest.py); the real one must stay at 12
    assert security.PASSWORD_HASH_ROUNDS == 12


def test_a_hash_made_with_fewer_rounds_still_verifies():
    # a hash stores its own rounds, so changing the setting never locks anyone out
    slow_hash = security.CryptContext(schemes=["bcrypt"], bcrypt__rounds=5).hash("river-stone-42")

    assert verify_password("river-stone-42", slow_hash) is True


def test_a_good_password_passes_the_rules():
    check_password_rules("river-stone-42", own_phone="254722123456")


def test_exactly_eight_characters_is_enough():
    check_password_rules("abcd1234")


@pytest.mark.parametrize("too_short", ["", "a", "abc1234"])
def test_a_password_under_eight_characters_is_refused(too_short):
    with pytest.raises(BusinessRuleError, match="at least 8 characters") as raised:
        check_password_rules(too_short)

    assert raised.value.field == "password"
    assert raised.value.status_code == 422


def test_seventy_two_bytes_is_allowed_and_seventy_three_is_too_long():
    check_password_rules("a1" * 36)

    with pytest.raises(BusinessRuleError, match="too long"):
        check_password_rules("a1" * 36 + "b")


def test_the_limit_counts_bytes_not_letters():
    # 36 letters that take 2 bytes each in UTF-8 make 72 bytes; one more is 74
    check_password_rules("é" * 35 + "ab")
    with pytest.raises(BusinessRuleError, match="too long"):
        check_password_rules("é" * 37)


@pytest.mark.parametrize("repeated", ["aaaaaaaa", "11111111", "        "])
def test_one_character_repeated_is_refused(repeated):
    with pytest.raises(BusinessRuleError, match="one character repeated"):
        check_password_rules(repeated)


@pytest.mark.parametrize("obvious", ["password", "Password", "PASSWORD", " password "])
def test_the_word_password_is_refused(obvious):
    with pytest.raises(BusinessRuleError, match="too easy to guess"):
        check_password_rules(obvious)


@pytest.mark.parametrize("phone_as_typed", ["0722123456", "254722123456", "+254722123456", "0722 123 456", "0722-123-456"])
def test_the_persons_own_phone_number_is_refused(phone_as_typed):
    with pytest.raises(BusinessRuleError, match="phone number"):
        check_password_rules(phone_as_typed, own_phone="254722123456")


def test_someone_elses_number_or_a_password_with_digits_in_it_is_fine():
    check_password_rules("0733987654", own_phone="254722123456")
    check_password_rules("my0722123456x", own_phone="254722123456")


def test_the_error_can_point_at_another_field():
    with pytest.raises(BusinessRuleError) as raised:
        check_password_rules("short", field="new_password")

    assert raised.value.field == "new_password"
