# `kernel/` — deciding when work runs, and running it

**The kernel contains no model call.** That is the single most useful thing to
know about it. Deciding *what* work is worth doing belongs to the agents on the
gateway; deciding *when* it may run, giving it a container, and judging whether
it finished belongs here. The one place a model appears in this tree is inside
the harness, driving an executor — and even there the harness itself is the
scaffolding, not the thinking.

If you have not read [`../HOW_A_STORY_EXECUTES.md`](../HOW_A_STORY_EXECUTES.md),
read it first. It defines *execution*, *leg*, *park* and *profile*, which
everything below assumes.

---

## The processes this package becomes

The kernel is a library. Nothing here is called `conductor`; the Deployment
named `conductor` runs the first entry in this table.

| Command | Runs as | Does |
|---|---|---|
| `python -m kernel.worker.service` | the **conductor**, 2 replicas | admit, dispatch, reconcile |
| `python -m kernel.verifier.service` | the **verifier**, 1 replica | re-run a claim's checks in a clean container |
| `python -m kernel.worker.pod_main` | the **worker container** of every execution pod | the leg loop and the workspace surface |
| `python -m kernel.assistant.api` | the assistant tool surface | the kernel verbs an assistant may call |
| `python -m kernel.console.server` | a local console | development and inspection |
| `python -m kernel.obs.alive` | every container's healthcheck | liveness |

Two conductor replicas run with **no leader election**. Safety comes from four
independent mechanisms instead: `FOR UPDATE SKIP LOCKED` row claims, leases
fenced by a generation integer, compare-and-set on every state write, and one
global advisory lock for the reconcile sweep.

---

## The packages, by what they are responsible for

### Scheduling — when may this run?

| | |
|---|---|
| `conductor/` | `PENDING → ADMITTED → DISPATCHING → RUNNING`. Pure state transitions driven by database reads. Holds the three admission ceilings. |
| `worker/` | The processes above. `service.py` is the conductor's loop; `pod_main.py` is what actually runs inside a pod. |
| `mailbox/` | The three intent vocabularies — steer, hold, cancel — and what makes each well-formed. Cancellation goes through here rather than writing a state directly, so the lease, the slot, the pod and the journal all follow. |

### Execution — the model, and everything holding it

| | |
|---|---|
| `harness/` | The largest package, and the one to read if you read one. Everything between "a model that can call tools" and "a system you would let near a repository": the leg loop, the six-stage interceptor chain, the plan gate, parking, stall detection, budgets and evidence. |
| `profiles/` | Resolving `(kind, domain, phase)` to a profile, and loading it. An anomaly in the tool arithmetic is a **load** error — the profile refuses to start rather than failing a call later. |
| `tools/` | The tool catalog: each tool declared once with a *side* and an *effect*. `dialect.py` is the schema intersection every provider accepts. |
| `bridge/` | The worker→sandbox channel on port 9100. The listener runs inside the sandbox; the model loop is the client. |

### The container and what it can reach

| | |
|---|---|
| `drivers/` | The substrate. `kubernetes.py` builds the pod; `k8s_sqlproxy.py` is the sidecar that gives an execution its own database login. |
| `workspace/` | A workspace is *composed*, not cloned: this turns a story's granted resources into checkouts and copy-ins, and serves them over the authenticated port 9101. |
| `credentials/` | Cloud identity for an execution — who decides, and who mints. The grant is the boundary; minting lives behind it. |
| `artifacts/` | The object store a run writes its deliverables to and the verifier reads them from. |

### Judging the result

| | |
|---|---|
| `verifier/` | Re-runs a claim's checks in a clean container, at the contract's digest, as a different user, against the committed tree. **No model in any of it** — when the optional model gate is on, that model can only *refuse*. |
| `answerer/` | Answering a person's question about work that is under way. |
| `knowledge/` | What the goal knows: the record behind the board. |

### The agent surfaces

| | |
|---|---|
| `assistant/` | The verbs an assistant may call — read its own status, start a story, steer, amend, cancel. |
| `go/` | The verbs the orchestrator may call. **A separate package on purpose**: the assistant asks and the orchestrator decides, and that split is only real if the two have different surfaces. |
| `gateway/` | Not the gateway. The kernel's knowledge of what the gateway *claims* to offer — the only part of that relationship the kernel can check for itself. |

### Cross-cutting

| | |
|---|---|
| `ledger/` | The kernel's auditable acts. Decisions, not mechanics. |
| `usage/` | LLM spend, recorded where the cost views can see it. |
| `obs/` | Structured logging bound to an execution and a leg. |
| `console/` | A development console and the contract a console run is dispatched under. |
| `corpus/` | Graded tasks run as real stories, for measuring the harness against itself. |

Top-level modules are the vocabulary the packages share: `spec.py` (the
substrate-agnostic execution spec), `envelope.py` (agent-level policy for the
interceptor chain), `addressing.py` (which story did you mean), `driver.py`,
`executors.py`, `story_kinds.py`, `sizes.py`.

---

## Rules that hold across the whole tree

**Nothing waits without a reason written on it.** An execution that cannot be
admitted stays `PENDING` with a `stall_reason` naming which ceiling stopped it.
A silent queue is a bug here, not a state.

**"Could not determine" is a failure, not a pass.** Four separate defects in one
build had the same shape: a question that could not fail visibly answered "no".
Code that cannot establish a fact says so rather than substituting the negative.

**The boundary is a missing function, not a prompt.** An assistant cannot
dispatch, provision, approve, verify, commit, or create a story because no such
function is reachable from its surface. Where a rule *is* enforced at runtime it
is enforced in SQL or in a NetworkPolicy, not in instructions to a model.

**The pod holds no cloud credential.** Artifacts, git credentials and granted
objects all cross through the control plane, authenticated with the execution's
own token — which is derived from its id, not stored, so a leak is one
execution's and cannot mint another.

**Comments carry the incident.** Much of this tree argues with itself in prose,
records what went wrong before a guard existed, and prefers refusing to
guessing. When a comment and this README disagree, the comment is closer to the
code and almost certainly right — and a pull request fixing the README is
welcome.

---

## Where the numbers live

| Number | Value | File |
|---|---|---|
| System-wide concurrent executions | 20 | `conductor/admit.py` |
| Per goal, per owner — orchestrator / assistant | 10 / 5 | `conductor/owner.py` |
| Leg context / spend / steps / count | 120K / 6M / 200 / 12 | `harness/models/config.py` |
| Per-goal tokens per leg / max legs | 20K–1M / 1–200 or -1 | `harness/models/leg_choice.py` |
| Park kinds, and who may answer each | 9 kinds | `harness/park_kinds.py`, `mailbox/schema.py` |
| Run states and transitions | 10 / 25, version 5 | `../seed/run_state_machine.json` |

Only the system ceiling reads an environment variable (`AGENTOS_SYSTEM_CEILING`).
The per-owner ceilings are code defaults copied into each goal's
`admission_buckets` row; widen a goal by editing that row.

The leg budget is settable per profile, and per goal: the goal's **Model
Configuration** page sets the executor model, verifier model, machine size
(`small` / `medium` / `large`), tokens per leg (capped to what the model can
hold with room for its reply) and max legs. Those are frozen onto the run at
dispatch (`executions.leg_budget`, migration `0008_goal_leg_budget`) and govern
the work phase, not the review.
