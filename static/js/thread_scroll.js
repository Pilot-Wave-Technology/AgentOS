/* Shared scroll management for paginated message threads.
 *
 * Used by the GO thread and the three read-only channels (council, security,
 * orchestrator). All four render only the newest window of messages and page
 * backwards on demand, which creates two problems that this module owns:
 *
 *  1. Appends must not yank a reader who has scrolled up. Every thread used to
 *     force scrollTop = scrollHeight unconditionally on each new message and
 *     indicator. Harmless while the page always sat at the bottom; hostile once
 *     "Load earlier" gives people a reason to read history. The GO thread's
 *     streaming indicator was the worst case — it fires once per token, so it
 *     dragged the viewport down continuously for a whole response.
 *
 *  2. Prepending older messages grows the container ABOVE the viewport, shifting
 *     whatever the reader is looking at. The height delta must be re-applied
 *     after the swap.
 *
 * Usage:
 *   const scroll = initThreadScroll({
 *     container: document.getElementById('messages'),
 *     pill: document.getElementById('new-messages-pill'),   // optional
 *     onInserted: (container) => { ...hydrate newly prepended rows... },
 *   });
 *   scroll.onAppend(true);    // after inserting real content
 *   scroll.onAppend(false);   // after showing a transient indicator
 *   scroll.jumpToLatest();
 */

// "Within 80px of the bottom" counts as pinned: absorbs sub-pixel rounding and
// a partially visible last line, so ordinary reading at the bottom still
// follows the conversation.
const THREAD_SCROLL_PIN_THRESHOLD_PX = 80;

function initThreadScroll(options) {
  const container = options.container;
  const pill = options.pill || null;
  const onInserted = options.onInserted || null;

  if (!container) {
    console.warn('initThreadScroll: no container; scroll management disabled');
    return { onAppend() {}, jumpToLatest() {}, isPinnedToBottom: () => true };
  }

  function isPinnedToBottom() {
    return (container.scrollHeight - container.scrollTop - container.clientHeight)
           <= THREAD_SCROLL_PIN_THRESHOLD_PX;
  }

  function setPillVisible(visible) {
    if (pill) pill.classList.toggle('hidden', !visible);
  }

  function jumpToLatest() {
    container.scrollTop = container.scrollHeight;
    setPillVisible(false);
  }

  /* Follow the conversation only if the reader had not scrolled up.
   *
   * `announce` marks appends carrying real content — those raise the pill when
   * the scroll is suppressed, because silently hiding an incoming message is
   * worse than the yank we are removing. Transient indicators ("thinking…",
   * per-token streaming) pass false so they never claim a message arrived. */
  function onAppend(announce) {
    if (isPinnedToBottom()) {
      container.scrollTop = container.scrollHeight;
    } else if (announce) {
      setPillVisible(true);
    }
  }

  // Scrolling back down manually dismisses the pill.
  container.addEventListener('scroll', () => {
    if (isPinnedToBottom()) setPillVisible(false);
  });

  if (pill) pill.addEventListener('click', jumpToLatest);

  // ── "Load earlier" scroll anchoring ──
  // htmx replaces #load-earlier with [next sentinel + older messages]. Capture
  // the geometry before the swap and re-apply the inserted height afterwards so
  // the reader stays on the message they were reading.
  let preSwapScrollHeight = 0;
  let preSwapScrollTop = 0;

  document.body.addEventListener('htmx:beforeSwap', (evt) => {
    if (evt.target && evt.target.id === 'load-earlier') {
      preSwapScrollHeight = container.scrollHeight;
      preSwapScrollTop = container.scrollTop;
    }
  });

  document.body.addEventListener('htmx:afterSwap', () => {
    // For an outerHTML swap evt.target is the parent, not #load-earlier, so we
    // gate on a pending capture rather than the element id.
    if (!preSwapScrollHeight) return;

    // Hydrate BEFORE measuring: markdown rendering changes row heights, so a
    // delta taken pre-hydration would leave the viewport off by the difference.
    if (onInserted) {
      try {
        onInserted(container);
      } catch (e) {
        console.warn('initThreadScroll: onInserted hook failed:', e);
      }
    }

    const insertedHeight = container.scrollHeight - preSwapScrollHeight;
    container.scrollTop = preSwapScrollTop + insertedHeight;
    preSwapScrollHeight = 0;
  });

  // Threads always open on the newest messages.
  container.scrollTop = container.scrollHeight;

  return { onAppend, jumpToLatest, isPinnedToBottom };
}
