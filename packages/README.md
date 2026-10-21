# `packages/` — libraries published separately

Code that is versioned and released on its own, rather than deployed with the
application.

| | |
|---|---|
| [`agentos-ledger-client/`](agentos-ledger-client/README.md) | a small client for reading and writing ledger events |

The control plane itself imports `agentos_ledger_client` — `app/ledger/`,
`app/routers/goal_api.py` and `kernel/harness/tools_worker/actions.py` among
others — and the `Dockerfile` installs it from this folder. It is also
published separately, for code outside this repository that talks *to*
AgentOS.
