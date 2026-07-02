"""Apple Calendar tools: list calendars, list/get events, create/update/delete events.

All calendar access goes through AppleScript (``tell application "Calendar"``).
Dates are accepted in ISO form (``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM``) and
converted into AppleScript by mutating a copy of ``current date`` rather than
emitting locale-dependent ``date "..."`` literals.
"""

from datetime import datetime
from typing import Optional, Tuple

from apple_mail_mcp.server import mcp
from apple_mail_mcp.core import (
    inject_preferences,
    escape_applescript,
    run_applescript,
)
from apple_mail_mcp.permissions import requires, Tier, DOMAIN_CALENDARS


# ---------------------------------------------------------------------------
# Date parsing / AppleScript date construction
# ---------------------------------------------------------------------------

# Accepted ISO input formats, in priority order.
_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")


def parse_iso_date(value: str) -> datetime:
    """Parse an ISO date string in ``YYYY-MM-DD`` or ``YYYY-MM-DD HH:MM`` form.

    Args:
        value: The date string to parse.

    Returns:
        A ``datetime``. Date-only strings default to 00:00.

    Raises:
        ValueError: If *value* matches neither accepted format.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Date must be a non-empty string")
    text = value.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Invalid date '{value}'. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'."
    )


def build_date_script(dt: datetime, var_name: str) -> str:
    """Return AppleScript that builds a date into *var_name* from *dt*.

    Mutates a copy of ``current date`` so no locale-dependent ``date "..."``
    literal is emitted. Day is set to 1 before the month change so that
    setting the month never overflows (e.g. current day 31 into February),
    then the real day is set explicitly afterwards.
    """
    return (
        f"set {var_name} to current date\n"
        f"        set day of {var_name} to 1\n"
        f"        set year of {var_name} to {dt.year}\n"
        f"        set month of {var_name} to {dt.month}\n"
        f"        set day of {var_name} to {dt.day}\n"
        f"        set hours of {var_name} to {dt.hour}\n"
        f"        set minutes of {var_name} to {dt.minute}\n"
        f"        set seconds of {var_name} to {dt.second}"
    )


def _parse_optional_date(value: Optional[str]) -> Tuple[Optional[datetime], Optional[str]]:
    """Parse an optional ISO date, returning (datetime|None, error|None)."""
    if value is None:
        return (None, None)
    try:
        return (parse_iso_date(value), None)
    except ValueError as exc:
        return (None, f"Error: {exc}")


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
@inject_preferences
@requires(Tier.READ, domain=DOMAIN_CALENDARS, account_arg="calendar")
def list_calendars() -> str:
    """
    List the calendars configured in Calendar.app.

    Returns each calendar's name and whether it is writable.

    Returns:
        A human-readable list of calendars, or an error string.
    """
    script = '''
    tell application "Calendar"
        try
            set outputText to "CALENDARS" & return & return
            set calCount to 0
            repeat with aCal in calendars
                set calName to name of aCal
                try
                    set calWritable to writable of aCal
                on error
                    set calWritable to true
                end try
                if calWritable then
                    set writeLabel to "writable"
                else
                    set writeLabel to "read-only"
                end if
                set outputText to outputText & "- " & calName & " (" & writeLabel & ")" & return
                set calCount to calCount + 1
            end repeat
            set outputText to outputText & return & "TOTAL: " & calCount & " calendar(s)" & return
            return outputText
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.READ, domain=DOMAIN_CALENDARS, account_arg="calendar")
def list_events(
    calendar: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    search: Optional[str] = None,
    max_events: int = 50,
) -> str:
    """
    List calendar events within a date range.

    Optionally restrict to a single calendar and/or filter by a case-sensitive
    substring of the event summary (title).

    Args:
        calendar: Optional calendar name to restrict the search to. When omitted,
            events from every calendar are listed.
        start_date: Optional lower bound (inclusive) in "YYYY-MM-DD" or
            "YYYY-MM-DD HH:MM" form. Defaults to today when omitted.
        end_date: Optional upper bound (inclusive) in the same forms. Defaults to
            30 days after the start when omitted.
        search: Optional substring to match within event summaries.
        max_events: Maximum number of events to return (default: 50).

    Returns:
        A human-readable list of events, or an error string.
    """
    start_dt, err = _parse_optional_date(start_date)
    if err:
        return err
    end_dt, err = _parse_optional_date(end_date)
    if err:
        return err

    # Default range: today through +30 days.
    now = datetime.now()
    if start_dt is None:
        start_dt = datetime(now.year, now.month, now.day, 0, 0, 0)
    if end_dt is None:
        from datetime import timedelta

        end_dt = start_dt + timedelta(days=30)

    start_script = build_date_script(start_dt, "rangeStart")
    end_script = build_date_script(end_dt, "rangeEnd")

    # Calendar selector: one named calendar or every calendar.
    if calendar:
        safe_cal = escape_applescript(calendar)
        cal_setup = f'set theCals to {{calendar "{safe_cal}"}}'
    else:
        cal_setup = "set theCals to calendars"

    # Summary filter fragment.
    if search:
        safe_search = escape_applescript(search)
        summary_cond = f'evtSummary contains "{safe_search}"'
    else:
        summary_cond = "true"

    script = f'''
    tell application "Calendar"
        try
            {start_script}
            {end_script}
            {cal_setup}
            set outputText to "EVENTS" & return & return
            set evtCount to 0
            repeat with aCal in theCals
                if evtCount >= {int(max_events)} then exit repeat
                set calName to name of aCal
                set theEvents to (every event of aCal whose start date is greater than or equal to rangeStart and start date is less than or equal to rangeEnd)
                repeat with anEvent in theEvents
                    if evtCount >= {int(max_events)} then exit repeat
                    try
                        set evtSummary to summary of anEvent
                    on error
                        set evtSummary to "(no title)"
                    end try
                    if {summary_cond} then
                        set evtStart to start date of anEvent
                        set evtId to uid of anEvent
                        set outputText to outputText & "- " & evtSummary & return
                        set outputText to outputText & "   Calendar: " & calName & return
                        set outputText to outputText & "   Start: " & (evtStart as string) & return
                        set outputText to outputText & "   ID: " & evtId & return & return
                        set evtCount to evtCount + 1
                    end if
                end repeat
            end repeat
            set outputText to outputText & "========================================" & return
            set outputText to outputText & "TOTAL: " & evtCount & " event(s)" & return
            return outputText
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.READ, domain=DOMAIN_CALENDARS, account_arg="calendar")
def get_event(calendar: str, event_id: str) -> str:
    """
    Get the details of a single event by its uid.

    Args:
        calendar: Name of the calendar containing the event.
        event_id: The event's uid (as returned by list_events).

    Returns:
        A human-readable description of the event, or an error string.
    """
    if not calendar or not calendar.strip():
        return "Error: 'calendar' is required."
    if not event_id or not event_id.strip():
        return "Error: 'event_id' is required."

    safe_cal = escape_applescript(calendar)
    safe_id = escape_applescript(event_id)

    script = f'''
    tell application "Calendar"
        try
            set theCal to calendar "{safe_cal}"
            set theEvents to (every event of theCal whose uid is "{safe_id}")
            if (count of theEvents) is 0 then
                return "Error: No event found with uid '{safe_id}' in calendar '{safe_cal}'."
            end if
            set anEvent to item 1 of theEvents
            set outputText to "EVENT" & return & return
            try
                set outputText to outputText & "Summary: " & (summary of anEvent) & return
            end try
            set outputText to outputText & "Calendar: {safe_cal}" & return
            set outputText to outputText & "Start: " & (start date of anEvent as string) & return
            try
                set outputText to outputText & "End: " & (end date of anEvent as string) & return
            end try
            try
                if allday event of anEvent then
                    set outputText to outputText & "All day: yes" & return
                end if
            end try
            try
                set evtLocation to location of anEvent
                if evtLocation is not missing value and evtLocation is not "" then
                    set outputText to outputText & "Location: " & evtLocation & return
                end if
            end try
            try
                set evtNotes to description of anEvent
                if evtNotes is not missing value and evtNotes is not "" then
                    set outputText to outputText & "Notes: " & evtNotes & return
                end if
            end try
            set outputText to outputText & "ID: " & (uid of anEvent) & return
            return outputText
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.SEND, domain=DOMAIN_CALENDARS, account_arg="calendar")
def create_event(
    calendar: str,
    summary: str,
    start_date: str,
    end_date: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
    all_day: bool = False,
    invitees: Optional[str] = None,
) -> str:
    """
    Create a new event in the given calendar.

    Args:
        calendar: Name of the target calendar.
        summary: Event title.
        start_date: Start time in "YYYY-MM-DD" or "YYYY-MM-DD HH:MM" form.
        end_date: Optional end time in the same forms. Defaults to one hour after
            start when omitted.
        location: Optional location string.
        notes: Optional notes / description.
        all_day: If True, create an all-day event.
        invitees: Optional comma-separated list of invitee email addresses.

    Returns:
        A confirmation string containing the new event's uid, or an error string.
    """
    if not calendar or not calendar.strip():
        return "Error: 'calendar' is required."
    if not summary or not summary.strip():
        return "Error: 'summary' is required."

    try:
        start_dt = parse_iso_date(start_date)
    except ValueError as exc:
        return f"Error: {exc}"

    if end_date is not None:
        try:
            end_dt = parse_iso_date(end_date)
        except ValueError as exc:
            return f"Error: {exc}"
    else:
        from datetime import timedelta

        end_dt = start_dt + timedelta(hours=1)

    if end_dt < start_dt:
        return "Error: end_date must not be before start_date."

    safe_cal = escape_applescript(calendar)
    safe_summary = escape_applescript(summary)

    start_script = build_date_script(start_dt, "startDate")
    end_script = build_date_script(end_dt, "endDate")

    props = [
        'summary:"' + safe_summary + '"',
        "start date:startDate",
        "end date:endDate",
    ]
    if all_day:
        props.append("allday event:true")
    if location:
        props.append('location:"' + escape_applescript(location) + '"')
    if notes:
        props.append('description:"' + escape_applescript(notes) + '"')
    props_str = ", ".join(props)

    # Invitee handling: add each address as an attendee after creation.
    invitee_script = ""
    if invitees:
        addresses = [a.strip() for a in invitees.split(",") if a.strip()]
        for addr in addresses:
            safe_addr = escape_applescript(addr)
            invitee_script += (
                f'\n            try\n'
                f'                make new attendee at newEvent with properties '
                f'{{email:"{safe_addr}"}}\n'
                f'            end try'
            )

    script = f'''
    tell application "Calendar"
        try
            set theCal to calendar "{safe_cal}"
            {start_script}
            {end_script}
            set newEvent to make new event at end of events of theCal with properties {{{props_str}}}{invitee_script}
            set newId to uid of newEvent
            return "OK Event created. ID: " & newId
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.SEND, domain=DOMAIN_CALENDARS, account_arg="calendar")
def update_event(
    calendar: str,
    event_id: str,
    summary: Optional[str] = None,
    start_date: Optional[str] = None,
    end_date: Optional[str] = None,
    location: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """
    Update fields of an existing event, changing only the fields provided.

    Args:
        calendar: Name of the calendar containing the event.
        event_id: The event's uid.
        summary: Optional new title.
        start_date: Optional new start time ("YYYY-MM-DD" or "YYYY-MM-DD HH:MM").
        end_date: Optional new end time in the same forms.
        location: Optional new location.
        notes: Optional new notes / description.

    Returns:
        A confirmation string, or an error string.
    """
    if not calendar or not calendar.strip():
        return "Error: 'calendar' is required."
    if not event_id or not event_id.strip():
        return "Error: 'event_id' is required."

    start_dt, err = _parse_optional_date(start_date)
    if err:
        return err
    end_dt, err = _parse_optional_date(end_date)
    if err:
        return err

    if (
        summary is None
        and start_dt is None
        and end_dt is None
        and location is None
        and notes is None
    ):
        return "Error: No fields to update. Provide at least one of: summary, start_date, end_date, location, notes."

    safe_cal = escape_applescript(calendar)
    safe_id = escape_applescript(event_id)

    date_setup = ""
    updates = ""
    if summary is not None:
        updates += f'\n            set summary of anEvent to "{escape_applescript(summary)}"'
    if start_dt is not None:
        date_setup += "\n            " + build_date_script(start_dt, "startDate")
        updates += "\n            set start date of anEvent to startDate"
    if end_dt is not None:
        date_setup += "\n            " + build_date_script(end_dt, "endDate")
        updates += "\n            set end date of anEvent to endDate"
    if location is not None:
        updates += f'\n            set location of anEvent to "{escape_applescript(location)}"'
    if notes is not None:
        updates += f'\n            set description of anEvent to "{escape_applescript(notes)}"'

    script = f'''
    tell application "Calendar"
        try
            set theCal to calendar "{safe_cal}"
            set theEvents to (every event of theCal whose uid is "{safe_id}")
            if (count of theEvents) is 0 then
                return "Error: No event found with uid '{safe_id}' in calendar '{safe_cal}'."
            end if
            set anEvent to item 1 of theEvents
            {date_setup}
            {updates}
            return "OK Event updated. ID: " & (uid of anEvent)
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.FULL, domain=DOMAIN_CALENDARS, account_arg="calendar")
def delete_event(calendar: str, event_id: str) -> str:
    """
    Delete an event by its uid.

    Args:
        calendar: Name of the calendar containing the event.
        event_id: The event's uid.

    Returns:
        A confirmation string, or an error string.
    """
    if not calendar or not calendar.strip():
        return "Error: 'calendar' is required."
    if not event_id or not event_id.strip():
        return "Error: 'event_id' is required."

    safe_cal = escape_applescript(calendar)
    safe_id = escape_applescript(event_id)

    script = f'''
    tell application "Calendar"
        try
            set theCal to calendar "{safe_cal}"
            set theEvents to (every event of theCal whose uid is "{safe_id}")
            if (count of theEvents) is 0 then
                return "Error: No event found with uid '{safe_id}' in calendar '{safe_cal}'."
            end if
            repeat with anEvent in theEvents
                delete anEvent
            end repeat
            return "OK Event deleted. ID: {safe_id}"
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)
