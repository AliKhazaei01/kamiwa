"""Rotating pool of licensed decorative photos (never user listing photos).

The pool lives in MEDIA_ROOT/site-pool/ with a manifest.json that records each file's
source and licence. Without a pool the bundled static images are used.
"""
import json
import random
from pathlib import Path

from django.conf import settings
from django.templatetags.static import static

POOL_DIR = "site-pool"
FALLBACK = ("kamiwa/images/truck-white.png", "kamiwa/images/truck-red.png", "kamiwa/images/trailer.png")
_cache = {"mtime": None, "items": []}


def pool():
    manifest = Path(settings.MEDIA_ROOT) / POOL_DIR / "manifest.json"
    try:
        mtime = manifest.stat().st_mtime
    except OSError:
        return []
    if _cache["mtime"] != mtime:
        try:
            items = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            items = []
        _cache.update(mtime=mtime, items=[item for item in items if isinstance(item, dict) and item.get("file")])
    return _cache["items"]


def _url(item):
    return f"{settings.MEDIA_URL}{POOL_DIR}/{item['file']}"


def random_image():
    items = pool()
    return _url(random.choice(items)) if items else static(random.choice(FALLBACK))


def listing_placeholder(listing_id):
    """Stable per listing, so a card does not change picture on every page view."""
    items = pool()
    if items:
        return _url(items[(listing_id or 0) % len(items)])
    return static(FALLBACK[(listing_id or 0) % len(FALLBACK)])
