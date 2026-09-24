#!/usr/bin/env python3
"""
Mirror a public Telegram channel into Quarto blog posts.

Reads the channel's public web preview (https://t.me/s/<channel>) and writes
one post per Telegram message to posts/tg-<id>/index.qmd, with photos
downloaded next to it. Safe to run repeatedly:
  * new posts are added,
  * edited posts are updated,
  * posts deleted from the channel are removed from the blog,
  * unchanged posts are left untouched (so git sees no diff).

Hand-written posts in posts/ are never touched (only tg-* folders are).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup, NavigableString, Tag

CHANNEL = os.environ.get("TG_CHANNEL", "iqtisodchiroq")
ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "posts"
DIR_PREFIX = "tg-"
MAX_PAGES = int(os.environ.get("TG_MAX_PAGES", "200"))
TITLE_MAX = 90
DESC_MAX = 200
CATEGORY = "Telegram"

SIGNATURE_RE = re.compile(r"@" + re.escape(CHANNEL) + r"\b", re.I)
POST_LINK_RE = re.compile(r"t\.me/(?:s/)?" + re.escape(CHANNEL) + r"/(\d+)", re.I)
BG_URL_RE = re.compile(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)")
OTHER_MEDIA = (
    ".tgme_widget_message_document, .tgme_widget_message_voice, "
    ".tgme_widget_message_poll, .tgme_widget_message_sticker_wrap, "
    ".tgme_widget_message_roundvideo_player, .tgme_widget_message_location_wrap"
)

session = requests.Session()
session.headers["User-Agent"] = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)


@dataclass
class Post:
    id: int
    date: str
    url: str
    text_html: str = ""
    photos: list[str] = field(default_factory=list)
    video_thumbs: list[str] = field(default_factory=list)
    other_media: bool = False
    reply_to: int | None = None
    reply_text: str = ""
    forwarded_from: str = ""
    link_preview: dict | None = None


# ----------------------------------------------------------------- fetching

def http_get(url: str) -> requests.Response:
    for attempt in range(4):
        try:
            r = session.get(url, timeout=30)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == 3:
                raise
            time.sleep(3 * 2**attempt)
    raise RuntimeError("unreachable")


def fetch_page(before: int | None = None) -> str:
    url = f"https://t.me/s/{CHANNEL}"
    if before:
        url += f"?before={before}"
    return http_get(url).text


# ------------------------------------------------------------------ parsing

def bg_url(tag: Tag | None) -> str | None:
    if tag is None:
        return None
    m = BG_URL_RE.search(tag.get("style", ""))
    return urljoin("https:", m.group(1)) if m else None


def outside(tag: Tag, *classes: str) -> bool:
    """True if tag is not nested inside an element with any of these classes."""
    for parent in tag.parents:
        if any(c in (parent.get("class") or []) for c in classes):
            return False
    return True


def clean_html(el: Tag) -> str:
    for e in el.select("i.emoji, tg-emoji"):
        e.replace_with(e.get_text())
    for e in el.select("tg-spoiler, .tg-spoiler"):
        e.name, e.attrs = "span", {"class": "tg-spoiler"}
    for a in el.find_all("a"):
        href = a.get("href", "")
        if href.startswith("?"):
            href = f"https://t.me/s/{CHANNEL}{href}"
        a.attrs = {"href": urljoin("https://t.me/", href), "target": "_blank", "rel": "noopener"}
    for t in el.find_all(True):
        if t.name != "a":
            keep = {"class": t["class"]} if t.get("class") == ["tg-spoiler"] else {}
            t.attrs = keep
    return el.decode_contents().strip()


def parse_page(html: str) -> tuple[list[Post], list[int]]:
    soup = BeautifulSoup(html, "lxml")
    posts, all_ids = [], []
    for el in soup.select("div.tgme_widget_message[data-post]"):
        try:
            pid = int(el["data-post"].rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        all_ids.append(pid)
        if "service_message" in (el.get("class") or []):
            continue
        time_el = el.select_one(".tgme_widget_message_date time[datetime]")
        if time_el is None:
            continue
        p = Post(id=pid, date=time_el["datetime"], url=f"https://t.me/{CHANNEL}/{pid}")

        texts = [t for t in el.select(".tgme_widget_message_text")
                 if outside(t, "tgme_widget_message_reply", "tgme_widget_message_link_preview")]
        if texts:
            p.text_html = clean_html(texts[0])

        for a in el.select("a.tgme_widget_message_photo_wrap"):
            if (u := bg_url(a)) and outside(a, "tgme_widget_message_reply"):
                p.photos.append(u)
        for v in el.select(".tgme_widget_message_video_player"):
            p.video_thumbs.append(bg_url(v.select_one(".tgme_widget_message_video_thumb")) or "")
        p.other_media = bool(el.select_one(OTHER_MEDIA))

        if (reply := el.select_one("a.tgme_widget_message_reply")) is not None:
            if m := POST_LINK_RE.search(reply.get("href", "")):
                p.reply_to = int(m.group(1))
            rt = reply.select_one(".js-message_reply_text, .tgme_widget_message_metatext, .tgme_widget_message_text")
            p.reply_text = squash(rt.get_text(" ")) if rt else ""

        if (fwd := el.select_one(".tgme_widget_message_forwarded_from")) is not None:
            p.forwarded_from = squash(fwd.get_text(" "))

        if (lp := el.select_one("a.tgme_widget_message_link_preview")) is not None:
            get = lambda sel: squash(x.get_text(" ")) if (x := lp.select_one(sel)) else ""
            p.link_preview = {
                "href": lp.get("href", ""),
                "site": get(".link_preview_site_name"),
                "title": get(".link_preview_title"),
                "desc": get(".link_preview_description"),
            }
        posts.append(p)
    return posts, all_ids


def scrape() -> tuple[dict[int, Post], bool]:
    """Walk the preview from newest to oldest. Returns (posts, reached_start)."""
    posts: dict[int, Post] = {}
    before = None
    for _ in range(MAX_PAGES):
        page_posts, ids = parse_page(fetch_page(before))
        if not ids:
            return posts, True
        for p in page_posts:
            posts.setdefault(p.id, p)
        oldest = min(ids)
        if oldest <= 1 or (before is not None and oldest >= before):
            return posts, True
        before = oldest
        time.sleep(1)
    return posts, False


# --------------------------------------------------------------- rendering

def squash(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def shorten(s: str, limit: int) -> str:
    s = squash(s)
    if len(s) <= limit:
        return s
    cut = s[:limit].rsplit(" ", 1)[0].rstrip(",;:—-")
    return cut + "…"


def plain_text(html: str) -> str:
    soup = BeautifulSoup(f"<div>{html}</div>", "lxml")
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for blk in soup.find_all(["blockquote", "pre", "p"]):
        blk.insert_after("\n")
    return soup.get_text()


def split_title(p: Post) -> tuple[str, str]:
    """Pick a title; if it is the post's whole first line, drop that line from the body."""
    body = p.text_html
    flat = squash(SIGNATURE_RE.sub("", plain_text(body)))
    lp_title = (p.link_preview or {}).get("title", "")
    if len(flat) < 20 and lp_title:            # e.g. a bare link with a preview card
        return shorten(lp_title, TITLE_MAX), body

    if body:
        div = BeautifulSoup(f"<div>{body}</div>", "lxml").div
        lead: list = []
        for node in div.contents:
            if isinstance(node, Tag) and node.name == "br":
                break
            lead.append(node)
        has_block = any(isinstance(n, Tag) and (n.name in ("blockquote", "pre")
                        or n.find(["blockquote", "pre", "br"]) is not None) for n in lead)
        first = squash(SIGNATURE_RE.sub("", "".join(
            n.get_text() if isinstance(n, Tag) else str(n) for n in lead)))
        if first and not has_block and 16 <= len(first) <= TITLE_MAX:
            for n in lead:
                n.extract()
            while div.contents and (
                (isinstance(div.contents[0], Tag) and div.contents[0].name == "br")
                or (isinstance(div.contents[0], NavigableString) and not div.contents[0].strip())
            ):
                div.contents[0].extract()
            return first, div.decode_contents().strip()
        if first and len(first) < 16 and p.reply_to and p.reply_text:
            return shorten(p.reply_text, 70) + " (continued)", body

    if flat:
        return shorten(flat, TITLE_MAX), body
    return f"Telegram post #{p.id}", body


