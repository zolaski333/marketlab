"""Tests for content-addressed blob storage (§8.2, §24.4)."""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from marketlab.core.failures import IntegrityError
from marketlab.storage.blobs import BlobStore, sha256_hex


@pytest.fixture(scope="module")
def shared_store(tmp_path_factory) -> BlobStore:
    """One store shared across Hypothesis examples.

    Sharing is safe here precisely because the store is content-addressed:
    writes are idempotent and independent, so examples cannot interfere. A
    function-scoped fixture would trip Hypothesis' health check for being reused
    across examples anyway.
    """
    return BlobStore(tmp_path_factory.mktemp("blobs"))


@given(content=st.binary(max_size=2048))
def test_round_trip_returns_the_exact_bytes(shared_store: BlobStore, content: bytes) -> None:
    ref = shared_store.put(content)
    assert shared_store.get(ref.digest) == content
    assert ref.digest == sha256_hex(content)
    assert ref.size == len(content)


def test_storing_identical_content_twice_is_idempotent(blob_store: BlobStore) -> None:
    """Replaying an interrupted ingestion must not duplicate anything (§30.6)."""
    first = blob_store.put(b"same bytes")
    second = blob_store.put(b"same bytes")
    assert first == second
    assert list(blob_store.iter_digests()) == [first.digest]


def test_distinct_content_yields_distinct_blobs(blob_store: BlobStore) -> None:
    """Near-duplicate documents stay distinct: §8.4 forbids dropping originals."""
    a = blob_store.put(b"Alpha beats estimates.")
    b = blob_store.put(b"Alpha beats estimates!")
    assert a.digest != b.digest
    assert len(list(blob_store.iter_digests())) == 2


def test_sharded_layout_matches_the_specified_path(blob_store: BlobStore) -> None:
    ref = blob_store.put(b"payload")
    path = blob_store.path_for(ref.digest)
    assert path.parent.name == ref.digest[2:4]
    assert path.parent.parent.name == ref.digest[:2]
    assert path.name == ref.digest


def test_missing_blob_raises_rather_than_returning_empty(blob_store: BlobStore) -> None:
    with pytest.raises(IntegrityError, match="Blob not found"):
        blob_store.get("a" * 64)


def test_corrupted_content_is_detected_on_read(blob_store: BlobStore) -> None:
    """Serving altered evidence would invalidate every claim citing it."""
    ref = blob_store.put(b"original evidence")
    blob_store.path_for(ref.digest).write_bytes(b"tampered evidence")

    with pytest.raises(IntegrityError, match="content was altered on disk"):
        blob_store.get(ref.digest)


def test_verify_reports_every_corrupted_blob(blob_store: BlobStore) -> None:
    good = blob_store.put(b"intact")
    bad = blob_store.put(b"will be altered")
    blob_store.path_for(bad.digest).write_bytes(b"altered")

    assert blob_store.verify() == [bad.digest]
    assert good.digest not in blob_store.verify()


@pytest.mark.parametrize(
    "malicious",
    [
        "../../../etc/passwd",
        "..\\..\\windows\\system32",
        "/absolute/path",
        "a" * 63,  # too short
        "a" * 65,  # too long
        "A" * 64,  # uppercase is not the canonical digest form
        "z" * 64,  # non-hex
    ],
)
def test_non_digest_addresses_are_refused(blob_store: BlobStore, malicious: str) -> None:
    """Digests build filesystem paths, so traversal must be unrepresentable."""
    with pytest.raises(ValueError, match="Not a SHA-256 hex digest"):
        blob_store.path_for(malicious)


def test_empty_content_is_storable(blob_store: BlobStore) -> None:
    """An empty response body is a real observation, not an absence."""
    ref = blob_store.put(b"")
    assert ref.size == 0
    assert blob_store.get(ref.digest) == b""


def test_no_temp_files_remain_after_a_successful_write(blob_store: BlobStore) -> None:
    blob_store.put(b"content")
    leftovers = [p for p in blob_store.root.rglob("*.tmp")]
    assert leftovers == []
