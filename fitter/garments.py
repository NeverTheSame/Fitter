"""Garment acquisition from a product URL.

Stub: returns a placeholder image seeded by the URL. The plan's order
(Shopify JSON -> JSON-LD / og:image -> Jina Reader -> Firecrawl -> screenshot)
is not implemented.
"""
from __future__ import annotations

from urllib.parse import quote, urlparse

from .schemas import Category, Garment, ValidationError


def garment_from_url(url: object) -> Garment:
    if not isinstance(url, str) or not url.strip():
        raise ValidationError("url is required")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValidationError("url must be an http(s) URL")
    return Garment(
        image=f"https://picsum.photos/seed/{quote(url, safe='')}/300/400",
        category=Category.TOPS,
        title="Placeholder garment",
        source_url=url,
    )
