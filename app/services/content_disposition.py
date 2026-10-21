"""A `Content-Disposition` header that survives a non-ASCII filename.

WHY THIS EXISTS. HTTP headers are latin-1. A filename with an em dash --
"Calculator Test Summary — Report.md", which is what an LLM writing a
report title produces without being asked -- cannot be encoded, and
Starlette raises `UnicodeEncodeError` while building the response. The
request dies as a 500 with a traceback about codecs, several frames away
from anything that mentions a filename.

Seen live: `GET /goals/ERR10/resources/art_a60f320cfa72/content` ->
500, `'latin-1' codec can't encode character '\u2014' in position 47`.

THE BUG WAS OLDER THAN THE BUTTON. The same header was already built for
the inline viewer; there was simply no way to ask for a file whose name
had one, so nothing ever hit it. Adding a download button made an
existing fault reachable, which is the usual way these surface.

RFC 6266 SAYS TO SEND BOTH FORMS. `filename=` carries an ASCII
approximation for anything old or strict, and `filename*=UTF-8''...`
carries the real name percent-encoded. Every current browser prefers the
second; nothing is lost by sending both, and sending only the first
would quietly mangle every non-English name.
"""

from __future__ import annotations

from urllib.parse import quote


def content_disposition(disposition: str, filename: str) -> str:
    """`attachment`/`inline` plus a filename that cannot break the header."""
    name = (filename or "download").strip() or "download"

    # THE ASCII FALLBACK. `errors="replace"` rather than dropping the
    # character, so "Report — v2.md" becomes "Report ? v2.md" and keeps
    # its shape instead of silently becoming "Report  v2.md". Quotes and
    # backslashes come out too: they would end the quoted string early,
    # which is a header-injection shape as well as a broken name.
    ascii_name = (name.encode("ascii", "replace").decode("ascii")
                  .replace("\\", "_").replace('"', "'"))
    # Control characters cannot appear in a header value at all.
    ascii_name = "".join(c for c in ascii_name if 32 <= ord(c) < 127) or "download"

    return (f'{disposition}; filename="{ascii_name}"; '
            f"filename*=UTF-8''{quote(name, safe='')}")
