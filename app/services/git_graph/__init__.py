"""GO Git Dependency-Graph Manager — deterministic engine package.

See GO_GIT_GRAPH_MANAGER_PLAN.md. This package holds the engine-side, no-LLM
git mechanics:

- repo_identity — canonical cross-goal repo identity (§3.1, §8)
- plan          — deterministic DAG→workstream/merge derivation (§4) + the
                  read-only plan preview; the persisting two-phase reconcile and
                  the GitGraphController loop are added in later build steps.

Nothing here mutates branches; branch creation/sync/merge live in the engine
worker (added later) and are gated behind GIT_GRAPH_ENABLED.
"""
