from __future__ import annotations

from app.logging_safety import redact_secrets


def test_tokens_and_query_secrets_are_redacted() -> None:
    value = redact_secrets(
        "Authorization: Bearer abcdefghijklmnop access_token=secretvalue "
        "{'Authorization': 'Basic dXNlcjpwYXNzd29yZA==', 'client_secret': 'anothersecret'}"
    )
    assert "abcdefghijklmnop" not in value
    assert "secretvalue" not in value
    assert "dXNlcjpwYXNzd29yZA==" not in value
    assert "anothersecret" not in value
