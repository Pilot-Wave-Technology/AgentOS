# Notion Inbound — Event Handling

Receives Notion webhooks and routes them to the correct AgentOS session.

---

## Files

| File | Purpose |
|---|---|
| `__init__.py` | `NotionInboundAdapter` — verify signatures, dispatch event types |
| `comments.py` | `_handle_comment` — full routing pipeline for `comment.created` |
| `attachments.py` | `_enrich_author_email` — fetch author email via Notion Users API |
| `page_updates.py` | `_handle_page_update` — stub routing for `page.content_updated` |

---

## NotionInboundAdapter (`__init__.py`)

Registered in `channels/registry.py` as the handler for platform slug `notion`.
`NotionBotAdapter` (`notion_bot/adapter.py`) subclasses this and overrides
`verify_request` to always return `True`.

```python
class NotionInboundAdapter:
    def verify_request(body, headers) -> bool
        # HMAC-SHA256 of raw body against signing secret from DB or env
        # Returns True in dev mode (NOTION_WEBHOOK_SECRET not set)

    async def handle_event(body, headers) -> None
        # 1. Dedup by x-notion-delivery-id
        # 2. Route comment.created  → _handle_comment(payload)
        # 3. Route page.content_updated → _handle_page_update(payload)
        # 4. All other types: log INFO, ignore
```

---

## Comment Routing Pipeline (`comments.py`)

`_handle_comment` is the main inbound pipeline. Steps in order:

```
1. Extract page_id, comment_id, author from payload
2. Drop if author.type == "bot"                        (echo prevention layer 1)
3. Drop if comment_id in messages.external_message_id  (echo prevention layer 2)
4. get_binding("notion", page_id)
   Not found → look up any active integration → post redirect comment → return
5. Drop if author.id == integration.bot_external_id    (echo prevention layer 3)
6. Resolve access_token for the integration
7. _enrich_author_email(author, access_token)          (adds email field to author dict)
8. resolve_notion_actor(author, email_domain_allowlist)
   Returns None (unresolved/pending_review) → drop
9. Member-page owner check:
   binding.actor_id IS NOT NULL AND commenter.actor_id != binding.actor_id
       → post static reply ("this page belongs to another member") → return
10. Fetch full comment text via notion_client.comments.retrieve(comment_id)
11. Look up sender uid + display_name from users table
12. route_to_session(binding, sender, text, platform="notion", sender_name=...)
```

### Redirect replies (no binding)

When `get_binding` finds no row for the page, a static comment is posted back to
Notion telling the user to use their personal member page. This handles comments
left on story pages (which are view-only) or on pages that haven't been provisioned.

### Wrong-owner replies (member page)

Member pages are bound to a specific user (`binding.actor_id IS NOT NULL`). If
someone else comments, a static reply is posted and the message is dropped. This
prevents cross-user message bleed on member pages.

---

## Author Enrichment (`attachments.py`)

`_enrich_author_email(author, access_token)` calls the Notion Users API
(`GET /users/{author.id}`) to add the `email` field to the author dict. This is
needed because Notion webhooks do not include email in the `authors` array.

Requires an OAuth token with `user.email` scope. Falls through silently if the
API call fails or the token is missing — actor resolution then proceeds without
email (which may result in `pending_review`).

---

## Page Update Routing (`page_updates.py`)

`_handle_page_update` sends a stub message `[Notion page updated: <page_id>]`
to the session bound to the updated page. Used to signal the Assistant that a page
it cares about has changed, without delivering full content (Notion doesn't
include page content in webhooks).
