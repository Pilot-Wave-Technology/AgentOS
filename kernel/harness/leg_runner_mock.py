"""The mock adapter the kill tests need, out of the runner.

A LEG THAT STAYS BUSY. `test_e5_kill` SIGKILLs the runner mid-step to
prove that a killed leg leaves a journal a later generation can be
fenced against, and a mock that answers instantly would be killed
between legs rather than inside one -- which proves the opposite of
what the test is for. So this makes `--mock-steps` bash calls before it
ends the turn, and each of those is a real round trip to the shim.

WHY IT MOVED OUT AT D2, and it is not only the 200-line ceiling.
`leg_runner` stopped being a test CLI and became the pod's leg entry
point (plan §6), so it is production code now and a mock adapter
declared in the middle of it is a fixture in the serving path. Keeping
it importable under its own name is also what stops the next person
copying it into a test: it is the ONE busy adapter, and two of them
would drift the way two worker loops did.

NOT A `models/` ADAPTER. It is not registered, `get_adapter` has never
heard of it, and it must stay that way -- `registry` is the list of
providers a run may be dispatched against, and a fake in it is a fake
one config typo away from serving a story.
"""

from __future__ import annotations


def build_mock_adapter(num_steps: int):
    """Build a mock adapter that makes num_steps bash calls then ends."""
    from kernel.harness.usage import NormalizedUsage
    from kernel.harness.models.types import (
        AdapterCapabilities,
        CompletionRequest,
        CompletionResponse,
    )

    class BusyMockAdapter:
        """Mock adapter that makes many bash calls — stays busy long enough
        for SIGKILL to land mid-step.
        """

        def __init__(self, steps: int):
            self._steps = steps
            self._call_count = 0

        @property
        def name(self) -> str:
            return "busy-mock"

        @property
        def capabilities(self) -> AdapterCapabilities:
            return AdapterCapabilities(
                reasoning="reported",
                leg_token_budget=100_000,
            )

        async def complete(self, request: CompletionRequest) -> CompletionResponse:
            self._call_count += 1
            usage = NormalizedUsage(input=10, output=5)

            if self._call_count <= self._steps:
                return CompletionResponse(
                    tool_call={
                        "tool": "bash",
                        "args": {"command": f"echo step_{self._call_count}"},
                        "id": f"tc-mock-{self._call_count}",
                    },
                    usage=usage,
                    stop_reason="tool_call",
                )
            else:
                return CompletionResponse(stop_reason="end_turn", usage=usage)

    return BusyMockAdapter(num_steps)
