"""Small text helpers shared across layers."""

import re
from html import unescape

_BLOCK_END = re.compile(r"(?i)<(?:/p|br\s*/?|/h[1-6]|/li|/tr|/div)>")
_TAG = re.compile(r"<[^>]+>")


def strip_html(markup: str | None) -> str:
    """Tiny HTML/XML to text: block ends become newlines, tags go, entities decode.

    Good enough for Confluence storage bodies and HTML-only emails; not a
    browser. The original markup is always preserved in raw_documents.
    """
    if not markup:
        return ""
    text = _TAG.sub(" ", _BLOCK_END.sub("\n", markup))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in unescape(text).splitlines()]
    return "\n".join(line for line in lines if line).strip()


def clip_text(text: str | None, max_chars: int, marker: str = "\n…[truncated]…") -> str:
    """Truncate `text` to ~max_chars, preferring a newline boundary near the end.

    Used to keep document payloads under the LLM's context/token budget before
    extraction. The full text is still preserved verbatim in `raw_documents`;
    only the copy sent to the model is clipped.
    """
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    budget = max_chars - len(marker)
    if budget <= 0:
        return text[:max_chars]
    cut = text.rfind("\n", 0, budget)
    if cut < int(budget * 0.6):  # no convenient newline -> hard cut
        cut = budget
    return text[:cut] + marker
