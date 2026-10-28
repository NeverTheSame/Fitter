"""Domain types shared by the API and the worker."""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

STEPS = ("acquire_garment", "check_inputs", "generate", "check_output", "persist")

_DATA_URL_RE = re.compile(r"^data:image/(png|jpeg|webp);base64,[A-Za-z0-9+/=]+$")
_HTTP_URL_RE = re.compile(r"^https?://[^\s]+$")


class ValidationError(ValueError):
    """Bad client input. The API turns it into a 400."""


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


class Category(str, Enum):
    TOPS = "tops"
    BOTTOMS = "bottoms"
    ONE_PIECES = "one-pieces"
    AUTO = "auto"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def check_image_ref(value: Any, field_name: str, max_bytes: int) -> str:
    """Accept a base64 data URL (upload) or an http(s) URL (product image)."""
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field_name} is required")
    if _HTTP_URL_RE.match(value):
        return value
    if not _DATA_URL_RE.match(value):
        raise ValidationError(f"{field_name} must be a png/jpeg/webp data URL or an http(s) URL")
    # base64 inflates by 4/3; compare the decoded size, roughly.
    payload = value.split(",", 1)[1]
    if len(payload) * 3 // 4 > max_bytes:
        raise ValidationError(f"{field_name} is larger than {max_bytes} bytes")
    return value


@dataclass
class Garment:
    image: str
    category: Category = Category.AUTO
    title: Optional[str] = None
    source_url: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "image": self.image,
            "category": self.category.value,
            "title": self.title,
            "source_url": self.source_url,
        }


@dataclass
class CostEvent:
    job_id: str
    step: str
    cost_usd: float
    engine: Optional[str] = None
    # True when the charge is the engine's guess (a render we stopped waiting
    # for that the vendor probably bills), not a known price for a delivered render.
    estimated: bool = False
    at: datetime = field(default_factory=utcnow)


@dataclass
class TryOnJob:
    user_id: str
    selfie: str
    garment: Garment
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: JobStatus = JobStatus.QUEUED
    step: Optional[str] = None
    cost_usd: float = 0.0
    error: Optional[str] = None  # short, user-facing
    error_detail: Optional[str] = None  # the step and exception, for logs and support; not public
    refunded: bool = False  # the credit taken for this job has been given back (at most once)
    render: Optional[str] = None
    created_at: datetime = field(default_factory=utcnow)
    updated_at: datetime = field(default_factory=utcnow)

    def public(self) -> dict:
        """What GET /api/tryons/{id} returns. Input images are left out."""
        return {
            "id": self.id,
            "status": self.status.value,
            "step": self.step,
            "cost_usd": round(self.cost_usd, 4),
            "error": self.error,
            "render": self.render,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


def parse_tryon_request(body: Any, max_image_bytes: int) -> tuple[str, Garment]:
    """Validate a POST /api/tryons body into (selfie, garment)."""
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")
    selfie = check_image_ref(body.get("selfie"), "selfie", max_image_bytes)
    raw = body.get("garment")
    # The web client sends the garment as a bare image string.
    if isinstance(raw, str):
        raw = {"image": raw}
    if not isinstance(raw, dict):
        raise ValidationError("garment is required")
    image = check_image_ref(raw.get("image"), "garment.image", max_image_bytes)
    try:
        category = Category(raw.get("category", Category.AUTO.value))
    except ValueError:
        raise ValidationError(f"garment.category must be one of {[c.value for c in Category]}")
    return selfie, Garment(
        image=image,
        category=category,
        title=raw.get("title"),
        source_url=raw.get("source_url"),
    )
