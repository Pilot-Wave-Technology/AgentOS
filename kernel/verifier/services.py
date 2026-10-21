"""F2d — the services a criterion needs, declared in the contract.

F2's Verifier gives a criterion one container, the committed tree and no
network. That verifies a pure-Python repository and nothing else: a real
suite needs a database listening, and F5's `env_install` reproduces
*packages*, not *a running service*.

    workspace_spec.services = [
      {"name": "db",
       "image": "postgres:17-alpine",
       "env": {"POSTGRES_PASSWORD": "verify"},
       "ready": {"command": "pg_isready -U postgres", "timeout": 60}}
    ]

DECLARED, NOT DISCOVERED. The criterion does not get whatever happens to
be running on the host — it gets exactly what the contract named, which
is the same rule as the command coming from the contract rather than the
repo. A criterion that passes because something was already listening is
a criterion that will fail on a machine where it is not.

AN **INTERNAL** NETWORK. Docker's `Internal: true` permits
container-to-container traffic on the network and denies everything
outside it. So the criterion reaches `db:5432` by name through the
embedded DNS, and still cannot fetch a version of itself that passes or
exfiltrate the tree it was given — the defence F2 got from having no
network at all, kept while adding the one thing it was blocking.

FRESH PER VERIFICATION. A service reused between runs carries state, and
a criterion that passes on the second run because the first left a row
behind is worse than one that fails: it passes for a reason nobody can
see.
"""

from __future__ import annotations

import asyncio
import logging

from kernel.spec import ExecutionSpec

logger = logging.getLogger(__name__)

READY_POLL_S = 0.5
DEFAULT_READY_TIMEOUT_S = 60.0


class ServiceError(Exception):
    """A declared service could not be brought up."""


def declared(contract: dict) -> list[dict]:
    """The services this contract's criteria run against."""
    spec = contract.get("workspace_spec") or {}
    services = spec.get("services") or []
    return [s for s in services if s.get("name") and s.get("image")]


