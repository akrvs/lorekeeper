"""Small text helpers shared across layers."""


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
