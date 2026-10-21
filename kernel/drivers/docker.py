"""DockerDriver — Docker Engine API implementation of the substrate seam.

Talks to the Docker daemon via raw HTTP (httpx.AsyncClient) through
the socket proxy. No docker-py — the seam gets a second implementation
at I2 (Kubernetes), so its shape is defined by ExecutionSpec, not by
an SDK's conventions.

Security: validate_spec() is called BEFORE any HTTP request in create().
The proxy gates which endpoint is called, never what payload it carries —
CONTAINERS=1 + POST=1 permits create with an arbitrary HostConfig, which
is root-equivalent on the host. The driver must refuse dangerous specs
locally (rule 14).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

import httpx

from kernel.driver import Driver
from kernel.conductor.ports import BRIDGE_PORT_LABEL
from kernel.ownership import KERNEL_SELECTOR
from kernel.spec import (ExecutionSpec, require_supported,
                         validate_spec)

logger = logging.getLogger(__name__)


class DockerDriver(Driver):
    """Docker Engine API driver via httpx through the socket proxy."""

    # WHAT DOCKER HERE CAN HONOUR. Everything is the default, and saying
    # so explicitly is the point -- the empty dict would mean the same
    # thing, but a reader of a Kubernetes driver's list needs this one
    # beside it to see what is being claimed.
    #
    # `isolation` is `shared` alone: a runsc runtime may exist on some
    # hosts, but "may exist" is not a capability, and probing the daemon
    # at import time would make the answer depend on which machine ran
    # the check. When a host is configured for it, add "syscall" here and
    # map it to HostConfig.Runtime -- one line, once it is true.
    #
    # `volume_class` cannot be anything else -- Docker has no storage
    # classes, and pretending "fast" meant something would be the silent
    # downgrade this whole contract exists to prevent.
    #
    # `node_class` ACCEPTS ALL THREE, which reads like the downgrade the
    # line above refuses and is not. A node class is a PLACEMENT request,
    # and on one machine every placement is satisfied by the same answer:
    # there is nowhere else to put it, so nothing is being quietly
    # substituted. What the tier also carries -- cpus and memory_mb -- is
    # applied here in full, so a `large` spec on a laptop fails for want
    # of fourteen cores, which is a true statement about the laptop. The
    # capability check exists to stop a substrate claiming an ISOLATION
    # or DURABILITY property it does not have; refusing to run a medium
    # story on E0 would protect nobody and would take the local dev loop
    # away from every tier but one.
    CAPABILITIES = {
        "isolation": ("shared",),
        "volume_class": ("standard",),
        "node_class": ("small", "medium", "large"),
    }

    def __init__(self, base_url: str | None = None) -> None:
        if base_url is None:
            base_url = os.environ["DOCKER_HOST"]
            # Convert tcp:// to http:// for httpx
            if base_url.startswith("tcp://"):
                base_url = "http://" + base_url[6:]
        self._client = httpx.AsyncClient(base_url=base_url)

    async def create(self, spec: ExecutionSpec, network_alias: str = "",
                     labels: dict | None = None) -> str:
        """Create and start a container from the spec.

        Validates the spec BEFORE any HTTP call — a SpecValidationError
        is raised locally, never a network error from the daemon.
        """
        # Validate BEFORE any HTTP — SpecValidationError, never a network call
        validate_spec(spec)
        # And refuse what this substrate cannot provide, by name, before
        # anything is created. A capability quietly dropped here is a
        # container the caller believes is isolated and is not.
        require_supported(spec, "docker", self.CAPABILITIES)

        # Build payload explicitly — no pass-through of arbitrary HostConfig.
        # Each field maps to one Docker API field; nothing else is forwarded.
        labels = {"managed-by": "kernel", **(labels or {})}
        if spec.execution_id is not None:
            labels["execution-id"] = str(spec.execution_id)
        # Never set execution-id to "" — int("") crashes the reconciler.

        # H1c: record the bridge port ON the container. The port already
        # lives in the spec's env because the shim reads it there, so this
        # records the same value rather than inventing a second source of
        # truth — and it is what finally makes exec()'s docstring true
        # after it described a label nothing set since ES3.
        bridge_port = spec.env.get("BRIDGE_PORT")
        if bridge_port:
            labels[BRIDGE_PORT_LABEL] = str(bridge_port)

        host_config = {
            "Memory": spec.memory_mb * 1024 * 1024,
            "NanoCpus": int(spec.cpus * 1e9),
            "NetworkMode": spec.network_mode,
        }

        # Bind mounts — validated by validate_spec above
        if spec.binds:
            host_config["Binds"] = spec.binds
        if spec.volume_mounts:
            host_config["Mounts"] = [
                {"Type": "volume", "Source": src, "Target": dst,
                 "VolumeOptions": {"Subpath": sub}} if sub else
                {"Type": "volume", "Source": src, "Target": dst}
                for src, dst, sub in spec.volume_mounts]

        payload = {
            "Image": spec.image,
            "Cmd": spec.command,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": labels,
            "HostConfig": host_config,
        }
        if spec.user:
            payload["User"] = spec.user
        if network_alias:
            # F2d: the name a criterion reaches this service by. Docker's
            # embedded DNS resolves aliases only on user-defined
            # networks, which is what the Verifier creates — the default
            # bridge would give the criterion an IP it cannot predict.
            payload["NetworkingConfig"] = {
                "EndpointsConfig": {
                    spec.network_mode: {"Aliases": [network_alias]}}}
        resp = await self._client.post("/containers/create", json=payload)
        resp.raise_for_status()
        cid = resp.json()["Id"]

        # Start — raise on failure. If the proxy denies start (403),
        # this surfaces immediately, not as a 30s TimeoutError from
        # await_ready.
        resp = await self._client.post(f"/containers/{cid}/start")
        resp.raise_for_status()
        return cid

    async def list_containers(self) -> list[dict]:
        """List all containers with the managed-by=kernel label.

        Uses GET /containers/json WITHOUT all=true — returns running
        containers only.  That default is load-bearing: a killed container
        reads as "compute gone" and drives recovery.  Adding all=true would
        make has_compute stay true for dead containers and silently break
        recovery.  Reaping stopped containers belonging to VERIFYING
        executions is F2c's job.

        THE SAME SELECTOR THE KUBERNETES DRIVER USES, from
        `kernel/ownership.py`: a Docker label filter and a Kubernetes
        label selector happen to spell `key=value` identically, and this
        was the fourth literal copy of the one fact that decides what the
        reapers can see at all.
        """
        resp = await self._client.get(
            "/containers/json",
            params={"filters": json.dumps({"label": [KERNEL_SELECTOR]})},
        )
        resp.raise_for_status()
        return [
            {
                "id": c["Id"],
                "labels": c.get("Labels", {}),
                "status": c.get("State", "unknown"),
            }
            for c in resp.json()
        ]

    async def await_ready(
        self, container_id: str, timeout: float = 30.0,
        bridge_port: int | None = None,
    ) -> bool:
        """Running, and — when a bridge port is given — answering.

        H1a. The docstring below has said since C3 that "at E2, ready
        becomes the bridge handshake", and it never did: readiness meant
        `State.Running`, so dispatch reported success for a container whose
        shim had not yet bound its port. The executor then failed on its
        first tool call with a connection error, which reads as a bridge
        fault rather than as the dispatch that was never ready.

        A container that is running but not listening is not ready. Passing
        `bridge_port=None` keeps the old meaning for the sleep-container
        tests that predate the shim.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            resp = await self._client.get(
                f"/containers/{container_id}/json"
            )
            resp.raise_for_status()
            state = resp.json().get("State", {})
            if state.get("Running", False):
                if bridge_port is None:
                    return True
                return await self._await_bridge(
                    container_id, bridge_port, deadline)
            # Detect early exit — don't poll 30s for a container that
            # already crashed. At C4 this distinguishes dispatch failure
            # from slow start.
            if state.get("Status") in ("exited", "dead"):
                raise RuntimeError(
                    f"container {container_id} exited before becoming "
                    f"ready: status={state.get('Status')} "
                    f"exit_code={state.get('ExitCode')}"
                )
            await asyncio.sleep(0.5)
        raise TimeoutError(
            f"container {container_id} not running after {timeout}s"
        )

    async def _await_bridge(self, container_id: str, port: int,
                            deadline: float) -> bool:
        """Wait for the shim to answer. The real readiness predicate."""
        from kernel.bridge.client import bridge_health

        while time.monotonic() < deadline:
            if await bridge_health(port=port, timeout=2):
                return True
            # Still check for a crash: a shim that dies on startup would
            # otherwise be waited on for the full timeout.
            resp = await self._client.get(f"/containers/{container_id}/json")
            resp.raise_for_status()
            state = resp.json().get("State", {})
            if state.get("Status") in ("exited", "dead"):
                raise RuntimeError(
                    f"sandbox {container_id} exited before its bridge "
                    f"answered: exit_code={state.get('ExitCode')}"
                )
            await asyncio.sleep(0.3)
        raise TimeoutError(
            f"sandbox {container_id} is running but its bridge did not "
            f"answer on port {port}. A running container is not a ready one."
        )

    async def destroy(
        self, container_id: str, stop_timeout: int = 10
    ) -> None:
        """Stop and remove the container. Idempotent.

        stop_timeout: seconds the daemon waits after SIGTERM before
        SIGKILL. Default 10 gives a real sandbox time for graceful
        shutdown. Tests pass 0 because sleep(1) as PID 1 ignores
        SIGTERM (no explicit handler) and always waits the full grace
        period — a 10s hang per test run.
        """
        # Stop (ignore 304 = already stopped, 404 = already removed —
        # trap cleanup and C6 orphan sweep hit this path).
        resp = await self._client.post(
            f"/containers/{container_id}/stop",
            params={"t": stop_timeout},
        )
        if resp.status_code not in (204, 304, 404):
            resp.raise_for_status()
        # Remove (ignore 404 = already gone)
        resp = await self._client.delete(f"/containers/{container_id}")
        if resp.status_code != 404:
            resp.raise_for_status()

    async def aclose(self) -> None:
        """Close the httpx client."""
        await self._client.aclose()

    async def resolve_bridge_port(self, container_id: str) -> int | None:
        """Read a sandbox's bridge port from its label. None if unlabelled.

        Reading beats recomputing: whichever port a sandbox actually got is
        the one recorded here, including when two allocations raced.
        """
        resp = await self._client.get(f"/containers/{container_id}/json")
        if resp.status_code == 404:
            # The container is gone. That is an answer, not a failure: the
            # caller decides whether to rebuild or leave it for reconcile.
            return None
        resp.raise_for_status()
        labels = (resp.json().get("Config", {}) or {}).get("Labels") or {}
        raw = labels.get(BRIDGE_PORT_LABEL)
        return int(raw) if raw else None

    def sandbox_attachments(self, workspace_container_path: str = "/workspace",
                            workspace_subpath: str = "") -> dict:
        """Docker's answer: binds built from this worker's own host paths.

        A bind is expressed in HOST paths, and the worker only knows its own
        container paths — `/app/kernel` is where the code is mounted inside
        it, not where it lives outside. Only the daemon knows where a named
        volume actually sits, so we ask.

        Getting this wrong does not raise: Docker creates an empty directory
        at the target and the sandbox starts with no shim. That is the
        failure six scripts hit earlier in this build when the bridge mount
        was widened, so a missing mount is refused here rather than
        discovered as a container that will not answer.
        """
        import json as _json
        from urllib.request import Request as _Request, urlopen as _urlopen

        proxy = (os.environ["DOCKER_HOST"] if "DOCKER_HOST" in os.environ
                 else "tcp://socket-proxy:2375").replace("tcp://", "http://")
        self_id = os.environ["HOSTNAME"] if "HOSTNAME" in os.environ else ""
        if not self_id:
            raise RuntimeError("HOSTNAME is unset; cannot inspect own mounts")

        with _urlopen(_Request(f"{proxy}/containers/{self_id}/json"),
                      timeout=5) as resp:
            info = _json.loads(resp.read())
        mounts = {m.get("Destination"): m.get("Source")
                  for m in info.get("Mounts", []) if m.get("Destination")}

        kernel_host = mounts.get("/app/kernel")
        workspace_host = mounts.get(workspace_container_path)
        if not kernel_host:
            raise RuntimeError(
                "no /app/kernel mount on this worker; a sandbox built from "
                "it would start with no shim to run")
        if not workspace_host:
            raise RuntimeError(
                f"no {workspace_container_path} mount on this worker; a "
                "sandbox built from it would have no workspace")

        sep = "\\" if "\\" in kernel_host else "/"

        # ONE STORY'S ROOT, NOT THE WHOLE VOLUME.
        #
        # This bound the entire /workspace volume regardless of which
        # directory the story owned, so every executor's cwd sat one
        # level ABOVE its own tree and a relative path reached every
        # other story's work. Execution 1544 tidied up with
        #
        #     rm -rf origins runs ... $(find . -name "__pycache__")
        #
        # from /workspace and deleted its own workspace and the seed it
        # was cloned from. The three verifications after that hashed an
        # empty tree and reported CHECK_SURFACE_CHANGED -- which reads as
        # the executor tampering with frozen tests, and was not.
        #
        # Per-execution ROOTS were necessary and not sufficient: paths
        # were isolated, the mount was not.
        # A NAMED VOLUME CANNOT BE SUBPATHED BY STRING SURGERY. The
        # workspace arrives here as `e0_e6_scratch` -- a volume name, not
        # a host path -- so appending `/runs/1558` to it produces a
        # volume name with slashes in it and the daemon refuses to create
        # the container at all. (The first attempt also took its
        # separator from the KERNEL bind, which is a Windows path, and
        # produced `e0_e6_scratch\runs\1558`.)
        #
        # Docker's Mounts API has carried VolumeOptions.Subpath since API
        # 1.45. It is the only way to hand a sandbox one story's
        # directory out of a shared volume, so the workspace moves from
        # `binds` to `volume_mounts` when it is a volume.
        is_volume = "/" not in workspace_host and "\\" not in workspace_host
        volume_mounts = ()
        if workspace_subpath and is_volume:
            volume_mounts = ((workspace_host, workspace_container_path,
                              workspace_subpath.strip("/")),)
        elif workspace_subpath:
            # A real host path: string surgery IS right here, with the
            # separator taken from the path being extended rather than
            # from an unrelated one.
            wsep = "\\" if "\\" in workspace_host else "/"
            tail = workspace_subpath.strip("/").replace("/", wsep)
            workspace_host = workspace_host.rstrip("/\\") + wsep + tail
        binds = [
            # Read-only: a sandbox able to edit the shim could rewrite
            # the thing that mediates its own tool calls.
            f"{kernel_host}{sep}bridge:/app/bridge:ro",
        ]
        if not volume_mounts:
            binds.append(f"{workspace_host}:{workspace_container_path}")
        return {
            "binds": binds,
            "volume_mounts": volume_mounts,
            # Shared namespace, so the shim on 0.0.0.0:PORT is reachable
            # from the worker as localhost without publishing to the host.
            "network_mode": f"container:{self_id}",
            "allowed_bind_prefixes": [kernel_host, workspace_host],
        }

    async def run_ephemeral(self, spec: ExecutionSpec,
                            timeout: float = 300.0,
                            upload: tuple[str, bytes] | None = None,
                            also_upload: list | None = None,
                            services: list | None = None) -> dict:
        """F2 — run a one-shot container to completion. Returns its verdict.

        The Verifier's whole mechanism. A throwaway container, created
        from a pinned digest, that runs one command and dies; locally a
        Docker container, in the cloud tiers a Cloud Run job or a k8s
        Job, and nothing above this seam knows which.

        `upload` is (destination_directory, tar_bytes), pushed in before
        the container starts. The Verifier hands its committed tree this
        way rather than by bind mount: `/workspace` is a named volume, so
        there is no host path to give a sibling container, and mounting
        the whole volume would hand every criterion the executor's live
        working copy. The destination directory must already exist in the
        image — Docker will not create it.

        Returns {"exit_code": int, "output": str, "timed_out": bool}.

        A substrate that can have a run ended by something other than
        the command adds `infra_error: str`, and sets `exit_code` to
        None when it does — see kernel/drivers/k8s_run.py. This driver
        never does: a container exits with a code or the API call
        raises, so there is no third outcome to report.

        NEVER RAISES ON A NON-ZERO EXIT. A failing check is the ordinary
        case and is data, not an error — the caller has to be able to
        tell "the check ran and said no" from "the check could not run",
        because R1 makes only the first of those a refusal.

        The container is removed in a `finally`. A verifier that leaks a
        container per criterion is a verifier nobody runs often.
        """
        # `services` IS ACCEPTED AND UNUSED HERE, deliberately. On this
        # substrate a declared service is already running on the
        # verification's internal network and the criterion reaches it by
        # name through `spec.network_mode` — Services.up started it
        # before this call. The argument exists because a substrate with
        # no networks puts those services in the criterion's own pod
        # instead, and the CALLER should not have to know which.
        #
        # Silently ignoring an argument is usually a bug; here it is the
        # correct answer, and saying so is what stops somebody "fixing"
        # it by starting the services twice.
        del services
        validate_spec(spec)
        labels = {"managed-by": "kernel", "agentos.ephemeral": "1"}
        if spec.execution_id is not None:
            labels["execution-id"] = str(spec.execution_id)

        payload = {
            "Image": spec.image,
            "Cmd": spec.command,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": labels,
            "HostConfig": {
                "Memory": spec.memory_mb * 1024 * 1024,
                "NanoCpus": int(spec.cpus * 1e9),
                "NetworkMode": spec.network_mode,
                **({"Binds": spec.binds} if spec.binds else {}),
            },
        }
        if spec.user:
            payload["User"] = spec.user

        resp = await self._client.post("/containers/create", json=payload)
        resp.raise_for_status()
        cid = resp.json()["Id"]
        timed_out = False
        try:
            for dest, blob in ([upload] if upload else []) + (also_upload or []):
                if not blob:
                    continue
                put = await self._client.put(
                    f"/containers/{cid}/archive", params={"path": dest},
                    content=blob, timeout=120.0)
                put.raise_for_status()
            resp = await self._client.post(f"/containers/{cid}/start")
            resp.raise_for_status()
            try:
                waited = await self._client.post(
                    f"/containers/{cid}/wait", timeout=timeout)
                waited.raise_for_status()
                exit_code = int(waited.json().get("StatusCode", 1))
            except httpx.TimeoutException:
                # A criterion that hangs must produce a result, not hang
                # the adjudicator behind it.
                timed_out, exit_code = True, 124
            output = await self._logs(cid)
        finally:
            await self.destroy(cid, stop_timeout=0)
        return {"exit_code": exit_code, "output": output,
                "timed_out": timed_out}

    async def _logs(self, container_id: str, limit: int = 64_000) -> str:
        """Combined stdout+stderr, de-multiplexed.

        Docker frames non-TTY logs as 8-byte headers followed by payload.
        Returned raw, the output reads as binary noise in a verdict a
        person has to act on.
        """
        try:
            resp = await self._client.get(
                f"/containers/{container_id}/logs",
                params={"stdout": 1, "stderr": 1, "tail": 2000})
            resp.raise_for_status()
        except Exception:                                   # noqa: BLE001
            return ""
        raw, out, i = resp.content, [], 0
        while i + 8 <= len(raw):
            if raw[i] in (0, 1, 2):
                n = int.from_bytes(raw[i + 4:i + 8], "big")
                out.append(raw[i + 8:i + 8 + n])
                i += 8 + n
            else:                       # not framed (TTY) — take the rest
                out.append(raw[i:])
                break
        text = b"".join(out).decode("utf-8", errors="replace")
        return text[-limit:]

    async def create_network(self, name: str, internal: bool = True) -> str:
        """F2d — a private network for one verification. Returns its id.

        `internal` denies everything outside the network while permitting
        container-to-container traffic on it. That is the whole trick: a
        criterion reaches its declared database by name and still cannot
        fetch a version of itself that passes.
        """
        resp = await self._client.post("/networks/create", json={
            "Name": name,
            "Driver": "bridge",
            "Internal": internal,
            "Labels": {"managed-by": "kernel", "agentos.ephemeral": "1"},
        })
        resp.raise_for_status()
        return resp.json()["Id"]

    async def remove_network(self, network_id: str) -> None:
        """Idempotent — tolerates already-gone, like destroy()."""
        try:
            resp = await self._client.delete(f"/networks/{network_id}")
            if resp.status_code not in (204, 404):
                resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            if e.response.status_code != 404:
                raise

    async def run_ephemeral_capture(self, spec: ExecutionSpec, capture: str,
                                    timeout: float = 300.0,
                                    upload: tuple[str, bytes] | None = None,
                                    ) -> dict:
        """F5 — run a one-shot container and pull a directory back out.

        The install half of the environment replay. Same lifecycle as
        `run_ephemeral`, plus `GET /containers/{id}/archive` before the
        container is destroyed — which is what lets the packages travel
        to a container that has no network of its own.

        Returns run_ephemeral's dict plus `archive`: the captured
        directory as a tar, or b"" if it could not be read.
        """
        validate_spec(spec)
        payload = {
            "Image": spec.image,
            "Cmd": spec.command,
            "Env": [f"{k}={v}" for k, v in spec.env.items()],
            "Labels": {"managed-by": "kernel", "agentos.ephemeral": "1"},
            "HostConfig": {
                "Memory": spec.memory_mb * 1024 * 1024,
                "NanoCpus": int(spec.cpus * 1e9),
                "NetworkMode": spec.network_mode,
            },
        }
        if spec.user:
            payload["User"] = spec.user

        resp = await self._client.post("/containers/create", json=payload)
        resp.raise_for_status()
        cid = resp.json()["Id"]
        timed_out, archive = False, b""
        try:
            if upload and upload[1]:
                # Same push run_ephemeral does: the prepare step needs
                # the committed tree in place before its setups run.
                put = await self._client.put(
                    f"/containers/{cid}/archive", params={"path": upload[0]},
                    content=upload[1], timeout=120.0)
                put.raise_for_status()
            resp = await self._client.post(f"/containers/{cid}/start")
            resp.raise_for_status()
            try:
                waited = await self._client.post(
                    f"/containers/{cid}/wait", timeout=timeout)
                waited.raise_for_status()
                exit_code = int(waited.json().get("StatusCode", 1))
            except httpx.TimeoutException:
                timed_out, exit_code = True, 124
            output = await self._logs(cid)
            if exit_code == 0:
                try:
                    got = await self._client.get(
                        f"/containers/{cid}/archive",
                        params={"path": capture}, timeout=120.0)
                    if got.status_code == 200:
                        archive = got.content
                except Exception as e:                      # noqa: BLE001
                    # A capture that fails is not a failed install; the
                    # caller sees an empty archive and reports `error`.
                    logger.warning("capture of %s failed: %s", capture, e)
        finally:
            await self.destroy(cid, stop_timeout=0)
        return {"exit_code": exit_code, "output": output,
                "timed_out": timed_out, "archive": archive}
