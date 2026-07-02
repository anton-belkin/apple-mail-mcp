"""Per-account permission gating for Apple Mail & Calendar MCP.

Every mutating tool is guarded by a required capability *tier*. Each account
(Mail account, Calendar, or Reminders list) is assigned a tier; a call is
allowed only when the account's tier is at least the tool's required tier.

Tier ordering (ascending)::

    none < read < send < full

- ``read`` — view / list / search only
- ``send`` — read + compose / reply / mark / move / create (non-destructive writes)
- ``full`` — send + delete / trash / permanent removal
- ``none`` — no access at all (account hidden / every tool denied)

Configuration precedence (highest wins):

1. ``--read-only`` CLI flag (``server.READ_ONLY``) — caps every account at ``read``.
2. ``APPLE_MCP_PERMISSIONS`` environment variable containing JSON.
3. ``~/.config/apple-mail-mcp/permissions.json``.
4. Built-in default — every account ``read``.

Config schema::

    {
      "default": "read",
      "accounts":  { "Work": "full", "Personal": "send", "Old": "none" },
      "calendars": { "default": "read", "Home": "full" },
      "reminders": { "default": "read", "Errands": "full" }
    }

``default`` is the fallback tier for any name not listed. Each domain may carry
its own ``default`` which overrides the top-level ``default`` for that domain.
"""

from __future__ import annotations

import functools
import inspect
import json
import os
from enum import IntEnum
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Union

# Domains -------------------------------------------------------------------
DOMAIN_ACCOUNTS = "accounts"
DOMAIN_CALENDARS = "calendars"
DOMAIN_REMINDERS = "reminders"


class Tier(IntEnum):
    """Ordered permission tiers. Higher grants strictly more than lower."""

    NONE = 0
    READ = 1
    SEND = 2
    FULL = 3

    @classmethod
    def parse(cls, value: Union[str, "Tier", None], default: "Tier") -> "Tier":
        """Parse a tier from a config string, tolerating aliases and junk."""
        if isinstance(value, Tier):
            return value
        if value is None:
            return default
        key = str(value).strip().lower()
        aliases = {
            "none": cls.NONE,
            "hidden": cls.NONE,
            "off": cls.NONE,
            "read": cls.READ,
            "readonly": cls.READ,
            "read-only": cls.READ,
            "view": cls.READ,
            "view-only": cls.READ,
            "send": cls.SEND,
            "write": cls.SEND,
            "compose": cls.SEND,
            "full": cls.FULL,
            "delete": cls.FULL,
            "admin": cls.FULL,
            "all": cls.FULL,
        }
        return aliases.get(key, default)


# Config loading ------------------------------------------------------------
# Built-in default is permissive (FULL) so the server is a drop-in upgrade:
# without any config every account keeps full access, exactly as before this
# layer existed. Restrictions are opt-in — set "default": "read" for a
# secure-by-default (lockdown) posture, or lower individual accounts.
_DEFAULT_TIER = Tier.FULL
_CONFIG_PATH = Path.home() / ".config" / "apple-mail-mcp" / "permissions.json"

# Module-level cache; cleared by reload_config() (used in tests / SIGHUP-style
# refresh). None means "not yet loaded".
_config_cache: Optional[Dict[str, Any]] = None


def _read_raw_config() -> Dict[str, Any]:
    """Load raw config JSON from env var or config file (env wins)."""
    env_value = os.environ.get("APPLE_MCP_PERMISSIONS", "").strip()
    if env_value:
        try:
            data = json.loads(env_value)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            # Malformed env config is ignored rather than crashing the server;
            # falls through to the file / built-in default.
            pass

    try:
        if _CONFIG_PATH.is_file():
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (OSError, json.JSONDecodeError):
        pass

    return {}


def load_config() -> Dict[str, Any]:
    """Return the parsed config, loading and caching it on first use."""
    global _config_cache
    if _config_cache is None:
        _config_cache = _read_raw_config()
    return _config_cache


