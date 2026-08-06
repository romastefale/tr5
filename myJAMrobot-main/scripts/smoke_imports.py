from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.bot.canvas import router as canvas_router
from app.bot.lyrics import router as lyrics_router
from app.bot.ops_control import router as onoff_router
from app.bot.radio import router as radio_router
from app.bot.setup_commands import command_scope_summary
from app.bot.story import router as story_router
from app.bot.telegram import bot_dispatcher
from app.security.rate_limit import check_command_rate_limit, reset_rate_limits

EXPECTED = {"start", "help", "login", "playing", "canvas", "story", "radio", "lyrics", "onoff"}


def main() -> None:
    assert bot_dispatcher is not None
    for router in [canvas_router, story_router, radio_router, lyrics_router, onoff_router]:
        assert router is not None
    scopes = command_scope_summary()
    assert set(scopes["all"]) == EXPECTED
    assert set(scopes["public"]) == EXPECTED - {"onoff"}
    assert scopes["owner_only"] == ["onoff"]
    reset_rate_limits()
    assert check_command_rate_limit("playing", 1, 1).allowed
    print("nine-command smoke ok")


if __name__ == "__main__":
    main()
