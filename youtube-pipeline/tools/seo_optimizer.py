"""
YouTube SEO Optimizer — Audits and improves video metadata for the Matrix monster channel.

Usage:
  python tools/seo_optimizer.py --channel tech-ai            # dry-run (report only)
  python tools/seo_optimizer.py --channel tech-ai --apply    # apply title/desc updates

Supports all pipeline channel IDs: tech-ai, finance, motivation, history-mystery, science-facts
"""

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("seo_optimizer")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PIPELINE_ROOT = Path(__file__).parent.parent
CONFIG_DIR = PIPELINE_ROOT / "config"
TOKENS_DIR = CONFIG_DIR / "tokens"
CLIENT_SECRET = CONFIG_DIR / "client_secret.json"
ANALYTICS_DIR = PIPELINE_ROOT / "analytics"
ANALYTICS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Niche detection keyword map
# ---------------------------------------------------------------------------
NICHE_KEYWORDS: dict[str, list[str]] = {
    "tech_ai": [
        "ai", "gpt", "llm", "machine learning", "artificial", "chatgpt",
        "claude", "openai", "neural", "robot", "automation", "tech", "coding",
        "python", "software", "algorithm", "deep learning", "transformer",
    ],
    "finance": [
        "money", "invest", "stock", "crypto", "budget", "wealth", "finance",
        "savings", "dividend", "portfolio", "market", "trading", "passive income",
        "retirement", "etf", "bond", "tax", "income",
    ],
    "motivation": [
        "mindset", "success", "habits", "productivity", "goal", "discipline",
        "achieve", "motivation", "hustle", "grind", "winning", "focus",
        "confidence", "mindfulness", "self improvement", "growth",
    ],
    "history": [
        "history", "ancient", "war", "empire", "civilization", "mystery",
        "secret", "historical", "medieval", "dynasty", "battle", "revolution",
        "king", "queen", "pharaoh", "roman", "greek", "wwii", "world war",
    ],
    "science": [
        "science", "physics", "space", "biology", "quantum", "discovery",
        "earth", "chemistry", "evolution", "nasa", "universe", "planet",
        "experiment", "atom", "dna", "black hole", "scientist",
    ],
}

# Power words that signal high-CTR titles
POWER_WORDS = [
    "secret", "hidden", "shocking", "truth", "exposed", "revealed",
    "mind-blowing", "incredible", "amazing", "terrifying", "real",
    "ultimate", "complete", "proven", "never", "always", "best",
    "worst", "dangerous", "surprising", "unbelievable", "changed",
    "destroyed", "failed", "winning", "warning",
]

# Affiliate link patterns (Amazon, etc.)
AFFILIATE_PATTERNS = [
    r"amazon\.com.*tag=",
    r"amzn\.to/",
    r"bit\.ly/",
    r"go\.skimresources",
    r"aff\.",
    r"affiliate",
    r"ref=",
    r"matrixmonster-20",
]

# CTA phrases
CTA_PHRASES = [
    "subscribe", "like", "comment", "share", "follow", "click",
    "link in bio", "link below", "check out", "watch next",
    "turn on notifications", "hit the bell",
]

# ---------------------------------------------------------------------------
# Channel config lookup
# ---------------------------------------------------------------------------

def load_channels_config() -> dict:
    cfg_path = CONFIG_DIR / "channels.json"
    return json.loads(cfg_path.read_text(encoding="utf-8"))


def get_channel_config(channel_id: str) -> dict:
    cfg = load_channels_config()
    for ch in cfg.get("channels", []):
        if ch["id"] == channel_id:
            return ch
    raise ValueError(f"Channel '{channel_id}' not found in channels.json")


# ---------------------------------------------------------------------------
# Auth (same pattern as youtube_uploader.py)
# ---------------------------------------------------------------------------

