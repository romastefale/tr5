from __future__ import annotations

import ast
from pathlib import Path

from app.bot.setup_commands import command_scope_summary

ROOT = Path(__file__).resolve().parents[1]
EXPECTED = {"start", "help", "login", "playing", "canvas", "story", "radio", "lyrics", "onoff"}


def test_command_menu_contract_is_exact() -> None:
    summary = command_scope_summary()
    assert set(summary["all"]) == EXPECTED
    assert set(summary["public"]) == EXPECTED - {"onoff"}
    assert summary["owner_only"] == ["onoff"]


def test_registered_command_filters_are_exact() -> None:
    found: list[str] = []
    for path in (ROOT / "app" / "bot").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = getattr(node.func, "id", None)
            if name != "Command":
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    found.append(argument.value)
    assert set(found) == EXPECTED
    assert len(found) == len(EXPECTED)


def test_removed_surfaces_are_absent() -> None:
    assert not (ROOT / "app" / "web_music" / "router.py").exists()
    assert not (ROOT / "app" / "models" / "spotify_token.py").exists()
    source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py"))
    assert '"/callback"' not in source
    assert "record_seen_update_payload" not in source
    assert "inline_query" not in source
    assert "web_app" not in source.casefold()
