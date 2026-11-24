"""Images attached to draft tweets: stored under data/media/<draft_id>/, uploaded to X at publish time."""

from __future__ import annotations

import re
import secrets
from pathlib import Path

from xdraft.config import Config
from xdraft.models import Draft

MAX_BYTES = 5 * 1024 * 1024  # X limit for tweet_image
MAX_PER_TWEET = 4            # X: up to 4 photos per post
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
_SIGNATURES = (  # file type from the bytes, not the client's filename or content type
    (b"\x89PNG\r\n\x1a\n", "png", "image/png"),
    (b"\xff\xd8\xff", "jpg", "image/jpeg"),
    (b"GIF87a", "gif", "image/gif"),
    (b"GIF89a", "gif", "image/gif"),
)


def image_type(data: bytes) -> tuple[str, str] | None:
    """(extension, mime) for PNG/JPG/GIF/WEBP bytes, else None."""
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp", "image/webp"
    for sig, ext, mime in _SIGNATURES:
        if data.startswith(sig):
            return ext, mime
    return None


def mime_for(name: str) -> str:
    ext = name.rsplit(".", 1)[-1].lower()
    return {"png": "image/png", "jpg": "image/jpeg", "gif": "image/gif", "webp": "image/webp"}.get(ext, "")


def media_dir(cfg: Config, draft_id: int) -> Path:
    return cfg.resolve_path("data/media") / str(draft_id)


def images_for(draft: Draft) -> list[list[str]]:
    """Image names per tweet, aligned with draft.tweets (missing entries are empty)."""
    imgs = list(draft.images or [])
    return [list(imgs[i]) if i < len(imgs) else [] for i in range(len(draft.tweets))]


def image_path(cfg: Config, draft: Draft, name: str) -> Path | None:
    """Path of an image that belongs to this draft; None for unknown or unsafe names."""
    if not NAME_RE.match(name) or not any(name in names for names in draft.images or []):
        return None
    return media_dir(cfg, draft.id) / name


def save_image(cfg: Config, draft: Draft, index: int, data: bytes) -> str:
    """Attach image bytes to tweet `index` (0-based). Caller commits the draft. Returns the stored name."""
    if not 0 <= index < len(draft.tweets):
        raise ValueError(f"这条草稿只有 {len(draft.tweets)} 条推文")
    kind = image_type(data)
    if not kind:
        raise ValueError("只支持 PNG、JPG、GIF、WEBP 图片")
    if len(data) > MAX_BYTES:
        raise ValueError("图片超过 5 MB（X 的上限）")
    images = images_for(draft)
    if len(images[index]) >= MAX_PER_TWEET:
        raise ValueError(f"每条推文最多 {MAX_PER_TWEET} 张图")
    name = f"{index + 1}-{secrets.token_hex(4)}.{kind[0]}"
    folder = media_dir(cfg, draft.id)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_bytes(data)
    images[index].append(name)
    draft.images = images
    return name


def remove_image(cfg: Config, draft: Draft, index: int, name: str) -> None:
    """Detach an image from tweet `index`. The file is kept so the removal can be undone. Caller commits."""
    images = images_for(draft)
    if not 0 <= index < len(images) or name not in images[index]:
        raise ValueError("找不到这张配图")
    images[index].remove(name)
    draft.images = images


def restore_image(cfg: Config, draft: Draft, index: int, name: str) -> None:
    """Undo remove_image: re-attach a file that is still in this draft's media folder. Caller commits."""
    images = images_for(draft)
    if not 0 <= index < len(images) or not NAME_RE.match(name) or not (media_dir(cfg, draft.id) / name).is_file():
        raise ValueError("这张配图已经找不到了")
    if name not in images[index]:
        if len(images[index]) >= MAX_PER_TWEET:
            raise ValueError(f"每条推文最多 {MAX_PER_TWEET} 张图")
        images[index].append(name)
    draft.images = images
