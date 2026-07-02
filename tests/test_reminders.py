"""Tests for the Apple Reminders tools (tools/reminders.py).

All tests mock ``run_applescript`` (patched where it is USED, i.e.
``apple_mail_mcp.tools.reminders.run_applescript``) so no real osascript /
Reminders.app is ever invoked. This mirrors the style of
test_core_applescript.py and test_permissions.py.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))

from apple_mail_mcp.tools import reminders as rem  # noqa: E402
from apple_mail_mcp.permissions import reload_config  # noqa: E402


# ---------------------------------------------------------------------------
# ISO date-parsing helper
# ---------------------------------------------------------------------------


class TestParseIsoDatetime(unittest.TestCase):
    def test_date_only(self):
        dt = rem._parse_iso_datetime("2026-07-02")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 7, 2))
        self.assertEqual((dt.hour, dt.minute), (0, 0))

    def test_date_with_time(self):
        dt = rem._parse_iso_datetime("2026-07-02 09:30")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 7, 2))
        self.assertEqual((dt.hour, dt.minute), (9, 30))

    def test_strips_surrounding_whitespace(self):
        dt = rem._parse_iso_datetime("  2026-01-15 08:05  ")
        self.assertEqual((dt.year, dt.month, dt.day, dt.hour, dt.minute),
                         (2026, 1, 15, 8, 5))

    def test_rejects_garbage(self):
        for bad in ("not-a-date", "07/02/2026", "2026-13-40", "2026-07-02 25:00", ""):
            with self.assertRaises(ValueError):
                rem._parse_iso_datetime(bad)

    def test_build_due_date_script_contains_components(self):
        dt = rem._parse_iso_datetime("2026-07-02 09:00")
        script = rem._build_due_date_script(dt)
        self.assertIn("set dueDate to current date", script)
        self.assertIn("set year of dueDate to 2026", script)
        self.assertIn("set month of dueDate to 7", script)
        self.assertIn("set day of dueDate to 2", script)
        self.assertIn("set hours of dueDate to 9", script)
        # No locale-fragile date literal.
        self.assertNotIn('date "', script)


# ---------------------------------------------------------------------------
# create_reminder
# ---------------------------------------------------------------------------


class TestCreateReminder(unittest.TestCase):
    def test_bad_due_date_returns_error_without_calling_applescript(self):
        with patch.object(rem, "run_applescript") as mock_run:
            result = rem.create_reminder("Errands", "Buy milk", due_date="tomorrow")
        self.assertTrue(result.startswith("Error:"))
        mock_run.assert_not_called()

    def test_good_args_calls_applescript_once_with_expected_content(self):
        with patch.object(rem, "run_applescript", return_value="OK") as mock_run:
            result = rem.create_reminder(
                "Errands", "Buy milk", due_date="2026-07-02 09:00", notes="2%"
            )
        self.assertEqual(result, "OK")
        mock_run.assert_called_once()
        script = mock_run.call_args[0][0]
        self.assertIn("Buy milk", script)          # escaped name
        self.assertIn('list "Errands"', script)    # list name
        self.assertIn("make new reminder", script)
        self.assertIn("due date:dueDate", script)
        self.assertIn('body:"2%"', script)

    def test_no_due_date_omits_due_property(self):
        with patch.object(rem, "run_applescript", return_value="OK") as mock_run:
            rem.create_reminder("Errands", "Call Bob")
        script = mock_run.call_args[0][0]
        self.assertNotIn("due date:dueDate", script)
        self.assertIn("make new reminder", script)

    def test_empty_name_returns_error(self):
        with patch.object(rem, "run_applescript") as mock_run:
            result = rem.create_reminder("Errands", "   ")
        self.assertTrue(result.startswith("Error:"))
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# list_reminders
# ---------------------------------------------------------------------------


class TestListReminders(unittest.TestCase):
    def test_scopes_script_to_named_list(self):
        with patch.object(rem, "run_applescript", return_value="ok") as mock_run:
            rem.list_reminders(list_name="Errands")
        script = mock_run.call_args[0][0]
        self.assertIn('list "Errands"', script)
        self.assertIn("set targetLists to", script)

    def test_all_lists_when_no_name(self):
        with patch.object(rem, "run_applescript", return_value="ok") as mock_run:
            rem.list_reminders()
        script = mock_run.call_args[0][0]
        self.assertIn("set targetLists to every list", script)

    def test_excludes_completed_by_default(self):
        with patch.object(rem, "run_applescript", return_value="ok") as mock_run:
            rem.list_reminders(list_name="Errands")
        script = mock_run.call_args[0][0]
        self.assertIn("whose completed is false", script)

    def test_includes_completed_when_requested(self):
        with patch.object(rem, "run_applescript", return_value="ok") as mock_run:
            rem.list_reminders(list_name="Errands", include_completed=True)
        script = mock_run.call_args[0][0]
        self.assertNotIn("whose completed is false", script)
        self.assertIn("reminders of aList", script)


# ---------------------------------------------------------------------------
# complete_reminder / update_reminder / delete_reminder
# ---------------------------------------------------------------------------


class TestCompleteReminder(unittest.TestCase):
    def test_builds_script_referencing_name_and_list(self):
        with patch.object(rem, "run_applescript", return_value="OK") as mock_run:
            rem.complete_reminder("Errands", "milk")
        script = mock_run.call_args[0][0]
        self.assertIn('name contains "milk"', script)
        self.assertIn('list "Errands"', script)
        self.assertIn("set completed of theReminder to true", script)


class TestUpdateReminder(unittest.TestCase):
    def test_only_changes_provided_fields(self):
        with patch.object(rem, "run_applescript", return_value="OK") as mock_run:
            rem.update_reminder("Errands", "milk", new_name="Buy oat milk")
        script = mock_run.call_args[0][0]
        self.assertIn('set name of theReminder to "Buy oat milk"', script)
        self.assertNotIn("set due date of theReminder", script)
        self.assertNotIn("set body of theReminder", script)

    def test_bad_due_date_returns_error_without_calling(self):
        with patch.object(rem, "run_applescript") as mock_run:
            result = rem.update_reminder("Errands", "milk", due_date="soon")
        self.assertTrue(result.startswith("Error:"))
        mock_run.assert_not_called()

    def test_nothing_to_update_returns_error(self):
        with patch.object(rem, "run_applescript") as mock_run:
            result = rem.update_reminder("Errands", "milk")
        self.assertTrue(result.startswith("Error:"))
        mock_run.assert_not_called()


class TestDeleteReminder(unittest.TestCase):
    def test_builds_script_referencing_name(self):
        with patch.object(rem, "run_applescript", return_value="OK") as mock_run:
            rem.delete_reminder("Errands", "milk")
        script = mock_run.call_args[0][0]
        self.assertIn('name contains "milk"', script)
        self.assertIn("delete theReminder", script)


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------


class TestPermissionDenial(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("APPLE_MCP_PERMISSIONS")
        os.environ["APPLE_MCP_PERMISSIONS"] = '{"reminders":{"Errands":"read"}}'
        reload_config()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("APPLE_MCP_PERMISSIONS", None)
        else:
            os.environ["APPLE_MCP_PERMISSIONS"] = self._prev
        reload_config()

    def test_delete_denied_at_read_tier(self):
        with patch.object(rem, "run_applescript") as mock_run:
            result = rem.delete_reminder("Errands", "milk")
        self.assertTrue(result.startswith("Error: Permission denied"))
        mock_run.assert_not_called()

    def test_read_allowed_at_read_tier(self):
        # list_reminders is READ tier — should pass the gate and run.
        with patch.object(rem, "run_applescript", return_value="ok") as mock_run:
            result = rem.list_reminders(list_name="Errands")
        self.assertEqual(result, "ok")
        mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
