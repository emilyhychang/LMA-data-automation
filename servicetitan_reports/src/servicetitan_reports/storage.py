"""Transactional persistent storage for the two shared LMA datasets."""

from __future__ import annotations

import hashlib
import zlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from .pipeline import (
    UploadedImportResult,
    csv_bytes,
    first_seen_customer_rows,
    read_and_clean_csv_bytes,
    row_snapshot,
    run_uploaded_import,
    validate_upload,
)


LOCK_KEY = 4_812_291


@dataclass(frozen=True)
class DatasetStatus:
    version: int
    updated_at: datetime
    updated_by: str


@dataclass(frozen=True)
class ImportHistoryEntry:
    record_id: int
    dataset_version: int
    imported_at: datetime
    imported_by: str
    source_name: str
    content_hash: str | None
    cleaned_csv: bytes | None
    analysis_csv: bytes | None
    master_csv: bytes | None = None
    new_leads_csv: bytes | None = None


@dataclass(frozen=True)
class DuplicateImportHistoryEntry:
    duplicate: ImportHistoryEntry
    keeper: ImportHistoryEntry


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
        row_snapshot(row, settings): row for row in master_rows
    }.values())
    lead_rows = first_seen_customer_rows(lead_rows, settings)
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

    def startup_status(self) -> DatasetStatus | None:
        """Check the schema and current version without executing migrations.

        Probe every column used by this app so older installations can still be
        upgraded deliberately, rather than running DDL on every page rerun.
        No master CSVs are transferred by these zero-row checks.
        """

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL statement_timeout = '15s'")
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute("""
                SELECT singleton, version, master_csv, new_leads_csv,
                       updated_at, updated_by
                FROM lma_private.dataset_state LIMIT 0
            """)
            cursor.execute("""
                SELECT id, version, master_csv, new_leads_csv, created_at,
                       created_by, source_name
                FROM lma_private.dataset_backups LIMIT 0
            """)
            cursor.execute("""
                SELECT id, dataset_version, imported_at, imported_by, source_name,
                       content_hash, cleaned_rows, appended_rows, updated_rows,
                       skipped_duplicate_rows, new_customers
                FROM lma_private.import_log LIMIT 0
            """)
            cursor.execute("""
                SELECT dataset_version, cleaned_csv, analysis_csv
                FROM lma_private.import_outputs LIMIT 0
            """)
            cursor.execute("""
                SELECT version, updated_at, updated_by
                FROM lma_private.dataset_state WHERE singleton = true
            """)
            row = cursor.fetchone()
        return DatasetStatus(*row) if row else None

    def ensure_schema(self) -> None:
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '3s'")
            cursor.execute("SET LOCAL statement_timeout = '30s'")
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
                    content_hash text,
                    cleaned_rows integer NOT NULL,
                    appended_rows integer NOT NULL,
                    updated_rows integer NOT NULL DEFAULT 0,
                    skipped_duplicate_rows integer NOT NULL,
                    new_customers integer NOT NULL
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS lma_private.import_outputs (
                    dataset_version bigint PRIMARY KEY,
                    cleaned_csv bytea NOT NULL,
                    analysis_csv bytea NOT NULL
                )
            """)
            cursor.execute("""
                ALTER TABLE lma_private.import_log
                ADD COLUMN IF NOT EXISTS updated_rows integer NOT NULL DEFAULT 0
            """)
            cursor.execute("""
                ALTER TABLE lma_private.import_log
                ADD COLUMN IF NOT EXISTS content_hash text
            """)
            cursor.execute("""
                CREATE INDEX IF NOT EXISTS import_log_content_hash_idx
                ON lma_private.import_log (content_hash)
                WHERE content_hash IS NOT NULL
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

    def import_history(self, limit: int | None = None, *, include_snapshots: bool = True) -> list[ImportHistoryEntry]:
        """Return all retained history by default, including unavailable old outputs."""

        with self.connect() as connection, connection.cursor() as cursor:
            if include_snapshots:
                cursor.execute("""
                    SELECT log.id, log.dataset_version, log.imported_at, log.imported_by,
                           log.source_name, log.content_hash,
                           outputs.cleaned_csv, outputs.analysis_csv,
                           COALESCE(
                               backup.master_csv,
                               CASE
                                   WHEN state.version = log.dataset_version
                                   THEN state.master_csv
                               END
                           ) AS master_csv,
                           COALESCE(
                               backup.new_leads_csv,
                               CASE
                                   WHEN state.version = log.dataset_version
                                   THEN state.new_leads_csv
                               END
                           ) AS new_leads_csv
                    FROM lma_private.import_log AS log
                    LEFT JOIN lma_private.import_outputs AS outputs
                      ON outputs.dataset_version = log.dataset_version
                    LEFT JOIN lma_private.dataset_backups AS backup
                      ON backup.version = log.dataset_version
                    CROSS JOIN lma_private.dataset_state AS state
                    ORDER BY log.imported_at DESC, log.id DESC
                    LIMIT %s
                """, (limit,))
            else:
                cursor.execute("""
                    SELECT log.id, log.dataset_version, log.imported_at, log.imported_by,
                           log.source_name, log.content_hash,
                           outputs.cleaned_csv, outputs.analysis_csv, NULL, NULL
                    FROM lma_private.import_log AS log
                    LEFT JOIN lma_private.import_outputs AS outputs
                      ON outputs.dataset_version = log.dataset_version
                    ORDER BY log.imported_at DESC, log.id DESC
                    LIMIT %s
                """, (limit,))
            rows = cursor.fetchall()
        return [
            ImportHistoryEntry(
                record_id=record_id,
                dataset_version=version,
                imported_at=imported_at,
                imported_by=imported_by,
                source_name=source_name,
                content_hash=content_hash,
                cleaned_csv=_uncompressed(cleaned) if cleaned is not None else None,
                analysis_csv=_uncompressed(analysis) if analysis is not None else None,
                master_csv=_uncompressed(master) if master is not None else None,
                new_leads_csv=(
                    _uncompressed(new_leads) if new_leads is not None else None
                ),
            )
            for record_id, version, imported_at, imported_by, source_name,
                content_hash, cleaned, analysis, master, new_leads in rows
        ]

    def history_snapshots(self, version: int) -> tuple[bytes | None, bytes | None]:
        """Load large master snapshots only for the requested history entry."""

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT master_csv, new_leads_csv
                FROM lma_private.dataset_backups WHERE version = %s
                UNION ALL
                SELECT master_csv, new_leads_csv
                FROM lma_private.dataset_state WHERE version = %s
                LIMIT 1
            """, (version, version))
            row = cursor.fetchone()
        if row is None:
            return None, None
        return _uncompressed(row[0]), _uncompressed(row[1])

    @staticmethod
    def _duplicate_history_entries(
        history: list[ImportHistoryEntry],
    ) -> list[DuplicateImportHistoryEntry]:
        """Group exact uploads, with a cleaned-output fallback for legacy rows."""

        groups: dict[str, list[ImportHistoryEntry]] = {}
        for entry in history:
            if not entry.content_hash and entry.cleaned_csv is None:
                continue
            fingerprint = entry.content_hash or (
                "legacy:" + hashlib.sha256(entry.cleaned_csv).hexdigest()
            )
            groups.setdefault(fingerprint, []).append(entry)

        duplicates = []
        for entries in groups.values():
            if len(entries) < 2:
                continue
            keeper = max(entries, key=lambda item: (item.imported_at, item.record_id))
            duplicates.extend(
                DuplicateImportHistoryEntry(duplicate=entry, keeper=keeper)
                for entry in entries
                if entry.record_id != keeper.record_id
            )
        return sorted(
            duplicates,
            key=lambda item: (
                item.duplicate.imported_at,
                item.duplicate.record_id,
            ),
            reverse=True,
        )

    def duplicate_import_history(self) -> list[DuplicateImportHistoryEntry]:
        """Preview removable history records without changing stored data."""

        return self._duplicate_history_entries(self.import_history(include_snapshots=False))

    def remove_duplicate_import_history(self, preview_ids) -> set[int]:
        """Delete only previewed rows that are still duplicates at confirmation."""

        requested = {int(record_id) for record_id in preview_ids if int(record_id) > 0}
        if not requested:
            return set()

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute("""
                SELECT log.id, log.dataset_version, log.imported_at, log.imported_by,
                       log.source_name, log.content_hash,
                       outputs.cleaned_csv, outputs.analysis_csv
                FROM lma_private.import_log AS log
                LEFT JOIN lma_private.import_outputs AS outputs
                  ON outputs.dataset_version = log.dataset_version
                ORDER BY log.imported_at DESC, log.id DESC
            """)
            history = [
                ImportHistoryEntry(
                    record_id=record_id,
                    dataset_version=version,
                    imported_at=imported_at,
                    imported_by=imported_by,
                    source_name=source_name,
                    content_hash=content_hash,
                    cleaned_csv=_uncompressed(cleaned) if cleaned is not None else None,
                    analysis_csv=_uncompressed(analysis) if analysis is not None else None,
                )
                for record_id, version, imported_at, imported_by, source_name,
                    content_hash, cleaned, analysis in cursor.fetchall()
            ]
            eligible = {
                item.duplicate.record_id
                for item in self._duplicate_history_entries(history)
            }
            to_delete = sorted(requested & eligible)
            if not to_delete:
                return set()
            cursor.execute("""
                DELETE FROM lma_private.import_log
                WHERE id = ANY(%s)
                RETURNING id
            """, (to_delete,))
            return {row[0] for row in cursor.fetchall()}

    def remove_import_history_entry(self, entry: ImportHistoryEntry) -> bool:
        """Remove one identified run from history, leaving datasets and backups intact."""

        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute("""
                DELETE FROM lma_private.import_log
                WHERE id = %s AND dataset_version = %s AND source_name = %s
                RETURNING id
            """, (entry.record_id, entry.dataset_version, entry.source_name))
            return cursor.fetchone() is not None

    def import_report(
        self,
        source_contents: bytes,
        source_name: str,
        actor: str,
        *,
        allow_historical: bool = False,
        allow_correction: bool = False,
    ) -> UploadedImportResult:
        """Serialize imports, back up both datasets, and commit them together."""

        cleaned_rows = read_and_clean_csv_bytes(source_contents, self.settings, "New ServiceTitan export")
        validate_upload(cleaned_rows, self.settings, allow_historical=allow_historical)
        content_hash = hashlib.sha256(source_contents).hexdigest()
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            # Generated outputs survive history-list deletion. Check those too so
            # hiding a listing does not allow an old upload to overwrite corrections.
            cursor.execute("""
                SELECT 1 FROM lma_private.import_log WHERE content_hash = %s
                UNION ALL
                SELECT 1 FROM lma_private.import_outputs WHERE cleaned_csv = %s
                LIMIT 1
            """, (content_hash, _compressed(csv_bytes(cleaned_rows, self.settings["master_columns"]))))
            if cursor.fetchone() and not allow_correction:
                raise ValueError("This exact file was already saved. Use its history downloads, or enable correction to intentionally reapply it.")
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
            result = run_uploaded_import(
                source_contents=source_contents,
                master_contents=master,
                settings=self.settings,
                allow_group_replacement=allow_correction,
            )
            if result.appended_rows == 0 and result.updated_rows == 0 and result.removed_rows == 0:
                raise ValueError("All uploaded records already match the master. No new version or backup was created.")
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
                    dataset_version, imported_by, source_name, content_hash, cleaned_rows,
                    appended_rows, updated_rows, skipped_duplicate_rows, new_customers
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                next_version,
                actor,
                source_name,
                content_hash,
                result.cleaned_rows,
                result.appended_rows,
                result.updated_rows,
                result.skipped_duplicate_rows,
                result.new_customers,
            ))
            cursor.execute("""
                INSERT INTO lma_private.import_outputs (
                    dataset_version, cleaned_csv, analysis_csv
                ) VALUES (%s, %s, %s)
            """, (
                next_version,
                _compressed(result.cleaned_csv),
                _compressed(result.analysis_csv),
            ))
            return result
