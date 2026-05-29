"""
Trend Hunter — Discovers viral topics using multiple free public sources and
ranks them into a scored list ready to feed into the OllamaWriter prompt pipeline.

Sources:
  1. YouTube channel RSS feeds (no API key)
  2. HackerNews frontpage RSS (tech_ai niche only)
  3. Reddit /r/{sub}/hot.json (no auth)
  4. pytrends Google Trends (optional — skipped gracefully if not installed)

Interface used by run_rerun.py:
    hunter = TrendHunter(cache_dir=str(cache), cache_ttl_hours=6)
    topics = hunter.get_trending_topics(niche, max_topics=10)
    seed   = hunter.to_ollama_seed(topics)

Each topic dict: {"title": str, "score": int, "source": str, "url": str}
"""

import json
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Optional pytrends
# ---------------------------------------------------------------------------
try:
    from pytrends.request import TrendReq as _TrendReq
    _PYTRENDS_AVAILABLE = True
except ImportError:
    _TrendReq = None  # type: ignore[assignment,misc]
    _PYTRENDS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_USER_AGENT = "YouTube-Pipeline/1.0 (automation)"
_TIMEOUT = 10  # seconds

# YouTube channel RSS feeds — no API key required.
_YT_CHANNEL_RSS_URL = "https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

_NICHE_YT_CHANNELS = {
    "tech_ai": [
        "UCsBjURrPoezykLs9EqgamOA",  # Fireship
        "UCSHZKyawb77ixDdsGog4iWA",  # Lex Fridman
        "UCbfYPyITQ-7l4upoX8nvctg",  # Two Minute Papers
    ],
    "finance": [
        "UCGy7SkBjcIAgTiwkXEtPnYg",  # Andrei Jikh
        "UCV6KDgJskWaEckne5aPA0aQ",  # Graham Stephan
        "UCL-uZD5jy-kY1SBVXkIH0ug",  # Mark Tilbury
    ],
    "motivation": [
        "UCnYMOamNKLGVlJgRUbamveA",  # Tom Bilyeu
        "UC0vBXGSyV14uvJ4hECDOl0Q",  # TED
        "UCGq-a57w-aPwyi3pW7XLiHw",  # Motivational Madness
    ],
    "history": [
        "UCpNxBDV4oqBN-EnqQdPkOzA",  # Absolute History
        "UCMmaBzfCCwZ2KqaBJjkj0fw",  # Kings & Generals
        "UCHdos_tnuTHpBZ2OUShGjAg",  # History Hit
    ],
    "science": [
        "UCsXVk37bltHxD1rDPwtNM8Q",  # Kurzgesagt
        "UCHnyfMqiRRG1u-2MsSQLbXA",  # Veritasium
        "UCZYTClx2T1of7BRZ86-8fow",  # SciShow
    ],
}

# Reddit subreddits per niche
_NICHE_SUBREDDITS = {
    "tech_ai":    ["artificial", "MachineLearning", "ChatGPT"],
    "finance":    ["personalfinance", "investing", "wallstreetbets"],
    "motivation": ["getmotivated", "selfimprovement", "productivity"],
    "history":    ["history", "AskHistorians", "HistoryMemes"],
    "science":    ["science", "Physics", "space"],
}

# pytrends seed keyword per niche
_NICHE_PYTRENDS_KW = {
    "tech_ai":    "artificial intelligence",
    "finance":    "personal finance",
    "motivation": "self improvement",
    "history":    "history documentary",
    "science":    "science discovery",
}

# Simple English stopwords for dedup normalisation
_STOPWORDS = frozenset(
    "a an the and or but in on at to for of with is are was were be been "
    "being have has had do does did will would could should may might must "
    "by from this that these those it its i you he she we they what how why "
    "when where who which all just so up out if about as into than then".split()
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _USER_AGENT})
    return s


def _norm_title(title: str) -> frozenset:
    """Return a frozenset of significant lowercase words for dedup comparison."""
    words = re.findall(r"[a-z0-9]+", title.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) > 1)


