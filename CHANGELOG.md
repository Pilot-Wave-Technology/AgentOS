# Changelog

This tracks **this fork's own changes** on top of upstream OpenClaw — not upstream's own release history. For upstream OpenClaw changes, see the [upstream project](https://github.com/openclaw/openclaw).

This project does not yet use a versioned release scheme. Changes are tracked by theme, not by commit.

---

## Unreleased

### Added
- Public open-source release — ARCHITECTURE.md, VISION.md, THIRD_PARTY_NOTICES.md on this branch; the repository's CONTRIBUTING.md and SECURITY.md ship on the `main` branch

---

## Initial release

First public release of the agent_os_openclaw gateway fork. Notable fork-specific work:

### Gateway extensions
- **Custom tools** (`extensions/custom-tools/`) — admin-defined tools (HTTP, OpenAPI, TypeScript/Deno sandbox, MCP) loaded from the control plane's PostgreSQL at runtime; capability-scoped V8 isolate runtime (`net`, `storage`, `secrets`, `mcp`); periodic self-heal refresh plus `tools.reload` fast path; audit event forwarding
- **Insights** (`extensions/insights/`) — read-only `sandbox.status` RPC for the AgentOS dashboard
- **Workspace operations** (`extensions/workspace-ops/`) — `workspace.mergePreview` and `workspace.gitDiff` admin RPCs for the merge/review flow
- **Model registry** (`extensions/model-registry/`) — serves the agent-facing model catalog from the control plane (polls `/internal/model_catalog`; `models.reload` RPC fast path)
- **Utility models** (`extensions/misc-models/`) — purpose-scoped model overrides for direct model calls outside the agent catalog, including the gateway's compaction summarizer (polls `/internal/misc_model_settings`; `misc-models.reload` RPC)

### Core gateway patches
- Workspace file and git RPCs for the control plane's story-integration flow (branch sync, pull, merge preview, file read/write)
- Memory consolidation and compaction system — threshold-based flush, async compaction, per-agent summarizer model/prompt overrides, `MEMORY.md` append-only guard
- Kill switch — abort RPCs for force-killing in-flight agent turns
- Cost and usage tracking — per-call webhooks, thinking-token accounting, production pricing registry for Anthropic and Google models
- Security hardening — per-agent scoped JWTs replacing shared-token auth for agent-initiated calls, gateway/provider secret scrubbing from agent-controlled shells
- Consistent agent and tool naming across tool names, endpoint paths, and hooks

### Deployment
- GCP Terraform module provisioning the gateway VM, VPC network, reserved internal address, archive bucket, and firewalls; it READS the secrets and service account that the platform branch's `prerequisites.sh` creates (it creates none). The VM's startup script installs the systemd unit and a timer that polls Secret Manager for the control plane URL
- Cloud Build push-to-deploy pipeline: `gcloud compute ssh` over IAP → `git pull` on the VM → `scripts/gateway-provision.sh` (the same idempotent script first boot runs)
- CLI wrapper scripts (`scripts/wrappers/`) for agent-initiated HTTP callbacks to the control plane (task reports, assistant messages)
- Gateway logs in JSON format (`logging.consoleStyle json`), written to `/var/log/openclaw-<port>.log` by the systemd unit

### Removed
- 38 bundled upstream extensions not used by this deployment, plus native client-app build artifacts, to keep the fork's surface area minimal
