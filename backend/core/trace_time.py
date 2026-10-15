"""Trace timestamps use the local China Standard Time required by the demo database."""
import os
from datetime import datetime, timedelta, timezone


def trace_now() -> datetime:
    """Return a naive UTC+8 datetime for MySQL DATETIME storage and DBeaver display."""
    try:
        offset_hours = int(os.getenv("ECHOMIND_TRACE_UTC_OFFSET_HOURS", "8"))
    except ValueError:
        offset_hours = 8
    offset_hours = max(-12, min(14, offset_hours))
    return datetime.now(timezone(timedelta(hours=offset_hours))).replace(tzinfo=None)
