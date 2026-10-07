"""Data-lake writer (docs/architecture/historical-data.md §6.3, backlog D-09 groundwork).

* ``raw/``: immutable, content-addressed, gzip of the exact vendor bytes plus a write-once JSON sidecar.
  Writing different bytes under an existing name raises ``LakeError``. Nothing in ``raw/`` is ever rewritten.
* ``clean/`` and ``quarantine/``: Parquet parts, each carrying its full lineage in the file metadata
  (key ``p100c.lineage``). Chunks whose data-quality result is BLOCKING go to ``quarantine/``, never ``clean/``.
* ``ref/``: reference tables such as the point-in-time instrument list.
* ``manifests/``: one JSON per ingested chunk (row counts, hashes, DQ results, lineage).

Every write is atomic (temp file, fsync, rename), so a crash never leaves a half-written file under a final name.
Writes are deterministic: re-ingesting the same raw bytes gives byte-identical Parquet and manifests.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from project100c.errors import LakeError

LAKE_LAYOUT_VERSION = "LAKE-2026-10-01.1"
LINEAGE_METADATA_KEY = b"p100c.lineage"
_SAFE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9=._+\-]*$")


class Zone(StrEnum):
    CLEAN = "clean"
    QUARANTINE = "quarantine"
    REF = "ref"


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json(obj: Any) -> bytes:
    """Deterministic JSON (sorted keys, no whitespace). Raises LakeError for non-JSON values (no silent str())."""
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()
    except (TypeError, ValueError) as e:
        raise LakeError(f"value is not canonical-JSON serialisable: {e}") from e


@dataclass(frozen=True, slots=True)
class RawRef:
    path: str  # relative to the lake root
    sha256: str  # of the uncompressed vendor bytes
    size: int  # uncompressed bytes
    fetched_at_utc: str  # first time these exact bytes were stored (from the sidecar)


@dataclass(frozen=True, slots=True)
class PartRef:
    path: str
    sha256: str  # of the Parquet file
    rows: int


@dataclass(frozen=True, slots=True)
class Lineage:
    """Where a Parquet part came from. ``request`` must be secret-free (it is embedded in files)."""

    source: str
    dataset: str
    data_version: str
    parser_version: str
    config_version: str
    dq_thresholds_version: str
    dq_status: str
    raw_path: str
    raw_sha256: str
    fetched_at_utc: str
    request: Mapping[str, Any]
    job_id: str | None = None
    chunk_key: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "lake_layout_version": LAKE_LAYOUT_VERSION,
            "source": self.source,
            "dataset": self.dataset,
            "data_version": self.data_version,
            "parser_version": self.parser_version,
            "config_version": self.config_version,
            "dq_thresholds_version": self.dq_thresholds_version,
            "dq_status": self.dq_status,
            "raw_path": self.raw_path,
            "raw_sha256": self.raw_sha256,
            "fetched_at_utc": self.fetched_at_utc,
            "request": dict(self.request),
            "job_id": self.job_id,
            "chunk_key": self.chunk_key,
        }


def _fsync_dir(d: Path) -> None:
    fd = os.open(d, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with tmp.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        _fsync_dir(path.parent)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise LakeError(f"atomic write failed for {path}: {e}") from e


class Lake:
    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        return self._root

    def _seg(self, s: str, what: str) -> str:
        if not _SAFE.match(s) or ".." in s:
            raise LakeError(f"unsafe {what} path segment {s!r}")
        return s

    def abspath(self, rel: str) -> Path:
        p = (self._root / rel).resolve()
        if self._root.resolve() not in p.parents:
            raise LakeError(f"path escapes the lake: {rel}")
        return p

    def _rel(self, p: Path) -> str:
        return p.relative_to(self._root).as_posix()

    # ---------------------------------------------------------------- raw
    def write_raw(
        self,
        source: str,
        dataset: str,
        day: date,
        stem: str,
        body: bytes,
        *,
        sidecar: Mapping[str, Any],
        ext: str = "json",
    ) -> RawRef:
        """Store vendor bytes exactly once. Same bytes again -> the existing ref (first sidecar wins).

        ``ext`` names the vendor format inside the gzip (``json`` for API replies, ``zip``/``csv`` for archives).
        """
        digest = sha256_hex(body)
        if ext not in ("json", "zip", "csv", "xml"):
            raise LakeError(f"unsupported raw extension {ext!r}")
        d = self._root / "raw" / self._seg(source, "source") / self._seg(dataset, "dataset") / f"date={day.isoformat()}"
        name = f"{self._seg(stem, 'stem')}.{digest[:16]}.{ext}.gz"
        path = d / name
        meta_path = d / f"{name}.meta.json"
        if path.exists():
            existing = self.read_raw(self._rel(path))
            if sha256_hex(existing) != digest:  # pragma: no cover - read_raw already verifies
                raise LakeError(f"raw immutability violated: {path}")
            if not meta_path.exists():
                raise LakeError(f"raw sidecar missing for existing raw file {path}")
            meta = json.loads(meta_path.read_bytes())
            return RawRef(self._rel(path), digest, len(body), str(meta["fetched_at_utc"]))
        if "fetched_at_utc" not in sidecar:
            raise LakeError("raw sidecar must contain fetched_at_utc")
        meta = {**dict(sidecar), "sha256": digest, "size": len(body)}
        _atomic_write(meta_path, canonical_json(meta))
        _atomic_write(path, gzip.compress(body, compresslevel=6, mtime=0))
        return RawRef(self._rel(path), digest, len(body), str(sidecar["fetched_at_utc"]))

    def read_raw(self, rel: str) -> bytes:
        path = self.abspath(rel)
        try:
            body = gzip.decompress(path.read_bytes())
        except FileNotFoundError as e:
            raise LakeError(f"raw file not found: {rel}") from e
        except (OSError, EOFError) as e:
            raise LakeError(f"raw file corrupt (gzip): {rel}: {e}") from e
        expected = path.name.split(".")[-3]
        if not sha256_hex(body).startswith(expected):
            raise LakeError(f"raw file corrupt: content hash does not match its name: {rel}")
        return body

    # ---------------------------------------------------------------- parts
    def write_part(
        self,
        zone: Zone,
        dataset: str,
        partitions: Sequence[tuple[str, str]],
        part_name: str,
        table: pa.Table,
        lineage: Lineage,
    ) -> PartRef:
        """Write one Parquet part (deterministic name, atomic replace). Lineage goes into the file metadata."""
        d = self._root / zone.value / self._seg(dataset, "dataset")
        for k, v in partitions:
            d = d / f"{self._seg(k, 'partition key')}={self._seg(v, 'partition value')}"
        path = d / f"part-{self._seg(part_name, 'part')}.parquet"
        meta = dict(table.schema.metadata or {})
        meta[LINEAGE_METADATA_KEY] = canonical_json(lineage.as_dict())
        t = table.replace_schema_metadata(meta)
        sink = pa.BufferOutputStream()
        try:
            pq.write_table(t, sink, compression="zstd", write_statistics=True)
        except (pa.ArrowException, ValueError, TypeError) as e:
            raise LakeError(f"parquet encode failed for {dataset}/{part_name}: {e}") from e
        data = sink.getvalue().to_pybytes()
        _atomic_write(path, data)
        return PartRef(self._rel(path), sha256_hex(data), t.num_rows)

    def discard_uncommitted_part(
        self, zone: Zone, dataset: str, partitions: Sequence[tuple[str, str]], part_name: str
    ) -> bool:
        """Remove a part left by an attempt that crashed before its commit (e.g. clean/ when the redo is now
        quarantined). Only callers that know the chunk is not committed may use this. Returns True if removed."""
        d = self._root / zone.value / self._seg(dataset, "dataset")
        for k, v in partitions:
            d = d / f"{self._seg(k, 'partition key')}={self._seg(v, 'partition value')}"
        path = d / f"part-{self._seg(part_name, 'part')}.parquet"
        if path.exists():
            path.unlink()
            return True
        return False

    def read_part(self, rel: str, *, expected_sha256: str | None = None) -> pa.Table:
        path = self.abspath(rel)
        try:
            data = path.read_bytes()
        except FileNotFoundError as e:
            raise LakeError(f"part not found: {rel}") from e
        if expected_sha256 is not None and sha256_hex(data) != expected_sha256:
            raise LakeError(f"part corrupt: sha256 mismatch for {rel}")
        try:
            return pq.read_table(pa.BufferReader(data))
        except (pa.ArrowException, OSError) as e:
            raise LakeError(f"part unreadable: {rel}: {e}") from e

    def lineage_of(self, rel: str) -> dict[str, Any]:
        meta = self.read_part(rel).schema.metadata or {}
        raw = meta.get(LINEAGE_METADATA_KEY)
        if raw is None:
            raise LakeError(f"part has no lineage metadata: {rel}")
        out: dict[str, Any] = json.loads(raw)
        return out

    def parts(self, zone: Zone, dataset: str) -> list[str]:
        base = self._root / zone.value / self._seg(dataset, "dataset")
        if not base.exists():
            return []
        return sorted(self._rel(p) for p in base.rglob("part-*.parquet"))

    # ---------------------------------------------------------------- manifests
    def write_manifest(self, dataset: str, name: str, payload: Mapping[str, Any]) -> str:
        path = self._root / "manifests" / self._seg(dataset, "dataset") / f"{self._seg(name, 'manifest')}.json"
        _atomic_write(path, canonical_json(dict(payload)))
        return self._rel(path)

    def manifest_rel(self, dataset: str, name: str) -> str:
        return f"manifests/{self._seg(dataset, 'dataset')}/{self._seg(name, 'manifest')}.json"

    def manifests(self, dataset: str) -> list[str]:
        """Relative paths of every manifest of ``dataset``, sorted (deterministic)."""
        base = self._root / "manifests" / self._seg(dataset, "dataset")
        if not base.exists():
            return []
        return sorted(self._rel(p) for p in base.glob("*.json"))

    def read_manifest(self, rel: str) -> dict[str, Any]:
        try:
            out: dict[str, Any] = json.loads(self.abspath(rel).read_bytes())
        except FileNotFoundError as e:
            raise LakeError(f"manifest not found: {rel}") from e
        except json.JSONDecodeError as e:
            raise LakeError(f"manifest corrupt: {rel}: {e}") from e
        return out