def ext_of(url: str) -> str:
    ext = Path(urlparse(url).path).suffix.lower()
    return ext if ext in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


def fetch_media(url: str, dest: Path) -> str:
    """Download once; return a path relative to the post, or the remote URL on failure."""
    if dest.exists() and dest.stat().st_size > 0:
        return dest.name
    try:
        dest.write_bytes(http_get(url).content)
        return dest.name
    except requests.RequestException as e:
        print(f"  warning: could not download {url}: {e}", file=sys.stderr)
        return url


def md_escape(s: str) -> str:
    """Titles/descriptions are read as Markdown; neutralise #, *, $, @ and friends."""
    return re.sub(r"([\\`*_\[\]{}#<>|$~^@])", r"\\\1", s)


def esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def post_dir(pid: int) -> Path:
    return POSTS_DIR / f"{DIR_PREFIX}{pid:04d}"


def build_post(p: Post, known_ids: set[int]) -> tuple[str, set[str]]:
    d = post_dir(p.id)
    d.mkdir(parents=True, exist_ok=True)
    title, body = split_title(p)
    desc = shorten(SIGNATURE_RE.sub("", plain_text(body)), DESC_MAX)
    if len(desc) < 20 and p.link_preview and p.link_preview["desc"]:
        desc = shorten(p.link_preview["desc"], DESC_MAX)

    parts: list[str] = ['<div class="tg-post">']
    if p.forwarded_from:
        parts.append(f'<p class="tg-meta">↪ {esc(p.forwarded_from)}</p>')
    if p.reply_to:
        href = (f"../{DIR_PREFIX}{p.reply_to:04d}/index.html" if p.reply_to in known_ids
                else f"https://t.me/{CHANNEL}/{p.reply_to}")
        label = esc(shorten(p.reply_text, 80)) or "previous post"
        parts.append(f'<p class="tg-meta">↩ Continues: <a href="{href}">{label}</a></p>')

    media_files: set[str] = set()
    thumb = None
    figures = []
    for i, url in enumerate(p.photos, 1):
        src = fetch_media(url, d / f"photo-{i}{ext_of(url)}")
        media_files.add(src)
        thumb = thumb or src
        figures.append(f'<img src="{esc(src)}" alt="" loading="lazy">')
    for i, url in enumerate(p.video_thumbs, 1):
        if url:
            src = fetch_media(url, d / f"video-{i}{ext_of(url)}")
            media_files.add(src)
            thumb = thumb or src
            inner = f'<img src="{esc(src)}" alt="Video thumbnail" loading="lazy">'
        else:
            inner = '<span class="tg-video-empty"></span>'
        figures.append(f'<a class="tg-video" href="{p.url}" target="_blank" rel="noopener" '
                       f'title="Watch on Telegram">{inner}<span class="tg-play">▶</span></a>')
    if figures:
        parts.append(f'<div class="tg-media tg-media-{min(len(figures), 3)}">' + "".join(figures) + "</div>")

    if body:
        parts.append(f'<div class="tg-text">{body}</div>')
    if lp := p.link_preview:
        parts.append(
            f'<a class="tg-link-preview" href="{esc(lp["href"])}" target="_blank" rel="noopener">'
            + (f'<span class="tg-lp-site">{esc(lp["site"])}</span>' if lp["site"] else "")
            + (f'<strong>{esc(lp["title"])}</strong>' if lp["title"] else "")
            + (f'<span class="tg-lp-desc">{esc(shorten(lp["desc"], 220))}</span>' if lp["desc"] else "")
            + "</a>")
    if p.other_media:
        parts.append('<p class="tg-meta">📎 This post includes media that can only be viewed on Telegram.</p>')
    parts.append(f'<p class="tg-source"><a href="{p.url}" target="_blank" rel="noopener">'
                 f'View on Telegram →</a></p>')
    parts.append("</div>")

    front = [
        "---",
        f"title: {json.dumps(md_escape(title), ensure_ascii=False)}",
        f"description: {json.dumps(md_escape(desc), ensure_ascii=False)}",
        f'date: "{p.date}"',
        f"categories: [{CATEGORY}]",
    ]
    if thumb:
        front.append(f"image: {json.dumps(thumb, ensure_ascii=False)}")
    front += [f"telegram-url: {p.url}", "---", ""]

    html = "\n".join(parts)
    fence = "`" * max(4, max((len(m) for m in re.findall(r"`+", html)), default=0) + 1)
    return "\n".join(front) + f"\n{fence}{{=html}}\n{html}\n{fence}\n", media_files


