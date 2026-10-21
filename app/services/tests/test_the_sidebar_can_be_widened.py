"""The sidebar is draggable, and collapsing it still works.

WHY IT NEEDED WIDENING. 240px truncated long goal names already, and
thread rows now carry six-word story titles instead of ids -- so the
default cuts more than it used to, on exactly the text that was added to
make the list readable.

THE BUG THIS SHAPE AVOIDS. The obvious implementation sets
`style.width` on the nav. `.sidebar-collapsed` declares a fixed 68px,
and an inline width beats a class -- so collapsing a widened sidebar
would leave a 400px rail of centred icons, and the only way out would be
to widen it back and collapse again. Writing a CSS VARIABLE instead
means only `.sidebar-expanded` reads it: collapse ignores it entirely
and expanding restores whatever the person chose.
"""

from __future__ import annotations

import io


def _css() -> str:
    return io.open("static/css/app.css", encoding="utf-8").read()


def _js() -> str:
    return io.open("static/js/app.js", encoding="utf-8").read()


def _base() -> str:
    return io.open("app/templates/base.html", encoding="utf-8").read()


def test_the_handle_exists_and_is_labelled():
    body = _base()
    assert 'id="sidebar-resizer"' in body
    assert 'aria-label="Resize sidebar"' in body, (
        "a drag target with no name is invisible to anyone not using a "
        "mouse")


def test_only_the_expanded_state_reads_the_custom_width():
    """THE COLLAPSE BUG. If `.sidebar-collapsed` also read it, a widened
    sidebar would collapse to a wide rail of icons."""
    css = _css()
    expanded = css.split(".sidebar-expanded {")[1].split("}")[0]
    collapsed = css.split(".sidebar-collapsed {")[1].split("}")[0]
    assert "--sidebar-user-width" in expanded
    assert "--sidebar-user-width" not in collapsed
    assert "68px" in collapsed, "the collapsed rail stays fixed"


def test_the_javascript_sets_a_variable_not_a_width():
    """`style.width` would beat the collapsed class. This is the whole
    reason the implementation looks indirect."""
    js = _js()
    i = js.index("Sidebar resize")
    block = js[i:]
    assert "setProperty('--sidebar-user-width'" in block
    assert ".style.width" not in block, (
        "an inline width defeats .sidebar-collapsed")


def test_the_width_is_bounded():
    """Unbounded, a drag can leave the sidebar one pixel wide with no
    handle left to grab."""
    js = _js()
    i = js.index("Sidebar resize")
    block = js[i:]
    assert "MIN = 180" in block and "MAX = 560" in block
    assert "clamp(" in block

    css = _css()
    expanded = css.split(".sidebar-expanded {")[1].split("}")[0]
    assert "min-width" in expanded and "max-width" in expanded, (
        "bounded in CSS too -- a stored width from an older build must "
        "not be able to render an unusable rail")


def test_it_persists_but_survives_storage_being_unavailable():
    """localStorage throws in a private window and returns nothing with
    site data blocked. Neither should break the drag."""
    js = _js()
    i = js.index("Sidebar resize")
    block = js[i:]
    assert "localStorage" in block
    assert block.count("try {") >= 3, (
        "every localStorage access needs its own guard -- read, write "
        "and reset")


def test_dragging_is_delegated_so_it_survives_a_swap():
    """base.html renders the handle on every page and this script runs
    once; htmx swaps chrome. A direct binding would silently stop
    working on a swapped page."""
    js = _js()
    i = js.index("Sidebar resize")
    block = js[i:]
    assert "document.addEventListener('pointerdown'" in block
    assert "e.target.id === 'sidebar-resizer'" in block


def test_there_is_a_way_back():
    """Somebody who drags it to 180px and cannot read it needs an exit
    that does not involve clearing site data."""
    js = _js()
    i = js.index("Sidebar resize")
    block = js[i:]
    assert "dblclick" in block
    assert "apply(240)" in block


def test_the_handle_is_hidden_when_collapsed():
    css = _css()
    assert ".sidebar-collapsed #sidebar-resizer" in css
    assert "display: none" in css.split(
        ".sidebar-collapsed #sidebar-resizer")[1][:80]


def test_the_nav_still_clips_its_labels():
    """`overflow-hidden` is what hides the labels during the collapse
    animation. An earlier version of this change dropped it to let the
    handle straddle the edge, which would have broken the animation --
    the handle sits inside instead."""
    body = _base()
    nav = body.split('id="sidebar"')[1][:400]
    assert "overflow-hidden" in nav
