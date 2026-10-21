# How a story executes, end to end

Read this before anything else. It defines the six words the rest of the
documentation uses without apology — **goal**, **story**, **errand**,
**execution**, **leg**, **park** — and shows where each one lives.

Everything here was read from the source. Where a number appears, it is the
number in the code, and the file that holds it is named so you can check.

---

## The shortest possible version

> A **person** writes a goal. An **orchestrator agent** breaks it into stories.
> A person accepts one. An **assistant** starts it, which creates an
> **execution**. The **conductor** decides when that execution may run and
> gives it a **pod**. Inside the pod an **executor** works in **legs** until it
> claims it is finished. A person accepts the claim. The work lands on trunk.

Four of those nouns are not interchangeable, and conflating them is the most
common mistake:

| | Is | Lives in | Unit of |
|---|---|---|---|
| **Story** | a piece of work a person agreed to | `stories` | *accountability* — a person accepts it |
| **Execution** | one attempt at running that work | `executions` | *scheduling* — the conductor admits it |
| **Leg** | one conversation with a model, start to stop | `step_journal` | *context* — it begins empty and ends when full |
| **Step** | one model turn plus at most one tool call | `step_journal` | *record* — written before the tool runs |

A story can have several executions. An execution has many legs. A leg has many
steps. **A story is not the thing that runs; an execution is.**

---

## 1. Four planes, and only one of them thinks

```
   CONTROL PLANE          Cloud Run, stateless          the web UI, the API,
   app/                   holds nothing durable          the board, the cards

   GATEWAY PLANE          one Node process on a VM       the agents that TALK:
   (OpenClaw)             holds agent sessions           orchestrator, assistants

   KERNEL PLANE           conductor x2, verifier x1      decides WHEN work runs.
   kernel/                no LLM at all                  Contains no model call.

   EXECUTION PLANE        one pod per execution          the workspace, the
   (the pod)              cannot reach the cloud         shell, the git
                                     |
                              PostgreSQL
                       the only shared state
```

Each boundary is held by something stronger than convention — a database grant,
a Kubernetes NetworkPolicy, or a function that simply does not exist.

**There is no message broker.** Fan-out is PostgreSQL `LISTEN/NOTIFY` plus
advisory locks, and every claim is `FOR UPDATE SKIP LOCKED`. That is why the
design ports to another cloud without a queue rewrite, and why two conductor
replicas need no leader election.

**There is no `conductor` binary.** `kernel/conductor/` is a library. The
Kubernetes Deployment named `conductor` runs `python -m kernel.worker.service`
(`deploy/k8s/conductor.yaml`), at **two replicas**. Safety comes from four
independent mechanisms rather than from an elected leader: row claims that skip
locked rows, leases fenced by a generation integer, compare-and-set on every
state write, and one global advisory lock for the reconcile sweep.

---

## 2. Goal

A container for accountability, and the thing a person actually owns.

Creating one is a single transaction that produces far more than a row: the
goal, its conversation threads, and an **orchestrator agent** — one per goal,
for the life of the goal. The creator becomes a goal admin automatically.

A goal's id is chosen by a person, 3–5 uppercase alphanumerics, because it
doubles as a chat channel name.

**Membership is stricter than most things here.** Changing who is in a goal is
goal-admin only, with no platform-admin bypass — a deliberate asymmetry, since
platform admins *can* read. There is a last-admin guard, and removing a member
who still holds stories returns `409 needs_reassignment` rather than orphaning
the work.

---

## 3. Story, and the two words that look alike

The orchestrator decomposes a goal into stories. A story is the unit a person
accepts. Its states:

```
proposed ──► accepted ──► in_progress ──► done ──► reviewed
    │            │             │  ▲         ▲
    │            │             ▼  │         │
    │            │          blocked─────────┘
    ▼            ▼             │
rejected     cancelled ◄───────┘
```

`done → reviewed` is written by the system, never by a person directly.

Two columns look similar and mean entirely different things:

