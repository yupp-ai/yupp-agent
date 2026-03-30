"""Test that all datetime/timestamp columns in the database use timezone-aware types."""

from pytest_alembic.runner import MigrationContext
from sqlalchemy import Engine, text


def test_all_datetime_columns_have_timezone(alembic_runner: MigrationContext, alembic_engine: Engine) -> None:
    """Verify that all timestamp/datetime columns use 'timestamp with time zone'."""
    alembic_runner.migrate_up_to("head")

    # Columns to ignore (legacy columns that may/may not be fixed in future migrations)
    ignored_columns: set[tuple[str, str, str]] = set()

    # Query PostgreSQL information_schema to find all timestamp/datetime columns
    query = text("""
        SELECT
            table_schema,
            table_name,
            column_name,
            data_type,
            udt_name
        FROM information_schema.columns
        WHERE
            table_schema = 'public'
            AND (
                data_type IN ('timestamp without time zone', 'timestamp with time zone')
                OR udt_name IN ('timestamp', 'timestamptz')
            )
        ORDER BY table_name, column_name
    """)

    with alembic_engine.connect() as connection:
        result = connection.execute(query)
        columns = result.fetchall()

    # Track which columns actually exist
    existing_columns = {(row[0], row[1], row[2]) for row in columns}

    # Check each column
    violations = []
    incorrectly_ignored = []

    # First, check if any ignored columns don't exist anymore
    missing_ignored = [
        f"{ignored_key[0]}.{ignored_key[1]}.{ignored_key[2]} is in ignored_columns but doesn't exist in database "
        "(should be removed from ignore list)"
        for ignored_key in ignored_columns
        if ignored_key not in existing_columns
    ]

    for row in columns:
        table_schema, table_name, column_name, data_type, udt_name = row
        column_key = (table_schema, table_name, column_name)

        # In PostgreSQL:
        # - 'timestamp without time zone' or udt_name='timestamp' = BAD (no timezone)
        # - 'timestamp with time zone' or udt_name='timestamptz' = GOOD (with timezone)
        is_bad = data_type == "timestamp without time zone" or (data_type == "timestamp" and udt_name == "timestamp")

        if column_key in ignored_columns:
            if not is_bad:
                incorrectly_ignored.append(
                    f"{table_schema}.{table_name}.{column_name} is in ignored_columns but uses '{data_type}' "
                    "(has timezone - should be removed from ignore list)"
                )
        elif is_bad:
            violations.append(
                f"{table_schema}.{table_name}.{column_name} uses '{data_type}' (should be 'timestamp with time zone')"
            )

    if missing_ignored:
        error_msg = "Columns in ignored_columns list don't exist anymore (remove them from ignore list):\n" + "\n".join(
            f"  - {v}" for v in missing_ignored
        )
        raise AssertionError(error_msg)

    if incorrectly_ignored:
        error_msg = "Columns in ignored_columns list are no longer bad (remove them from ignore list):\n" + "\n".join(
            f"  - {v}" for v in incorrectly_ignored
        )
        raise AssertionError(error_msg)

    if violations:
        error_msg = "Found datetime/timestamp columns without timezone:\n" + "\n".join(f"  - {v}" for v in violations)
        raise AssertionError(error_msg)
