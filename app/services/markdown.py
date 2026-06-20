from __future__ import annotations

import markdown as _markdown
import nh3


def render_markdown(text: str | None) -> str:
    if not text:
        return ""
    html = _markdown.markdown(text, extensions=["extra", "sane_lists", "nl2br"])
    # nh3's default allowlist permits common formatting but strips scripts, event
    # handlers, and unknown/unsafe tags & attributes.
    return nh3.clean(html)
