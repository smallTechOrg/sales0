"""An in-memory stand-in for ``google.cloud.storage.Client`` (only what ``GcsStore`` uses). Tests never call Google."""

from __future__ import annotations

from google.api_core.exceptions import NotFound


class FakeBlob:
    def __init__(self, bucket: FakeBucket, key: str) -> None:
        self.bucket, self.key = bucket, key

    def upload_from_string(self, data: bytes, content_type: str | None = None) -> None:
        self.bucket.objects[self.key] = bytes(data)
        self.bucket.client.calls.append(("put", self.bucket.name, self.key))

    def download_as_bytes(self) -> bytes:
        self.bucket.client.calls.append(("get", self.bucket.name, self.key))
        if self.key not in self.bucket.objects:
            raise NotFound(f"no such object: {self.key}")
        return self.bucket.objects[self.key]

    def exists(self) -> bool:
        return self.key in self.bucket.objects

    def delete(self) -> None:
        if self.key not in self.bucket.objects:
            raise NotFound(f"no such object: {self.key}")
        del self.bucket.objects[self.key]


class FakeBucket:
    def __init__(self, client: FakeGcsClient, name: str) -> None:
        self.client, self.name = client, name
        self.objects: dict[str, bytes] = {}

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(self, key)


class FakeGcsClient:
    def __init__(self) -> None:
        self.buckets: dict[str, FakeBucket] = {}
        self.calls: list[tuple[str, str, str]] = []

    def bucket(self, name: str) -> FakeBucket:
        return self.buckets.setdefault(name, FakeBucket(self, name))
