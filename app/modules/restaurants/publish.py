"""Publishing a menu.

Finds a photo for every dish, uploads it to object storage, records the URL on
the dish, and marks the restaurant live. Runs as a background job because
finding 40 photos takes long enough that no browser should wait on it.

Nothing here writes to local disk: a deployed container's filesystem is
temporary, so anything stored there vanishes on the next restart.
"""

from __future__ import annotations

import tempfile
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.core.config import Settings, get_settings
from app.core.db import get_sessionmaker
from app.core.storage import StorageClient
from app.models import MenuItem, MenuSection, Restaurant
from app.modules.images.service import generate_image


@dataclass
class PublishJob:
    id: str
    restaurant_id: uuid.UUID
    status: str = "running"  # running | done | error
    step: str = "starting"
    done: int = 0
    total: int = 0
    error: str | None = None
    menu_url: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "job_id": self.id,
            "status": self.status,
            "step": self.step,
            "done": self.done,
            "total": self.total,
            "error": self.error,
            "menu_url": self.menu_url,
            **self.extra,
        }


# In-memory job registry. Fine while one process serves the API; Redis takes
# this over when we scale past a single container.
JOBS: dict[str, PublishJob] = {}


async def run_publish(
    job_id: str,
    restaurant_id: uuid.UUID,
    *,
    generate_images: bool,
    max_images: int,
    replace_existing: bool = False,
) -> None:
    job = JOBS[job_id]
    settings: Settings = get_settings()
    sessionmaker = get_sessionmaker()

    try:
        async with sessionmaker() as session:
            restaurant = (
                await session.execute(
                    select(Restaurant)
                    .where(Restaurant.id == restaurant_id)
                    .options(selectinload(Restaurant.sections).selectinload(MenuSection.items))
                )
            ).scalar_one_or_none()

            if restaurant is None:
                job.status, job.error = "error", "That menu no longer exists."
                return

            # A dish the owner already personalised is never overwritten.
            targets: list[MenuItem] = []
            for section in restaurant.sections:
                for item in section.items:
                    already = bool(item.image_url)
                    if already and not (replace_existing and item.image_source != "owner"):
                        continue
                    targets.append(item)

            if generate_images and max_images:
                targets = targets[:max_images]
            if not generate_images:
                targets = []

            job.total = len(targets)

            if targets:
                job.step = "finding dish photos"
                storage = StorageClient.from_settings(settings)
                storage.ensure_bucket()

                with tempfile.TemporaryDirectory(prefix="menusnap_") as tmp:
                    tmp_dir = Path(tmp)
                    for item in targets:
                        local = tmp_dir / f"{item.id}.png"
                        try:
                            generate_image(item.name, item.description or "", settings, local)
                            url = storage.upload(
                                f"{restaurant.id}/{item.id}.png", local.read_bytes()
                            )
                            item.image_url = url
                            item.image_source = settings.image_provider
                        except Exception as exc:  # one bad dish must not stop the menu
                            print(f"  ! photo failed for {item.name}: {exc}")
                        finally:
                            job.done += 1
                            local.unlink(missing_ok=True)

            job.step = "publishing"
            restaurant.is_published = True
            restaurant.published_at = datetime.now(timezone.utc)
            await session.commit()

            job.menu_url = settings.menu_url_for(restaurant.slug)
            job.extra = {"slug": restaurant.slug}
            job.step = "complete"
            job.status = "done"

    except Exception as exc:
        traceback.print_exc()
        job.status = "error"
        job.error = str(exc)
