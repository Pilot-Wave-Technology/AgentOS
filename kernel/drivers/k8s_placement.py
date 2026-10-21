"""One story per machine, said in the only language the scheduler reads.

THE SELECTOR IS NOT ENOUGH, which is the thing to understand before
changing anything here. `nodeSelector: agentos.io/node-class=small` says
WHICH POOL a pod belongs in. It says nothing about how many pods share a
machine once they are in it, and the default scheduler's whole instinct
is to pack: given an e2-standard-4 holding one 3.25-CPU story, it will
happily add anything that fits in what is left, and given two stories
that each fit in half a node it will put them on one node and leave the
other empty.

Packing is the right instinct for web servers and the wrong one here. A
story is a build: it compiles, it runs a test suite, it saturates
whatever it is given for minutes at a time. Two stories on one machine
do not run at half speed each, they run at rather worse than half speed
each, because they contend for cache and memory bandwidth as well as for
cores -- and neither of them fails, so the only evidence is that
everything got slower and nobody can say when.

So the pool answers WHERE and the anti-affinity answers HOW MANY. They
are separate mechanisms and both are required; removing either one
re-opens the same door from a different side.

WHY A LABEL RATHER THAN THE APP NAME. The rule has to cover execution
pods and verification Jobs together -- a verifier re-running a story's
test suite is exactly as hungry as the story was, and a verifier landing
on a busy story's machine is the same defect wearing a different name.
Those two carry different `app` labels for reasons that predate this
file (kernel/drivers/k8s_spec.py explains the NetworkPolicy that reads
them), so the rule selects on a label that means what the rule is about:
`agentos.io/kind`, one of `execution` or `verification`, with an
`In (execution, verification)` match that covers both and a third kind
the day there is one.

THE WARM PLACEHOLDERS ARE DELIBERATELY EXCLUDED, and deploy/k8s/
warm-pool.yaml says so on its own side too. A pause pod's entire job is
to hold a machine open until a real story evicts it; giving it this
label would make it repel the story it exists to make room for.
"""
from __future__ import annotations

# The label, its two values, and the topology the rule spreads over.
KIND_LABEL = "agentos.io/kind"
EXECUTION = "execution"
VERIFICATION = "verification"

# Every value the rule repels. Listed rather than inferred from the
# pod's own label, because the rule is "no OTHER work on this machine",
# not "no other work of my own kind" -- a verifier must repel stories
# and a story must repel verifiers, and both fall out of one list.
WORK = (EXECUTION, VERIFICATION)

# `kubernetes.io/hostname` is the machine. Spreading over a zone or a
# region instead would be a different and much weaker claim.
TOPOLOGY = "kubernetes.io/hostname"


def kind_label(kind: str) -> dict:
    """The one label that makes a pod visible to the rule below."""
    if kind not in WORK:
        raise ValueError(
            f"{kind!r} is not work: expected one of {', '.join(WORK)}. A "
            f"pod that is not work must not carry {KIND_LABEL} at all -- "
            f"see the warm placeholders, which depend on being invisible "
            f"to the anti-affinity rule.")
    return {KIND_LABEL: kind}


def one_per_node() -> dict:
    """`spec.affinity`: no other work on this machine.

    REQUIRED, NOT PREFERRED, and the difference is the whole point.
    `preferredDuringScheduling` is a hint the scheduler may ignore under
    pressure -- which is to say it holds exactly until the fleet is busy,
    and then stops holding at the moment it was supposed to matter. The
    cost of REQUIRED is that a story waits for a machine instead of
    sharing one; with a pool that autoscales, waiting is measured in the
    ninety seconds a node takes to arrive.

    There is no `IgnoredDuringExecution` counterpart to choose: pods are
    not rescheduled once bound, so this is enforced at placement and
    never revisited.
    """
    return {
        "podAntiAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": [{
                "labelSelector": {
                    "matchExpressions": [{
                        "key": KIND_LABEL,
                        "operator": "In",
                        "values": list(WORK),
                    }],
                },
                "topologyKey": TOPOLOGY,
            }],
        },
    }