def reload_config() -> None:
    """Drop the cached config so the next lookup re-reads env/file."""
    global _config_cache
    _config_cache = None


# Tier resolution -----------------------------------------------------------
def _global_cap() -> Tier:
    """Return the ceiling imposed by the global --read-only flag, if any."""
    # Imported lazily so the flag is read at call time, not import time.
    import apple_mail_mcp.server as server

    return Tier.READ if getattr(server, "READ_ONLY", False) else Tier.FULL


def resolve_tier(domain: str, name: Optional[str]) -> Tier:
    """Resolve the effective tier for *name* within *domain*.

    Matching is case-insensitive. Falls back to the domain default, then the
    top-level default, then the built-in ``read`` default. The global
    ``--read-only`` cap is applied last.
    """
    config = load_config()

    top_default = Tier.parse(config.get("default"), _DEFAULT_TIER)
    domain_cfg = config.get(domain)
    if not isinstance(domain_cfg, dict):
        domain_cfg = {}
    domain_default = Tier.parse(domain_cfg.get("default"), top_default)

    tier = domain_default
    if name:
        wanted = name.strip().casefold()
        for key, value in domain_cfg.items():
            if key == "default":
                continue
            if key.casefold() == wanted:
                tier = Tier.parse(value, domain_default)
                break

    return min(tier, _global_cap())


# Enforcement ---------------------------------------------------------------
class PermissionDenied(Exception):
    """Raised internally when an account lacks the required tier."""


def check(domain: str, name: Optional[str], required: Tier) -> Optional[str]:
    """Return an error string if denied, else ``None``.

    When *name* is falsy the call targets *all* accounts (an aggregate view);
    such calls are only ever allowed for ``read`` and below, since a mutation
    must always name its target.
    """
    if name is None or not str(name).strip():
        # Aggregate/no-target call: permit read-level work, deny mutations.
        if required <= Tier.READ:
            return None
        return (
            f"Error: Permission denied — this operation requires a specific "
            f"account with '{required.name.lower()}' access, but none was given."
        )

    tier = resolve_tier(domain, name)
    if tier >= required:
        return None

    label = {DOMAIN_ACCOUNTS: "account", DOMAIN_CALENDARS: "calendar",
             DOMAIN_REMINDERS: "reminders list"}.get(domain, "account")
    return (
        f"Error: Permission denied — {label} '{name}' has '{tier.name.lower()}' "
        f"access, but this operation requires '{required.name.lower()}'. "
        f"Grant it via APPLE_MCP_PERMISSIONS or "
        f"~/.config/apple-mail-mcp/permissions.json."
    )


RequiredArg = Union[Tier, Callable[[Dict[str, Any]], Tier]]


def requires(
    required: RequiredArg,
    domain: str = DOMAIN_ACCOUNTS,
    account_arg: str = "account",
) -> Callable:
    """Decorator gating a tool by permission tier.

    Args:
        required: A fixed :class:`Tier`, or a callable receiving the tool's
            bound arguments (a dict) and returning the :class:`Tier` needed for
            that particular call (used for mode/action-dependent tools).
        domain: Which permission domain the ``account_arg`` belongs to.
        account_arg: Name of the parameter holding the target account/calendar/
            list name.

    On denial the wrapped tool returns the error string (tools communicate
    failures as strings, never exceptions), so the decorator matches that
    contract.
    """

    def decorator(func: Callable) -> Callable:
        sig = inspect.signature(func)

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            try:
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                bound_args = dict(bound.arguments)
            except TypeError:
                # Let the real call raise the natural signature error.
                return func(*args, **kwargs)

            needed = required(bound_args) if callable(required) else required
            name = bound_args.get(account_arg)
            denial = check(domain, name, needed)
            if denial is not None:
                return denial
            return func(*args, **kwargs)

        # Expose metadata for introspection / tests.
        wrapper.__mcp_required__ = required  # type: ignore[attr-defined]
        wrapper.__mcp_domain__ = domain  # type: ignore[attr-defined]
        return wrapper

    return decorator
