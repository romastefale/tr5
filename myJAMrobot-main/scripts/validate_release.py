from __future__ import annotations

import ast
import asyncio
import ipaddress
import re
import sys
from collections import Counter
from pathlib import Path
from types import CodeType
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
EXPECTED = {"start", "help", "login", "playing", "canvas", "story", "radio", "lyrics", "onoff"}
REMOVED_MODULES = {
    "monthfm.py",
    "weekfm.py",
    "tnow.py",
    "tcanvas.py",
    "tstory.py",
    "tly.py",
    "radiofm.py",
    "myself.py",
    "songcharts.py",
    "music_groups.py",
}


def _module_subset(path: Path, names: set[str]) -> CodeType:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    nodes: list[ast.stmt] = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            target_names = {target.id for target in targets if isinstance(target, ast.Name)}
            if target_names & names:
                nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            nodes.append(node)
    return compile(ast.Module(body=nodes, type_ignores=[]), str(path), "exec")


def _registered_commands() -> list[str]:
    found: list[str] = []
    for path in (ROOT / "app" / "bot").glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "Command":
                continue
            for argument in node.args:
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    found.append(argument.value)
    return found


def _menu_commands() -> list[str]:
    path = ROOT / "app" / "bot" / "setup_commands.py"
    found: list[str] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if not isinstance(node, ast.Call) or getattr(node.func, "id", None) != "CommandDef":
            continue
        if node.args and isinstance(node.args[0], ast.Constant):
            found.append(str(node.args[0].value))
    return found


def _routes() -> set[tuple[str, str]]:
    tree = ast.parse((ROOT / "app" / "main.py").read_text(encoding="utf-8"))
    routes: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if not isinstance(decorator.func.value, ast.Name) or decorator.func.value.id != "app":
                continue
            if decorator.func.attr not in {"get", "post", "put", "patch", "delete"} or not decorator.args:
                continue
            if isinstance(decorator.args[0], ast.Constant):
                routes.add((decorator.func.attr, str(decorator.args[0].value)))
    return routes


def validate_contract() -> None:
    registered = _registered_commands()
    assert set(registered) == EXPECTED and len(registered) == len(EXPECTED)
    menu = _menu_commands()
    assert set(menu) == EXPECTED and len(menu) == len(EXPECTED)
    assert _routes() == {("get", "/healthz"), ("get", "/readyz"), ("post", "/webhook")}
    assert not (ROOT / "app" / "web_music").exists()
    assert not (ROOT / "app" / "models" / "spotify_token.py").exists()
    app_source = "\n".join(path.read_text(encoding="utf-8") for path in (ROOT / "app").rglob("*.py"))
    assert "inline_query" not in app_source and "web_app" not in app_source.casefold()
    for name in REMOVED_MODULES:
        assert not (ROOT / "app" / "bot" / name).exists(), name
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "aiogram==3.30.0" in requirements
    assert "pytest" not in requirements
    rich = (ROOT / "app" / "bot" / "presentation.py").read_text(encoding="utf-8")
    assert all(token in rich for token in ("InputRichMessage", "InputRichMessageMedia", "send_rich_message", "rich_message=rich"))


def validate_local_imports() -> None:
    missing: list[str] = []
    for path in (ROOT / "app").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom) or not node.module or not node.module.startswith("app."):
                continue
            relative = Path(*node.module.split("."))
            if not (ROOT / relative.with_suffix(".py")).exists() and not (ROOT / relative / "__init__.py").exists():
                missing.append(f"{path.relative_to(ROOT)} -> {node.module}")
    assert not missing, missing


def validate_login_parser() -> None:
    namespace = {"re": re}
    names = {"_USERNAME_PATTERN", "_USERNAME_RE", "_PREFIX_RE", "normalize_login_username"}
    exec(_module_subset(ROOT / "app" / "services" / "lastfm.py", names), namespace)
    normalize = namespace["normalize_login_username"]
    for value, expected in (("username", "username"), ("@username", "username"), ("last.fm/username", "username")):
        assert normalize(value) == expected
    for value in (
        "LAST.FM/username",
        "last.fm/user/username",
        "https://last.fm/username",
        "@@username",
        "user name",
        "user.name",
        "1username",
        "thisusernameistoolong",
        "a",
        "",
    ):
        try:
            normalize(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"login form should be rejected: {value!r}")


def validate_lyrics_excerpt() -> None:
    namespace = {"re": re, "Counter": Counter}
    names = {
        "MAX_EXCERPT_WORDS",
        "_SECTION_RE",
        "_ANY_SECTION_RE",
        "_WORD_RE",
        "_line_key",
        "_limit_words",
        "bound_excerpt_text",
        "select_lyric_excerpt",
        "extract_chorus_excerpt",
    }
    exec(_module_subset(ROOT / "app" / "services" / "lyrics.py", names), namespace)
    extract = namespace["extract_chorus_excerpt"]
    word_re = namespace["_WORD_RE"]
    explicit = extract("[Verse]\nNot this line\n[Chorus]\nWe sing this bright refrain together every single night again\n[Verse]\nEnd")
    assert explicit and explicit.startswith("We sing this bright refrain")
    assert len(word_re.findall(explicit)) <= 10
    assert namespace["select_lyric_excerpt"]("[Chorus]\nSing this again")[1] == "chorus"
    huge = extract(" ".join(f"word{index}" for index in range(500)))
    assert huge and len(word_re.findall(huge)) == 10
    assert namespace["select_lyric_excerpt"](" ".join(f"word{index}" for index in range(500)))[1] == "excerpt"
    bounded = namespace["bound_excerpt_text"](" ".join(f"cached{index}" for index in range(40)))
    assert bounded and len(word_re.findall(bounded)) == 10

    cache_namespace = {"re": re}
    cache_names = {"_WS_RE", "_SNIPPET_WORD_RE", "_MAX_SNIPPET_WORDS", "_bounded_snippet"}
    exec(_module_subset(ROOT / "app" / "services" / "lyrics_cache.py", cache_names), cache_namespace)
    persisted = cache_namespace["_bounded_snippet"](" ".join(f"stored{index}" for index in range(40)))
    assert persisted and len(word_re.findall(persisted)) == 10


