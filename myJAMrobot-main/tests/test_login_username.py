from __future__ import annotations

import pytest

from app.services.lastfm import normalize_login_username


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("username", "username"),
        ("@username", "username"),
        ("last.fm/username", "username"),
        ("last.fm/User_Name-2", "User_Name-2"),
    ],
)
def test_exact_login_forms(value: str, expected: str) -> None:
    assert normalize_login_username(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "https://last.fm/user/username",
        "last.fm/user/username",
        "LAST.FM/username",
        "last.fm/username/extra",
        "@@username",
        "user name",
        "user.name",
        "1username",
        "thisusernameistoolong",
        "a",
        "",
    ],
)
def test_other_login_forms_are_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_login_username(value)
