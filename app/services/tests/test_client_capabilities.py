"""Reading what a client says it can handle, off the request header.

This is the seam that lets a safety guard be relaxed for new clients without breaking
installed ones, so the parsing needs to be boring and total. The rule that matters:
an absent header and an empty header mean the SAME thing — assume only what the client
has always done — because a client too old to declare anything sends no header at all.
"""
from __future__ import annotations

from app.routers.local_common import (
    CAP_MULTI_REPO,
    CAP_PATH_CONTRACT,
    client_capabilities,
    supports_multi_repo,
    supports_path_contract,
)


class _Req:
    """Minimal stand-in — only `.headers` is read."""

    def __init__(self, headers: dict):
        self.headers = headers


def test_absent_header_declares_nothing():
    """A pre-header client. Must be indistinguishable from one that declares nothing,
    since neither can be assumed to do anything new."""
    assert client_capabilities(_Req({})) == set()
    assert supports_path_contract(_Req({})) is False
    assert supports_multi_repo(_Req({})) is False


def test_empty_and_whitespace_headers_declare_nothing():
    for value in ["", "   ", ",", " , , "]:
        req = _Req({"X-AgentOS-Client-Capabilities": value})
        assert client_capabilities(req) == set(), f"value={value!r}"
        assert supports_path_contract(req) is False
        assert supports_multi_repo(req) is False


def test_single_capability():
    req = _Req({"X-AgentOS-Client-Capabilities": CAP_PATH_CONTRACT})
    assert client_capabilities(req) == {CAP_PATH_CONTRACT}
    assert supports_path_contract(req) is True
    assert supports_multi_repo(req) is False


def test_several_capabilities_with_untidy_spacing():
    """The client joins with "," but a proxy or a future client may add spaces."""
    req = _Req({"X-AgentOS-Client-Capabilities": f" {CAP_PATH_CONTRACT} , {CAP_MULTI_REPO} ,"})
    assert client_capabilities(req) == {CAP_PATH_CONTRACT, CAP_MULTI_REPO}
    assert supports_path_contract(req) is True
    assert supports_multi_repo(req) is True


def test_path_contract_alone_does_not_imply_multi_repo():
    """The two capabilities are independent (see CAP_MULTI_REPO's doc comment): an
    extension can resolve per-repo directories without yet being able to clone, diff,
    or push more than one."""
    req = _Req({"X-AgentOS-Client-Capabilities": CAP_PATH_CONTRACT})
    assert supports_multi_repo(req) is False


def test_an_unknown_capability_does_not_imply_a_known_one():
    """Guards against a substring or prefix match creeping in: declaring something else
    entirely must not switch on the path contract."""
    for value in ["multi-repo", "path", "path-contract-v2", "no-path-contract"]:
        req = _Req({"X-AgentOS-Client-Capabilities": value})
        assert supports_path_contract(req) is False, f"value={value!r}"


def test_capability_is_matched_exactly_not_case_folded():
    """The token is a protocol constant, not user text. Accepting a case variant would
    mean two spellings drift into the wire format."""
    req = _Req({"X-AgentOS-Client-Capabilities": "Path-Contract"})
    assert supports_path_contract(req) is False
