"""Tests for the Apple Calendar tools (tools/calendar.py).

Runs without Calendar.app: run_applescript is patched everywhere it is used
(apple_mail_mcp.tools.calendar.run_applescript) so no real osascript executes.
"""

import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "plugin"))

from apple_mail_mcp.tools import calendar as cal  # noqa: E402
from apple_mail_mcp.permissions import reload_config  # noqa: E402


# ---------------------------------------------------------------------------
# ISO date parsing helper
# ---------------------------------------------------------------------------


class ParseIsoDateTests(unittest.TestCase):
    def test_date_only(self):
        dt = cal.parse_iso_date("2026-07-02")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 7, 2))
        self.assertEqual((dt.hour, dt.minute), (0, 0))

    def test_date_with_time(self):
        dt = cal.parse_iso_date("2026-07-02 14:30")
        self.assertEqual((dt.year, dt.month, dt.day), (2026, 7, 2))
        self.assertEqual((dt.hour, dt.minute), (14, 30))

    def test_garbage_raises(self):
        with self.assertRaises(ValueError):
            cal.parse_iso_date("not-a-date")

    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            cal.parse_iso_date("")

    def test_wrong_format_raises(self):
        with self.assertRaises(ValueError):
            cal.parse_iso_date("07/02/2026")

    def test_build_date_script_contains_components(self):
        dt = cal.parse_iso_date("2026-07-02 14:30")
        script = cal.build_date_script(dt, "theDate")
        self.assertIn("set theDate to current date", script)
        self.assertIn("set year of theDate to 2026", script)
        self.assertIn("set month of theDate to 7", script)
        self.assertIn("set day of theDate to 2", script)
        self.assertIn("set hours of theDate to 14", script)
        self.assertIn("set minutes of theDate to 30", script)
        # Day is set to 1 before month to avoid overflow.
        self.assertIn("set day of theDate to 1", script)


# ---------------------------------------------------------------------------
# create_event
# ---------------------------------------------------------------------------


class CreateEventTests(unittest.TestCase):
    def test_bad_date_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.create_event("Home", "Lunch", "not-a-date")
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()

    def test_missing_summary_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.create_event("Home", "", "2026-07-02")
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()

    def test_good_args_calls_once_with_expected_script(self):
        with patch.object(cal, "run_applescript", return_value="OK Event created. ID: X") as mock_run:
            result = cal.create_event(
                "Home",
                'Team "Standup"',
                "2026-07-02 09:00",
                location="Room 5",
            )
            mock_run.assert_called_once()
            script = mock_run.call_args[0][0]
            # Escaped summary (quotes escaped).
            self.assertIn('Team \\"Standup\\"', script)
            self.assertIn('calendar "Home"', script)
            self.assertIn("make new event", script)
            self.assertIn('location:"Room 5"', script)
            self.assertEqual(result, "OK Event created. ID: X")

    def test_end_before_start_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.create_event(
                "Home", "Lunch", "2026-07-02 14:00", end_date="2026-07-02 13:00"
            )
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()

    def test_invitees_added(self):
        with patch.object(cal, "run_applescript", return_value="OK") as mock_run:
            cal.create_event(
                "Home",
                "Meeting",
                "2026-07-02 09:00",
                invitees="a@example.com, b@example.com",
            )
            script = mock_run.call_args[0][0]
            self.assertIn("make new attendee", script)
            self.assertIn('email:"a@example.com"', script)
            self.assertIn('email:"b@example.com"', script)


# ---------------------------------------------------------------------------
# list_events
# ---------------------------------------------------------------------------


class ListEventsTests(unittest.TestCase):
    def test_calendar_filter_in_script(self):
        with patch.object(cal, "run_applescript", return_value="EVENTS") as mock_run:
            cal.list_events(calendar="Work")
            script = mock_run.call_args[0][0]
            self.assertIn('set theCals to {calendar "Work"}', script)

    def test_no_calendar_uses_all(self):
        with patch.object(cal, "run_applescript", return_value="EVENTS") as mock_run:
            cal.list_events()
            script = mock_run.call_args[0][0]
            self.assertIn("set theCals to calendars", script)

    def test_search_filter_in_script(self):
        with patch.object(cal, "run_applescript", return_value="EVENTS") as mock_run:
            cal.list_events(search="Standup")
            script = mock_run.call_args[0][0]
            self.assertIn('evtSummary contains "Standup"', script)

    def test_bad_start_date_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.list_events(start_date="garbage")
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# get_event / update_event / delete_event
# ---------------------------------------------------------------------------


class GetEventTests(unittest.TestCase):
    def test_references_uid(self):
        with patch.object(cal, "run_applescript", return_value="EVENT") as mock_run:
            cal.get_event("Home", "UID-123")
            script = mock_run.call_args[0][0]
            self.assertIn('whose uid is "UID-123"', script)
            self.assertIn('calendar "Home"', script)


class UpdateEventTests(unittest.TestCase):
    def test_no_fields_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.update_event("Home", "UID-1")
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()

    def test_only_provided_fields_in_script(self):
        with patch.object(cal, "run_applescript", return_value="OK") as mock_run:
            cal.update_event("Home", "UID-1", summary="New title")
            script = mock_run.call_args[0][0]
            self.assertIn('set summary of anEvent to "New title"', script)
            # location was not provided, so must not appear.
            self.assertNotIn("set location of anEvent", script)

    def test_bad_date_no_applescript(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.update_event("Home", "UID-1", start_date="nope")
            self.assertTrue(result.startswith("Error:"), result)
            mock_run.assert_not_called()


class DeleteEventTests(unittest.TestCase):
    def test_references_uid(self):
        with patch.object(cal, "run_applescript", return_value="OK Event deleted") as mock_run:
            cal.delete_event("Home", "UID-999")
            script = mock_run.call_args[0][0]
            self.assertIn('whose uid is "UID-999"', script)
            self.assertIn("delete anEvent", script)


# ---------------------------------------------------------------------------
# Permission gating
# ---------------------------------------------------------------------------


class PermissionTests(unittest.TestCase):
    def setUp(self):
        self._prev = os.environ.get("APPLE_MCP_PERMISSIONS")
        os.environ["APPLE_MCP_PERMISSIONS"] = '{"calendars":{"Home":"read"}}'
        reload_config()

    def tearDown(self):
        if self._prev is None:
            os.environ.pop("APPLE_MCP_PERMISSIONS", None)
        else:
            os.environ["APPLE_MCP_PERMISSIONS"] = self._prev
        reload_config()

    def test_delete_denied_read_only_calendar(self):
        with patch.object(cal, "run_applescript") as mock_run:
            result = cal.delete_event("Home", "UID-1")
            self.assertTrue(
                result.startswith("Error: Permission denied"),
                result,
            )
            mock_run.assert_not_called()

    def test_read_allowed(self):
        with patch.object(cal, "run_applescript", return_value="EVENTS") as mock_run:
            result = cal.list_events(calendar="Home")
            self.assertEqual(result, "EVENTS")
            mock_run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