def get_credentials(channel_id: str):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    token_file = TOKENS_DIR / f"{channel_id}_token.json"
    if not token_file.exists():
        raise FileNotFoundError(
            f"No OAuth token for channel '{channel_id}'. "
            f"Run: python auth_manual.py {channel_id}"
        )

    creds = Credentials.from_authorized_user_file(str(token_file))

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            logger.info(f"Token refreshed for {channel_id}")
        except Exception as e:
            raise RuntimeError(
                f"OAuth token for '{channel_id}' expired and could not be refreshed. "
                f"Re-authenticate: python auth_manual.py {channel_id}"
            ) from e

    if not creds or not creds.valid:
        raise RuntimeError(
            f"OAuth token for '{channel_id}' is invalid. "
            f"Re-authenticate: python auth_manual.py {channel_id}"
        )

    with open(token_file, "w") as f:
        f.write(creds.to_json())

    return creds


def build_service(channel_id: str):
    from googleapiclient.discovery import build
    creds = get_credentials(channel_id)
    return build("youtube", "v3", credentials=creds)


# ---------------------------------------------------------------------------
# Niche detection
# ---------------------------------------------------------------------------

def detect_niche(title: str, description: str = "", tags: list = None) -> str:
    text = (title + " " + description + " " + " ".join(tags or [])).lower()
    scores = {}
    for niche, keywords in NICHE_KEYWORDS.items():
        scores[niche] = sum(1 for kw in keywords if kw in text)
    best = max(scores, key=scores.get)
    return best if scores[best] > 0 else "tech_ai"


# ---------------------------------------------------------------------------
# SEO Scoring
# ---------------------------------------------------------------------------

def score_title(title: str, niche: str) -> dict:
    """Score a title 0-40 and return breakdown."""
    score = 0
    breakdown = {}
    t = title.strip()

    # Has year? +5
    has_year = bool(re.search(r"\b(202[0-9]|201[0-9])\b", t))
    if has_year:
        score += 5
    breakdown["has_year"] = has_year

    # Has number? +5
    has_number = bool(re.search(r"\b\d+\b", t))
    if has_number:
        score += 5
    breakdown["has_number"] = has_number

    # 40-70 chars? +10
    good_length = 40 <= len(t) <= 70
    if good_length:
        score += 10
    breakdown["good_length"] = good_length
    breakdown["title_length"] = len(t)

    # Has question? +5
    has_question = "?" in t
    if has_question:
        score += 5
    breakdown["has_question"] = has_question

    # Power words? +10
    t_lower = t.lower()
    found_power = [w for w in POWER_WORDS if w in t_lower]
    if found_power:
        score += 10
    breakdown["power_words"] = found_power

    # Has niche keyword? +5
    niche_kws = NICHE_KEYWORDS.get(niche, [])
    found_niche_kw = [kw for kw in niche_kws if kw in t_lower]
    if found_niche_kw:
        score += 5
    breakdown["niche_keywords"] = found_niche_kw

    return {"score": score, "max": 40, "breakdown": breakdown}


def score_description(description: str) -> dict:
    """Score a description 0-30 and return breakdown."""
    score = 0
    breakdown = {}
    d = description or ""

    # Length > 200 chars? +10
    good_length = len(d) > 200
    if good_length:
        score += 10
    breakdown["has_sufficient_length"] = good_length
    breakdown["description_length"] = len(d)

    # Has affiliate links? +10
    has_affiliates = any(re.search(p, d, re.IGNORECASE) for p in AFFILIATE_PATTERNS)
    if has_affiliates:
        score += 10
    breakdown["has_affiliate_links"] = has_affiliates

    # Has timestamps? +5
    has_timestamps = bool(re.search(r"\b\d+:\d{2}\b", d))
    if has_timestamps:
        score += 5
    breakdown["has_timestamps"] = has_timestamps

    # Has CTA? +5
    d_lower = d.lower()
    found_cta = [c for c in CTA_PHRASES if c in d_lower]
    if found_cta:
        score += 5
    breakdown["cta_phrases"] = found_cta

    return {"score": score, "max": 30, "breakdown": breakdown}


def score_tags(tags: list, niche: str) -> dict:
    """Score tags 0-30 and return breakdown."""
    score = 0
    breakdown = {}
    tags = tags or []

    # 10+ tags? +10
    has_enough_tags = len(tags) >= 10
    if has_enough_tags:
        score += 10
    breakdown["tag_count"] = len(tags)
    breakdown["has_enough_tags"] = has_enough_tags

    # Has long-tail tags (3+ words)? +10
    long_tail = [t for t in tags if len(t.split()) >= 3]
    if long_tail:
        score += 10
    breakdown["long_tail_tags"] = long_tail[:5]

    # First tag is primary niche keyword? +10
    niche_kws = NICHE_KEYWORDS.get(niche, [])
    first_tag_is_primary = bool(
        tags and any(kw in tags[0].lower() for kw in niche_kws)
    )
    if first_tag_is_primary:
        score += 10
    breakdown["first_tag"] = tags[0] if tags else ""
    breakdown["first_tag_is_primary"] = first_tag_is_primary

    return {"score": score, "max": 30, "breakdown": breakdown}