def write_post(p: Post, known_ids: set[int]) -> bool:
    d = post_dir(p.id)
    content, media = build_post(p, known_ids)
    # Remove media left over from an earlier version of an edited post.
    for f in d.glob("*"):
        if f.name != "index.qmd" and re.match(r"(photo|video)-\d+\.", f.name) and f.name not in media:
            f.unlink()
    target = d / "index.qmd"
    if target.exists() and target.read_text(encoding="utf-8") == content:
        return False
    target.write_text(content, encoding="utf-8")
    return True


def prune(known_ids: set[int]) -> int:
    existing = {int(d.name[len(DIR_PREFIX):]): d for d in POSTS_DIR.glob(f"{DIR_PREFIX}*")
                if d.is_dir() and d.name[len(DIR_PREFIX):].isdigit()}
    gone = [pid for pid in existing if pid not in known_ids]
    if len(gone) > max(3, len(existing) // 2):
        print(f"  refusing to delete {len(gone)} of {len(existing)} posts — looks like a "
              "scraping problem, not real deletions.", file=sys.stderr)
        return 0
    for pid in gone:
        for f in sorted(existing[pid].rglob("*"), reverse=True):
            f.unlink() if f.is_file() else f.rmdir()
        existing[pid].rmdir()
    return len(gone)


def main() -> None:
    posts, complete = scrape()
    if not posts:
        sys.exit(f"No posts found at https://t.me/s/{CHANNEL} — is the channel public, "
                 "or has Telegram changed its page layout?")
    POSTS_DIR.mkdir(exist_ok=True)
    ids = set(posts)
    changed = sum(write_post(posts[i], ids) for i in sorted(ids))
    removed = prune(ids) if complete else 0
    print(f"Scraped {len(posts)} posts; {changed} new or updated, {removed} removed.")


if __name__ == "__main__":
    main()
