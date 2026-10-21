"""Credential-key assertion for executor environment variables.

Raises on dangerous keys — never silently filters. A misconfigured
allowlist is a bug, not a recoverable condition.
"""

from __future__ import annotations

# Suffixes that indicate a credential. Checked case-sensitively —
# environment variable names are conventionally uppercase.
_CREDENTIAL_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD")


class CredentialLeakError(Exception):
    """Raised when an env dict contains a key that looks like a credential."""

    def __init__(self, key_name: str) -> None:
        self.key_name = key_name
        super().__init__(
            f"credential key '{key_name}' found in executor env — "
            f"the allowlist must not include credential keys. "
            f"This is a raise, never a silent filter."
        )


def assert_no_credentials(env: dict[str, str]) -> None:
    """Raise CredentialLeakError if any key matches a credential pattern.

    Patterns:
      - Exact: DATABASE_URL
      - Suffix: _KEY, _TOKEN, _SECRET, _PASSWORD
    """
    for key in env:
        if key == "DATABASE_URL":
            raise CredentialLeakError(key)
        if any(key.endswith(suffix) for suffix in _CREDENTIAL_SUFFIXES):
            raise CredentialLeakError(key)