def _titles_overlap(a: str, b: str, threshold: float = 0.6) -> bool:
    """Return True when two titles share >= threshold of their significant words."""
    sa = _norm_title(a)
    sb = _norm_title(b)
    if not sa or not sb:
        return a.lower().strip() == b.lower().strip()
    overlap = len(sa & sb) / min(len(sa), len(sb))
    return overlap >= threshold


# ---------------------------------------------------------------------------
# TrendHunter
# ---------------------------------------------------------------------------

class TrendHunter:
    """Pulls trending topics from multiple free public sources.

    Args:
        cache_dir: Directory where per-niche JSON caches are stored.
        cache_ttl_hours: How long a cached result is considered fresh.
    """

    def __init__(self, cache_dir: str = "cache/trends", cache_ttl_hours: int = 6):
        self.cache_dir = Path(cache_dir)
        self.cache_ttl = timedelta(hours=cache_ttl_hours)
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            print(f"[trend_hunter] WARNING: could not create cache dir: {exc}",
                  file=sys.stderr)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_trending_topics(self, niche: str, max_topics: int = 10) -> list:
        """Return up to *max_topics* scored topic dicts for the given niche.

        Dict shape: {"title": str, "score": int, "source": str, "url": str}

        Never raises — all fetch failures are caught and logged to stderr.
        Falls back to a single placeholder entry when every source fails.
        """
        niche = (niche or "tech_ai").strip().lower()

        # --- Cache hit ---
        cached = self._load_cache(niche)
        if cached is not None:
            return cached[:max_topics]

        topics: list = []

        # Source 1: YouTube channel RSS
        try:
            topics.extend(self._fetch_youtube_rss(niche))
        except Exception as exc:
            print(f"[trend_hunter] youtube_rss({niche}) failed: {exc}", file=sys.stderr)

        # Source 1b: HackerNews (tech_ai only)
        if niche == "tech_ai":
            try:
                topics.extend(self._fetch_hackernews())
            except Exception as exc:
                print(f"[trend_hunter] hackernews({niche}) failed: {exc}", file=sys.stderr)

        # Source 2: Reddit hot.json
        try:
            topics.extend(self._fetch_reddit(niche))
        except Exception as exc:
            print(f"[trend_hunter] reddit({niche}) failed: {exc}", file=sys.stderr)

        # Source 3: pytrends (optional)
        if _PYTRENDS_AVAILABLE:
            try:
                topics.extend(self._fetch_pytrends(niche))
            except Exception as exc:
                print(f"[trend_hunter] pytrends({niche}) failed: {exc}", file=sys.stderr)

        # Deduplicate and sort
        topics = self._deduplicate(topics)
        topics.sort(key=lambda d: d.get("score", 0), reverse=True)

        # Fallback so caller always gets something
        if not topics:
            topics = [{
                "title": f"What's happening in {niche.replace('_', ' ')} right now",
                "score": 0,
                "source": "fallback",
                "url": "",
            }]

        # Save to cache
        try:
            self._save_cache(niche, topics)
        except Exception as exc:
            print(f"[trend_hunter] cache write failed: {exc}", file=sys.stderr)

        return topics[:max_topics]

    def to_ollama_seed(self, topics: list) -> str:
        """Format the topic list into an Ollama prompt fragment.

        Example output:
            TRENDING TOPICS FOR INSPIRATION (pick the most relevant):
            1. [YouTube trending] Why OpenAI Just Changed Everything (score: 100)
            2. [Reddit r/artificial] Claude vs GPT-4: Real test results (score: 847)
            ...
            Use these as inspiration for video topic selection. Prioritize topics
            with high engagement signals.
        """
        lines = ["TRENDING TOPICS FOR INSPIRATION (pick the most relevant):"]
        for i, t in enumerate(topics or [], 1):
            title = (t.get("title") or "").strip() or "(untitled)"
            score = int(t.get("score") or 0)
            source = t.get("source", "unknown")

            # Format source label nicely
            if source == "youtube_channel":
                label = "YouTube popular"
            elif source == "hackernews":
                label = "Hacker News"
            elif source.startswith("reddit:"):
                sub = source.split(":", 1)[1]
                label = f"Reddit r/{sub}"
            elif source == "pytrends":
                label = "Google Trends"
            else:
                label = source

            lines.append(f"{i}. [{label}] {title} (score: {score})")

        lines.append(
            "Use these as inspiration for video topic selection. "
            "Prioritize topics with high engagement signals."
        )
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Source fetchers
    # ------------------------------------------------------------------

    def _fetch_youtube_rss(self, niche: str) -> list:
        """Fetch recent videos from popular YouTube channels for the niche."""
        channel_ids = _NICHE_YT_CHANNELS.get(niche, [])

        # YouTube Atom feed namespace
        ns = {
            "atom":  "http://www.w3.org/2005/Atom",
            "yt":    "http://www.youtube.com/xml/schemas/2015",
            "media": "http://search.yahoo.com/mrss/",
        }

        sess = _session()
        results = []

        for channel_id in channel_ids:
            url = _YT_CHANNEL_RSS_URL.format(channel_id=channel_id)
            try:
                resp = sess.get(url, timeout=_TIMEOUT)
                resp.raise_for_status()
                root = ET.fromstring(resp.content)

                for entry in root.findall("atom:entry", ns):
                    title_el = entry.find("atom:title", ns)
                    link_el  = entry.find("atom:link", ns)
                    stats_el = entry.find("yt:statistics", ns)

                    title = (title_el.text or "").strip() if title_el is not None else ""
                    if not title:
                        continue

                    url_str = ""
                    if link_el is not None:
                        url_str = link_el.get("href", "")

                    # yt:statistics viewCount is sometimes present
                    view_count = 0
                    if stats_el is not None:
                        try:
                            view_count = int(stats_el.get("viewCount", 0) or 0)
                        except (ValueError, TypeError):
                            view_count = 0

                    # Base score: channel upload = 80; boost modestly by views
                    score = 80
                    if view_count > 1_000_000:
                        score = min(score + 50, 200)
                    elif view_count > 100_000:
                        score = min(score + 20, 200)

                    results.append({
                        "title":  title,
                        "score":  score,
                        "source": "youtube_channel",
                        "url":    url_str,
                    })
            except Exception as exc:
                print(
                    f"[trend_hunter] youtube_channel {channel_id} fetch failed: {exc}",
                    file=sys.stderr,
                )

        return results

    def _fetch_hackernews(self) -> list:
        """Fetch top stories from Hacker News RSS (useful for tech_ai niche)."""
        url = "https://hnrss.org/frontpage?count=15"
        try:
            sess = _session()
            resp = sess.get(url, timeout=_TIMEOUT)
            resp.raise_for_status()
            root = ET.fromstring(resp.content)
            results = []
            for item in root.findall(".//item")[:15]:
                title_el = item.find("title")
                link_el = item.find("link")
                title = (title_el.text or "").strip() if title_el is not None else ""
                url_str = (link_el.text or "").strip() if link_el is not None else ""
                if not title or "Ask HN" in title or "Tell HN" in title:
                    continue
                results.append({
                    "title": title,
                    "score": 75,
                    "source": "hackernews",
                    "url": url_str,
                })
            return results
        except Exception as exc:
            print(f"[trend_hunter] hackernews fetch failed: {exc}", file=sys.stderr)
            return []

    def _fetch_reddit(self, niche: str) -> list:
        """Fetch hot posts from each subreddit mapped to this niche."""
        subs = _NICHE_SUBREDDITS.get(niche, ["popular"])
        sess = _session()
        results = []

        for sub in subs:
            reddit_url = f"https://www.reddit.com/r/{sub}/hot.json?limit=10"
            try:
                resp = sess.get(reddit_url, timeout=_TIMEOUT)
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                print(f"[trend_hunter] reddit r/{sub} fetch failed: {exc}",
                      file=sys.stderr)
                continue

            children = (
                (payload.get("data") or {}).get("children") or []
            )
            for child in children:
                d = child.get("data") or {}
                title = (d.get("title") or "").strip()
                if not title:
                    continue
                # Skip stickied mod posts
                if d.get("stickied"):
                    continue

                post_score   = int(d.get("score")        or 0)
                num_comments = int(d.get("num_comments") or 0)
                permalink    = d.get("permalink") or ""
                post_url     = (f"https://www.reddit.com{permalink}"
                                if permalink else "")

                # Engagement formula: votes + comments * 3
                score = post_score + num_comments * 3

                results.append({
                    "title":  title,
                    "score":  score,
                    "source": f"reddit:{sub}",
                    "url":    post_url,
                })

        return results

    def _fetch_pytrends(self, niche: str) -> list:
        """Fetch Google Trends interest via pytrends (optional dependency)."""
        if not _PYTRENDS_AVAILABLE or _TrendReq is None:
            return []

        kw = _NICHE_PYTRENDS_KW.get(niche, niche.replace("_", " "))
        pt = _TrendReq(hl="en-US", tz=360, timeout=(10, 10))
        pt.build_payload([kw], cat=0, timeframe="now 7-d")
        df = pt.interest_over_time()

        if df is None or df.empty:
            return []

        # Average interest score (0-100) over the week
        if kw in df.columns:
            avg = float(df[kw].mean())
        else:
            return []

        return [{
            "title":  f"{kw} — trending this week",
            "score":  int(avg),
            "source": "pytrends",
            "url":    (
                "https://trends.google.com/trends/explore"
                f"?q={kw.replace(' ', '+')}&date=now+7-d"
            ),
        }]

    # ------------------------------------------------------------------
    # Deduplication
    # ------------------------------------------------------------------

    def _deduplicate(self, topics: list) -> list:
        """Remove near-duplicate titles, keeping the highest-scoring entry."""
        kept: list = []
        for candidate in topics:
            title = candidate.get("title", "")
            if not title:
                continue
            # Check against already-kept entries
            duplicate = False
            for existing in kept:
                if _titles_overlap(title, existing.get("title", "")):
                    # Keep whichever has the higher score
                    if candidate.get("score", 0) > existing.get("score", 0):
                        existing.update(candidate)
                    duplicate = True
                    break
            if not duplicate:
                kept.append(dict(candidate))
        return kept

    # ------------------------------------------------------------------
    # Cache
    # ------------------------------------------------------------------

    def _cache_path(self, niche: str) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_\-]", "_", niche) or "default"
        return self.cache_dir / f"{safe}_trends.json"

    def _load_cache(self, niche: str) -> list | None:
        path = self._cache_path(niche)
        if not path.exists():
            return None
        try:
            blob = json.loads(path.read_text(encoding="utf-8"))
            fetched_at = blob.get("fetched_at")
            topics = blob.get("topics")
            if not fetched_at or not isinstance(topics, list):
                return None
            ts = datetime.fromisoformat(fetched_at)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = datetime.now(timezone.utc) - ts
            if age <= self.cache_ttl:
                return topics
            return None
        except Exception as exc:
            print(f"[trend_hunter] cache read failed: {exc}", file=sys.stderr)
            return None

    def _save_cache(self, niche: str, topics: list) -> None:
        path = self._cache_path(niche)
        blob = {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "topics": topics,
        }
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        tmp.replace(path)


