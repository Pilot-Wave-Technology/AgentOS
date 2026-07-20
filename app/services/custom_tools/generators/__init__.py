"""Tool source + manifest generators — wizard outputs become capability-runtime tools.

Each of the four import wizards (HTTP action / OpenAPI / TypeScript / MCP)
ends up calling one of these generators. The generator takes the wizard's
domain inputs and returns three things:

    1. source_code   — TypeScript that defines `async function execute(args)`,
                       to be evaluated inside the capability runtime
    2. manifest      — declared capabilities and limits, enforced by the runtime
    3. parameters_schema — what the agent sees (operation enum + arguments object,
                       or whatever the wizard chose)

Loose coupling rule: nothing outside this package imports a specific generator
module directly. Callers ask `generate(tier, inputs)` and get back a result.
Adding a new wizard = adding a new generator file + registry entry, with no
edits to wizard handlers, CRUD, or the runtime.

The generators do NOT touch the database, do NOT call the runtime, do NOT
read environment. They are pure transforms: inputs in, GeneratedTool out.
"""
from typing import Protocol, TypedDict


class GeneratedTool(TypedDict):
    """What every generator returns. All three fields are required."""
    source_code: str
    manifest: dict
    parameters_schema: dict


class ToolGenerator(Protocol):
    """Generator interface. Each tier has one concrete implementation."""
    def __call__(self, inputs: dict) -> GeneratedTool: ...


# Registry mapping tier names to their generator. Populated below.
# Wizards never index into this directly; they call `generate(tier, inputs)`.
_REGISTRY: dict[str, ToolGenerator] = {}


def register(tier: str, generator: ToolGenerator) -> None:
    """Bind a generator to a tier name. Last call wins (for monkey-patching in tests)."""
    _REGISTRY[tier] = generator


def generate(tier: str, inputs: dict) -> GeneratedTool:
    """Look up and invoke the generator for `tier`. Raises ValueError on unknown tier."""
    gen = _REGISTRY.get(tier)
    if gen is None:
        raise ValueError(f"no generator registered for tier {tier!r} (available: {list(_REGISTRY)})")
    return gen(inputs)


def available_tiers() -> list[str]:
    return list(_REGISTRY)


# ── Bind defaults ───────────────────────────────────────────────────────────
#
# Importing the four tier modules below registers them via the register() call
# at the bottom of each module. Tests can call `register(...)` again to swap.

from . import http_action as _http_action  # noqa: E402, F401
from . import openapi as _openapi          # noqa: E402, F401
from . import mcp_server as _mcp_server    # noqa: E402, F401
from . import typescript as _typescript    # noqa: E402, F401
