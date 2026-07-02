"""Apple Reminders tools: list, create, complete, update, and delete reminders.

All operations drive Reminders.app through AppleScript (``tell application
"Reminders"``). Every tool is gated by the per-list permission layer
(:data:`DOMAIN_REMINDERS`) and returns a human-readable string — errors are
reported as ``"Error: ..."`` strings rather than raised, matching the rest of
the server.

Date handling
-------------
Due dates are accepted as ISO strings (``"YYYY-MM-DD"`` or
``"YYYY-MM-DD HH:MM"``) and parsed in Python. Rather than emitting a
locale-fragile ``date "..."`` literal, the generated AppleScript builds the
date by mutating a copy of ``current date`` (see :func:`_build_due_date_script`),
which is locale-independent.
"""

from datetime import datetime
from typing import Optional, Tuple

from apple_mail_mcp.server import mcp
from apple_mail_mcp.core import inject_preferences, escape_applescript, run_applescript
from apple_mail_mcp.permissions import requires, Tier, DOMAIN_REMINDERS


# ---------------------------------------------------------------------------
# ISO date parsing / AppleScript date construction
# ---------------------------------------------------------------------------

_DATE_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d")


def _parse_iso_datetime(value: str) -> datetime:
    """Parse an ISO due-date string into a ``datetime``.

    Accepts ``"YYYY-MM-DD"`` (time defaults to 00:00) or
    ``"YYYY-MM-DD HH:MM"``. Raises :class:`ValueError` on anything else.

    Args:
        value: The ISO date/datetime string to parse.

    Returns:
        The parsed :class:`datetime`.

    Raises:
        ValueError: If *value* matches none of the supported formats.
    """
    text = (value or "").strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise ValueError(
        f"Invalid due_date '{value}'. Use 'YYYY-MM-DD' or 'YYYY-MM-DD HH:MM'."
    )


