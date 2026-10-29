"""Transactional persistent storage for the two shared LMA datasets."""

from __future__ import annotations

import hashlib
import logging
import zlib
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, Callable
from types import SimpleNamespace

from .pipeline import (
    UploadedImportResult,
    csv_bytes,
    first_seen_customer_rows,
    read_and_clean_csv_bytes,
    row_snapshot,
    run_uploaded_import,
    validate_upload,
    infer_period,
    category_period_comparisons,
    analysis_csv_bytes,
    job_status_analysis,
)


LOCK_KEY = 4_812_291
MAP_TRACKING_SCHEMA = """
    CREATE TABLE IF NOT EXISTS lma_private.map_refresh_state (
        singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
        attempt_version bigint NOT NULL,
        success_version bigint,
        refreshed_at timestamptz,
        error text
    )
"""


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
    outputs_loaded: bool = True
    has_cleaned: bool = False
    has_analysis: bool = False


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
            cursor.execute(MAP_TRACKING_SCHEMA)
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

    def history_listing(self):
        """List all reports without transferring any generated-file blobs."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT log.id, log.dataset_version, log.imported_at, log.imported_by,
                       log.source_name, log.content_hash,
                       outputs.cleaned_csv IS NOT NULL, outputs.analysis_csv IS NOT NULL
                FROM lma_private.import_log log
                LEFT JOIN lma_private.import_outputs outputs ON outputs.dataset_version = log.dataset_version
                ORDER BY log.imported_at DESC, log.id DESC
            """)
            return [ImportHistoryEntry(*row[:6], None, None, outputs_loaded=False,
                                       has_cleaned=row[6], has_analysis=row[7]) for row in cursor.fetchall()]

    def history_outputs(self, record_id):
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT outputs.cleaned_csv, outputs.analysis_csv
                FROM lma_private.import_log log
                LEFT JOIN lma_private.import_outputs outputs ON outputs.dataset_version = log.dataset_version
                WHERE log.id = %s
            """, (record_id,))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("This report is no longer listed.")
        return tuple(_uncompressed(value) if value is not None else None for value in row)

    def saved_reports(self):
        """Load report selector metadata without downloading generated files."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT id, dataset_version, source_name, imported_at
                FROM lma_private.import_log ORDER BY imported_at DESC, id DESC
            """)
            return cursor.fetchall()

    def load_saved_report(self, record_id: int, *, cached_snapshot=None):
        """Restore original metrics; regenerate analysis from the saved master only."""
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("""
                SELECT log.dataset_version, log.source_name, log.cleaned_rows,
                       log.appended_rows, log.updated_rows, log.skipped_duplicate_rows,
                       log.new_customers, outputs.cleaned_csv, outputs.analysis_csv,
                       CASE WHEN log.dataset_version = %s THEN NULL ELSE
                           COALESCE(backup.master_csv,
                           CASE WHEN state.version = log.dataset_version THEN state.master_csv END) END
                FROM lma_private.import_log log
                LEFT JOIN lma_private.import_outputs outputs ON outputs.dataset_version = log.dataset_version
                LEFT JOIN lma_private.dataset_backups backup ON backup.version = log.dataset_version
                CROSS JOIN lma_private.dataset_state state
                WHERE log.id = %s
            """, (cached_snapshot[0] if cached_snapshot else None, record_id))
            row = cursor.fetchone()
        if row is None:
            raise ValueError("This report is no longer listed. Select another report.")
        version, name, valid, added, updated, unchanged, customers, cleaned, original, master = row
        cleaned = _uncompressed(cleaned) if cleaned is not None else None
        original = _uncompressed(original) if original is not None else None
        master = _uncompressed(master) if master is not None else None
        cached_rows = None
        if cached_snapshot and cached_snapshot[0] == version:
            _, master, cached_rows = cached_snapshot
        start = end = None
        comparisons = None
        corrected = None
        if cleaned is not None:
            incoming = read_and_clean_csv_bytes(cleaned, self.settings, "Saved report")
            start, end = infer_period(incoming, self.settings)
            if master is not None:
                master_rows = cached_rows if cached_rows is not None else read_and_clean_csv_bytes(master, self.settings, "Saved master")
                comparisons = category_period_comparisons(master_rows, start, end, self.settings)
                statuses, columns = job_status_analysis(incoming)
                corrected = analysis_csv_bytes(comparisons, statuses, columns)
        return SimpleNamespace(
            dataset_version=version, source_name=name, cleaned_rows=valid,
            appended_rows=added, updated_rows=updated, skipped_duplicate_rows=unchanged,
            new_customers=customers, cleaned_csv=cleaned, original_analysis_csv=original,
            analysis_csv=corrected, master_csv=master, period_start=start, period_end=end,
            category_comparisons=comparisons, removed_rows=0,
        )

    def map_status(self, *, allow_setup=False):
        import psycopg
        try:
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SELECT attempt_version, success_version, refreshed_at, error FROM lma_private.map_refresh_state WHERE singleton = true")
                row = cursor.fetchone()
        except psycopg.errors.UndefinedTable:
            if not allow_setup:
                return {"setup_required": True}
            # Optional map tracking must never block core dataset startup.
            with self.connect() as connection, connection.cursor() as cursor:
                cursor.execute("SET LOCAL lock_timeout = '3s'")
                cursor.execute("SET LOCAL statement_timeout = '15s'")
                cursor.execute(MAP_TRACKING_SCHEMA)
            return self.map_status()
        return dict(zip(("attempt_version", "success_version", "refreshed_at", "error"), row)) if row else None

    def refresh_maps(self, publish, *, prepared=None):
        """Publish the latest committed version while holding the import lock.

        No older import result can overwrite a newer version. Google failures
        are caught inside the transaction so the failure status is committed.
        The two separate spreadsheets cannot be updated atomically together.
        """
        from .zip_maps import zip_code_summaries, map_error_message
        with self.connect() as connection, connection.cursor() as cursor:
            cursor.execute("SET LOCAL lock_timeout = '5s'")
            cursor.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))
            cursor.execute("SELECT attempt_version FROM lma_private.map_refresh_state LIMIT 0")
            cursor.execute("SELECT version, CASE WHEN version = %s THEN NULL ELSE master_csv END FROM lma_private.dataset_state WHERE singleton = true",
                           (getattr(prepared, 'dataset_version', None),))
            version, compressed_master = cursor.fetchone()
            error = refreshed_at = None
            try:
                if prepared is not None and prepared.dataset_version == version:
                    payload = prepared
                else:
                    rows = read_and_clean_csv_bytes(_uncompressed(compressed_master), self.settings, "Master")
                    year, revenue, leads = zip_code_summaries(rows, first_seen_customer_rows(rows, self.settings), self.settings)
                    payload = SimpleNamespace(zip_year=year, zip_revenue_rows=revenue, zip_new_lead_rows=leads)
                refreshed_at = publish(payload)
                if not refreshed_at:
                    raise ValueError("Missing map configuration")
            except Exception as exception:
                error = map_error_message(exception)
                status_code = getattr(getattr(exception, "response", None), "status_code", None)
                logging.getLogger(__name__).warning("Map refresh failed: %s (HTTP %s)", type(exception).__name__, status_code if isinstance(status_code, int) else "unavailable")
            cursor.execute("""
                INSERT INTO lma_private.map_refresh_state
                    (singleton, attempt_version, success_version, refreshed_at, error)
                VALUES (true, %s, %s, %s, %s)
                ON CONFLICT (singleton) DO UPDATE SET
                    attempt_version = EXCLUDED.attempt_version,
                    success_version = COALESCE(EXCLUDED.success_version, map_refresh_state.success_version),
                    refreshed_at = COALESCE(EXCLUDED.refreshed_at, map_refresh_state.refreshed_at),
                    error = EXCLUDED.error
                RETURNING attempt_version, success_version, refreshed_at, error
            """, (version, version if error is None else None, refreshed_at, error))
            return dict(zip(("attempt_version", "success_version", "refreshed_at", "error"), cursor.fetchone()))

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
                cleaned_rows=cleaned_rows,
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
            return replace(result, dataset_version=next_version, source_name=source_name)