# ---------------------------------------------------------------------------
# CLI (python -m pipeline.trend_hunter --niche tech_ai)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Fetch and display trending topics for a niche."
    )
    parser.add_argument(
        "--niche", default="tech_ai",
        choices=list(_NICHE_YT_CHANNELS.keys()),
        help="Content niche.",
    )
    parser.add_argument("--max", type=int, default=10, dest="max_topics",
                        help="Number of topics to return.")
    parser.add_argument("--cache-dir", default="cache/trends",
                        help="Cache directory path.")
    parser.add_argument("--ttl", type=int, default=6,
                        help="Cache TTL in hours.")
    parser.add_argument("--no-cache", action="store_true",
                        help="Ignore and overwrite existing cache.")
    args = parser.parse_args()

    hunter = TrendHunter(cache_dir=args.cache_dir, cache_ttl_hours=args.ttl)

    if args.no_cache:
        cp = hunter._cache_path(args.niche)
        if cp.exists():
            cp.unlink()

    topics = hunter.get_trending_topics(args.niche, max_topics=args.max_topics)

    print(f"\nTop {len(topics)} trending topics for niche '{args.niche}':\n")
    for i, t in enumerate(topics, 1):
        print(f"  {i:>2}. [{t['score']:>5}] {t['source']:<25} {t['title']}")

    print()
    print(hunter.to_ollama_seed(topics))
