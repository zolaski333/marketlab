"""Content-addressed blob storage (§8.2).

Every byte received from an external source is kept verbatim, addressed by the
SHA-256 of its content::

    data/blobs/sha256/ab/cd/abcd...  (full 64-hex name)

Content addressing gives three things the study depends on:

* **Immutability by construction.** The name *is* the hash, so a modified blob
  is a different blob. There is no in-place edit that could go unnoticed.
* **Free deduplication.** Identical payloads collapse to one file, while §8.4
  is still honoured because deduplication happens at the *byte* level, not the
  document level — two near-duplicate news items remain two distinct blobs.
* **Verifiability.** :meth:`BlobStore.verify` recomputes every digest, so
  ``marketlab audit verify`` can prove the raw corpus was not altered (§24.4).

Writes are atomic: content goes to a temporary file in the same directory and is
then moved into place with :func:`os.replace`, which is atomic on both POSIX and
Windows for same-volume moves. A crash mid-write leaves a stray temp file, never
a truncated blob that would fail its own hash forever after.

Metadata (media type, size, source, licence, ingestion event) lives in the
relational store rather than beside the file, so it can be queried and versioned;
this module owns only the bytes.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import Integer
from sqlalchemy.orm import Mapped, mapped_column

from marketlab.core.failures import IntegrityError
from marketlab.storage.base import Base, HashStr, InstantStr, ShortStr

__all__ = ["BlobMetadataRow", "BlobRef", "BlobStore", "sha256_hex"]

_HEX_DIGITS = frozenset("0123456789abcdef")


def sha256_hex(content: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``content``."""
    return hashlib.sha256(content).hexdigest()


def _validate_digest(digest: str) -> str:
    """Reject anything that is not a well-formed SHA-256 hex digest.

    This is also the path-traversal guard: digests are used to build filesystem
    paths, and an attacker-influenced value such as ``"../../etc/passwd"`` must
    never reach :meth:`Path.__truediv__`. Restricting to 64 hex characters makes
    traversal unrepresentable rather than merely unlikely.
    """
    if len(digest) != 64 or not set(digest).issubset(_HEX_DIGITS):
        raise ValueError(
            f"Not a SHA-256 hex digest: {digest!r}. Blob addresses are restricted "
            "to 64 lowercase hex characters so they cannot express a path."
        )
    return digest


@dataclass(frozen=True, slots=True)
class BlobRef:
    """A stored blob: its digest and its size in bytes."""

    digest: str
    size: int


class BlobMetadataRow(Base):
    """Provenance and reuse terms for one stored blob (§8.2, §8.7).

    Kept relational rather than as a sidecar file so it can be queried and
    joined. ``licence`` and ``redistributable`` are what let
    ``marketlab export release`` decide, per artefact, whether the bytes may be
    published or only their hash (§8.7).
    """

    __tablename__ = "blob_metadata"

    digest: Mapped[str] = mapped_column(HashStr, primary_key=True)
    media_type: Mapped[str] = mapped_column(ShortStr, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    source_id: Mapped[str] = mapped_column(ShortStr, nullable=False, index=True)
    """Which provider adapter produced these bytes."""

    licence: Mapped[str] = mapped_column(ShortStr, nullable=False)
    redistributable: Mapped[bool] = mapped_column(nullable=False, default=False)
    """Whether the raw bytes may appear in a published release (§8.7)."""

    first_seen_at: Mapped[str] = mapped_column(InstantStr, nullable=False, index=True)
    """When the platform first observed this content — the field that gates
    availability to a decision (§6.2), not the source's own publication time."""

    ingested_at: Mapped[str] = mapped_column(InstantStr, nullable=False)
    ingestion_event_id: Mapped[str] = mapped_column(HashStr, nullable=False, index=True)


class BlobStore:
    """Append-only, content-addressed byte storage."""

    __slots__ = ("_root",)

    def __init__(self, root: Path | str) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, digest: str) -> Path:
        """Return the on-disk path for ``digest`` without touching the disk."""
        safe = _validate_digest(digest)
        return self._root / "sha256" / safe[:2] / safe[2:4] / safe

    def put(self, content: bytes) -> BlobRef:
        """Store ``content`` and return its reference.

        Idempotent: storing identical bytes twice is a no-op that returns the
        same reference, which is what lets an interrupted ingestion be replayed
        safely (§30.6).
        """
        digest = sha256_hex(content)
        target = self.path_for(digest)

        if target.exists():
            existing = target.stat().st_size
            if existing != len(content):  # pragma: no cover - corruption guard
                raise IntegrityError(
                    f"Blob {digest} exists with size {existing} but new content is "
                    f"{len(content)} bytes; the store is corrupt.",
                    digest=digest,
                )
            return BlobRef(digest, len(content))

        target.parent.mkdir(parents=True, exist_ok=True)
        # Write to a sibling temp file, then move into place atomically so a
        # crash can never leave a truncated file under a valid digest name.
        handle, temp_name = tempfile.mkstemp(dir=target.parent, suffix=".tmp")
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_name, target)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise

        return BlobRef(digest, len(content))

    def get(self, digest: str, *, verify: bool = True) -> bytes:
        """Read a blob back.

        Args:
            verify: recompute the digest and refuse content that does not match
                its address. Defaults to on; the cost is trivial next to a model
                call, and silently serving corrupted evidence would invalidate
                every claim that cites it.
        """
        path = self.path_for(digest)
        if not path.exists():
            raise IntegrityError(f"Blob not found: {digest}", digest=digest)
        content = path.read_bytes()
        if verify:
            actual = sha256_hex(content)
            if actual != digest:  # pragma: no cover - corruption guard
                raise IntegrityError(
                    f"Blob {digest} hashes to {actual}: content was altered on disk.",
                    digest=digest,
                    actual=actual,
                )
        return content

    def exists(self, digest: str) -> bool:
        return self.path_for(digest).exists()

    def iter_digests(self) -> Iterator[str]:
        """Yield every stored digest."""
        base = self._root / "sha256"
        if not base.exists():
            return
        for path in sorted(base.rglob("*")):
            if path.is_file() and len(path.name) == 64:
                yield path.name

    def verify(self) -> list[str]:
        """Recompute every digest; return the addresses that failed.

        Used by ``marketlab audit verify`` (§24.4).
        """
        corrupted: list[str] = []
        for digest in self.iter_digests():
            content = self.path_for(digest).read_bytes()
            if sha256_hex(content) != digest:
                corrupted.append(digest)
        return corrupted
