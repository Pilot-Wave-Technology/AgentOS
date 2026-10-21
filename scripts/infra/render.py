#!/usr/bin/env python3
"""render.py <template> — substitute ${NAME} from the environment.

    python3 scripts/infra/render.py deploy/k8s/conductor.yaml | kubectl apply -f -

WHY NOT envsubst. Two reasons, and the first is simply that it is not
there: `gcr.io/google.com/cloudsdktool/cloud-sdk` ships kubectl and
gke-gcloud-auth-plugin but NOT gettext, so `envsubst` is a command-not-
found in the one image the deploy runs in. A comment in conductor.yaml
asserted the opposite for several commits.

The second reason is why this is not just `apt-get install gettext-base`.
envsubst REPLACES AN UNSET VARIABLE WITH THE EMPTY STRING. A manifest
rendered with ARTIFACT_BUCKET unexported does not fail — it applies,
with `value: ""`, and the conductor starts and writes every artifact to
a bucket named nothing. That is the class of failure this repository
keeps paying for: correct-looking output from a step that silently did
the wrong thing.

So: every ${NAME} in the template must be set and non-empty, or nothing
is rendered and the names are listed. A deploy that cannot name its
bucket should stop at the deploy, not three hours later in a log.
"""

from __future__ import annotations

import os
import re
import sys

# Upper-case only, which is what every substitution in these manifests
# is. Narrow on purpose: a shell snippet inside a ConfigMap may contain
# `${lower}` that belongs to the shell and not to this.
PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")


def render(text: str, env: dict) -> str:
    """The template with every ${NAME} replaced, or a ValueError."""
    wanted = set(PATTERN.findall(text))
    missing = sorted(name for name in wanted if not env.get(name))
    if missing:
        raise ValueError(
            "these are unset or empty, and rendering with them blank "
            "would apply a manifest that names nothing: "
            + ", ".join(missing))
    return PATTERN.sub(lambda m: env[m.group(1)], text)


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} <template>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8") as handle:
            rendered = render(handle.read(), dict(os.environ))
    except (OSError, ValueError) as exc:
        print(f"render: {exc}", file=sys.stderr)
        return 1

    # BYTES, AND UTF-8 NAMED. `sys.stdout.write` encodes with the
    # locale's codec, which on Windows is cp1252 — and these manifests
    # are full of box-drawing characters in their comments, so the write
    # raises and the pipeline delivers an EMPTY document that kubectl
    # accepts as "nothing to apply".
    #
    # G8 found exactly this once already, in a subprocess reader that
    # decoded cp1252 and came back empty while reporting success. The
    # deploy runs on Linux where the default happens to be UTF-8, which
    # is why a bug like this survives: it only appears where somebody
    # tests it by hand.
    sys.stdout.buffer.write(rendered.encode("utf-8"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
