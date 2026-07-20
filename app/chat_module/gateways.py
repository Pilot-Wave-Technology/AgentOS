"""Gateway registry + routing seam (multi-gateway ready).

The whole point of this module: the rest of the system never hard-codes "the
gateway." It calls `gateway_for(...)` / `assign_gateway(...)`. Today that
resolves to the single configured gateway; adding a VM later is an INSERT into
`gateways` + the placement policy picking it up — no call-site changes.
"""
from __future__ import annotations

import logging
from typing import Optional

from . import repository as repo
from .config import get_chat_config
from .models import Gateway, GatewayStatus

_log = logging.getLogger(__name__)


async def bootstrap_self() -> None:
    """Register THIS process's configured gateway in the registry. Idempotent.
    Call at startup on any service that talks to a gateway. With multiple
    services (main, acp) each registers its own gateway id/url."""
    cfg = get_chat_config()
    await repo.upsert_gateway(
        gateway_id=cfg.gateway_id,
        ws_url=cfg.gateway_ws_url,
        name=cfg.gateway_id,
    )
    _log.info("chat_module: registered gateway %s -> %s", cfg.gateway_id, cfg.gateway_ws_url)


async def gateway_for(agent_id: str) -> Gateway:
    """The gateway hosting `agent_id`. Falls back to the single configured
    gateway when the agent has no explicit binding yet (the common case until
    multi-gateway is turned on)."""
    cfg = get_chat_config()
    gid = await repo.gateway_id_for_agent(agent_id)
    if gid:
        for gw in await repo.list_active_gateways():
            if gw.id == gid:
                return gw
        _log.warning(
            "chat_module: agent %s bound to gateway %s which is not active — "
            "falling back to configured gateway", agent_id, gid,
        )
    return _configured_gateway()


async def assign_gateway(agent_id: str) -> Gateway:
    """Placement policy for a NEW agent: least-loaded active gateway (weighted
    by current_agents/max_agents). Records the binding + bumps the count.
    Falls back to the configured gateway if the registry is empty."""
    actives = await repo.list_active_gateways()
    if not actives:
        gw = _configured_gateway()
    else:
        # list_active_gateways already orders by load then weight.
        gw = next((g for g in actives if g.has_capacity), actives[0])
    try:
        await repo.bind_agent_to_gateway(agent_id=agent_id, gateway_id=gw.id)
    except Exception:
        # Binding row may not exist yet (agent_gateway_bindings is created at
        # provision time). Non-fatal: gateway_for falls back to configured.
        _log.warning("chat_module: could not bind agent %s to %s", agent_id, gw.id,
                     exc_info=True)
    return gw


def _configured_gateway() -> Gateway:
    cfg = get_chat_config()
    return Gateway(
        id=cfg.gateway_id,
        ws_url=cfg.gateway_ws_url,
        status=GatewayStatus.ACTIVE,
    )
