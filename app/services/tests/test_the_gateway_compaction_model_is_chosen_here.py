"""The gateway's compaction summarizer is picked on the Model Registry page.

A misc-model purpose like the others, with two differences: the gateway
calls it itself, so it may be any provider the gateway runs (not only
those this process has an SDK for), and its effective value is ALWAYS
served -- default included -- so this page, not a provisioning script,
is the source of truth.
"""
from __future__ import annotations

import io
import pathlib

from app.services import misc_models

ROOT = pathlib.Path(__file__).resolve().parents[3]


def _read(rel: str) -> str:
    return io.open(ROOT / rel, encoding="utf-8").read()


def test_the_purpose_exists_and_defaults_to_flash():
    meta = misc_models.MISC_MODEL_PURPOSES["gateway_compaction"]
    assert meta["default_model_id"] == "google/gemini-3.8-flash"
    assert meta["serve_default"] is True


def test_it_may_pick_any_provider_the_gateway_runs():
    assert misc_models.providers_for("gateway_compaction") >= {"google", "anthropic"}
    # The control panel's own utility calls stay on what it can call.
    assert misc_models.providers_for("thread_title") == misc_models.SUPPORTED_UTILITY_PROVIDERS


def test_its_value_is_always_served_to_the_gateway():
    src = _read("app/routers/model_registry.py")
    assert 'if row["is_override"] or row["serve_default"]' in src


def test_saving_tells_the_gateway_at_once_and_never_fails_the_save():
    src = _read("app/routers/model_registry.py")
    fn = src[src.index("async def admin_update_misc_model("):]
    fn = fn[:fn.index("\n@router")] if "\n@router" in fn else fn
    assert 'gateway_admin.call("misc-models.reload"' in fn
    assert "except Exception" in fn


def test_the_dropdown_offers_each_purpose_its_own_models():
    page = _read("app/templates/admin/model_registry.html")
    assert "pm.options or utility_model_options" in page
