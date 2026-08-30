"""Supabase Storage.

Dish images used to be written next to the app on local disk. A deployed
container gets a fresh filesystem on every restart, so anything written there
disappears — along with every restaurant's menu photos. Images now go to
object storage and menus keep only the public URL.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass

import httpx

from app.core.config import Settings


class StorageError(RuntimeError):
    pass


@dataclass(frozen=True)
class StorageClient:
    base_url: str
    service_key: str
    bucket: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "StorageClient":
        if not settings.supabase_url or not settings.supabase_service_key:
            raise StorageError(
                "SUPABASE_URL and SUPABASE_SERVICE_KEY must be set to store images."
            )
        return cls(
            base_url=settings.supabase_url.rstrip("/"),
            service_key=settings.supabase_service_key,
            bucket=settings.supabase_storage_bucket,
        )

    @property
    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.service_key}",
            "apikey": self.service_key,
        }

    def ensure_bucket(self) -> None:
        """Create the images bucket if it isn't there. Safe to call repeatedly.

        The bucket is public-read: a diner's phone loads these directly, and
        signing every dish photo would add latency for no benefit — menus are
        public information by design.
        """
        with httpx.Client(timeout=30) as client:
            existing = client.get(
                f"{self.base_url}/storage/v1/bucket/{self.bucket}",
                headers=self._headers,
            )
            if existing.status_code == 200:
                return

            created = client.post(
                f"{self.base_url}/storage/v1/bucket",
                headers={**self._headers, "Content-Type": "application/json"},
                json={
                    "id": self.bucket,
                    "name": self.bucket,
                    "public": True,
                    "file_size_limit": 5 * 1024 * 1024,
                    "allowed_mime_types": ["image/png", "image/jpeg", "image/webp"],
                },
            )
            # 409 means another worker created it first — that's success too.
            if created.status_code not in (200, 201, 409):
                raise StorageError(
                    f"Could not create bucket '{self.bucket}': "
                    f"{created.status_code} {created.text[:200]}"
                )

    def upload(self, path: str, data: bytes, *, content_type: str | None = None) -> str:
        """Upload bytes and return the public URL. Overwrites an existing object."""
        ctype = content_type or mimetypes.guess_type(path)[0] or "image/png"
        with httpx.Client(timeout=60) as client:
            res = client.post(
                f"{self.base_url}/storage/v1/object/{self.bucket}/{path}",
                headers={
                    **self._headers,
                    "Content-Type": ctype,
                    # Replace rather than fail when a menu is re-published.
                    "x-upsert": "true",
                },
                content=data,
            )
        if res.status_code not in (200, 201):
            raise StorageError(
                f"Upload of {path} failed: {res.status_code} {res.text[:200]}"
            )
        return self.public_url(path)

    def public_url(self, path: str) -> str:
        return f"{self.base_url}/storage/v1/object/public/{self.bucket}/{path}"

    def remove_prefix(self, prefix: str) -> None:
        """Delete every object under a prefix — used when a menu is deleted."""
        with httpx.Client(timeout=60) as client:
            listing = client.post(
                f"{self.base_url}/storage/v1/object/list/{self.bucket}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefix": prefix, "limit": 1000},
            )
            if listing.status_code != 200:
                return
            names = [f"{prefix}/{obj['name']}" for obj in listing.json()]
            if not names:
                return
            client.request(
                "DELETE",
                f"{self.base_url}/storage/v1/object/{self.bucket}",
                headers={**self._headers, "Content-Type": "application/json"},
                json={"prefixes": names},
            )