def compute_seo_score(title: str, description: str, tags: list, niche: str) -> dict:
    title_result = score_title(title, niche)
    desc_result = score_description(description)
    tags_result = score_tags(tags, niche)

    total = title_result["score"] + desc_result["score"] + tags_result["score"]
    return {
        "total": total,
        "max": 100,
        "pct": round(total, 1),
        "title": title_result,
        "description": desc_result,
        "tags": tags_result,
    }


# ---------------------------------------------------------------------------
# Title generation
# ---------------------------------------------------------------------------

def extract_topic(title: str, niche: str) -> str:
    """Pull a short topic phrase from an existing title by stripping common filler."""
    stopwords = {
        "the", "a", "an", "of", "in", "on", "at", "to", "for", "and", "or",
        "is", "are", "was", "were", "be", "been", "being", "have", "has",
        "had", "do", "does", "did", "will", "would", "could", "should",
        "may", "might", "must", "shall", "can", "how", "why", "what",
        "when", "where", "who", "which", "this", "that", "these", "those",
        "with", "from", "by", "about", "as", "into", "through", "during",
        "before", "after", "above", "below", "between", "out", "off", "over",
        "under", "again", "further", "then", "once", "here", "there",
        "your", "my", "our", "their", "its", "we", "you", "he", "she", "they",
        "i", "it", "not", "no", "nor", "so", "yet", "both", "either",
        "neither", "just", "but", "if", "because", "although", "while",
    }
    # Remove year, pipe, numbers at start, trailing punctuation
    cleaned = re.sub(r"\b(202[0-9]|201[0-9])\b", "", title)
    cleaned = re.sub(r"[|!?:]", " ", cleaned)
    cleaned = re.sub(r"^\d+[\.\)]\s*", "", cleaned.strip())
    words = [w for w in cleaned.split() if w.lower() not in stopwords and len(w) > 2]
    # Take up to 5 meaningful words
    topic = " ".join(words[:5]).strip().strip("-").strip()
    return topic or title[:40]


TITLE_TEMPLATES: dict[str, str] = {
    "tech_ai": "{topic} Explained in 5 Minutes | AI {year}",
    "finance": "The {topic} Strategy That Changed My Life in {year} | Finance Tips",
    "motivation": "Why 99% of People Fail at {topic} (and How to Beat the Odds)",
    "history": "The REAL Story Behind {topic} That Nobody Talks About",
    "science": "Mind-Blowing Facts About {topic} You Never Knew",
}


def generate_improved_title(original_title: str, niche: str) -> str:
    year = datetime.now().year
    topic = extract_topic(original_title, niche)
    template = TITLE_TEMPLATES.get(niche, TITLE_TEMPLATES["tech_ai"])
    candidate = template.format(topic=topic, year=year)
    # Truncate to YouTube's 100-char limit
    return candidate[:100]


