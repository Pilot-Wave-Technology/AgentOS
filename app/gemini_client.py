"""Lightweight Gemini API client for summarization tasks."""
import logging

from google import genai
from google.genai import types

from app.config import get_settings

_log = logging.getLogger(__name__)

_SUMMARY_MODEL = "gemini-2.5-flash"

_SUMMARY_PROMPT = """You are summarizing a conversation between a human and an AI project manager agent.
The agent is about to resume this conversation after a break and needs context.

Summarize the following conversation history. Focus on:
1. Key decisions made (tech stack, architecture, approach)
2. Tasks created or discussed (with their status if mentioned)
3. Current state of work — what's done, what's in progress, what's pending
4. User preferences and constraints mentioned
5. Any open questions or blockers

Give MORE detail to recent messages and LESS to older ones.
Keep the summary under 1500 characters.
Do not include pleasantries or meta-commentary. Be factual and concise.

CONVERSATION:
{conversation}"""


async def summarize_conversation(messages: list[dict]) -> str:
    """Call Gemini to summarize a conversation for agent context.

    Args:
        messages: List of dicts with keys: role, content, created_at

    Returns:
        Summary string (max ~1500 chars)
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        _log.warning("GEMINI_API_KEY not configured — skipping summarization")
        return ""

    # Format messages for the prompt
    conversation_lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")
        ts = msg.get("created_at", "")
        if ts:
            ts_str = ts.strftime("%m/%d %H:%M") if hasattr(ts, "strftime") else str(ts)[:16]
            conversation_lines.append(f"[{role} {ts_str}] {content}")
        else:
            conversation_lines.append(f"[{role}] {content}")

    conversation_text = "\n".join(conversation_lines)
    prompt = _SUMMARY_PROMPT.format(conversation=conversation_text)

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model=_SUMMARY_MODEL,
            contents=prompt,
        )
        summary = response.text.strip()
        _log.info("Generated conversation summary (%d chars)", len(summary))
        return summary
    except Exception as exc:
        _log.warning("Gemini summarization failed: %s", exc)
        return ""


async def reconcile_goal_summary(system_prompt: str, user_prompt: str) -> str:
    """Run the rolling goal-summary reconciliation turn.

    The caller composes user_prompt (current summary + new activity + story
    snapshot) and passes the system_prompt separately. Returns the updated
    summary text, or "" on failure (caller leaves the prior summary intact).
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        _log.warning("GEMINI_API_KEY not configured — skipping goal summary")
        return ""

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model=_SUMMARY_MODEL,
            contents=user_prompt,
            config=types.GenerateContentConfig(system_instruction=system_prompt),
        )
        return (response.text or "").strip()
    except Exception as exc:
        _log.warning("Goal summary reconciliation failed: %s", exc)
        return ""


_TITLE_PROMPT = """Generate a short title (max 10 words) for this conversation between a human and an AI project manager.
The title should capture the main topic or goal discussed.

Rules:
- Maximum 10 words
- No quotes, no punctuation at the end
- Be specific (not "General Discussion" or "Chat")
- Use title case
{uniqueness_hint}

CONVERSATION:
{conversation}

TITLE:"""


async def generate_thread_title(
    messages: list[dict],
    existing_titles: list[str] | None = None,
) -> str:
    """Generate a short title for a conversation thread.

    Args:
        messages: List of dicts with keys: role, content
        existing_titles: Titles already used (to avoid duplicates)

    Returns:
        Title string (max ~10 words), or empty string on failure.
    """
    settings = get_settings()
    if not settings.gemini_api_key:
        return ""

    conversation_lines = []
    for msg in messages:
        role = msg.get("role", "unknown")
        content = msg.get("content", "")[:500]
        conversation_lines.append(f"[{role}] {content}")

    conversation_text = "\n".join(conversation_lines)

    uniqueness_hint = ""
    if existing_titles:
        uniqueness_hint = f"- Do NOT use any of these existing titles: {', '.join(existing_titles)}"

    prompt = _TITLE_PROMPT.format(
        conversation=conversation_text,
        uniqueness_hint=uniqueness_hint,
    )

    try:
        client = genai.Client(api_key=settings.gemini_api_key)
        response = await client.aio.models.generate_content(
            model=_SUMMARY_MODEL,
            contents=prompt,
        )
        title = response.text.strip().strip('"').strip("'")
        # Enforce max 10 words
        words = title.split()
        if len(words) > 10:
            title = " ".join(words[:10])
        _log.info("Generated thread title: %s", title)
        return title
    except Exception as exc:
        _log.warning("Thread title generation failed: %s", exc)
        return ""
