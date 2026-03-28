#!/usr/bin/env python3
"""Fetch a Bluesky feed and generate a blog post digest for Zensical."""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from atproto import Client
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yml"
DOCS_DIR = ROOT / "docs"
BLOG_POSTS_DIR = DOCS_DIR / "blog" / "posts"
DATA_DIR = ROOT / "data"
DIGESTS_JSON = DATA_DIR / "digests.json"

PUBLIC_API = "https://public.api.bsky.app"
AUTH_API = "https://bsky.social"


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


# ---------------------------------------------------------------------------
# Date helpers — Monday-to-Sunday weeks
# ---------------------------------------------------------------------------

def week_range(ref_date: date | None = None) -> tuple[date, date]:
    """Return (monday, sunday) of the most recent completed Mon-Sun week.

    If *ref_date* is a Monday the "completed" week is the one that just ended
    (previous Mon-Sun).  Otherwise it is the Mon-Sun that contains the most
    recent Sunday.
    """
    if ref_date is None:
        ref_date = date.today()
    days_since_monday = ref_date.weekday()          # Mon=0 … Sun=6
    current_monday = ref_date - timedelta(days=days_since_monday)
    end = current_monday - timedelta(days=1)        # previous Sunday
    start = end - timedelta(days=6)                 # previous Monday
    return start, end


# ---------------------------------------------------------------------------

# Auth and API client using atproto
# ---------------------------------------------------------------------------

def create_atproto_client() -> Client:
    handle = os.environ.get("BLUESKY_HANDLE", "")
    password = os.environ.get("BLUESKY_APP_PASSWORD", "")
    if not handle or not password:
        print(
            "ERROR: auth_required is true but BLUESKY_HANDLE / "
            "BLUESKY_APP_PASSWORD not set.",
            file=sys.stderr,
        )
        sys.exit(1)
    client = Client()
    client.login(handle, password)
    return client


# ---------------------------------------------------------------------------
# Fetch feed
# ---------------------------------------------------------------------------

