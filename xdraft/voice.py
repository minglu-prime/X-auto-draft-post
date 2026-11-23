"""The author's real writing (and writers they admire), used as examples so drafts sound like them.

voice/
  mine/         your own writing: x-archive.json (from `xdraft style-import`) and any .md/.txt you paste
  references/   writers you like: pasted text or pages saved with `xdraft voice add-url`

Plain text files may hold several pieces separated by a line of `---`.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import httpx

from xdraft.config import Config

_CJK_RE = re.compile(r"[一-鿿]")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9+#.\-]{1,}")
_SPLIT_RE = re.compile(r"(?m)^\s*-{3,}\s*$")


@dataclass
class Sample:
    text: str
    lang: str
    likes: int = 0
    source: str = ""


def detect_lang(text: str) -> str:
    letters = sum(1 for c in text if c.isalpha())
    return "zh" if letters and len(_CJK_RE.findall(text)) / letters > 0.2 else "en"


def _tokens(text: str) -> set[str]:
    words = {w.lower() for w in _WORD_RE.findall(text)}
    cjk = "".join(_CJK_RE.findall(text))
    return words | {cjk[i : i + 2] for i in range(len(cjk) - 1)}


def chunk_text(text: str, max_chars: int) -> list[str]:
    """Split pasted text into pieces: on `---` lines, then long pieces on paragraph boundaries."""
    pieces = []
    for part in _SPLIT_RE.split(text):
        part = part.strip()
        if not part:
            continue
        if len(part) <= max_chars:
            pieces.append(part)
            continue
        buf = ""
        for para in re.split(r"\n\s*\n", part):
            para = para.strip()
            if buf and len(buf) + len(para) > max_chars:
                pieces.append(buf)
                buf = ""
            buf = f"{buf}\n\n{para}".strip() if buf else para[:max_chars]
        if buf:
            pieces.append(buf)
    return [p for p in pieces if len(p.strip()) >= 6]  # short Chinese posts are fine


def voice_dirs(cfg: Config) -> tuple[Path, Path]:
    root = cfg.resolve_path(cfg.voice.dir)
    return root / "mine", root / "references"


def _load_dir(folder: Path, max_chars: int, kind: str) -> list[Sample]:
    samples: list[Sample] = []
    if not folder.is_dir():
        return samples
    for path in sorted(folder.iterdir()):
        if path.name.startswith((".", "_")):
            continue
        if path.suffix.lower() == ".json":
            try:
                rows = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for r in rows if isinstance(rows, list) else []:
                text = str(r.get("text", "")).strip()
                if text:
                    likes = int(r.get("likes", 0) or 0) + 3 * int(r.get("retweets", 0) or 0)
                    samples.append(Sample(text, detect_lang(text), likes, f"{kind}:{path.name}"))
        elif path.suffix.lower() in (".md", ".txt"):
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            text = re.sub(r"^<!--.*?-->\s*", "", text, flags=re.S)  # header comment from add-url
            for piece in chunk_text(text, max_chars):
                samples.append(Sample(piece, detect_lang(piece), 0, f"{kind}:{path.name}"))
    return samples


def load_own(cfg: Config) -> list[Sample]:
    return _load_dir(voice_dirs(cfg)[0], cfg.voice.excerpt_chars, "mine")


def load_references(cfg: Config) -> list[Sample]:
    return _load_dir(voice_dirs(cfg)[1], cfg.voice.excerpt_chars, "ref")


def pick_examples(samples: list[Sample], topic_text: str, lang: str, n: int) -> list[Sample]:
    """Same language first; half by engagement, half by overlap with the topic; deterministic."""
    if n <= 0 or not samples:
        return []
    same = [s for s in samples if s.lang == lang]
    pool = same if len(same) >= max(1, n // 2) else samples
    topic = _tokens(topic_text)

    def overlap(s: Sample) -> float:
        return len(topic & _tokens(s.text)) / math.sqrt(1 + len(_tokens(s.text)))

    picked: list[Sample] = []
    for s in sorted(pool, key=lambda s: (-s.likes, len(s.text)))[: n // 2]:
        picked.append(s)
    for s in sorted(pool, key=lambda s: (-overlap(s), -s.likes, len(s.text))):
        if len(picked) >= n:
            break
        if s not in picked:
            picked.append(s)
    return picked


def format_own(samples: list[Sample]) -> str:
    if not samples:
        return ""
    body = "\n\n".join(f"<example>\n{s.text}\n</example>" for s in samples)
    return ("<my_writing>\nReal posts by the author. Imitate their sentence length, word choice, punctuation, "
            "rhythm and tone. Never copy their content or phrases.\n" + body + "\n</my_writing>")


def format_references(samples: list[Sample]) -> str:
    if not samples:
        return ""
    body = "\n\n".join(f"<excerpt>\n{s.text}\n</excerpt>" for s in samples)
    return ("<admired_writers>\nWriting the author likes. Borrow structure and rhythm only, never words or claims.\n"
            + body + "\n</admired_writers>")


def voice_block(cfg: Config, topic_text: str, lang: str) -> str:
    own = pick_examples(load_own(cfg), topic_text, lang, cfg.voice.own_examples)
    refs = pick_examples(load_references(cfg), topic_text, lang, cfg.voice.reference_excerpts)
    return "\n\n".join(b for b in (format_own(own), format_references(refs)) if b)


# ---- adding samples --------------------------------------------------------------

def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]+", "-", text.lower()).strip("-")[:40] or "text"


def save_text(cfg: Config, kind: str, text: str, title: str = "") -> Path:
    """kind: 'mine' or 'reference'. Returns the file written."""
    mine, refs = voice_dirs(cfg)
    folder = mine if kind == "mine" else refs
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{datetime.now():%Y%m%d-%H%M%S}-{_slug(title or text[:30])}.md"
    path.write_text(text.strip() + "\n", encoding="utf-8")
    return path


def save_archive(cfg: Config, tweets: list[dict]) -> Path:
    mine, _ = voice_dirs(cfg)
    mine.mkdir(parents=True, exist_ok=True)
    path = mine / "x-archive.json"
    rows = [{k: t.get(k) for k in ("text", "likes", "retweets", "created_at")} for t in tweets]
    path.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    return path


def fetch_article(url: str, client: httpx.Client | None = None, max_chars: int = 8000) -> tuple[str, str]:
    """Download a page and extract its main text. Returns (title, text)."""
    import trafilatura

    own = client is None
    client = client or httpx.Client(timeout=30.0, follow_redirects=True,
                                    headers={"User-Agent": "xdraft/0.1 (personal reader)"})
    try:
        resp = client.get(url)
        resp.raise_for_status()
        html = resp.text
    finally:
        if own:
            client.close()
    text = trafilatura.extract(html, include_comments=False, include_tables=False) or ""
    meta = trafilatura.extract_metadata(html)
    title = (meta.title if meta and meta.title else "") or url
    if not text.strip():
        raise ValueError(f"No readable text found at {url}")
    return title, text.strip()[:max_chars]


def save_url(cfg: Config, url: str, client: httpx.Client | None = None) -> Path:
    title, text = fetch_article(url, client)
    mine, refs = voice_dirs(cfg)
    refs.mkdir(parents=True, exist_ok=True)
    path = refs / f"{_slug(title)}.md"
    path.write_text(f"<!-- {title} | {url} -->\n{text}\n", encoding="utf-8")
    return path


def summary(cfg: Config) -> dict:
    own, refs = load_own(cfg), load_references(cfg)
    return {
        "own": len(own), "own_zh": sum(s.lang == "zh" for s in own), "own_en": sum(s.lang == "en" for s in own),
        "references": len(refs), "dir": str(cfg.resolve_path(cfg.voice.dir)),
    }