def generate_improved_description(
    original_desc: str,
    title: str,
    niche: str,
    channel_name: str,
) -> str:
    """
    Return an improved description that adds affiliate hook, timestamps
    placeholder, and CTA if they are missing.
    """
    year = datetime.now().year
    desc = original_desc or ""

    # If already great, leave it alone
    current_score = score_description(desc)
    if current_score["score"] >= 25:
        return desc

    topic = extract_topic(title, niche)

    # Niche-specific affiliate line
    affiliate_hooks = {
        "tech_ai": (
            f"Recommended tools and resources: https://amzn.to/ai-tools?tag=matrixmonster-20\n"
            f"Best AI books 2024: https://amzn.to/ai-books-{year}?tag=matrixmonster-20"
        ),
        "finance": (
            f"Top investing resources: https://amzn.to/finance-books?tag=matrixmonster-20\n"
            f"My recommended budgeting tools: https://amzn.to/budget-tools-{year}?tag=matrixmonster-20"
        ),
        "motivation": (
            f"Mindset books that changed my life: https://amzn.to/mindset-books?tag=matrixmonster-20\n"
            f"Best productivity tools: https://amzn.to/productivity-{year}?tag=matrixmonster-20"
        ),
        "history": (
            f"Fascinating history books: https://amzn.to/history-books?tag=matrixmonster-20\n"
            f"Recommended documentaries and resources: https://amzn.to/history-docs-{year}?tag=matrixmonster-20"
        ),
        "science": (
            f"Must-read science books: https://amzn.to/science-books?tag=matrixmonster-20\n"
            f"Best science tools and kits: https://amzn.to/science-kits-{year}?tag=matrixmonster-20"
        ),
    }

    niche_intro = {
        "tech_ai": f"In this video, we break down everything you need to know about {topic} in plain English.",
        "finance": f"In this video, we reveal the exact {topic} strategy used by top investors to build wealth.",
        "motivation": f"In this video, discover how mastering {topic} can completely transform your results.",
        "history": f"In this video, we uncover the real story behind {topic} — the part history books skip.",
        "science": f"In this video, we explore the mind-blowing science of {topic} that will change how you see the world.",
    }

    intro = niche_intro.get(niche, f"In this video, we explore {topic}.")

    timestamps = (
        "00:00 Introduction\n"
        "01:30 Key Concepts\n"
        "04:00 Deep Dive\n"
        "06:30 Practical Tips\n"
        "08:00 Summary & Takeaways"
    )

    cta = (
        f"If you found this helpful, SUBSCRIBE to {channel_name} for daily videos!\n"
        "Hit the LIKE button and turn on notifications so you never miss an upload."
    )

    affiliate = affiliate_hooks.get(niche, "")

    # Assemble: keep original if it has meaningful content, then append missing sections
    has_timestamps = bool(re.search(r"\b\d+:\d{2}\b", desc))
    has_cta = any(c in desc.lower() for c in CTA_PHRASES)
    has_affiliate = any(re.search(p, desc, re.IGNORECASE) for p in AFFILIATE_PATTERNS)

    parts = []

    if len(desc.strip()) < 100:
        parts.append(intro)
    else:
        parts.append(desc.strip())

    if not has_timestamps:
        parts.append("\n--- CHAPTERS ---\n" + timestamps)

    if not has_affiliate and affiliate:
        parts.append("\n--- RESOURCES & LINKS ---\n" + affiliate)

    if not has_cta:
        parts.append("\n" + cta)

    # Standard hashtags
    niche_hashtags = {
        "tech_ai": "#AI #ArtificialIntelligence #Tech #MachineLearning #ChatGPT",
        "finance": "#Finance #Investing #Money #Wealth #PersonalFinance",
        "motivation": "#Motivation #Mindset #Success #Productivity #SelfImprovement",
        "history": "#History #Mystery #Documentary #AncientHistory #Historical",
        "science": "#Science #Facts #Space #Physics #MindBlown",
    }
    parts.append("\n" + niche_hashtags.get(niche, "#YouTube"))

    return "\n\n".join(p for p in parts if p.strip())[:5000]