def _build_due_date_script(dt: datetime, var_name: str = "dueDate") -> str:
    """Return AppleScript that builds a date in *var_name* by mutating current date.

    Avoids ``date "..."`` literals (which depend on the system locale) by
    setting each component on a copy of ``current date``. The day is set to 1
    first so setting the month never overflows (e.g. mutating a 31st-of-month
    base date into February).
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


def _resolve_due_date(
    due_date: Optional[str], var_name: str = "dueDate"
) -> Tuple[Optional[str], Optional[str]]:
    """Parse *due_date* and return ``(script, error)``.

    Returns ``(None, None)`` when *due_date* is not provided,
    ``(script, None)`` on success, or ``(None, error_string)`` on bad input.
    """
    if due_date is None or not str(due_date).strip():
        return (None, None)
    try:
        dt = _parse_iso_datetime(due_date)
    except ValueError as exc:
        return (None, f"Error: {exc}")
    return (_build_due_date_script(dt, var_name), None)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
@inject_preferences
@requires(Tier.READ, domain=DOMAIN_REMINDERS, account_arg="list_name")
def list_reminder_lists() -> str:
    """
    List all Apple Reminders lists with their reminder counts.

    Returns:
        A human-readable list of every Reminders list and how many
        reminders each contains, or an "Error: ..." string on failure.
    """
    script = '''
    tell application "Reminders"
        set outputText to "REMINDER LISTS" & return & return
        try
            set allLists to every list
            if (count of allLists) is 0 then
                return outputText & "(no reminder lists found)"
            end if
            repeat with aList in allLists
                set listName to name of aList
                set itemCount to count of reminders of aList
                set outputText to outputText & "- " & listName & " (" & itemCount & ")" & return
            end repeat
        on error errMsg
            return "Error: " & errMsg
        end try
        return outputText
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.READ, domain=DOMAIN_REMINDERS, account_arg="list_name")
def list_reminders(
    list_name: Optional[str] = None,
    include_completed: bool = False,
    max_items: int = 100,
) -> str:
    """
    List reminders in a specific list, or across all lists.

    Args:
        list_name: Name of the reminders list to read. Omit to list
            reminders from every list.
        include_completed: If True, include completed reminders (default:
            False — only incomplete reminders are shown).
        max_items: Maximum number of reminders to return per list (safety
            limit, default: 100).

    Returns:
        A human-readable listing of reminders (name, due date, completion
        status), or an "Error: ..." string on failure.
    """
    if max_items <= 0:
        return "Error: max_items must be a positive integer."

    # When include_completed is False, restrict the query to incomplete items.
    if include_completed:
        reminders_expr = "reminders of aList"
    else:
        reminders_expr = "(reminders of aList whose completed is false)"

    if list_name and list_name.strip():
        safe_list = escape_applescript(list_name)
        lists_setup = f'set targetLists to {{list "{safe_list}"}}'
    else:
        lists_setup = "set targetLists to every list"

    script = f'''
    tell application "Reminders"
        set outputText to "REMINDERS" & return & return
        try
            {lists_setup}
            repeat with aList in targetLists
                set listName to name of aList
                set outputText to outputText & "== " & listName & " ==" & return
                set theReminders to {reminders_expr}
                set shownCount to 0
                repeat with aReminder in theReminders
                    if shownCount is greater than or equal to {max_items} then exit repeat
                    set reminderName to name of aReminder
                    set isDone to completed of aReminder
                    if isDone then
                        set statusMark to "[x] "
                    else
                        set statusMark to "[ ] "
                    end if
                    set dueText to ""
                    try
                        set theDue to due date of aReminder
                        if theDue is not missing value then
                            set dueText to " (due " & (theDue as string) & ")"
                        end if
                    end try
                    set outputText to outputText & statusMark & reminderName & dueText & return
                    set shownCount to shownCount + 1
                end repeat
                if shownCount is 0 then
                    set outputText to outputText & "(none)" & return
                end if
                set outputText to outputText & return
            end repeat
        on error errMsg
            return "Error: " & errMsg
        end try
        return outputText
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.SEND, domain=DOMAIN_REMINDERS, account_arg="list_name")
def create_reminder(
    list_name: str,
    name: str,
    due_date: Optional[str] = None,
    notes: Optional[str] = None,
    priority: Optional[int] = None,
) -> str:
    """
    Create a new reminder in the given list.

    Args:
        list_name: Name of the reminders list to add the reminder to.
        name: The reminder's title.
        due_date: Optional due date as "YYYY-MM-DD" or "YYYY-MM-DD HH:MM".
        notes: Optional body text / notes for the reminder.
        priority: Optional priority (Reminders uses 0=none, 1=high, 5=medium,
            9=low).

    Returns:
        A confirmation string, or an "Error: ..." string on failure.
    """
    if not name or not name.strip():
        return "Error: Reminder name cannot be empty."
    if not list_name or not list_name.strip():
        return "Error: list_name cannot be empty."

    due_script, due_error = _resolve_due_date(due_date)
    if due_error is not None:
        return due_error

    if priority is not None and priority < 0:
        return "Error: priority must be a non-negative integer."

    safe_list = escape_applescript(list_name)
    safe_name = escape_applescript(name)

    # Assemble the property list piece by piece so only supplied fields appear.
    props = [f'name:"{safe_name}"']
    if notes is not None and str(notes).strip():
        props.append(f'body:"{escape_applescript(notes)}"')
    if priority is not None:
        props.append(f"priority:{priority}")
    if due_script is not None:
        props.append("due date:dueDate")
    props_str = "{" + ", ".join(props) + "}"

    due_setup = f"{due_script}\n" if due_script is not None else ""

    script = f'''
    tell application "Reminders"
        try
            {due_setup}set targetList to list "{safe_list}"
            make new reminder at end of reminders of targetList with properties {props_str}
            return "OK Created reminder '{safe_name}' in list '{safe_list}'"
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.SEND, domain=DOMAIN_REMINDERS, account_arg="list_name")
def complete_reminder(list_name: str, name: str) -> str:
    """
    Mark the first matching incomplete reminder as completed.

    Matches by name substring against incomplete reminders in the list.

    Args:
        list_name: Name of the reminders list to search.
        name: Substring to match against reminder names.

    Returns:
        A confirmation string naming the completed reminder, or an
        "Error: ..." string on failure (including when no match is found).
    """
    if not list_name or not list_name.strip():
        return "Error: list_name cannot be empty."
    if not name or not name.strip():
        return "Error: name cannot be empty."

    safe_list = escape_applescript(list_name)
    safe_name = escape_applescript(name)

    script = f'''
    tell application "Reminders"
        try
            set targetList to list "{safe_list}"
            set matches to (reminders of targetList whose name contains "{safe_name}" and completed is false)
            if (count of matches) is 0 then
                return "Error: No incomplete reminder matching '{safe_name}' found in list '{safe_list}'"
            end if
            set theReminder to item 1 of matches
            set doneName to name of theReminder
            set completed of theReminder to true
            return "OK Completed reminder '" & doneName & "' in list '{safe_list}'"
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.SEND, domain=DOMAIN_REMINDERS, account_arg="list_name")
def update_reminder(
    list_name: str,
    name: str,
    new_name: Optional[str] = None,
    due_date: Optional[str] = None,
    notes: Optional[str] = None,
) -> str:
    """
    Update fields of the first matching incomplete reminder.

    Only the fields you pass are changed; omitted fields are left as-is.
    Matches by name substring against incomplete reminders in the list.

    Args:
        list_name: Name of the reminders list to search.
        name: Substring to match against reminder names.
        new_name: Optional new title for the reminder.
        due_date: Optional new due date as "YYYY-MM-DD" or "YYYY-MM-DD HH:MM".
        notes: Optional new body text / notes.

    Returns:
        A confirmation string, or an "Error: ..." string on failure.
    """
    if not list_name or not list_name.strip():
        return "Error: list_name cannot be empty."
    if not name or not name.strip():
        return "Error: name cannot be empty."

    has_update = (
        (new_name is not None and str(new_name).strip())
        or (due_date is not None and str(due_date).strip())
        or (notes is not None)
    )
    if not has_update:
        return "Error: Nothing to update. Provide new_name, due_date, or notes."

    due_script, due_error = _resolve_due_date(due_date)
    if due_error is not None:
        return due_error

    safe_list = escape_applescript(list_name)
    safe_name = escape_applescript(name)

    updates = ""
    if new_name is not None and str(new_name).strip():
        updates += f'\n            set name of theReminder to "{escape_applescript(new_name)}"'
    if notes is not None:
        updates += f'\n            set body of theReminder to "{escape_applescript(notes)}"'
    if due_script is not None:
        updates += "\n            set due date of theReminder to dueDate"

    due_setup = f"{due_script}\n            " if due_script is not None else ""

    script = f'''
    tell application "Reminders"
        try
            {due_setup}set targetList to list "{safe_list}"
            set matches to (reminders of targetList whose name contains "{safe_name}" and completed is false)
            if (count of matches) is 0 then
                return "Error: No incomplete reminder matching '{safe_name}' found in list '{safe_list}'"
            end if
            set theReminder to item 1 of matches{updates}
            return "OK Updated reminder in list '{safe_list}'"
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)


@mcp.tool()
@inject_preferences
@requires(Tier.FULL, domain=DOMAIN_REMINDERS, account_arg="list_name")
def delete_reminder(list_name: str, name: str) -> str:
    """
    Delete the first reminder matching the given name substring.

    Matches by name substring (both completed and incomplete reminders are
    eligible for deletion).

    Args:
        list_name: Name of the reminders list to search.
        name: Substring to match against reminder names.

    Returns:
        A confirmation string naming the deleted reminder, or an
        "Error: ..." string on failure (including when no match is found).
    """
    if not list_name or not list_name.strip():
        return "Error: list_name cannot be empty."
    if not name or not name.strip():
        return "Error: name cannot be empty."

    safe_list = escape_applescript(list_name)
    safe_name = escape_applescript(name)

    script = f'''
    tell application "Reminders"
        try
            set targetList to list "{safe_list}"
            set matches to (reminders of targetList whose name contains "{safe_name}")
            if (count of matches) is 0 then
                return "Error: No reminder matching '{safe_name}' found in list '{safe_list}'"
            end if
            set theReminder to item 1 of matches
            set doneName to name of theReminder
            delete theReminder
            return "OK Deleted reminder '" & doneName & "' from list '{safe_list}'"
        on error errMsg
            return "Error: " & errMsg
        end try
    end tell
    '''
    return run_applescript(script)
