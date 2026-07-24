"""Content-addressed blob store for artifact bytes.

Layout: ``<root>/<sha256[:2]>/<sha256>`` under the profile's hermes home
(``~/.hermes/science/blobs`` by default), so each profile gets its own store.
Blobs are immutable and deduplicated by content; artifact_versions rows point
at them via ``storage_path`` + ``checksum``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class BlobRef:
    sha256: str
    size_bytes: int
    path: str


class BlobStore:
    def __init__(self, root: Path):
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)

    @property
    def root(self) -> Path:
        return self._root

    def _path_for(self, sha256: str) -> Path:
        return self._root / sha256[:2] / sha256

    def put_bytes(self, data: bytes) -> BlobRef:
        digest = hashlib.sha256(data).hexdigest()
        dest = self._path_for(digest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".ingest-")
            try:
                with os.fdopen(fd, "wb") as fh:
                    fh.write(data)
                os.replace(tmp, dest)  # atomic; loser of a race just re-links
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return BlobRef(sha256=digest, size_bytes=len(data), path=str(dest))

    def put_path(self, source: Path) -> BlobRef:
        source = Path(source)
        digest = _hash_file(source)
        size = source.stat().st_size
        dest = self._path_for(digest)
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(dest.parent), prefix=".ingest-")
            os.close(fd)
            try:
                shutil.copyfile(source, tmp)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)
        return BlobRef(sha256=digest, size_bytes=size, path=str(dest))

    def exists(self, sha256: str) -> bool:
        return self._path_for(sha256).is_file()

    def read_bytes(self, sha256: str) -> bytes:
        return self._path_for(sha256).read_bytes()

    def materialize(self, sha256: str, dest: Path) -> Path:
        """Copy a blob out to *dest* (a verified copy — the CAS path is never
        handed to kernels directly, so a cell cannot corrupt the store)."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(self._path_for(sha256), dest)
        return dest


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


_store: Optional[BlobStore] = None
_store_lock = threading.Lock()


def get_blob_store() -> BlobStore:
    global _store
    with _store_lock:
        if _store is None:
            from opencodon_constants import get_hermes_home

            _store = BlobStore(Path(get_hermes_home()) / "science" / "blobs")
        return _store


def reset_blob_store() -> None:
    """Test hook: drop the singleton."""
    global _store
    with _store_lock:
        _store = None