def generate_improved_tags(original_tags: list, niche: str, title: str) -> list:
    """Return an improved, budgeted tag list."""
    niche_tag_sets = {
        "tech_ai": [
            "artificial intelligence", "AI tutorial", "machine learning explained",
            "ChatGPT tips", "AI tools 2026", "OpenAI", "tech news", "AI news",
            "deep learning", "neural networks", "AI for beginners",
            "technology trends", "future of AI", "AI productivity",
        ],
        "finance": [
            "personal finance", "investing for beginners", "stock market tips",
            "passive income ideas", "how to save money", "wealth building",
            "financial freedom", "dividend investing", "crypto tips",
            "budget tips", "money management", "financial independence",
            "how to invest", "market analysis 2026",
        ],
        "motivation": [
            "motivation speech", "mindset shift", "success habits",
            "productivity tips", "self improvement", "goal setting",
            "discipline and success", "how to be successful",
            "morning routine", "growth mindset", "positive mindset",
            "how to stay motivated", "success mindset 2026",
        ],
        "history": [
            "history documentary", "ancient history", "historical mystery",
            "untold history", "world history", "secret history",
            "historical events", "history facts", "ancient civilization",
            "mystery of history", "hidden history", "historical truth",
            "history explained", "historical documentary 2026",
        ],
        "science": [
            "science facts", "space facts", "physics explained",
            "science documentary", "mind blowing facts", "science news",
            "quantum physics", "biology facts", "earth science",
            "science discoveries", "NASA latest", "universe facts",
            "science for beginners", "amazing science facts 2026",
        ],
    }

    base_tags = niche_tag_sets.get(niche, [])

    # Extract topic word(s) as a specific tag
    topic = extract_topic(title, niche)
    topic_tags = [topic, f"{topic} explained", f"{topic} {datetime.now().year}"]

    # Combine: original first (preserve any good ones), then topic-specific, then niche base
    combined = list(original_tags or []) + topic_tags + base_tags

    # Deduplicate (case-insensitive)
    seen = set()
    deduped = []
    for t in combined:
        key = t.lower().strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(t.strip())

    # Budget to YouTube's ~475 combined char limit
    return _budget_tags(deduped)


def _budget_tags(tags: list, max_total_chars: int = 475, max_per_tag: int = 100) -> list:
    seen, out, total = set(), [], 0
    for raw in tags or []:
        t = str(raw).strip().strip('"').strip("'")
        if not t or len(t) > max_per_tag:
            continue
        key = t.lower()
        if key in seen:
            continue
        cost = len(t) + (2 if " " in t else 0) + 1
        if total + cost > max_total_chars:
            break
        seen.add(key)
        out.append(t)
        total += cost
    return out


# ---------------------------------------------------------------------------
# YouTube API helpers
# ---------------------------------------------------------------------------

def fetch_all_video_ids(youtube, youtube_channel_id: str, max_videos: int = 500) -> list[str]:
    """
    Fetch video IDs for the channel ordered by view count.
    Returns up to max_videos IDs.
    """
    video_ids = []
    page_token = None
    logger.info(f"Fetching video IDs for channel {youtube_channel_id}...")

    while len(video_ids) < max_videos:
        params = {
            "part": "id",
            "channelId": youtube_channel_id,
            "type": "video",
            "order": "viewCount",
            "maxResults": 50,
        }
        if page_token:
            params["pageToken"] = page_token

        try:
            resp = youtube.search().list(**params).execute()
        except Exception as e:
            logger.error(f"search.list failed: {e}")
            break

        items = resp.get("items", [])
        for item in items:
            vid_id = item.get("id", {}).get("videoId")
            if vid_id:
                video_ids.append(vid_id)

        page_token = resp.get("nextPageToken")
        if not page_token:
            break

        # Rate-limit courtesy pause
        time.sleep(0.3)

    logger.info(f"Found {len(video_ids)} videos")
    return video_ids[:max_videos]


def fetch_video_details(youtube, video_ids: list[str]) -> list[dict]:
    """
    Fetch snippet + statistics for a list of video IDs.
    YouTube allows up to 50 IDs per request.
    """
    details = []
    batch_size = 50

    for i in range(0, len(video_ids), batch_size):
        batch = video_ids[i : i + batch_size]
        try:
            resp = youtube.videos().list(
                part="snippet,statistics",
                id=",".join(batch),
                maxResults=50,
            ).execute()
        except Exception as e:
            logger.error(f"videos.list failed for batch {i}-{i+batch_size}: {e}")
            continue

        for item in resp.get("items", []):
            snippet = item.get("snippet", {})
            stats = item.get("statistics", {})
            details.append({
                "video_id": item["id"],
                "title": snippet.get("title", ""),
                "description": snippet.get("description", ""),
                "tags": snippet.get("tags", []),
                "published_at": snippet.get("publishedAt", ""),
                "category_id": snippet.get("categoryId", ""),
                "views": int(stats.get("viewCount", 0)),
                "likes": int(stats.get("likeCount", 0)),
                "comments": int(stats.get("commentCount", 0)),
            })

        time.sleep(0.2)

    return details


