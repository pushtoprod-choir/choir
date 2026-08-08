"""Shared text-formatting helpers for messages sent with parse_mode=MARKDOWN
(Telegram's legacy Markdown, not MarkdownV2)."""

_MARKDOWN_SPECIAL_CHARS = ("_", "*", "`", "[")


def escape_markdown(text: str) -> str:
    """Escapes the four characters Telegram's legacy Markdown parse mode
    treats as entity delimiters, so arbitrary text (onboarding answers,
    LLM-generated negotiation text) can be safely interpolated into a
    parse_mode=MARKDOWN message. Without this, an unmatched delimiter in the
    interpolated text (e.g. a single "_" in a user-typed area) makes Telegram
    reject the whole send with telegram.error.BadRequest: "can't find end of
    the entity starting at byte offset ..." — the exact crash this fixes."""
    for char in _MARKDOWN_SPECIAL_CHARS:
        text = text.replace(char, f"\\{char}")
    return text
