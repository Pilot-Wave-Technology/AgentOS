"""Re-export of kernel.gateway.surface.

THE MODULE MOVED, AND WHY. This comparison — does the gateway offer the
model exactly the tools the kernel serves? — lived here, in app/. Two E0
suites need it (H10, the tool surface over HTTP; H13, a person can ask)
and neither could import it, because E0's container ships kernel/ and
deliberately not app/. Both died with ModuleNotFoundError and were left
out of the gate, which is precisely the silence that module exists to
break: the fourth declaration going unchecked is how five tools came to
be described in prompts the models could never call.

Nothing in it was app-shaped. It reads kernel.go.tools and
kernel.assistant and imports nothing from app/, so the move is the layer
being corrected rather than a convenience.

This file stays so `from app.services.gateway_surface import ...` keeps
working for callers on the app side, of which there are two tests. It is
a re-export and must never grow logic — a second implementation of this
comparison is the fifth declaration the original set out to prevent.
"""

from __future__ import annotations

from kernel.gateway.surface import (  # noqa: F401
    FILES,
    compare,
    declared,
    served,
    specs_dir,
)

__all__ = ["FILES", "compare", "declared", "served", "specs_dir"]