def apply_video_update(youtube, video_id: str, title: str, description: str, tags: list, category_id: str) -> bool:
    """Update a video's snippet via videos.update. Returns True on success."""
    try:
        # Fetch current snippet first to avoid clobbering other fields
        current = youtube.videos().list(part="snippet", id=video_id).execute()
        if not current.get("items"):
            logger.warning(f"Video {video_id} not found for update")
            return False

        snippet = current["items"][0]["snippet"]
        snippet["title"] = title[:100]
        snippet["description"] = description[:5000]
        snippet["tags"] = _budget_tags(tags)
        if category_id:
            snippet["categoryId"] = category_id

        youtube.videos().update(
            part="snippet",
            body={"id": video_id, "snippet": snippet},
        ).execute()
        logger.info(f"Updated {video_id}: '{title[:60]}'")
        return True
    except Exception as e:
        logger.error(f"Failed to update {video_id}: {e}")
        return False


# ---------------------------------------------------------------------------
# Core audit logic
# ---------------------------------------------------------------------------

def audit_channel(
    channel_id: str,
    youtube_channel_id: str,
    channel_config: dict,
    apply: bool = False,
) -> dict:
    niche = channel_config.get("niche", "tech_ai")
    channel_name = channel_config.get("name", "Matrix monster")

    youtube = build_service(channel_id)

    # Step 1: Fetch all video IDs
    video_ids = fetch_all_video_ids(youtube, youtube_channel_id, max_videos=500)
    if not video_ids:
        logger.error("No videos found. Check channel ID and token binding.")
        return {}

    # Step 2: Fetch full details
    logger.info(f"Fetching details for {len(video_ids)} videos...")
    videos = fetch_video_details(youtube, video_ids)
    logger.info(f"Got details for {len(videos)} videos")

    # Step 3: Score each video
    scored = []
    for v in videos:
        detected_niche = detect_niche(v["title"], v["description"], v["tags"])
        # Use channel niche as primary, detected as fallback context
        effective_niche = niche

        seo = compute_seo_score(v["title"], v["description"], v["tags"], effective_niche)
        v["seo"] = seo
        v["detected_niche"] = detected_niche
        v["effective_niche"] = effective_niche
        scored.append(v)

    # Step 4: Sort by views descending for top-20
    by_views = sorted(scored, key=lambda x: x["views"], reverse=True)
    top_20_by_views = by_views[:20]

    # Step 5: Sort by SEO score ascending for bottom-20 candidates
    by_seo = sorted(scored, key=lambda x: x["seo"]["total"])
    bottom_20_candidates = by_seo[:20]

    # Step 6: Generate improvements for bottom 20
    improvements = []
    for v in bottom_20_candidates:
        n = v["effective_niche"]
        new_title = generate_improved_title(v["title"], n)
        new_desc = generate_improved_description(v["description"], v["title"], n, channel_name)
        new_tags = generate_improved_tags(v["tags"], n, v["title"])

        new_seo = compute_seo_score(new_title, new_desc, new_tags, n)

        improvements.append({
            "video_id": v["video_id"],
            "video_url": f"https://www.youtube.com/watch?v={v['video_id']}",
            "views": v["views"],
            "original": {
                "title": v["title"],
                "seo_score": v["seo"]["total"],
                "seo_breakdown": v["seo"],
            },
            "improved": {
                "title": new_title,
                "description_preview": new_desc[:300] + "..." if len(new_desc) > 300 else new_desc,
                "tags": new_tags,
                "seo_score": new_seo["total"],
                "seo_breakdown": new_seo,
            },
            "score_delta": new_seo["total"] - v["seo"]["total"],
        })

    # Step 7: Compute overall channel health
    all_scores = [v["seo"]["total"] for v in scored]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0
    channel_health = round(avg_score, 1)

    # Distribution
    excellent = sum(1 for s in all_scores if s >= 80)
    good = sum(1 for s in all_scores if 60 <= s < 80)
    average = sum(1 for s in all_scores if 40 <= s < 60)
    poor = sum(1 for s in all_scores if s < 40)

    report = {
        "generated_at": datetime.now().isoformat(),
        "channel_id": channel_id,
        "youtube_channel_id": youtube_channel_id,
        "channel_name": channel_name,
        "niche": niche,
        "mode": "apply" if apply else "dry_run",
        "summary": {
            "total_videos_audited": len(scored),
            "channel_health_score": channel_health,
            "score_distribution": {
                "excellent_80_100": excellent,
                "good_60_79": good,
                "average_40_59": average,
                "poor_0_39": poor,
            },
            "avg_seo_score": round(avg_score, 1),
            "top_video_by_views": {
                "title": by_views[0]["title"] if by_views else "",
                "views": by_views[0]["views"] if by_views else 0,
                "seo_score": by_views[0]["seo"]["total"] if by_views else 0,
            } if by_views else {},
        },
        "top_20_by_views": [
            {
                "video_id": v["video_id"],
                "video_url": f"https://www.youtube.com/watch?v={v['video_id']}",
                "title": v["title"],
                "views": v["views"],
                "seo_score": v["seo"]["total"],
                "detected_niche": v["detected_niche"],
            }
            for v in top_20_by_views
        ],
        "bottom_20_by_seo_with_improvements": improvements,
        "recommendations": generate_channel_recommendations(channel_health, poor, average, len(scored)),
    }

    # Step 8: Apply updates if --apply flag set
    if apply:
        logger.info(f"APPLY MODE: updating {len(improvements)} videos...")
        updated = 0
        failed = 0
        for imp in improvements:
            vid_id = imp["video_id"]
            new_title = imp["improved"]["title"]
            # Reconstruct full new description (not just preview)
            orig_v = next((v for v in scored if v["video_id"] == vid_id), None)
            if orig_v is None:
                continue
            new_desc = generate_improved_description(
                orig_v["description"], orig_v["title"],
                orig_v["effective_niche"], channel_name
            )
            new_tags = imp["improved"]["tags"]
            cat_id = orig_v.get("category_id", "28")

            ok = apply_video_update(youtube, vid_id, new_title, new_desc, new_tags, cat_id)
            if ok:
                updated += 1
                imp["applied"] = True
            else:
                failed += 1
                imp["applied"] = False

            # Respect YouTube quota: 1 update per second
            time.sleep(1.2)

        report["apply_results"] = {"updated": updated, "failed": failed}
        logger.info(f"Apply complete: {updated} updated, {failed} failed")

    return report


