"""Destination @username validation (format only - existence cannot be checked through the Bot API)."""
import pytest

from stars_targets import InvalidUsername, normalize_username


@pytest.mark.parametrize("raw,expected", [
    ("@username", "username"), ("username", "username"), ("  @User_Name1 ", "User_Name1"),
    ("https://t.me/some_user", "some_user"), ("t.me/some_user?start=1", "some_user"), ("@abcde", "abcde"),
])
def test_valid(raw, expected):
    assert normalize_username(raw) == expected


@pytest.mark.parametrize("raw", [
    "", "@", "@abcd", "@" + "a" * 33, "@1username", "@_user", "@user_", "@user name", "@user-name", "@کاربر",
    "12345",            # looks like a login code: must be rejected, never stored
    "@user\n@other", "https://example.com/user",
])
def test_invalid(raw):
    with pytest.raises(InvalidUsername):
        normalize_username(raw)


def test_length_limits():
    assert normalize_username("@" + "a" * 32) == "a" * 32
    assert normalize_username("@abcde") == "abcde"
