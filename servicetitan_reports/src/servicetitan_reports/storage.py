"""Transactional persistent storage for the two shared LMA datasets."""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .pipeline import (
    UploadedImportResult,
    csv_bytes,
    fingerprint,
    read_and_clean_csv_bytes,
    run_uploaded_import,
)


LOCK_KEY = 4_812_291
BACKUP_RETENTION = 25


@dataclass(frozen=True)
class DatasetStatus:
    version: int
    updated_at: datetime
    updated_by: str


def _compressed(contents: bytes) -> bytes:
    return zlib.compress(contents, level=6)


def _uncompressed(contents: bytes | memoryview) -> bytes:
    return zlib.decompress(bytes(contents))


def prepare_seed(
    master_contents: bytes,
    new_leads_contents: bytes,
    settings: dict[str, Any],
) -> tuple[bytes, bytes]:
    """Validate, normalize, and deduplicate the initial shared datasets."""

    columns = settings["master_columns"]
    master_rows = read_and_clean_csv_bytes(
        master_contents, settings, "Master dataset"
    )
    lead_rows = read_and_clean_csv_bytes(
        new_leads_contents, settings, "New-leads-only dataset", allow_empty=True
    )
    master_rows = list({
        fingerprint(row, settings["dedupe_columns"]): row
        for row in master_rows
    }.values())
    lead_rows = list({row["Customer ID"]: row for row in lead_rows}.values())
    return csv_bytes(master_rows, columns), csv_bytes(lead_rows, columns)


class PersistentDatasetStore:
    """Keep both datasets and their backups together in PostgreSQL."""

    def __init__(
        self,
        database_url: str,
        settings: dict[str, Any],
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.database_url = database_url
        self.settings = settings
        self._connect = connect or self._default_connect

    @staticmethod
    def _default_connect(database_url: str):
        import psycopg

        return psycopg.connect(database_url, connect_timeout=15)

    def connect(self):
        return self._connect(self.database_url)

    def ensure_schema(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("CREATE SCHEMA IF NOT EXISTS lma_private")
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lma_private.dataset_state (
                    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
                    version bigint NOT NULL,
                    master_csv bytea NOT NULL,
                    new_leads_csv bytea NOT NULL,
                    updated_at timestamptz NOT NULL DEFAULT now(),
                    updated_by text NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lma_private.dataset_backups (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    version bigint NOT NULL UNIQUE,
                    master_csv bytea NOT NULL,
                    new_leads_csv bytea NOT NULL,
                    created_at timestamptz NOT NULL DEFAULT now(),
                    created_by text NOT NULL,
                    source_name text NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lma_private.import_log (
                    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                    dataset_version bigint NOT NULL,
                    imported_at timestamptz NOT NULL DEFAULT now(),
                    imported_by text NOT NULL,
                    source_name text NOT NULL,
                    cleaned_rows integer NOT NULL,
                    appended_rows integer NOT NULL,
                    skipped_duplicate_rows integer NOT NULL,
                    new_customers integer NOT NULL
                )
            """)

    def status(self) -> DatasetStatus | None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT version, updated_at, updated_by
                FROM lma_private.dataset_state
                WHERE singleton = true
            """)
            row = cursor.fetchone()
        return DatasetStatus(*row) if row else None

    def seed(
        self,
        master_contents: bytes,
        new_leads_contents: bytes,
        actor: str,
    ) -> DatasetStatus:
        master, leads = prepare_seed(
            master_contents, new_leads_contents, self.settings
        )
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute("SELECT 1 FROM lma_private.dataset_state WHERE singleton = true")
            if cursor.fetchone():
                raise ValueError("Shared datasets are already initialized.")
            cursor.execute("""
                INSERT INTO lma_private.dataset_state (
                    singleton, version, master_csv, new_leads_csv, updated_by
                ) VALUES (true, 1, %s, %s, %s)
                RETURNING version, updated_at, updated_by
            """, (_compressed(master), _compressed(leads), actor))
            return DatasetStatus(*cursor.fetchone())

    def downloads(self) -> tuple[bytes, bytes, DatasetStatus]:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT version, master_csv, new_leads_csv, updated_at, updated_by
                FROM lma_private.dataset_state
                WHERE singleton = true
            """)
            row = cursor.fetchone()
        if not row:
            raise ValueError("Shared datasets have not been initialized.")
        version, master, leads, updated_at, updated_by = row
        return (
            _uncompressed(master),
            _uncompressed(leads),
            DatasetStatus(version, updated_at, updated_by),
        )

    def import_report(
        self,
        source_contents: bytes,
        source_name: str,
        actor: str,
    ) -> UploadedImportResult:
        """Serialize imports, back up both datasets, and commit them together."""

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute("""
                SELECT version, master_csv, new_leads_csv
                FROM lma_private.dataset_state
                WHERE singleton = true
                FOR UPDATE
            """)
            state = cursor.fetchone()
            if not state:
                raise ValueError("Shared datasets have not been initialized.")

            version, stored_master, stored_leads = state
            master = _uncompressed(stored_master)
            leads = _uncompressed(stored_leads)
            result = run_uploaded_import(
                source_contents=source_contents,
                source_name=source_name,
                master_contents=master,
                new_leads_contents=leads,
                settings=self.settings,
            )
            next_version = version + 1

            cursor.execute("""
                INSERT INTO lma_private.dataset_backups (
                    version, master_csv, new_leads_csv, created_by, source_name
                ) VALUES (%s, %s, %s, %s, %s)
            """, (version, stored_master, stored_leads, actor, source_name))
            cursor.execute("""
                UPDATE lma_private.dataset_state
                SET version = %s,
                    master_csv = %s,
                    new_leads_csv = %s,
                    updated_at = now(),
                    updated_by = %s
                WHERE singleton = true
            """, (
                next_version,
                _compressed(result.master_csv),
                _compressed(result.new_leads_csv),
                actor,
            ))
            cursor.execute("""
                INSERT INTO lma_private.import_log (
                    dataset_version, imported_by, source_name, cleaned_rows,
                    appended_rows, skipped_duplicate_rows, new_customers
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                next_version,
                actor,
                source_name,
                result.cleaned_rows,
                result.appended_rows,
                result.skipped_duplicate_rows,
                result.new_customers,
            ))
            cursor.execute("""
                DELETE FROM lma_private.dataset_backups
                WHERE id NOT IN (
                    SELECT id
                    FROM lma_private.dataset_backups
                    ORDER BY id DESC
                    LIMIT %s
                )
            """, (BACKUP_RETENTION,))
            return result
