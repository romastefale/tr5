from __future__ import annotations

CONNECT_HINT_GROUP = "Informe seu usuário da LAST FM com <code>/login username</code>."
CONNECT_HINT_PRIVATE = (
    "<b>Informe seu usuário da LAST FM</b>\n"
    "Use <code>/login username</code>, <code>/login @username</code> ou "
    "<code>/login last.fm/username</code>."
)


def connect_hint_for(chat_type: str | None) -> str:
    return CONNECT_HINT_PRIVATE if chat_type == "private" else CONNECT_HINT_GROUP
