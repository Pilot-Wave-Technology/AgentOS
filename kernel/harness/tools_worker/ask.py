"""`ask_human` — stop and ask, with evidence.

Worker-side because it writes to the database and parks the run:
parking is a state transition on the execution, not an effect on a
workspace, so the sandbox has nothing to do with it.

Classified `contained`. ASKING IS NOT AN EFFECT — and classifying it
otherwise, which is what leaving it out of the effects table did,
meant the tool whose entire purpose is to reach a person needed a
person's approval to run.
"""
from __future__ import annotations


from kernel.harness.models.types import ToolResult

async def ask_human(conn, execution_id: int, generation: int,
                    tool_args: dict, tool_call_id: str) -> ToolResult:
    """E6i — park the run on a question. The leg ends; the answer arrives next.

    Raises ParkRequested rather than returning normally: the leg has to END,
    or the tool would hold the lease and burn the wall clock waiting for a
    person, then die on the lease predicate with the question unanswered.
    """
    from kernel.harness.park import ParkRequested, open_request

    question = (tool_args.get("question") or "").strip()
    if not question:
        return ToolResult(tool_call_id=tool_call_id, output="",
                          error="ask_human: question is required", exit_code=1)

    # H1's evidence floor: what was tried, what happens if nobody answers,
    # and what this blocks. Validated in kernel.mailbox.schema, not here —
    # one definition of a well-formed ask, wherever it comes from.
    attempts = tool_args.get("attempts")
    payload = {
        "question": question,
        "context": tool_args.get("context", ""),
        "attempts": attempts if isinstance(attempts, list) else [],
        "proposed": (tool_args.get("proposed") or "").strip(),
        "blocking_on": (tool_args.get("blocking_on") or "").strip(),
        "reversible": bool(tool_args.get("reversible", True)),
    }
    try:
        request_id = await open_request(
            conn, execution_id, generation, "question", payload)
    except Exception as e:
        # The one-open-request index refuses a second park. Tell the model
        # plainly: it is already waiting, and asking again changes nothing.
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"ask_human: this run is already waiting on a human ({e})",
            exit_code=1)

    if not isinstance(request_id, int):
        # A refusal the model can act on, not a park. It gets the code and
        # the reason and keeps working — which is the point of the floor:
        # the cheap outcome of asking too early is another tool call, not
        # a stopped run and an interrupted person.
        return ToolResult(
            tool_call_id=tool_call_id, output="",
            error=f"ask_human refused [{request_id.code}]: "
                  f"{request_id.message}",
            exit_code=1)

    raise ParkRequested(request_id, "question",
                        f"parked on a question: {question[:120]}")
