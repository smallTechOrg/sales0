"""Where deck PDFs (outreach attachments) live: a tiny content-addressed object store with two backends.

* ``local`` - ``<data_dir>/<key>`` on disk (local mode and every test).
* ``gcs``   - Google Cloud Storage bucket ``SALES0_ATTACHMENTS_BUCKET`` (hosted). Application Default Credentials (the
  Cloud Run service account); no key file anywhere.

Keys are ``decks/<sha256>.pdf``, so each file is stored once whatever references it. The database keeps only metadata
(``decks``: sha256, size, filename, backend, key). Hosted mode with no bucket is refused loudly
(``attachments_not_configured``): the hosted ``/tmp`` is in-memory and wiped on restart, so it must never be used.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from sales0.config import Settings, get_settings
from sales0.errors import DomainError

BACKENDS = ("local", "gcs")


def deck_key(sha256: str) -> str:
    return f"decks/{sha256}.pdf"


class AttachmentStore(Protocol):
    name: str

    def put(self, key: str, data: bytes) -> None: ...
    def get(self, key: str) -> bytes | None: ...  # None = no such object
    def exists(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...


def _safe(key: str) -> str:
    parts = Path(key).parts
    if not key or key.startswith("/") or ".." in parts or "\\" in key:
        raise DomainError("bad_request", "Invalid attachment key.", 400)
    return key


class LocalStore:
    name = "local"

    def __init__(self, root: Path) -> None:
        self.root = root

    def _path(self, key: str) -> Path:
        return self.root / _safe(key)

    def put(self, key: str, data: bytes) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, target)

    def get(self, key: str) -> bytes | None:
        try:
            return self._path(key).read_bytes()
        except OSError:
            return None

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class GcsStore:
    """``client`` is a ``google.cloud.storage.Client`` (or a test double with ``bucket(name).blob(key)``)."""

    name = "gcs"

    def __init__(self, bucket: str, client: Any | None = None) -> None:
        if client is None:
            from google.cloud import storage as gcs  # imported lazily: local mode never needs it

            client = gcs.Client()  # Application Default Credentials
        self._bucket = client.bucket(bucket)
        self.bucket_name = bucket

    def put(self, key: str, data: bytes) -> None:
        self._bucket.blob(_safe(key)).upload_from_string(data, content_type="application/pdf")

    def get(self, key: str) -> bytes | None:
        from google.api_core.exceptions import NotFound

        try:
            return bytes(self._bucket.blob(_safe(key)).download_as_bytes())
        except NotFound:
            return None

    def exists(self, key: str) -> bool:
        return bool(self._bucket.blob(_safe(key)).exists())

    def delete(self, key: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._bucket.blob(_safe(key)).delete()
        except NotFound:
            pass


_gcs_client_factory: Callable[[], Any] | None = None
_cache: dict[tuple[str, str], AttachmentStore] = {}


def set_gcs_client_factory(factory: Callable[[], Any] | None) -> None:
    """Test seam: a fake GCS client (tests never call Google)."""
    global _gcs_client_factory
    _gcs_client_factory = factory
    _cache.clear()


def reset_storage() -> None:
    set_gcs_client_factory(None)


def _not_configured() -> DomainError:
    return DomainError(
        "attachments_not_configured",
        "Attachment storage is not configured: set SALES0_ATTACHMENTS_BUCKET (hosted decks live in Google Cloud Storage).",
        409,  # a configuration error, not a transient one: clients must not retry it
    )


def current_backend(settings: Settings | None = None) -> str:
    """The backend new uploads go to: ``gcs`` when a bucket is set; ``local`` otherwise, except in hosted mode (error)."""
    settings = settings or get_settings()
    if settings.attachments_bucket:
        return "gcs"
    if settings.mode == "hosted":
        raise _not_configured()
    return "local"


def store_for(backend: str, settings: Settings | None = None) -> AttachmentStore:
    settings = settings or get_settings()
    if backend == "gcs":
        bucket = settings.attachments_bucket
        if not bucket:
            raise _not_configured()
        cache_key = ("gcs", bucket)
        if cache_key not in _cache:
            _cache[cache_key] = GcsStore(bucket, _gcs_client_factory() if _gcs_client_factory else None)
        return _cache[cache_key]
    if backend == "local":
        if settings.mode == "hosted":  # the hosted /tmp is wiped on restart: never store or trust files there
            raise _not_configured()
        return LocalStore(settings.data_path)
    raise DomainError("bad_request", f"Unknown attachment storage {backend!r}.", 400)


def current_store(settings: Settings | None = None) -> AttachmentStore:
    return store_for(current_backend(settings), settings)


__all__ = [
    "BACKENDS", "AttachmentStore", "GcsStore", "LocalStore", "current_backend", "current_store", "deck_key",
    "reset_storage", "set_gcs_client_factory", "store_for",
]
