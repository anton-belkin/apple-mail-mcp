"""Tests for the per-account permission layer (permissions.py).

Runs without Mail.app: all checks are on tier resolution and the gate
decorator, none of which invoke osascript.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))

from apple_mail_mcp import permissions as perm  # noqa: E402
from apple_mail_mcp.permissions import (  # noqa: E402
    Tier,
    requires,
    resolve_tier,
    check,
    reload_config,
    DOMAIN_ACCOUNTS,
    DOMAIN_CALENDARS,
)


class _ConfigBase(unittest.TestCase):
    """Sets an env config for the duration of a test and clears the cache."""

    config = ""

    def setUp(self):
        self._env = patch.dict(
            os.environ, {"APPLE_MCP_PERMISSIONS": self.config}, clear=False
        )
        self._env.start()
        # Ensure no config file bleeds in and the global cap is off.
        self._nofile = patch.object(perm, "_CONFIG_PATH", perm.Path("/nonexistent/x"))
        self._nofile.start()
        self._server = patch("apple_mail_mcp.server.READ_ONLY", False)
        self._server.start()
        reload_config()

    def tearDown(self):
        self._env.stop()
        self._nofile.stop()
        self._server.stop()
        reload_config()


class TestTierParse(unittest.TestCase):
    def test_aliases(self):
        self.assertEqual(Tier.parse("view-only", Tier.NONE), Tier.READ)
        self.assertEqual(Tier.parse("SEND", Tier.NONE), Tier.SEND)
        self.assertEqual(Tier.parse("delete", Tier.NONE), Tier.FULL)
        self.assertEqual(Tier.parse("hidden", Tier.READ), Tier.NONE)

    def test_unknown_falls_back_to_default(self):
        self.assertEqual(Tier.parse("bogus", Tier.SEND), Tier.SEND)
        self.assertEqual(Tier.parse(None, Tier.READ), Tier.READ)

    def test_ordering(self):
        self.assertTrue(Tier.NONE < Tier.READ < Tier.SEND < Tier.FULL)


class TestResolveTier(_ConfigBase):
    config = (
        '{"default":"read",'
        '"accounts":{"Work":"full","Personal":"send","Old":"none"},'
        '"calendars":{"default":"send","Home":"full"}}'
    )

    def test_explicit_account_tiers(self):
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Work"), Tier.FULL)
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Personal"), Tier.SEND)
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Old"), Tier.NONE)

    def test_top_level_default(self):
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Unlisted"), Tier.READ)

    def test_case_insensitive(self):
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "work"), Tier.FULL)
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "  PERSONAL "), Tier.SEND)

    def test_domain_default_overrides_top_default(self):
        # calendars default is "send", not the top-level "read"
        self.assertEqual(resolve_tier(DOMAIN_CALENDARS, "Unlisted"), Tier.SEND)
        self.assertEqual(resolve_tier(DOMAIN_CALENDARS, "Home"), Tier.FULL)


class TestDefaultsWhenNoConfig(_ConfigBase):
    config = ""

    def test_permissive_by_default(self):
        self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Anything"), Tier.FULL)


class TestGlobalReadOnlyCap(_ConfigBase):
    config = '{"accounts":{"Work":"full"}}'

    def test_read_only_caps_at_read(self):
        with patch("apple_mail_mcp.server.READ_ONLY", True):
            self.assertEqual(resolve_tier(DOMAIN_ACCOUNTS, "Work"), Tier.READ)


class TestCheckAggregate(_ConfigBase):
    config = '{"default":"read"}'

    def test_no_name_allows_read(self):
        self.assertIsNone(check(DOMAIN_ACCOUNTS, None, Tier.READ))
        self.assertIsNone(check(DOMAIN_ACCOUNTS, "", Tier.READ))

    def test_no_name_denies_mutation(self):
        self.assertIsNotNone(check(DOMAIN_ACCOUNTS, None, Tier.SEND))
        self.assertIsNotNone(check(DOMAIN_ACCOUNTS, "  ", Tier.FULL))

    def test_named_denial_and_allow(self):
        self.assertIsNotNone(check(DOMAIN_ACCOUNTS, "X", Tier.SEND))  # default read
        self.assertIsNone(check(DOMAIN_ACCOUNTS, "X", Tier.READ))


class TestRequiresDecorator(_ConfigBase):
    config = '{"default":"read","accounts":{"Work":"full"}}'

    def _make(self, required):
        @requires(required)
        def tool(account, action=None, mode=None):
            return f"ran:{account}"

        return tool

    def test_denied_returns_error_string_without_running(self):
        tool = self._make(Tier.SEND)
        out = tool("Unlisted")  # default read < send
        self.assertTrue(out.startswith("Error: Permission denied"))

    def test_allowed_runs(self):
        tool = self._make(Tier.SEND)
        self.assertEqual(tool("Work"), "ran:Work")

    def test_callable_required_uses_bound_args(self):
        # delete=full, else read
        def level(bound):
            return Tier.FULL if bound.get("action") == "delete" else Tier.READ

        tool = self._make(level)
        # Work=full: both allowed
        self.assertEqual(tool("Work", action="delete"), "ran:Work")
        # default read: list allowed, delete denied
        self.assertEqual(tool("Unlisted", action="list"), "ran:Unlisted")
        self.assertTrue(
            tool("Unlisted", action="delete").startswith("Error: Permission denied")
        )

    def test_metadata_exposed(self):
        tool = self._make(Tier.FULL)
        self.assertEqual(tool.__mcp_domain__, DOMAIN_ACCOUNTS)


class TestModeAwareTiers(_ConfigBase):
    config = ""

    def test_compose_tier(self):
        from apple_mail_mcp.tools.compose import _compose_tier, _reply_tier, _drafts_tier

        self.assertEqual(_compose_tier({"mode": "draft"}), Tier.READ)
        self.assertEqual(_compose_tier({"mode": "send"}), Tier.SEND)
        self.assertEqual(_compose_tier({}), Tier.SEND)  # default mode is send

        self.assertEqual(_reply_tier({"mode": "draft"}), Tier.READ)
        self.assertEqual(_reply_tier({"send": False}), Tier.READ)
        self.assertEqual(_reply_tier({"send": True}), Tier.SEND)
        self.assertEqual(_reply_tier({"mode": "open"}), Tier.SEND)

        self.assertEqual(_drafts_tier({"action": "list"}), Tier.READ)
        self.assertEqual(_drafts_tier({"action": "send"}), Tier.SEND)
        self.assertEqual(_drafts_tier({"action": "delete"}), Tier.FULL)

    def test_move_tier(self):
        from apple_mail_mcp.tools.manage import _move_tier

        self.assertEqual(_move_tier({"to_mailbox": "Archive"}), Tier.SEND)
        self.assertEqual(_move_tier({"to_mailbox": "Trash"}), Tier.FULL)
        self.assertEqual(_move_tier({"to_mailbox": "deleted items"}), Tier.FULL)


if __name__ == "__main__":
    unittest.main()
