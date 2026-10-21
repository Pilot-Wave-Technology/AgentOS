"""How one tool is declared — the shape every family module fills in.

ONE DECLARATION PER TOOL. `side` and `effect` used to live somewhere else:
in `SANDBOX_TOOLS` / `WORKER_TOOLS` in `executor.py`, and in `_CONTAINED` /
`_REVERSIBLE` / `_BY_ARGUMENT` in `effects.py`. Three module-level literals
keyed by tool name, in three files, none of which knew when another was
incomplete.

The effect one was forgotten FIVE times. Each omission classified the tool
IRREVERSIBLE and so denied it at chain stage 3, on every profile offering
it — a failure that surfaces a long way from the mistake and reads as a
policy decision rather than a gap. `goal_knowledge` spent its whole life
that way, denied on all three profile surfaces. The routing one was
forgotten in the other direction: `web_fetch` had an entry in
`WORKER_TOOLS` and no function behind it, so every call came back
`E_TOOL_RAISED: 'web_fetch' raised AttributeError`.

Declaring all three together is what makes half a tool impossible to
write. There is no state in which a model can see a tool that has no
class, because the same call produces both.
"""
from __future__ import annotations

_S = "string"
_I = "integer"
_B = "boolean"

# WHERE A TOOL RUNS, and it is a closed set.
#   sandbox — over the bridge, in the disposable container
#   worker  — in the worker process, which holds the database, the git
#             workspace and the profile, and must therefore never be
#             reachable from the model
#   loop    — intercepted by the harness before it reaches an executor at
#             all, which today is only `propose_completion`
SIDES = ("sandbox", "worker", "loop")

# HOW FAR A CALL REACHES. `by_argument` means the class depends on what it
# was called with — a shell, essentially — and `effects.py` decides.
EFFECTS = ("contained", "reversible", "by_argument")


def _schema(name: str, description: str, props: dict, required: list[str],
            *, side: str, effect: str = "") -> dict:
    """One tool, declared once.

    Both fields are validated here rather than at first call, so a typo is
    an import error naming the tool rather than a stage-3 denial weeks
    later. An empty `effect` is reserved for PENDING tools, which have no
    implementation to classify.
    """
    if side not in SIDES:
        raise ValueError(
            f"tool {name!r}: side must be one of {SIDES}, got {side!r}")
    if effect and effect not in EFFECTS:
        raise ValueError(
            f"tool {name!r}: effect must be one of {EFFECTS}, got {effect!r}")
    return {
        "name": name,
        "description": description,
        "parameters": {
            "type": "object",
            "properties": props,
            "required": required,
        },
        # Underscored: these are OURS, not the provider's. `schemas_for`
        # strips them before anything reaches an adapter, because a key
        # the wire protocol has never heard of comes back as a 400 naming
        # a field the author never wrote.
        "_side": side,
        "_effect": effect,
    }
