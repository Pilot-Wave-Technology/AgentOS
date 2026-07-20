# Third-Party Notices

This repository is a fork of [OpenClaw](https://github.com/openclaw/openclaw), with additions layered on top by Port33, inc. for the [AgentOS control plane](https://github.com/Pilot-Wave-Technology/AgentOS) integration. It is dual-licensed to keep that distinction clear and to comply with the upstream MIT license's attribution requirement.

## Upstream: OpenClaw

- **Project:** OpenClaw ([openclaw/openclaw](https://github.com/openclaw/openclaw))
- **Copyright:** (c) 2026 OpenClaw Foundation
- **License:** MIT — full text in [LICENSE-MIT](LICENSE-MIT)

The MIT License requires that its copyright notice and permission notice be included in all copies or substantial portions of the software. That notice is preserved, unmodified, in [LICENSE-MIT](LICENSE-MIT), and applies to the upstream codebase this repo builds on: `src/`, `apps/`, `packages/`, `ui/`, `documentation/` (excluding `documentation/deployment/`), and every `extensions/` plugin except the ones listed below.

This repository's root [LICENSE](LICENSE) file is Apache 2.0, covering the additions made by Port33, inc. (below) — it is not a relicensing of the upstream MIT code, which remains under [LICENSE-MIT](LICENSE-MIT) regardless of which file GitHub displays as the repo's primary license.

## Adapted third-party code: Pi / pi-mono

Per upstream's own third-party notice, portions of OpenClaw's source were adapted from Pi / pi-mono, and OpenClaw depends on packages from that project for its terminal UI and agent runtime — beyond normal package-manager dependency metadata, so it's called out here rather than left to `pnpm licenses list`.

- **Upstream:** https://github.com/earendil-works/pi-mono
- **Packages this repo depends on:** `@mariozechner/pi-agent-core`, `@mariozechner/pi-ai`, `@mariozechner/pi-coding-agent`, `@mariozechner/pi-tui` (pinned to `0.57.1` in `package.json`)
- **License:** MIT
- **Copyright:** (c) 2025 Mario Zechner

```
MIT License

Copyright (c) 2025 Mario Zechner

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

## This fork's own additions

The following are original work by Port33, inc., licensed separately under **Apache License 2.0** — full text in [LICENSE](LICENSE):

- `extensions/custom-tools/`
- `extensions/insights/`
- `extensions/workspace-ops/`
- `infra/`
- `documentation/deployment/`
- `start-openclaw.sh`
- `scripts/gateway-provision.sh`, `scripts/wrappers/`
- The AgentOS-specific patches to core gateway `src/` described in [ARCHITECTURE.md](ARCHITECTURE.md#core-gateway-patches) — these are modifications to upstream files rather than new files, and remain layered on top of the upstream MIT-licensed source they modify. The Apache 2.0 license applies to the modifications themselves; the surrounding upstream file remains MIT-licensed per the terms above.

`extensions/memory-core/` is upstream OpenClaw code, not an addition by Port33, inc. — it remains MIT-licensed under the upstream notice, even though this fork chose to keep it rather than remove it during extension cleanup.

## Third-party dependencies

This repo's `package.json` and `pnpm-lock.yaml` pull in a large number of upstream OpenClaw's own npm dependencies. This document does not attempt to catalog every dependency's license — that inventory belongs to, and is maintained by, the upstream OpenClaw project. Run `pnpm licenses list` for a live report of what's actually installed.