| Column | Values | Means |
|---|---|---|
| `work_kind` | `NULL` (a story) or `errand` | **how it runs** — which profile, whether it is reviewed, whether it can be cancelled mid-flight |
| `story_kind` | `code` or `merge` | **whether it is work at all**, or the git engine's own row for landing a branch |

The kernel refuses `code` or `merge` passed as a *work* kind, with an error that
names the confusion — the collision was anticipated.

**Dependencies are a `TEXT[]` with no foreign key.** A dependency id matching no
row is silently non-blocking. When a story is cancelled its dependents are
re-linked onto *its* dependencies, so `a→b→c` with `b` cancelled becomes `a→c`
rather than leaving a dangling edge.

---

## 4. Errand

Some work is not a feature: *"run the billing reconciliation and report drift"*.
That is an errand — no branch that merges, no reviewer, and a deliverable that
is an artifact rather than a diff.

The orchestrator **proposes** an errand; it cannot approve its own. A person
approves, and in doing so becomes the ledger principal and the cost centre.

**No agent role can create the grant that authorises one.** The control plane,
which otherwise connects with broad rights, *lowers itself* with
`SET LOCAL ROLE agentos_human` around exactly the two authority-carrying writes
and resets afterwards. The privilege boundary is SQL, not policy.

**What a standing grant buys is narrower than it sounds.** It short-circuits the
*plan* gate only — run two onwards does not re-approve a plan a person already
read. **Completion always parks for a person.** The fully-autonomous finish was
built and then deliberately withdrawn, on the grounds that finishing is not only
"was the plan carried out" but "is this enough?", and nobody can answer that in
advance because the answer depends on what the work found.

**Nothing runs on a clock.** Recurrence was removed; a second run is a
deliberate act that requires a human uid and refuses while a run is live.

---

## 5. Resource

Two separate acts, deliberately:

- **Registering** says the goal owns a thing — a repository, a document — and
  whether a story may *ever* write it.
- **Granting** says *this* story holds it *now*.

| Relation | Means | How it is mounted |
|---|---|---|
| `needs` | read only | cloned or copied in, not writable |
| `produces` / `modifies` | this story may write it | writable checkout, branch cut |

A write grant with no stated reason is refused: the reason reaches the
executor's brief as the line saying what each checkout is *for*, and a story
touching two repositories cannot tell them apart without it.

When an execution is created, its mounts are **copied** from the story's grants
rather than read through a view — so granting a resource later cannot change
what an old verdict claims to have been checked against.

---

## 6. The handoff: from a sentence to an execution

The orchestrator writes a story thinly. What repositories it touches, what must
not break, and what "done" means come out of a conversation with a person — and
the **assistant** is the one having it.

An assistant is **one per member per goal**, not one per story. Stories rotate
through a single conversation with one shared workspace; sessions are split per
thread so context windows stay separate.

| | Orchestrator | Assistant |
|---|---|---|
| Create or update a story | yes | no — it asks |
| Grant a resource | yes | no — it asks |
| Start a run | no | **yes** |
| Re-scope an approved story | **no, deliberately** | yes |
| Approve anything | **neither — approval requires a human uid** | |

The orchestrator has no re-scope verb because re-scoping withdraws an approval
and writes a new contract version — the one verb in that family that changes
what a person agreed to.

Six capabilities are absent from the assistant **because no function exists**,
not because a prompt forbids them: dispatch, provision, approve, verify, commit,
and create-a-story. The absence is the boundary.

Starting a story creates an **execution**, and stops at `PENDING`. Its checks,
in order: the task has prose, the story is named, the kind resolves, the kind is
one the kernel runs, the story has mounts — and then an *achievability* test
that refuses criteria which the grants held could never satisfy.

---

## 7. Admission: two ceilings, two mechanisms

Nothing starts until a slot is claimed.