def fetch_feed(cfg: dict) -> list[dict]:
    """Return a list of feed-view post objects from the Bluesky API using atproto SDK."""
    feed_uri = cfg["feed_uri"]
    # Extract DID or handle from AT URI (e.g., 'at://did:plc:xxx/app.bsky.feed.generator/yyy')
    if feed_uri.startswith("at://"):
        repo = feed_uri.split("/")[2]
    else:
        repo = feed_uri
    auth_required = cfg.get("auth_required", False)

    posts: list[dict] = []
    cursor: str | None = None
    import httpx
    feed_uri = cfg["feed_uri"]
    # Detect algorithmic feed ("What's Hot")
    is_whats_hot = (
        "whats-hot" in feed_uri
        or feed_uri.strip() == "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
    )
    if is_whats_hot:
        posts = []
        cursor = None
        for _ in range(3):
            params = {"limit": 100}
            if cursor:
                params["cursor"] = cursor
            # Try getPopular first
            resp = httpx.get(
                "https://public.api.bsky.app/xrpc/app.bsky.feed.getPopular",
                params=params,
                timeout=30,
            )
            if resp.status_code == 501:
                # Fallback to getTimeline if not implemented
                resp = httpx.get(
                    "https://public.api.bsky.app/xrpc/app.bsky.feed.getTimeline",
                    params=params,
                    timeout=30,
                )
            if resp.status_code == 401:
                print("[ERROR] Unauthorized: The public API does not allow access to this feed.")
                return []
            resp.raise_for_status()
            data = resp.json()
            posts.extend(data.get("feed", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return posts
    elif auth_required:
        client = create_atproto_client()
        for _ in range(3):
            params = {"feed": feed_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = client.app.bsky.feed.get_feed(params)
            feed = getattr(resp, "feed", [])
            posts.extend(feed)
            cursor = getattr(resp, "cursor", None)
            if not cursor:
                break
        return posts
    else:
        for _ in range(3):
            params = {"feed": feed_uri, "limit": 100}
            if cursor:
                params["cursor"] = cursor
            resp = httpx.get(
                "https://public.api.bsky.app/xrpc/app.bsky.feed.getFeed",
                params=params,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            posts.extend(data.get("feed", []))
            cursor = data.get("cursor")
            if not cursor:
                break
        return posts


# ---------------------------------------------------------------------------
# Post extraction helpers
# ---------------------------------------------------------------------------

def extract_uri_from_facets(record: dict | None) -> str | None:
    if not record or not isinstance(record, dict):
        return None
    facets = record.get("facets")
    if not facets or not isinstance(facets, list):
        return None
    for facet in facets:
        features = facet.get("features") if isinstance(facet, dict) else None
        if not features or not isinstance(features, list):
            continue
        for feature in features:
            uri = feature.get("uri") if isinstance(feature, dict) else None
            if uri:
                return uri
    return None


def extract_uri_from_embed(post: dict) -> str | None:
    embed = post.get("embed") or {}
    external = embed.get("external") or {}
    return external.get("uri")


def post_bsky_url(post_uri: str) -> str:
    url = post_uri.replace("at://", "https://bsky.app/profile/")
    url = url.replace("app.bsky.feed.post/", "post/")
    return url


_INVISIBLE = re.compile(r"[\u2028\u2029\u000b]")
_TRUNCATED_URL = re.compile(r"https?://\S+\.\.\.+")
_BROKEN_HTML = re.compile(r"[<>]")


def clean_text(text: str) -> str:
    text = _INVISIBLE.sub(" ", text)
    text = _TRUNCATED_URL.sub("", text)
    text = _BROKEN_HTML.sub("", text)
    words = text.split()
    words = [w for w in words if not re.search(r"[.…]{3,}$", w)]
    text = " ".join(words)
    text = text.replace("#", "")
    return text.strip()


# ---------------------------------------------------------------------------
# Filter & transform
# ---------------------------------------------------------------------------

def filter_posts(
    raw: list[dict],
    cfg: dict,
    start_date: date,
    end_date: date,
) -> list[dict]:
    min_len = int(cfg.get("min_post_length", 50))

    results: list[dict] = []
    for item in raw:
        post = item.get("post", {})
        record = post.get("record", {})
        author = post.get("author", {})

        # Convert record and author to dicts if they are pydantic models
        if hasattr(record, "model_dump"):
            record = record.model_dump()
        elif hasattr(record, "dict"):
            record = record.dict()
        if hasattr(author, "model_dump"):
            author = author.model_dump()
        elif hasattr(author, "dict"):
            author = author.dict()


        # Try to robustly extract createdAt (could be 'createdAt', 'created_at', etc.)
        created = record.get("createdAt") or record.get("created_at") or record.get("timestamp")
        if not created:
            # Debug: print record structure if date missing
            print("[DEBUG] No createdAt found in record:", record)
            continue
        try:
            post_date = datetime.fromisoformat(
                created.replace("Z", "+00:00")
            ).date()
        except (ValueError, AttributeError):
            print(f"[DEBUG] Could not parse date '{created}' in record: {record}")
            continue

        if post_date < start_date or post_date > end_date:
            continue

        text = clean_text(record.get("text", ""))
        if len(text) < min_len:
            continue


        # Ensure record is a dict for extract_uri_from_facets
        article_uri = extract_uri_from_facets(record)
        if not article_uri:
            article_uri = extract_uri_from_embed(post)

        # Try to get author info from multiple possible locations
        author_handle = author.get("handle") or record.get("author") or record.get("handle") or "unknown"
        author_name = author.get("displayName") or author.get("handle") or record.get("author") or record.get("handle") or "unknown"
        author_avatar = author.get("avatar", "")

        # Debug: print author info if unknown
        if author_handle == "unknown":
            print(f"[DEBUG] Author unknown for post record: {record} | author: {author}")

        # Try to get like count from multiple possible locations
        likes = (
            post.get("likeCount")
            or record.get("likeCount")
            or record.get("likes")
            or record.get("reactions", {}).get("like")
            or 0
        )

        # Debug: print like info if 0
        if likes == 0:
            print(f"[DEBUG] Likes is 0 for post record: {record} | post: {post}")

        results.append(
            {
                "author_handle": author_handle,
                "author_name": author_name,
                "author_avatar": author_avatar,
                "text": text,
                "date": post_date.isoformat(),
                "likes": likes,
                "article_uri": article_uri,
                "bsky_url": post_bsky_url(post.get("uri", "")),
            }
        )

    results.sort(key=lambda p: (p["date"], p["likes"]), reverse=True)
    return results


# ---------------------------------------------------------------------------
# Blog post generation
# ---------------------------------------------------------------------------

def generate_blog_post(
    posts: list[dict],
    cfg: dict,
    start_date: date,
    end_date: date,
) -> str:
    """Generate a Markdown blog post for the Material blog plugin."""
    feed_name = cfg.get("feed_name", "Bluesky Feed")
    logo_url = cfg.get("logo_url", "")
    feed_bsky_url = cfg.get("feed_bsky_url", "")
    week_id = end_date.strftime("%G-W%V")

    lines: list[str] = []

    # Blog front matter
    lines.append("---")
    lines.append(f"date: {end_date.isoformat()}")
    lines.append(f'description: "{len(posts)} posts from {start_date} to {end_date}"')
    lines.append("hide:")
    lines.append("  - navigation")
    lines.append("  - toc")
    lines.append("---")
    lines.append("")

    # Title
    icon = (
        f'<img src="{logo_url}" alt="" style="height:1.2em;vertical-align:middle"> '
        if logo_url
        else ""
    )
    lines.append(
        f"# {icon}{feed_name} Digest — {week_id}"
    )
    lines.append("")
    lines.append(
        f"Posts from **{start_date.strftime('%B %d, %Y')}** to "
        f"**{end_date.strftime('%B %d, %Y')}**. "
        f"Total: **{len(posts)}** posts."
    )
    lines.append("")

    # Excerpt separator for blog listing
    lines.append("<!-- more -->")
    lines.append("")
    lines.append("---")
    lines.append("")

    # Posts
    for post in posts:
        handle = post["author_handle"]
        avatar = post["author_avatar"]
        profile_url = f"https://bsky.app/profile/{handle}"

        # Avatar + author line using inline HTML for layout
        if avatar:
            lines.append(
                f'<div class="post-card" markdown>'
            )
            lines.append(
                f'<img src="{avatar}" alt="" class="avatar">'
            )
            lines.append(
                f'**{post["author_name"]}** '
                f'[@{handle}]({profile_url}){{:target="_blank"}}'
            )
        else:
            lines.append('<div class="post-card" markdown>')
            lines.append(
                f'**{post["author_name"]}** '
                f'[@{handle}]({profile_url}){{:target="_blank"}}'
            )

        lines.append("")
        lines.append(
            f'<span class="post-meta">{post["date"]} · '
            f':heart: {post["likes"]}</span>'
        )
        lines.append("")
        lines.append(post["text"])
        lines.append("")

        if post["article_uri"]:
            lines.append(
                f':link: <{post["article_uri"]}>'
            )
            lines.append("")

        lines.append(
            f'[:fontawesome-brands-bluesky: View on Bluesky]'
            f'({post["bsky_url"]}){{:target="_blank" .md-button}}'
        )
        lines.append("")
        lines.append("</div>")
        lines.append("")
        lines.append("---")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Data index
# ---------------------------------------------------------------------------

def load_digests_index() -> list[dict]:
    if DIGESTS_JSON.exists():
        with open(DIGESTS_JSON) as f:
            return json.load(f)
    return []


def save_digests_index(index: list[dict]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(DIGESTS_JSON, "w") as f:
        json.dump(index, f, indent=2)


def update_index(
    post_count: int, start_date: date, end_date: date
) -> None:
    week_id = end_date.strftime("%G-W%V")
    index = load_digests_index()
    entry = {
        "id": week_id,
        "date_from": start_date.isoformat(),
        "date_to": end_date.isoformat(),
        "post_count": post_count,
    }
    index = [e for e in index if e["id"] != week_id]
    index.append(entry)
    index.sort(key=lambda e: e["id"], reverse=True)
    save_digests_index(index)
    print(f"  Updated digests index: {len(index)} total digests")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    cfg = load_config()

    # Compute Monday–Sunday week range
    start_date, end_date = week_range()

    # Allow override via env (workflow_dispatch)
    override = os.environ.get("PERIOD_DAYS")
    if override:
        period = int(override)
        end_date = date.today()
        start_date = end_date - timedelta(days=period)

    feed_name = cfg.get("feed_name", "Bluesky Feed")
    print(f"Fetching {feed_name} feed...")
    print(f"  Period: {start_date} (Mon) to {end_date} (Sun)")

    raw_posts = fetch_feed(cfg)
    print(f"  Fetched {len(raw_posts)} raw posts from API")

    posts = filter_posts(raw_posts, cfg, start_date, end_date)
    print(f"  Filtered to {len(posts)} posts in date range")

    if not posts:
        print("  No posts found for this period. Generating empty digest.")

    # Generate blog post
    BLOG_POSTS_DIR.mkdir(parents=True, exist_ok=True)
    week_id = end_date.strftime("%G-W%V")
    blog_md = generate_blog_post(posts, cfg, start_date, end_date)
    post_path = BLOG_POSTS_DIR / f"{week_id}.md"
    post_path.write_text(blog_md)
    print(f"  Saved blog post: {post_path.relative_to(ROOT)}")

    # Update metadata index
    update_index(len(posts), start_date, end_date)

    print("Done!")


if __name__ == "__main__":
    main()