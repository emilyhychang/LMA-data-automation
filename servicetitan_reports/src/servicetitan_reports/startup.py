"""Bounded database startup recovery with credential-safe diagnostics."""

import logging
import time

import psycopg


LOGGER = logging.getLogger(__name__)
MISSING_SCHEMA = {"42P01", "42703", "3F000"}
TRANSIENT_STATES = {"40001", "40P01", "55P03", "57014", "57P01", "57P02", "57P03"}


class StorageStartupError(RuntimeError):
    """A safe explanation to display without exposing database credentials."""


def _is_transient(error):
    state = getattr(error, "sqlstate", None)
    if state:
        return state.startswith(("08", "53")) or state in TRANSIENT_STATES
    return isinstance(error, (psycopg.OperationalError, psycopg.InterfaceError))


def _explanation(error):
    state = getattr(error, "sqlstate", None)
    if state and state.startswith("28"):
        return "The database rejected its sign-in credentials. An administrator must check the database secret."
    if state == "42501":
        return "The database account lacks permission to open or prepare the shared datasets."
    if state in MISSING_SCHEMA:
        return "The database schema needs setup or an update. An administrator must check the app's database setup."
    if _is_transient(error):
        return "The database is temporarily unreachable or busy. Please retry."
    return "The shared datasets could not be opened. An administrator must check the database startup diagnostic."


def open_dataset_store(store, *, allow_schema_setup=False, sleep=time.sleep):
    """Retry startup reads only; initialize/upgrade only a confirmed missing schema.

    Import, seed, and deletion writes are never retried here. A ready schema
    requires no DDL privileges. Each attempt opens a fresh connection.
    """
    for attempt in range(3):
        try:
            try:
                return store.startup_status()
            except psycopg.Error as error:
                if error.sqlstate not in MISSING_SCHEMA or not allow_schema_setup:
                    raise
                store.ensure_schema()
                return store.startup_status()
        except Exception as error:
            state = getattr(error, "sqlstate", None)
            # Raw database exceptions may include a connection URL or user name.
            # Keep logs and the displayed diagnostic limited to type and SQLSTATE.
            LOGGER.warning(
                "Database startup attempt %s failed: %s (SQLSTATE %s)",
                attempt + 1, type(error).__name__, state or "unavailable",
            )
            if attempt < 2 and _is_transient(error):
                sleep(0.5 * (attempt + 1))
                continue
            diagnostic = f"{type(error).__name__} / {state or 'no SQLSTATE'}"
            raise StorageStartupError(
                f"{_explanation(error)} Diagnostic: {diagnostic}."
            ) from None
