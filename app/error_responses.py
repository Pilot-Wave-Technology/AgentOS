"""What a 401 looks like to whoever asked.

THE 405 THAT WAS A 401. Both apps answered every 401 with
`RedirectResponse("/login")`. Starlette's default is 307, which
preserves the method, so a machine caller's POST to `/internal/...`
became a POST to `/login` -- a GET-only route -- and the caller was
handed:

    HTTP 405 Method Not Allowed

The gateway reported `create_story` as "the board API is rejecting the
request", the control panel's log showed 307 then 405, and nothing
anywhere said "your bearer token was refused". Every `/internal/*` call
from the gateway had been failing this way since the environment came
up -- lifecycle webhooks, the model catalog, the per-agent token mint --
and each one reported a different-looking transport problem.

A redirect to a login PAGE is the right answer for a browser and is
never the right answer for a caller that presented a bearer token: it
cannot follow it, cannot read it, and the status it finally sees names
the wrong fault. So the browser keeps the redirect and everyone else
gets the status that is true.
"""
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

# Machine-to-machine prefixes. `/internal/*` is the agent/tool surface by
# convention throughout this app (see app/internal_auth.py and
# kernel/assistant/asgi.py, which mounts the kernel tool surface under
# /internal/kernel precisely so the split is visible to anything
# fronting it).
_MACHINE_PREFIXES = ("/internal/", "/api/")


def wants_html(request: Request) -> bool:
    """Would a redirect to the login page help this caller?

    Only a browser navigation. A machine caller is identified by the
    surface it called, not by its Accept header alone -- `fetch` sends
    `*/*` by default, and a bearer-token caller that gets HTML back has
    been told nothing.
    """
    if request.url.path.startswith(_MACHINE_PREFIXES):
        return False
    if request.headers.get("authorization"):
        return False
    return "text/html" in request.headers.get("accept", "")


def unauthorized_response(request: Request):
    """401 for machines, login page for browsers."""
    if wants_html(request):
        return RedirectResponse(url="/login")
    return JSONResponse(
        {"detail": "Invalid or missing credentials",
         "hint": "This endpoint authenticates with a bearer token, not a "
                 "browser session."},
        status_code=401,
    )
