"""The dialect intersection — one tool schema that every provider accepts.

Two problems, opposite directions, both carried as Stage-E debt since ES3.

SCHEMAS GOING OUT. JSON Schema is a large spec and providers implement
different subsets. Gemini's function declarations reject `$ref`,
`additionalProperties`, `oneOf`/`anyOf`/`allOf` and `default`; Anthropic
accepts them. A schema authored against Anthropic therefore fails on
Gemini at call time, in the provider's words, mid-story.

The fix is not to strip the offending keywords per-provider. Stripping
changes what the schema MEANS — dropping `oneOf` from a two-shape argument
leaves a schema that accepts neither shape correctly — and it does so
silently, on one provider only. Instead the catalog is authored in the
intersection and `validate_schema` fails loudly at load if it is not.
Strictness at authoring time beats a rewrite at call time, because the
author can see the error and a running story cannot.

NAMES COMING BACK. Gemini returns function calls under a namespace
(`default_api:read`) while the journal, the predicates and the progress
clock all key on the bare name. Unnormalised, the same tool called on two
providers produces two different journal rows, and LR5's progress clock
sees a model alternating tools when it is repeating one.
"""

from __future__ import annotations

# Accepted by every adapter we support. Deliberately small: a keyword
# earns its place here by working on all of them, not by being useful.
ALLOWED_KEYWORDS = frozenset({
    "type", "description", "properties", "required", "items", "enum",
    "nullable", "format",
})

# Rejected with a reason, so the error says what to do instead rather than
# just naming the offending key.
REJECTED_KEYWORDS = {
    "$ref": "inline the definition — Gemini does not resolve references",
    "$defs": "inline the definitions — Gemini does not resolve references",
    "definitions": "inline the definitions — Gemini does not resolve references",
    "additionalProperties": "Gemini rejects it; omit and document the shape "
                            "in the description",
    "oneOf": "Gemini rejects it; use one shape and branch inside the tool",
    "anyOf": "Gemini rejects it; use one shape and branch inside the tool",
    "allOf": "Gemini rejects it; inline the merged shape",
    "not": "Gemini rejects it",
    "patternProperties": "Gemini rejects it",
    "default": "Gemini rejects it; state the default in the description and "
               "apply it in the tool",
}

# Namespaces providers wrap tool names in. Stripped so the journal, the
# predicates and the progress clock all see the same bare name whichever
# provider produced it.
_NAME_PREFIXES = ("default_api:", "default_api.", "functions.")


class SchemaDialectError(Exception):
    """A tool schema uses a keyword some provider will reject."""


def _walk(node, path: str, problems: list[str]) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            here = f"{path}.{key}" if path else key
            if key in REJECTED_KEYWORDS:
                problems.append(f"{here}: {REJECTED_KEYWORDS[key]}")
            elif path.endswith("properties") or path.endswith("items"):
                # Inside a properties/items map the keys are FIELD names,
                # not schema keywords — do not judge them.
                pass
            elif key not in ALLOWED_KEYWORDS and not path.endswith("properties"):
                problems.append(
                    f"{here}: unknown keyword; the intersection is "
                    f"{', '.join(sorted(ALLOWED_KEYWORDS))}"
                )
            _walk(value, here, problems)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            _walk(item, f"{path}[{i}]", problems)


def validate_schema(tool: dict, where: str = "") -> None:
    """Raise if this tool's parameters use anything outside the intersection.

    Called on the whole catalog by CI, so a schema that only works on one
    provider is caught by a test rather than by a story failing on the
    other one.
    """
    params = tool.get("parameters")
    if params is None:
        return
    problems: list[str] = []
    # Start the walk INSIDE parameters: the top level is a schema object,
    # and its `properties` map holds field names rather than keywords.
    for key, value in params.items():
        if key in REJECTED_KEYWORDS:
            problems.append(f"{key}: {REJECTED_KEYWORDS[key]}")
            continue
        if key == "properties" and isinstance(value, dict):
            for field, subschema in value.items():
                _walk(subschema, f"properties.{field}", problems)
            continue
        if key not in ALLOWED_KEYWORDS:
            problems.append(
                f"{key}: unknown keyword; the intersection is "
                f"{', '.join(sorted(ALLOWED_KEYWORDS))}"
            )
        else:
            _walk(value, key, problems)

    if problems:
        name = tool.get("name", "<unnamed>")
        prefix = f"{where}: " if where else ""
        raise SchemaDialectError(
            f"{prefix}tool {name!r} is not in the provider dialect "
            f"intersection:\n  - " + "\n  - ".join(problems)
        )


def normalise_tool_name(name: str) -> str:
    """Strip a provider's namespace so every layer sees the bare name.

    Applied in the adapter, at the point the provider's own name enters
    the harness. Anywhere later is too late: the journal row, the
    idempotency key and LR5's progress hash are all written from it.
    """
    if not name:
        return name
    for prefix in _NAME_PREFIXES:
        if name.startswith(prefix):
            return name[len(prefix):]
    return name
