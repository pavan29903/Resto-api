"""Object storage for dish photographs and logos.

Dish images used to be written next to the app on local disk. A deployed
container gets a fresh filesystem on every restart, so anything written there
disappears — along with every restaurant's menu photos. Images now go to object
storage and menus keep only the public URL.

Two backends, chosen by STORAGE_PROVIDER:

  supabase  one account for database, auth and files. Simplest, and what this
            started on — but the free tier allows 5 GB of egress a month, and
            every diner who scans a code downloads the photographs again.

  r2        Cloudflare R2. Charges nothing for egress, ever, which removes the
            ceiling rather than raising it. 10 GB of storage free is roughly
            eleven thousand restaurants at WebP sizes.

Both present the same three methods, and the rest of the system only ever
stores the URL that comes back — so switching is an environment variable, not
a migration. Images already written to the old backend keep working, because
their URLs are absolute and still point there.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import Protocol

import httpx

from app.core.config import Settings

# Dish photographs never change once written — replacing one writes a new
# cache-busting query string — so they can be cached for a year. Without this
# every scan re-downloads images the browser already had.
CACHE_CONTROL = "public, max-age=31536000, immutable"


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    def ensure_bucket(self) -> None: ...
    def upload(self, path: str, data: bytes, *, content_type: str | None = None) -> str: ...
    def public_url(self, path: str) -> str: ...
    def remove_prefix(self, prefix: str) -> None: ...


def storage_for(settings: Settings) -> Storage:
    provider = (settings.storage_provider or "supabase").strip().lower()
    if provider == "r2":
        return R2Client.from_settings(settings)
    if provider == "supabase":
        return StorageClient.from_settings(settings)
    raise StorageError(f"Unknown STORAGE_PROVIDER '{provider}'. Use 'supabase' or 'r2'.")


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
                    "cache-control": CACHE_CONTROL,
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


@dataclass(frozen=True)
class R2Client:
    """Cloudflare R2, over its S3-compatible API.

    The reason to be here is one line of R2's pricing: egress is free. Storage
    is billed, bandwidth is not, and bandwidth is what scales with the number
    of people eating in the restaurants you serve.

    Reads and writes use different hosts. Writes go to the S3 endpoint with
    credentials; diners read from a public bucket URL — the r2.dev address, or
    a custom domain — which never sees a key.
    """

    account_id: str
    access_key_id: str
    secret_access_key: str
    bucket: str
    public_base: str

    @classmethod
    def from_settings(cls, settings: Settings) -> "R2Client":
        missing = [
            name
            for name, value in (
                ("R2_ACCOUNT_ID", settings.r2_account_id),
                ("R2_ACCESS_KEY_ID", settings.r2_access_key_id),
                ("R2_SECRET_ACCESS_KEY", settings.r2_secret_access_key),
                ("R2_PUBLIC_BASE_URL", settings.r2_public_base_url),
            )
            if not value
        ]
        if missing:
            raise StorageError(
                f"STORAGE_PROVIDER=r2 needs {', '.join(missing)}. "
                "Find them in Cloudflare → R2 → Manage API tokens."
            )
        return cls(
            account_id=settings.r2_account_id,  # type: ignore[arg-type]
            access_key_id=settings.r2_access_key_id,  # type: ignore[arg-type]
            secret_access_key=settings.r2_secret_access_key,  # type: ignore[arg-type]
            bucket=settings.r2_bucket,
            public_base=settings.r2_public_base_url.rstrip("/"),  # type: ignore[union-attr]
        )

    def _client(self):
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise StorageError(
                "STORAGE_PROVIDER=r2 but boto3 is not installed. "
                "Run: uv sync --extra r2"
            ) from exc

        return boto3.client(
            "s3",
            endpoint_url=f"https://{self.account_id}.r2.cloudflarestorage.com",
            aws_access_key_id=self.access_key_id,
            aws_secret_access_key=self.secret_access_key,
            # R2 ignores regions but the SDK insists on one being set.
            region_name="auto",
            config=Config(signature_version="s3v4", retries={"max_attempts": 3}),
        )

    def ensure_bucket(self) -> None:
        """Create the bucket if it isn't there. Safe to call repeatedly.

        No public-read flag to set, unlike Supabase: an R2 bucket is made
        readable by attaching the r2.dev subdomain or a custom domain in the
        Cloudflare dashboard, which is a one-off setup step rather than
        something this code can do.
        """
        client = self._client()
        try:
            client.head_bucket(Bucket=self.bucket)
            return
        except Exception:
            pass  # missing, or no permission to check — try to create

        try:
            client.create_bucket(Bucket=self.bucket)
        except Exception as exc:
            # Another worker won the race, which is success.
            if "BucketAlreadyOwnedByYou" in str(exc) or "BucketAlreadyExists" in str(exc):
                return
            raise StorageError(f"Could not create R2 bucket '{self.bucket}': {exc}") from exc

    def upload(self, path: str, data: bytes, *, content_type: str | None = None) -> str:
        ctype = content_type or mimetypes.guess_type(path)[0] or "image/webp"
        try:
            self._client().put_object(
                Bucket=self.bucket,
                Key=path,
                Body=data,
                ContentType=ctype,
                CacheControl=CACHE_CONTROL,
            )
        except Exception as exc:
            raise StorageError(f"Upload of {path} failed: {exc}") from exc
        return self.public_url(path)

    def public_url(self, path: str) -> str:
        return f"{self.public_base}/{path}"

    def remove_prefix(self, prefix: str) -> None:
        client = self._client()
        try:
            listing = client.list_objects_v2(Bucket=self.bucket, Prefix=prefix)
        except Exception:
            return  # deletion is housekeeping; never fail a request over it

        keys = [{"Key": obj["Key"]} for obj in listing.get("Contents", [])]
        if not keys:
            return
        try:
            client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        except Exception as exc:
            print(f"  ! could not delete {prefix} from R2: {exc}")