def generate_channel_recommendations(health: float, poor_count: int, avg_count: int, total: int) -> list[str]:
    recs = []

    if health < 40:
        recs.append(
            "CRITICAL: Channel SEO health is very low. Run seo_optimizer.py --apply immediately "
            "to update the 20 worst-performing videos."
        )
    elif health < 60:
        recs.append(
            "SEO health is below average. Focus on improving title click-through rate with "
            "power words and optimal 40-70 character length."
        )
    else:
        recs.append("SEO health is moderate. Continue optimizing descriptions and tags for remaining videos.")

    if poor_count > total * 0.3:
        recs.append(
            f"{poor_count} videos ({round(poor_count/total*100)}%) score below 40. "
            "These need immediate title and description rewrites — they are invisible in search."
        )

    recs.append(
        "Add timestamps (chapters) to every video description — YouTube promotes "
        "videos with chapters in search results."
    )
    recs.append(
        "Ensure the first tag in every video is the primary niche keyword "
        "(e.g. 'artificial intelligence' for tech_ai). This is the single highest-value tag signal."
    )
    recs.append(
        "Target titles of 50-65 characters. Titles under 40 chars miss keyword density; "
        "over 70 chars get truncated in search results."
    )
    recs.append(
        "Add affiliate links (matrixmonster-20 tag) to all video descriptions. "
        "Even low-view videos can generate passive Amazon revenue over time."
    )
    recs.append(
        "Upload new videos consistently at optimal times: "
        "tech_ai at 17:00 UTC weekdays, finance at 13:00 UTC, motivation at 12:00 UTC."
    )

    return recs


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="YouTube SEO Optimizer — audit and improve video metadata for subscriber growth"
    )
    parser.add_argument(
        "--channel",
        required=True,
        choices=["tech-ai", "finance", "motivation", "history-mystery", "science-facts"],
        help="Pipeline channel ID to audit",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Apply title/description updates to the 20 worst SEO videos (default: dry-run only)",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=500,
        help="Maximum number of videos to fetch and audit (default: 500)",
    )
    args = parser.parse_args()

    logger.info(f"SEO Optimizer starting — channel={args.channel} apply={args.apply}")

    # Load channel config
    try:
        channel_config = get_channel_config(args.channel)
    except ValueError as e:
        logger.error(str(e))
        sys.exit(1)

    youtube_channel_id = channel_config.get("youtube_channel_id", "")
    if not youtube_channel_id:
        logger.error(f"No youtube_channel_id configured for {args.channel}")
        sys.exit(1)

    logger.info(
        f"Auditing '{channel_config.get('name')}' "
        f"(YouTube ID: {youtube_channel_id}, niche: {channel_config.get('niche')})"
    )

    # Run audit
    report = audit_channel(
        channel_id=args.channel,
        youtube_channel_id=youtube_channel_id,
        channel_config=channel_config,
        apply=args.apply,
    )

    if not report:
        logger.error("Audit produced no results. Check authentication and channel ID.")
        sys.exit(1)

    # Save report
    date_str = datetime.now().strftime("%Y%m%d")
    report_path = ANALYTICS_DIR / f"seo_audit_{args.channel}_{date_str}.json"
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    logger.info(f"Report saved: {report_path}")

    # Print human-readable summary
    summary = report.get("summary", {})
    improvements = report.get("bottom_20_by_seo_with_improvements", [])

    print("\n" + "=" * 70)
    print(f"  SEO AUDIT REPORT — {channel_config.get('name', args.channel)}")
    print("=" * 70)
    print(f"  Videos audited:      {summary.get('total_videos_audited', 0)}")
    print(f"  Channel health:      {summary.get('channel_health_score', 0)}/100")
    print(f"  Average SEO score:   {summary.get('avg_seo_score', 0)}/100")

    dist = summary.get("score_distribution", {})
    print(f"  Score distribution:")
    print(f"    Excellent (80-100): {dist.get('excellent_80_100', 0)}")
    print(f"    Good      (60-79):  {dist.get('good_60_79', 0)}")
    print(f"    Average   (40-59):  {dist.get('average_40_59', 0)}")
    print(f"    Poor      (0-39):   {dist.get('poor_0_39', 0)}")

    top_vid = summary.get("top_video_by_views", {})
    if top_vid:
        print(f"\n  Top video by views:  {top_vid.get('views', 0):,} views")
        print(f"    Title: {top_vid.get('title', '')[:65]}")
        print(f"    SEO score: {top_vid.get('seo_score', 0)}/100")

    print(f"\n  TOP 20 VIDEOS BY VIEWS (already performing well):")
    for i, v in enumerate(report.get("top_20_by_views", [])[:5], 1):
        print(f"  {i:2}. [{v['seo_score']:3}/100] {v['views']:>7,} views — {v['title'][:55]}")
    if len(report.get("top_20_by_views", [])) > 5:
        print(f"       ... and {len(report['top_20_by_views']) - 5} more (see report JSON)")

    print(f"\n  BOTTOM 20 BY SEO SCORE — SUGGESTED IMPROVEMENTS:")
    for i, imp in enumerate(improvements[:10], 1):
        delta = imp["score_delta"]
        delta_str = f"+{delta}" if delta >= 0 else str(delta)
        print(f"  {i:2}. Score: {imp['original']['seo_score']:3} -> {imp['improved']['seo_score']:3} ({delta_str}) | {imp['views']:>6,} views")
        print(f"      OLD: {imp['original']['title'][:62]}")
        print(f"      NEW: {imp['improved']['title'][:62]}")
    if len(improvements) > 10:
        print(f"       ... and {len(improvements) - 10} more (see report JSON)")

    print(f"\n  RECOMMENDATIONS:")
    for i, rec in enumerate(report.get("recommendations", [])[:5], 1):
        print(f"  {i}. {rec[:90]}")

    if args.apply:
        ar = report.get("apply_results", {})
        print(f"\n  APPLIED UPDATES: {ar.get('updated', 0)} updated, {ar.get('failed', 0)} failed")
    else:
        print(f"\n  MODE: DRY RUN (no changes made)")
        print(f"  To apply updates: python tools/seo_optimizer.py --channel {args.channel} --apply")

    print(f"\n  Full report: {report_path}")
    print("=" * 70 + "\n")


if __name__ == "__main__":
    main()