def validate_media_policy() -> None:
    namespace = {"re": re, "ipaddress": ipaddress, "urlsplit": urlsplit}
    names = {"_HOSTS_BY_KIND", "_SEARCH_CONTROL_RE", "sanitize_search_term", "_allowed_host", "validate_media_url"}
    exec(_module_subset(ROOT / "app" / "security" / "media.py", names), namespace)
    validate = namespace["validate_media_url"]
    sanitize = namespace["sanitize_search_term"]
    assert validate("https://cdn-images.dzcdn.net/images/cover/a.jpg", kind="cover")
    assert validate("https://canvaz.scdn.co/upload/a.mp4", kind="canvas")
    assert validate("https://127.0.0.1/a.jpg", kind="cover") is None
    assert validate("https://cdn-images.dzcdn.net.evil.test/a.jpg", kind="cover") is None
    assert validate("https://user:pass@i.scdn.co/a.jpg", kind="cover") is None
    assert validate("https://i.scdn.co/image/a\nb", kind="cover") is None
    assert validate("https://i.scdn.co:8443/image/a", kind="cover") is None
    clean = sanitize("song\n\x00artist" + "x" * 500)
    assert "\n" not in clean and "\x00" not in clean and len(clean) <= 120


def validate_cover_selection() -> None:
    namespace: dict[str, object] = {}
    exec(_module_subset(ROOT / "app" / "services" / "spotify.py", {"_largest_image"}), namespace)
    selected = namespace["_largest_image"](
        [
            {"url": "small", "width": 64, "height": 64},
            {"url": "largest", "width": 1000, "height": 1000},
            {"url": "medium", "width": 640, "height": 640},
        ]
    )
    assert selected == ("largest", 1000, 1000)
    story_card = (ROOT / "app" / "services" / "story_card.py").read_text(encoding="utf-8")
    story_video = (ROOT / "app" / "services" / "story_video.py").read_text(encoding="utf-8")
    canvas_audio = (ROOT / "app" / "services" / "canvas_audio.py").read_text(encoding="utf-8")
    assert "max_dim: int = 2048" in story_card
    assert "STORY_W = 1080" in story_video and "STORY_H = 1920" in story_video
    assert "if not (CANVAS_CACHE_ENABLED and CANVAS_CACHE_CHANNEL_ID)" not in canvas_audio
    assert "audio_canvas_bytes if want_bytes or not file_id else None" in canvas_audio


async def validate_bounded_background_work() -> None:
    from app.security.work_limits import BackgroundTaskPool

    pool = BackgroundTaskPool(2)
    release = asyncio.Event()
    started: list[str] = []

    async def worker(name: str) -> None:
        started.append(name)
        await release.wait()

    assert pool.submit("first", lambda: worker("first"))
    assert pool.submit("second", lambda: worker("second"))
    assert not pool.submit("third", lambda: worker("third"))
    assert not pool.submit("first", lambda: worker("duplicate"))
    await asyncio.sleep(0)
    assert pool.active == 2 and set(started) == {"first", "second"}
    release.set()
    for _ in range(10):
        await asyncio.sleep(0)
        if pool.active == 0:
            break
    assert pool.active == 0
    assert pool.submit("third", lambda: worker("third"))
    await asyncio.sleep(0)
    assert "third" in started
    await pool.shutdown()

    lyrics_handler = (ROOT / "app" / "bot" / "lyrics.py").read_text(encoding="utf-8")
    assert "lyrics_task_pool.submit" in lyrics_handler
    assert "asyncio.create_task" not in lyrics_handler


def validate_webhook_bound() -> None:
    source = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    assert "async for chunk in request.stream()" in source
    assert "received > MAX_WEBHOOK_BYTES" in source
    assert "await request.body()" not in source
    railway = (ROOT / "railway.toml").read_text(encoding="utf-8")
    bootstrap = (ROOT / "app" / "bootstrap.py").read_text(encoding="utf-8")
    assert 'healthcheckPath = "/readyz"' in railway
    assert 'forwarded_allow_ips="*"' not in bootstrap
    database = (ROOT / "app" / "db" / "database.py").read_text(encoding="utf-8")
    settings = (ROOT / "app" / "config" / "settings.py").read_text(encoding="utf-8")
    assert "DROP TABLE" not in database.upper() and "drop_all" not in database
    assert "chmod(0o600)" in database
    assert "refusing an implicit database fallback" in settings


def validate_log_redaction() -> None:
    from app.logging_safety import redact_secrets

    raw = (
        "Authorization: Bearer abcdefghijklmnop access_token=secretvalue "
        "{'Authorization': 'Basic dXNlcjpwYXNzd29yZA==', 'client_secret': 'anothersecret'}"
    )
    redacted = redact_secrets(raw)
    for secret in ("abcdefghijklmnop", "secretvalue", "dXNlcjpwYXNzd29yZA==", "anothersecret"):
        assert secret not in redacted


def main() -> None:
    validate_contract()
    validate_local_imports()
    validate_login_parser()
    validate_lyrics_excerpt()
    validate_media_policy()
    validate_cover_selection()
    validate_webhook_bound()
    validate_log_redaction()
    asyncio.run(validate_bounded_background_work())
    print("release validation: PASS")


if __name__ == "__main__":
    main()