| Ceiling | Value | Enforced by | Exact? |
|---|---|---|---|
| System-wide concurrent executions | **20** | a count, checked before the claim | may overshoot by one per replica |
| Per goal, per owner — orchestrator | **10** | a row claim | exact |
| Per goal, per owner — an assistant | **5** | a row claim | exact |

*(`kernel/conductor/admit.py`, `kernel/conductor/owner.py`.)* Only the system
ceiling reads an environment variable, `AGENTOS_SYSTEM_CEILING`. The per-owner
ceilings are code defaults, copied into the goal's `admission_buckets` row when
the bucket is created; widen one goal by editing that row — the row's ceiling
wins over the default on every later tick.

The bucket key is the **owner**, not the work type: an errand with a live grant
is charged to the orchestrator, everything else to the assistant that started it.

**When full, nothing is dropped.** The row stays `PENDING` with a `stall_reason`
naming *which* ceiling, and the next tick tries again. The invariant across the
whole conductor is that **nothing waits without a reason written on it**.

---

## 8. The pod: two containers, two users

One execution, one pod. The split is the security boundary: the model's shell
never shares a user with the process holding the database.

```
  pod exec-<id>          processNamespace NOT shared
  ├── worker container   kernel image. Holds the DB connection,
  │                      the git credential, the model key.
  └── sandbox container  the goal's own base image, DIFFERENT uid,
                         no secrets. This is where bash runs.

  worker ──POST /tool :9100──► sandbox      (no auth: possession is the boundary)
  conductor ──:9101──► worker               (authenticated, token DERIVED not stored)
  pod ──HTTPS──► control plane              (the pod holds no cloud credential)
```

Process namespaces are deliberately **not** shared: sharing would let the
sandbox read `/proc/<worker>/environ` and take the database password. The SQL
proxy listens on a unix socket on a worker-only volume, never TCP — a TCP
listener would be reachable from the model's own bash, since both containers
share a network namespace.

Port 9100 has no authentication because reachability *is* the boundary. Port
9101 is authenticated, after a live run reached `curl 127.0.0.1:9101` from the
model's own shell and found nine write operations open.

---

## 9. Profile — what an executor is allowed to be

A profile is a directory of data, not a class: a TOML file, a prompt, and
optional skills. The effective tool surface is **(base + add) − deny**, and
every anomaly in that arithmetic is a *load* error, not a runtime refusal.

| Profile | kind / domain / phase | Tools | Notable |
|---|---|---|---|
| `story-coding` | story / coding | **29** | the full executor — shell, commit, subagents |
| `errand-ops` | errand / ops | **25** | no verifier; the exit code is the verdict |
| `story-business` | story / business | **18** | no shell, no commit — documents, not a tree |
| `story-coding-review` | story / coding / **review** | **10** | read-only; own model; 3 legs |

*(counted from `profiles/*/profile.toml` against `profiles/base.toml`)*

**There is no default profile.** An unknown `(kind, domain)` fails at dispatch,
before a container exists — falling back to the coding profile would hand an
unclassified execution `write` and `bash`.

The review profile is the sharpest piece of design here. Same pod, same
workspace, after the work finished: every writing tool is refused *by name*,
including the whole background-job set and `run_check` — because a reviewer that
can run the suite can report its own measurement as a finding about someone
else's work. It loads the project's `AGENTS.md` but not the executor's
`SOUL.md`: a reviewer wearing the author's personality is not an independent
read. And it has **no diff tool** — what moved is computed once and handed over
in the brief, by code that reads trunk from the git graph instead of guessing.

---

## 10. Leg — the unit of work

A leg is one conversation. It starts with an **empty** context, calls the model
until a stop predicate fires, and leaves a brief for the next leg.

| Term | Is | Default |
|---|---|---|
| **Leg** | one conversation, start to stop predicate | 120,000 context tokens · 6,000,000 spend · 200 steps |
| **Generation** | an attempt at a leg; a crashed attempt is over, so the next is a new generation — this integer fences the leases | **12** legs (a review gets **3**) |
| **Step** | one model turn plus at most one tool call | journalled **before** the tool runs |

