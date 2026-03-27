from datetime import UTC, datetime, timedelta


def get_start_of_monday_utc() -> datetime:
    """Get the start of the current week (Monday) in UTC."""
    today = datetime.now(UTC)
    monday = today - timedelta(days=today.weekday())
    return datetime(monday.year, monday.month, monday.day, tzinfo=UTC)
