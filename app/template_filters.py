"""Shared Jinja2 custom filters.

Usage in any router:
    from app.template_filters import register_filters
    templates = Jinja2Templates(directory="app/templates")
    register_filters(templates.env)
"""
import logging
import re

_log = logging.getLogger(__name__)


def _render_markdown(text: str) -> str:
    """Convert markdown text to safe HTML. Falls back to escaped plain text."""
    if not text:
        return ""
    try:
        import markdown as _md
        return _md.markdown(
            text,
            extensions=["fenced_code", "tables", "nl2br"],
        )
    except Exception as exc:
        _log.warning("markdown render failed: %s", exc)
        import html
        return f"<pre>{html.escape(text)}</pre>"


def _strip_md(text: str, max_chars: int = 300) -> str:
    """Strip markdown syntax to produce plain text, suitable for preview blurbs."""
    if not text:
        return ""
    # Remove fenced code blocks entirely — they're noise in a one-line preview
    text = re.sub(r"```[\s\S]*?```", "[code]", text)
    # Remove inline code
    text = re.sub(r"`[^`\n]+`", "", text)
    # Remove heading markers
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    # Remove bold / italic markers (up to triple asterisk/underscore)
    text = re.sub(r"\*{1,3}([^*\n]+)\*{1,3}", r"\1", text)
    text = re.sub(r"_{1,3}([^_\n]+)_{1,3}", r"\1", text)
    # Collapse [text](url) links to just the text
    text = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", text)
    # Strip any residual HTML tags
    text = re.sub(r"<[^>]+>", "", text)
    # Collapse runs of whitespace / newlines into a single space
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_chars]


def _abbreviate_number(value) -> str:
    """Format a large integer with a K/M/B suffix, e.g. 30876662 -> "30.9M".

    Used for token counts and other magnitudes where the exact value is
    less useful at a glance than the order of magnitude (dashboards/stat
    tiles). Falls back to the plain integer below 1,000.
    """
    try:
        n = int(value)
    except (TypeError, ValueError):
        return str(value)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n < 1_000:
        return f"{sign}{n}"
    for divisor, suffix in ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K")):
        if n >= divisor:
            return f"{sign}{n / divisor:.1f}{suffix}"
    return f"{sign}{n}"  # pragma: no cover - unreachable, divisors cover >=1000


def _tojson_pretty(value, indent: int = 2) -> str:
    """Serialise a Python object to indented JSON for display in <pre> blocks.

    If value is a JSON-encoded string (common when DB columns store JSONB as
    text), it is parsed first so the output is a formatted object rather than
    a string literal with escaped quotes.
    """
    import json
    try:
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                pass  # Not JSON — render as a plain string
        return json.dumps(value, indent=indent, default=str, ensure_ascii=False)
    except Exception as exc:
        _log.warning("tojson_pretty failed: %s", exc)
        return str(value)


def register_filters(env) -> None:
    """Register all custom filters onto a Jinja2 Environment."""
    env.filters["md"] = _render_markdown
    env.filters["strip_md"] = _strip_md
    env.filters["tojson_pretty"] = _tojson_pretty
    env.filters["abbrev_num"] = _abbreviate_number
