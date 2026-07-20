# Notice

This repository is a fork of [OpenClaw](https://github.com/openclaw/openclaw).

Copyright 2026 Port33, inc.

The following are original work by Port33, inc., licensed under the Apache License, Version 2.0 — see [LICENSE](LICENSE):

- `extensions/custom-tools/`
- `extensions/insights/`
- `extensions/workspace-ops/`
- `infra/`
- `documentation/deployment/`
- `start-openclaw.sh`
- `scripts/gateway-provision.sh`, `scripts/wrappers/`
- The AgentOS-specific patches to core gateway `src/` described in [ARCHITECTURE.md](ARCHITECTURE.md#core-gateway-patches) — modifications to upstream files, layered on top of the upstream MIT-licensed source they modify.

Everything else in this repository is the upstream OpenClaw codebase, licensed under the MIT License — see [LICENSE-MIT](LICENSE-MIT), preserved unmodified per its attribution requirement.

See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for third-party code adapted into OpenClaw itself (e.g. Pi / pi-mono).