*(`kernel/harness/models/config.py::LegBudget`)*

Halving the context budget does not halve cost — it doubles the number of legs,
and every boundary costs several thousand tokens to re-orient. So a small leg
wants a *larger* ceiling, not the same one.

**A goal can choose its own.** The goal's **Model Configuration** page sets, for
that goal's executor:

| Setting | Range | Blank means |
|---|---|---|
| Executor model | any model in the registry | the default for the work |
| Verifier model | any model in the registry | the default for the work |
| Machine size | `small` · `medium` · `large` (a story's own size wins over it) | the profile's size, else `small` |
| Tokens per leg | 20,000 – 1,000,000, capped to what the model can hold with room for its reply | the model's and profile's default |
| Max legs | 1 – 200, or `-1` for no limit | 12 |

The choice is **frozen onto the run at dispatch** (`executions.leg_budget`,
migration `0008_goal_leg_budget`), so editing the goal changes the next run,
never a running one. It governs the work phase only: the review keeps its own
budget, because a goal that asked for short work legs did not ask for a review
that cannot finish. *(`kernel/harness/models/leg_choice.py`,
`kernel/conductor/size_choice.py`)*

**Six stages wrap every tool call**, and a denial in the first four
short-circuits:

```
 1 envelope   is the path inside the writable scope?
 2 policy     is this tool on this profile's effective list?   (fail-closed)
 3 HITL       does this call's effect need a person?
 4 budget
 5 execute
 6 audit
```

Each tool is declared once with a **side** (`sandbox`, `worker`, or `loop`) and
an **effect** (`contained`, `reversible`, or `by_argument` — `bash` is the case
that made the third necessary). A profile may loosen the effect policy only as
far as approving contained or reversible calls. **`irreversible` can never be
loosened**, and naming it in a profile is a refusal to start.

---

## 11. Park — how work asks for a person

Not an error, and not a budget ending: a run that cannot proceed until somebody
acts. The leg ends, the journal holds what happened, and the next leg starts
with the answer in front of it.

| Kind | Opened by | Who may answer |
|---|---|---|
| `clarification` | the executor asking a question | **anyone**, including an assistant |
| `authorization` | registering a plan | a person — one orchestrator exemption |
| `acceptance` | a completion claim | a person only |
| `permission` | a single irreversible call | a person only |
| `escalation` | the harness, after repeated refusals | a person only |
| `direction` | a person pausing the run | a person only |
| `closeout` | the work is on trunk; is the story finished? | a person only |
| `pr_closed` | a person closed the PR without merging | a person only |
| `landing` | waiting for the branch to reach trunk | **nobody** — the merge is the answer |

*(`kernel/harness/park_kinds.py`, `kernel/mailbox/schema.py`)*

`clarification` is deliberately outside the human-only set — an assistant may
answer one, which is what makes asking cheap. `permission` is the sharpest case:
an agent able to answer its own *may I run `terraform destroy`* is no gate at
all. And `landing` is the one nobody answers, because a card asking a person to
confirm what the engine is already watching for would be a second authority over
one fact, free to disagree with the first.

**Approving is a human act, enforced at one seam.** Resolving raises
`E_APPROVAL_NOT_HUMAN` on a blank human uid, and that uid defaults to nothing
unless a call site supplies one. The field an agent fills in cannot serve that
role. The single exemption — an autonomous errand's *plan*, settled by the
orchestrator — re-checks the grant itself rather than trusting a flag. Acceptance
is never covered by it.

Asking is not free: a question demands **three distinct prior attempt
signatures** before it will park. `permission` has no such floor — the executor
is not stuck, it is holding a loaded gun and asking whether to fire.

---

## 12. Completion, and three different self-checks

The executor believes it is done. Three separate mechanisms disagree about what
that means, and conflating them is the easiest mistake in the system.

| Check | Measures | Authority |
|---|---|---|
| Executor self-check, at the claim | **artifacts only**, from the manifest | blocks the claim; returns as a tool result |
| Worker record check | artifacts from the record; commands `unmeasured` | on pass, parks for acceptance |
| **The Verifier** | commands re-run in a **clean container, at the contract's digest, different uid, committed tree** | writes the verdict; moves the machine |

Command checks are deliberately *not* re-run at the claim: the shell is capped
at 120 seconds, so a slow suite could never be claimed.

Verification is **off by default**, consulted at exactly one place on the way
in, so `VERIFYING` is unreachable rather than reachable-and-abandoned. When it
is on, no model writes the verdict: the one model in that path can only
*refuse*, and every ambiguous outcome — no key, timeout, adapter error —
accepts.

**Done means delivered.** Publish happens *before* anything else on the finish
path. The first live story passed with seven commits still local to the sandbox,
so the order is now fixed: publish, then park for acceptance, then finish. If the
acceptance park cannot be opened, the run **stalls loudly rather than
finishing** — a gate that opens when its own machinery fails is not a gate.

One person-free `DONE` remains: an errand whose declared terminal command exits
zero. That is the whole of what "no reviewer" buys.

---

## 13. The run state machine

Ten states, twenty-five declared transitions, versioned and immutable. **Version
5** is current, and every execution records which version it ran under
(`seed/run_state_machine.json`).

```
          ┌─────────────────────────── cancel ──────────────────────────┐
          │                                                             ▼
   PENDING ──admitted──► ADMITTED ──spec_ready──► DISPATCHING ──lease──► RUNNING
      ▲                                               ▲                  │
      │                                               │     ┌────────────┼────────────┐
      │                                        resume │     ▼            ▼            ▼
      │                                          PARKED_COLD ◄─demote─ PARKED_HOT  VERIFYING
      │                                                                              │
      └──────────────── reopened ──── DONE ◄──accepted / verified / errand_terminal──┘

   terminal: DONE, FAILED, CANCELLED          FAILED is a dead end — no edge leaves it
```

Two decisions worth naming. There is no `accepted` edge from `PENDING`,
`ADMITTED` or `DISPATCHING` — a story that never ran has no work to accept. And
re-scoping a finished story takes `DONE → PENDING`, not `DONE → RUNNING`,
because the admission slot was released at `DONE`; skipping admission would let
a goal exceed its ceiling by finishing and reopening.

**The machine is logged, not enforced.** A trigger records every transition;
nothing validates one before writing it. Validating would turn an undeclared
transition into a hard failure, so the console flags them instead. Three places
consult the machine voluntarily: accept, cancel-when-no-leg, and reopen.

---

## 14. When it goes wrong

The failure design is mostly bounded counters and level-triggered sweeps rather
than retries.

| Counter | Bound | Then |
|---|---|---|
| Undecidable verification | 3 | back to `RUNNING`, with the reason as a steer |
| Adjudicator crash | 3 | park on an escalation |
| The same refusal, repeated | 2 | park asking whether the *bar* is wrong |
| Legs without converging | 12 by default; the goal's max legs if set | `FAILED` — except a review, which ends `DONE` |
| Identical step signatures | 8 | the leg ends, stalled |

A pod that will not schedule is read from its own `PodScheduled` condition,
which separates *quota refused* (budget shortened to 30s, so it stops squatting
on the quota it is queued for) from *no node yet* (the full wait) from
*installing*.

Cancellation never writes `CANCELLED` directly. It goes through the mailbox, so
the lease, the slot, the pod and the journal all follow.

The reconcile sweep runs under a global advisory lock, and **dry run is the
default** — deleting anything requires an explicit environment flag. With an
early labelling bug, a live sweep would have had the conductor delete itself and
its own egress proxy on every pass.

---

## Where to go next

- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the same system by component rather
  than by chronology.
- [`kernel/README.md`](kernel/README.md) — the plane that decides when work runs.
- [`deploy/README.md`](deploy/README.md) — standing it up.
- [`preflight.sh`](preflight.sh) — asking whether what you stood up is complete.