class Services:
    """The service graph for ONE verification. Torn down whatever happens.

    Not a pool and not a cache. Each verification gets its own network
    and its own containers, so two verifications of the same contract
    cannot see each other's data — which is the difference between a
    verdict about this commit and a verdict about the order the queue
    happened to run in.
    """

    def __init__(self, driver, verification_id: str):
        self._driver = driver
        self._id = verification_id
        self.network: str | None = None
        self._containers: list[str] = []
        self.alias_of: dict[str, str] = {}
        # SIDECARS, when the substrate has no networks to make. Empty on
        # Docker and populated on Kubernetes; the criterion's spec
        # carries them into its own pod instead of reaching a peer by
        # name across a network. See Driver.SERVICE_MODEL.
        self.sidecars: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.teardown()
        return False

    async def up(self, services: list[dict]) -> None:
        """Make the declared services reachable, however this substrate can.

        TWO SHAPES, ONE GUARANTEE. The guarantee is what the module
        docstring states and does not change: the criterion gets exactly
        what the contract named, fresh, and cannot reach anything else.

          network   a private internal network with the services on it,
                    reached by name. Docker.
          sidecar   the services go in the criterion's OWN pod and answer
                    on localhost, because a pod's namespace already is a
                    private network. Kubernetes.

        The second is strictly stronger: there is no network object to
        leak, to name-collide, or to misconfigure into admitting a second
        criterion, and nothing to tear down if the process dies.
        """
        if not services:
            return

        # NO PROXY VARIABLES ON ANY OF THIS, DELIBERATELY — the same
        # question `prepare` and `build_deps` just answered the other
        # way, answered here as no.
        #
        # Those two exist to FETCH and are labelled `egress: proxy`. A
        # declared service exists to LISTEN, and gets its bytes from an
        # image the kubelet pulled before it started. Neither shape has
        # a route to put a proxy on: on Docker the service sits on an
        # `internal=True` network, which by definition has no egress,
        # and on Kubernetes it is a sidecar in the CRITERION's pod,
        # which carries `egress: store` and reaches 3129 alone. Handing
        # a proxy address to a container inside a criterion's own pod
        # would be exactly the containment regression F2 splits the
        # ports to prevent: the criterion could reach the internet
        # through its own database.
        #
        # AND THERE IS NO HANG TO FIX HERE. The executor's defect was an
        # unbounded wait; `_await_ready` below is bounded twice over, by
        # the declared readiness timeout and by 20s per probe, so a
        # service that can reach nothing fails its deadline and says so.
        if getattr(self._driver, "SERVICE_MODEL", "network") == "sidecar":
            from kernel.drivers.k8s_job import service_sidecar

            self.sidecars = [
                service_sidecar(s["name"], s["image"],
                                env=dict(s.get("env") or {}),
                                memory_mb=int(s.get("memory_mb") or 512))
                for s in services]
            # NO READINESS LOOP HERE, and that is not an omission. A
            # sidecar starts with the criterion rather than before it, so
            # there is no window in which to poll — the criterion's own
            # command has to tolerate a service that is still coming up,
            # which is what a `ready` check in the contract describes and
            # what any real test suite does anyway.
            logger.info("verification %s: %d service(s) as sidecars",
                        self._id, len(self.sidecars))
            return

        self.network = await self._driver.create_network(
            f"verify-{self._id}", internal=True)
        logger.info("verification %s: internal network %s", self._id,
                    (self.network or "")[:12])
        for service in services:
            await self._start(service)

    async def _start(self, service: dict) -> None:
        name = service["name"]
        spec = ExecutionSpec(
            image=service["image"],
            command=service.get("command") or [],
            env=dict(service.get("env") or {}),
            network_mode=self.network or "none",
            memory_mb=int(service.get("memory_mb") or 512),
        )
        cid = await self._driver.create(spec, network_alias=name,
                                        labels={"agentos.verify": self._id})
        self._containers.append(cid)
        self.alias_of[name] = cid
        await self._await_ready(service, cid)

    async def _await_ready(self, service: dict, cid: str) -> None:
        """Run the declared readiness check until it passes.

        WITHOUT THIS THE FIRST CRITERION RACES THE DATABASE. Postgres
        reports its container as running several seconds before it
        accepts connections, so a criterion started on container-up
        fails with "connection refused" — and that is `fail`, which
        REFUSES the story. An infrastructure race must never look like
        wrong work; R1's whole point is that only a check which RAN and
        said no may refuse.

        THE PROBE RUNS IN ITS OWN CONTAINER ON THE NETWORK, not via
        `docker exec` in the service. Two reasons, and the second is the
        better one. The socket proxy gates `/exec/*` separately and it
        is the grant worth least — it runs arbitrary commands in any
        container. And a probe from outside asks the question that
        actually matters: can something ON THIS NETWORK reach the
        service? An `exec` inside it can pass while the criterion still
        cannot connect.
        """
        ready = service.get("ready") or {}
        command = ready.get("command")
        if not command:
            return
        timeout = float(ready.get("timeout") or DEFAULT_READY_TIMEOUT_S)
        image = ready.get("image") or service["image"]
        deadline = asyncio.get_event_loop().time() + timeout
        last = ""
        while asyncio.get_event_loop().time() < deadline:
            result = await self._driver.run_ephemeral(
                ExecutionSpec(image=image, command=["sh", "-c", command],
                              network_mode=self.network or "none",
                              memory_mb=256),
                timeout=min(20.0, timeout))
            if result.get("exit_code") == 0:
                logger.info("service %s ready", service["name"])
                return
            last = (result.get("output") or "").strip()[-200:]
            await asyncio.sleep(READY_POLL_S)
        raise ServiceError(
            f"service {service['name']!r} did not become ready within "
            f"{timeout:.0f}s; last check said: {last or '(no output)'}")

    async def teardown(self) -> None:
        """Remove everything, in the order that can succeed.

        Containers before the network — Docker refuses to remove a
        network with endpoints attached, and a leaked internal network
        per verification is the same class of leak F2c just closed.
        """
        for cid in self._containers:
            try:
                await self._driver.destroy(cid, stop_timeout=0)
            except Exception as e:                          # noqa: BLE001
                logger.warning("service container %s: %s", cid[:12], e)
        self._containers.clear()
        if self.network:
            try:
                await self._driver.remove_network(self.network)
            except Exception as e:                          # noqa: BLE001
                logger.warning("network %s: %s", self.network[:12], e)
            self.network = None
