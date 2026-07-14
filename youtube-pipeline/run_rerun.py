"""
Robust YouTube Pipeline Runner - Production-grade daily pipeline.
Generates long-form videos + Shorts for 5 YouTube channels daily.
Features: PID lockfile, GPU music, SEO optimization, thumbnails, auto-upload.
"""
# SSL fix: use Windows system cert store (certifi bundle is outdated)
try:
    import truststore; truststore.inject_into_ssl()
except ImportError:
    pass
import sys
import os
import time
import subprocess
import traceback
import gc
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from run_pipeline import run_channel as _run_channel

# Force GPU for MusicGen BEFORE any torch import
os.environ["MUSICGEN_DEVICE"] = "cuda"
os.environ["FOR_DISABLE_CONSOLE_CTRL_HANDLER"] = "1"
os.environ["MKL_THREADING_LAYER"] = "GNU"

ROOT = Path(__file__).parent


def _clean_short_title(hook_text: str, fallback_title: str) -> str:
    """Build an upload-safe Shorts title from a spoken hook.

    The writer's hooks are teasers and frequently end on an ellipsis
    ("Most scientists got this wrong: Black holes are..."). metadata_gate
    blocks any title ending in "..." (it reads as generator truncation), so
    those Shorts were rendered in full and then dropped at the upload step.

    Guarantees: no trailing ellipsis, no mid-word cut, no dangling
    conjunction, and total length (incl. the " #Shorts" suffix) <= 100.
    """
    SUFFIX = " #Shorts"
    BUDGET = 100 - len(SUFFIX)

    t = " ".join(str(hook_text or "").split())
    # Normalise unicode punctuation the gate/YouTube dislike.
    for _bad, _good in ((chr(0x2026), "..."), (chr(0x2018), "'"), (chr(0x2019), "'"),
                        (chr(0x201C), '"'), (chr(0x201D), '"'),
                        (chr(0x2014), " - "), (chr(0x2013), "-")):
        t = t.replace(_bad, _good)
    t = " ".join(t.split())

    # Strip the teaser ellipsis (and any trailing dangling punctuation).
    t = t.rstrip()
    while t.endswith("..") or t.endswith("."):
        t = t[:-1].rstrip()
    t = t.rstrip(",;:-–— ").strip()

    # Word-boundary truncation to the budget.
    if len(t) > BUDGET:
        cut = t[:BUDGET + 1]
        sp = cut.rfind(" ")
        t = (cut[:sp] if sp > 0 else t[:BUDGET]).rstrip(",;:-. ")

    # A title ending on a conjunction/preposition reads as truncated too.
    _DANGLING = {"and", "or", "but", "the", "a", "an", "of", "to", "in", "on",
                 "for", "with", "that", "this", "is", "are", "was", "were",
                 "into", "at", "by", "from", "as", "it", "its"}
    words = t.split()
    while words and words[-1].lower().strip(",;:-") in _DANGLING:
        words.pop()
    t = " ".join(words).rstrip(",;:-. ").strip()

    if not t:
        t = " ".join(str(fallback_title or "Shorts").split())[:BUDGET].strip()

    return f"{t}{SUFFIX}"[:100]


LOG = ROOT / "logs" / "rerun_robust.log"
LOCK_FILE = ROOT / "logs" / "pipeline.lock"
FAILED_QUEUE_FILE = ROOT / "state" / "failed_uploads.json"  # persistent upload retry queue
LOG.parent.mkdir(parents=True, exist_ok=True)

# Daily log rotation Ã¢â‚¬â€ keep last 14 days of logs
def _rotate_log():
    if LOG.exists() and LOG.stat().st_size > 0:
        today = datetime.now().strftime("%Y%m%d")
        archive = LOG.parent / f"pipeline_{today}.log"
        if not archive.exists():
            import shutil
            shutil.copy2(str(LOG), str(archive))
            LOG.write_text("")  # clear main log for today
    # Prune old archives (keep 14 days)
    for old_log in sorted(LOG.parent.glob("pipeline_*.log"))[:-14]:
        try:
            old_log.unlink()
        except Exception:
            pass

_rotate_log()


# === PREMIUM_REVENUE_PATCH === BEGIN imports
try:
  from pipeline.trend_hunter import TrendHunter as _TrendHunter
except Exception as _e:
  _TrendHunter = None
  print(f"[premium] trend_hunter import failed: {_e}", flush=True)
try:
  from pipeline.affiliate_injector import AffiliateInjector as _AffiliateInjector
except Exception as _e:
  _AffiliateInjector = None
  print(f"[premium] affiliate_injector import failed: {_e}", flush=True)
try:
  from pipeline.syndicator import Syndicator as _Syndicator
except Exception as _e:
  _Syndicator = None
  print(f"[premium] syndicator import failed: {_e}", flush=True)

_AFFILIATE = None
def _get_affiliate():
  global _AFFILIATE
  if _AFFILIATE is None and _AffiliateInjector is not None:
    try:
      _AFFILIATE = _AffiliateInjector(str(ROOT / "config" / "affiliate.json"))
    except Exception as _e:
      print(f"[premium] AffiliateInjector init failed: {_e}", flush=True)
  return _AFFILIATE

def _prefetch_trends(channels):
  """Run TrendHunter for every distinct niche and write state/trends_{niche}.txt seed prompts."""
  if _TrendHunter is None:
    return
  state = ROOT / "state"
  state.mkdir(exist_ok=True)
  cache = ROOT / "cache" / "trends"
  cache.mkdir(parents=True, exist_ok=True)
  hunter = _TrendHunter(cache_dir=str(cache), cache_ttl_hours=6)
  niches = sorted({c.get("niche") for c in channels if c.get("niche")})
  for niche in niches:
    try:
      topics = hunter.get_trending_topics(niche, max_topics=10)
      seed = hunter.to_ollama_seed(topics)
      (state / f"trends_{niche}.txt").write_text(seed, encoding="utf-8")
      log(f"  trends prefetched: {niche} ({len(topics)} topics)")
    except Exception as _e:
      log(f"  trends prefetch failed for {niche}: {_e}")

def _prefetch_winners(channels, global_cfg):
  """Pull each channel's best-performing recent videos and write
  state/winners_{niche}.txt so generate_topic can double down on the angles and
  formats that already earn the most views. Best-effort: any channel that errors
  (quota, no videos, auth) is simply skipped — generation is unaffected."""
  try:
    from uploaders.youtube_uploader import YouTubeUploader
  except Exception as _e:
    log(f"  winners prefetch: uploader import failed: {_e}")
    return
  state = ROOT / "state"
  state.mkdir(exist_ok=True)
  try:
    up = YouTubeUploader(global_cfg)
  except Exception as _e:
    log(f"  winners prefetch: uploader init failed: {_e}")
    return
  for ch in channels:
    ch_id = ch.get("id"); niche = ch.get("niche", "")
    if not ch_id or not niche:
      continue
    try:
      vids = up.get_recent_video_analytics(ch_id, max_results=25) or []
    except Exception as _e:
      log(f"  winners: {ch_id} analytics fetch failed: {_e}")
      continue
    scored = []
    for v in vids:
      views = int(v.get("views", 0) or 0)
      engagement = 3 * int(v.get("likes", 0) or 0) + 5 * int(v.get("comments", 0) or 0)

      # Weight views by WATCH-THROUGH, not raw views.
      #
      # This used to be `score = views + engagement`, which is a bad teacher:
      # a Short with 500 views at 20% watched is a thumbnail that oversold,
      # while one with 300 views at 75% genuinely landed. Ranking on raw views
      # makes the topic writer imitate the first. avp now comes back from
      # get_recent_video_analytics().
      #
      # avp routinely exceeds 100% on Shorts -- that is LOOPING (a viewer
      # watched it 2-6x), which is the strongest Shorts signal there is. So we
      # credit above-100% rather than clipping it, but damp it so one freak
      # looper can't swamp the ranking.
      #
      # avp == 0 means the Analytics enrichment was unavailable (quota/auth/new
      # channel), NOT that nobody watched -- in that case fall back to the old
      # view-only behaviour rather than zeroing every candidate.
      avp = float(v.get("avp", 0.0) or 0.0)
      if avp > 0:
        quality = min(avp, 100.0) / 100.0 + max(0.0, avp - 100.0) / 100.0 * 0.35
        score = views * quality + engagement
      else:
        score = views + engagement

      if v.get("title") and views >= 1:
        scored.append((score, views, v.get("title")))
    scored.sort(key=lambda x: x[0], reverse=True)
    # 2026-07-11: separate Shorts from long-form. A mixed top-4 was 100% Shorts
    # on every channel (Shorts out-view longs 3-50x here), so LONG-FORM topic
    # generation was learning exclusively from Shorts hooks.
    _is_short = lambda t: "#short" in str(t).lower()
    top_longs  = [x for x in scored if not _is_short(x[2])][:3]
    top_shorts = [x for x in scored if _is_short(x[2])][:3]
    top = top_longs + top_shorts or scored[:4]
    if not top:
      continue
    _sections = []
    if top_longs:
      _sections.append("Best LONG-FORM videos (mine these angles for the MAIN video):\n"
                       + "\n".join(f"- {t} ({vw:,} views)" for _s, vw, t in top_longs))
    if top_shorts:
      _sections.append("Best SHORTS (imitate these hook styles for shorts_hooks ONLY):\n"
                       + "\n".join(f"- {t} ({vw:,} views)" for _s, vw, t in top_shorts))
    lines = "\n\n".join(_sections)
    seed = (
      "YOUR BEST-PERFORMING VIDEOS on this channel so far (highest views + engagement). "
      "Study WHY they worked — the format, the angle, the emotional hook — and create a NEW "
      "topic in the SAME proven vein but on a clearly DIFFERENT specific subject. "
      "Never reuse these exact titles:\n" + lines
    )
    try:
      (state / f"winners_{niche}.txt").write_text(seed, encoding="utf-8")
      log(f"  winners prefetched: {niche} (top {len(top)}, best={top[0][1]:,} views)")
    except Exception as _e:
      log(f"  winners write failed for {niche}: {_e}")
    # Thumbnail A/B: learn the best-performing style for this channel from CTR.
    try:
      from pipeline import thumb_ab as _thumb_ab
      _hist = _thumb_ab._read_log(str(ROOT), ch_id).get("history", [])
      _vids = [e.get("video_id") for e in _hist if e.get("video_id")][-50:]
      if _vids:
        _ctr = up.get_video_ctr(ch_id, _vids)
        if _ctr:
          _win, _summ = _thumb_ab.compute_winner(str(ROOT), ch_id, _ctr)
          if _win:
            log(f"  thumb A/B winner [{ch_id}]: {_thumb_ab.STYLE_NAMES.get(_win, _win)} (CTR by style: {_summ['means']})")
    except Exception as _te:
      log(f"  thumb A/B winner calc failed ({ch_id}): {_te}")

def _inject_affiliates(description, niche):
  inj = _get_affiliate()
  if inj is None or not niche:
    return description
  try:
    return inj.inject_into_description(description, niche)
  except Exception as _e:
    log(f"  affiliate inject failed: {_e}")
    return description

def _run_syndicator(channel, ch_id, niche, channel_name, shorts_dir=None):
  if _Syndicator is None:
    return
  try:
    # Use the dated shorts dir passed from the caller; fall back to legacy path.
    if shorts_dir is None:
      today = datetime.now().strftime("%Y%m%d")
      shorts_dir = ROOT / "output" / ch_id / today / "shorts"
    shorts_dir = Path(shorts_dir)
    if not shorts_dir.exists():
      log(f"  {ch_id}: syndicator skipped â€” shorts_dir not found: {shorts_dir}")
      return
    syn = _Syndicator(output_root=str(ROOT / "syndicate"))
    results = syn.package_channel(
      channel_id=ch_id,
      shorts_dir=str(shorts_dir),
      niche=niche,
      channel_name=channel_name,
      scripts_dir=str(shorts_dir) if shorts_dir.exists() else None,
    )
    log(f"  {ch_id}: syndicator packaged {len(results)} shorts -> syndicate/{ch_id}/")
  except Exception as _e:
    log(f"  {ch_id}: syndicator failed: {_e}")
# === PREMIUM_REVENUE_PATCH === END imports


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    # Windows console may be cp1252 â€” encode safely so emoji/Unicode never crash the run
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        print(line.encode("ascii", errors="replace").decode("ascii"), flush=True)


def acquire_lock():
    """Acquire PID lockfile. Returns True if acquired, False if another instance is running."""
    if LOCK_FILE.exists():
        try:
            old_pid = int(LOCK_FILE.read_text(encoding="utf-8").strip())
            # Check if PID is still alive without psutil dependency
            import ctypes
            kernel32 = ctypes.windll.kernel32
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, old_pid)
            if handle:
                kernel32.CloseHandle(handle)
                log(f"  ABORT: Pipeline already running (PID {old_pid})")
                return False
            else:
                log(f"  Stale lockfile from PID {old_pid} (dead) - cleaning up")
        except Exception:
            log(f"  Stale lockfile (unreadable) - cleaning up")
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    LOCK_FILE.unlink(missing_ok=True)


def ensure_ollama():
    import requests
    for attempt in range(3):
        try:
            r = requests.get("http://localhost:11434/api/tags", timeout=5)
            if r.status_code == 200:
                log(f"  Ollama OK ({len(r.json().get('models', []))} models loaded)")
                return True
        except Exception:
            pass
        log(f"  Ollama not responding (attempt {attempt+1}/3), restarting...")
        try:
            subprocess.Popen(
                ["cmd", "/c", "start", "", "C:\\Users\\markm\\AppData\\Local\\Programs\\Ollama\\ollama app.exe"],
                shell=False
            )
            time.sleep(8)
        except Exception as e:
            log(f"  Failed to start Ollama: {e}")
    log("  WARNING: Ollama could not be started after 3 attempts")
    return False


def ensure_fooocus():
    """Check if Fooocus-API is running on :7865 with /v1/ endpoint; auto-start if not.

    Launches C:\\Users\\markm\\Fooocus-API\\main.py as a detached background process and
    waits up to 180s for the /v1/ endpoint to become live. Returns True on success.
    """
    import requests
    import sys as _sys

    FOOOCUS_API_DIR = Path("C:/Users/markm/Fooocus-API")
    PYTHON_EXE = r"C:\Users\markm\AppData\Local\Programs\Python\Python310\python.exe"

    def _probe():
        try:
            r = requests.get("http://localhost:7865/docs", timeout=5)
            return r.status_code == 200
        except Exception:
            return False

    if _probe():
        log("  Fooocus-API OK (already running)")
        return True

    # Not running - try to start it
    if not FOOOCUS_API_DIR.exists():
        log(f"  WARNING: Fooocus-API not found at {FOOOCUS_API_DIR}")
        log(f"  Images will use gradient placeholders")
        return False

    out_log = ROOT / "logs" / "fooocus_api.out.log"
    err_log = ROOT / "logs" / "fooocus_api.err.log"
    log(f"  Starting Fooocus-API from {FOOOCUS_API_DIR}...")

    try:
        # DETACHED_PROCESS=0x8, CREATE_NEW_PROCESS_GROUP=0x200, CREATE_NO_WINDOW=0x08000000
        creationflags = 0x08 | 0x200 | 0x08000000 if _sys.platform == "win32" else 0
        subprocess.Popen(
            [PYTHON_EXE, "main.py", "--host", "127.0.0.1", "--port", "7865", "--skip-pip"],
            cwd=str(FOOOCUS_API_DIR),
            stdout=open(out_log, "a", encoding="utf-8", errors="replace"),
            stderr=open(err_log, "a", encoding="utf-8", errors="replace"),
            creationflags=creationflags,
            close_fds=True,
        )
    except Exception as e:
        log(f"  Failed to start Fooocus-API: {e}")
        return False

    # Wait up to 180s for /v1/ endpoint (model loading takes 60-120s on first run)
    for i in range(36):
        time.sleep(5)
        if _probe():
            log(f"  Fooocus-API ready after {(i+1)*5}s")
            return True
        if (i + 1) % 6 == 0:
            log(f"  Waiting for Fooocus-API to finish loading models ({(i+1)*5}s elapsed)...")

    log("  WARNING: Fooocus-API did not respond within 180s; continuing with placeholders")
    return False


def _load_failed_queue() -> list:
    """Load the persistent failed-upload queue from disk."""
    try:
        if FAILED_QUEUE_FILE.exists():
            return json.loads(FAILED_QUEUE_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return []


def _save_failed_queue(queue: list) -> None:
    """Persist the failed-upload queue to disk."""
    try:
        FAILED_QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
        FAILED_QUEUE_FILE.write_text(json.dumps(queue, indent=2), encoding="utf-8")
    except Exception as e:
        log(f"  [RETRY-Q] Failed to save queue: {e}")


def _enqueue_failed_upload(entry: dict) -> None:
    """Add a failed upload to the persistent retry queue."""
    q = _load_failed_queue()
    # Avoid duplicates: match on video_path
    if not any(e.get("video_path") == entry.get("video_path") for e in q):
        entry["failed_at"] = datetime.now().isoformat()
        entry["retry_count"] = 0
        q.append(entry)
        _save_failed_queue(q)
        log(f"  [RETRY-Q] Queued for retry: {entry.get('video_path', '?')}")


def _retry_failed_uploads(uploader) -> int:
    """Re-attempt uploads from the persistent queue. Returns count of successful retries."""
    q = _load_failed_queue()
    if not q:
        return 0
    log(f"  [RETRY-Q] {len(q)} pending retries from previous runs")
    survived = []
    retried = 0
    for entry in q:
        video_path = entry.get("video_path", "")
        if not video_path or not Path(video_path).exists():
            log(f"  [RETRY-Q] Skipping (file gone): {video_path}")
            continue  # file deleted after cleanup -- skip
        if entry.get("retry_count", 0) >= 3:
            log(f"  [RETRY-Q] Max retries (3) reached, dropping: {Path(video_path).name}")
            continue
        try:
            # Strip queue-bookkeeping keys — they are NOT upload_video() kwargs.
            # (last_error is added below when a retry fails and would otherwise
            # be re-passed on the next run, raising "unexpected keyword argument".)
            _meta_keys = ("failed_at", "retry_count", "last_error")
            result = uploader.upload_video(**{k: v for k, v in entry.items()
                                             if k not in _meta_keys})
            vid_id = result.get("video_id", "?")
            log(f"  [RETRY-Q] SUCCESS: {Path(video_path).name} -> https://youtube.com/watch?v={vid_id}")
            retried += 1
        except Exception as e:
            log(f"  [RETRY-Q] Retry failed: {Path(video_path).name}: {e}")
            entry["retry_count"] = entry.get("retry_count", 0) + 1
            entry["last_error"] = str(e)
            survived.append(entry)
    _save_failed_queue(survived)
    return retried


def _clear_gpu_memory():
    """Release CUDA memory and Python-level model caches between pipeline runs.

    Called after every channel in PHASE 1 and PHASE 2 to prevent VRAM accumulation
    across 5 channels (root cause of the known OOM crash after the finance channel's
    long FFmpeg pass fills VRAM with LTX model weights + video buffers simultaneously).
    """
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            freed = torch.cuda.memory_reserved() - torch.cuda.memory_allocated()
            log(f"  [GPU] CUDA cache cleared â€” {freed / 1024**2:.0f} MB reserved freed")
    except Exception as e:
        log(f"  [GPU] torch.cuda.empty_cache() skipped: {e}")

    # Also ask Python's garbage collector to collect cyclic refs from model objects
    try:
        import gc
        gc.collect()
    except Exception:
        pass


def _purge_module_caches():
    """Evict model caches from pipeline submodules that persist across channels."""
    import importlib
    heavy_modules = [
        "pipeline.ltx_animator",
        "pipeline.audiocraft_music",
        "pipeline.kokoro_tts",
        "pipeline.fooocus_images",
        "pipeline.caption_generator",
    ]
    for mod_name in heavy_modules:
        mod = sys.modules.get(mod_name)
        if mod is None:
            continue
        # Clear any module-level _MODEL_CACHE, _model, _instance etc.
        for attr in list(vars(mod).keys()):
            if attr.startswith("_") and any(k in attr.lower() for k in ("cache", "model", "instance", "loaded")):
                try:
                    setattr(mod, attr, None)
                except Exception:
                    pass
    gc.collect()


def restart_fooocus():
    """Force-restart Fooocus-API to clear CUDA state. Blocks until /v1/ responds or 180s timeout.

    Used when image generation returns placeholders due to GPU state corruption
    (e.g., 'CUDA error: unspecified launch failure' after MusicGen runs on the same GPU).
    """
    import requests
    import sys as _sys

    log("  Restarting Fooocus-API (clearing CUDA state)...")

    # Kill any process listening on :7865
    try:
        # Windows: find PIDs bound to 7865 and terminate them
        result = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True, timeout=10
        )
        pids_to_kill = set()
        for line in result.stdout.splitlines():
            if ":7865" in line and "LISTENING" in line:
                parts = line.split()
                if parts and parts[-1].isdigit():
                    pids_to_kill.add(parts[-1])
        for pid in pids_to_kill:
            try:
                subprocess.run(
                    ["taskkill", "/F", "/PID", pid],
                    capture_output=True, timeout=10
                )
                log(f"    Killed Fooocus-API PID {pid}")
            except Exception as e:
                log(f"    Failed to kill PID {pid}: {e}")
    except Exception as e:
        log(f"    netstat lookup failed: {e}")

    time.sleep(3)

    # Now ensure_fooocus() will fresh-start it
    return ensure_fooocus()


def generate_images_with_retry(
    img_gen_factory,
    sections,
    img_dir,
    style_preset,
    max_attempts=2,
    negative_prompt=None,
):
    """Generate section images with automatic Fooocus restart on placeholder fallback.

    Returns (image_files, real_count). Each retry kills Fooocus-API and starts a fresh
    instance to clear any corrupted CUDA state. If all attempts still produce placeholders,
    the caller should treat the channel as FAILED rather than shipping a video with
    placeholder images.

    `negative_prompt` (optional) is a niche-specific negative prompt string from
    `niche_prompts.json` â€” forwarded to Fooocus to suppress watermarks, deformed
    anatomy, cartoon aesthetic, etc.
    """
    image_files = []
    real_count = 0
    total = len(sections)

    for attempt in range(1, max_attempts + 1):
        img_gen = img_gen_factory()
        kwargs = {"style_preset": style_preset}
        if negative_prompt:
            kwargs["negative_prompt"] = negative_prompt
        image_files = img_gen.generate_section_images(sections, img_dir, **kwargs)
        real_count = sum(
            1 for f in image_files
            if os.path.exists(f["path"]) and os.path.getsize(f["path"]) > 10000
        )
        if real_count == total:
            log(f"  Images: {total} generated ({real_count} real, 0 placeholder) [attempt {attempt}]")
            return image_files, real_count

        log(
            f"  Images attempt {attempt}: {real_count}/{total} real, "
            f"{total - real_count} placeholder â€” Fooocus-API likely unhealthy"
        )
        if attempt < max_attempts:
            restart_fooocus()

    log(f"  Images FINAL: {real_count}/{total} real after {max_attempts} attempts")
    return image_files, real_count


def check_channel_tokens(global_cfg, channel_ids):
    """Pre-flight check of YouTube OAuth tokens for each channel.

    Returns dict mapping channel_id -> status ('ok', 'expired', 'missing', 'revoked').
    Logs findings. Does NOT abort the run â€” generation proceeds even if uploads will fail,
    so content accumulates for a post-reauth bulk upload via run_upload.py.
    """
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

    tokens_dir = ROOT / global_cfg.get("youtube_tokens_dir", "./config/tokens/")
    status = {}
    log("Checking YouTube OAuth tokens...")
    for ch_id in channel_ids:
        tf = tokens_dir / f"{ch_id}_token.json"
        if not tf.exists():
            status[ch_id] = "missing"
            log(f"  [TOKEN] {ch_id}: MISSING â€” run `python auth_manual.py {ch_id}`")
            continue
        try:
            creds = Credentials.from_authorized_user_file(str(tf))
            if creds.valid:
                status[ch_id] = "ok"
                log(f"  [TOKEN] {ch_id}: valid (expires {creds.expiry})")
                continue
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    tf.write_text(creds.to_json())
                    status[ch_id] = "ok"
                    log(f"  [TOKEN] {ch_id}: refreshed (new expiry {creds.expiry})")
                    continue
                except Exception as e:
                    msg = str(e).lower()
                    if "revoked" in msg or "invalid_grant" in msg:
                        status[ch_id] = "revoked"
                        log(
                            f"  [TOKEN] {ch_id}: REVOKED â€” Google rejected refresh token. "
                            f"Run `python auth_manual.py {ch_id}` to re-authenticate."
                        )
                    else:
                        status[ch_id] = "expired"
                        log(f"  [TOKEN] {ch_id}: refresh error â€” {type(e).__name__}: {str(e)[:100]}")
                    continue
            status[ch_id] = "expired"
            log(f"  [TOKEN] {ch_id}: expired and no refresh_token")
        except Exception as e:
            status[ch_id] = "error"
            log(f"  [TOKEN] {ch_id}: load error â€” {type(e).__name__}: {str(e)[:100]}")

    bad = [c for c, s in status.items() if s != "ok"]
    if bad:
        log(f"  [TOKEN] {len(bad)}/{len(channel_ids)} channels need re-auth: {', '.join(bad)}")
        log(f"  [TOKEN] Content will still be generated; run `python reauth_all.py` then `python run_upload.py` to publish.")
    else:
        log(f"  [TOKEN] All {len(channel_ids)} channels authenticated and ready to upload.")
    return status


# Per-niche CTA templates - tied to the channel's specific value proposition.
# ASCII-safe on purpose: emoji here were mojibake'd once already by a
# cp1252/UTF-8 round trip and shipped broken to live descriptions.
_NICHE_CTA = {
    "tech_ai": (
        "Subscribe to {ch_name} for daily AI tool breakdowns, automation shortcuts, "
        "and strategies that save you hours every week - new video every single day.\n"
        "Comment below: which AI tool do you want me to cover next?"
    ),
    "finance": (
        "Subscribe to {ch_name} for daily investing strategies, personal finance insights, "
        "and money moves that actually work - posted every morning.\n"
        "Tell me below: what's your #1 financial goal right now?"
    ),
    "motivation": (
        "Subscribe to {ch_name} for daily motivation, mindset shifts, and the "
        "productivity habits that top performers actually use - new video every day.\n"
        "Comment your biggest goal for this week - I read every single one."
    ),
    "history": (
        "Subscribe to {ch_name} for daily deep dives into untold history, "
        "forgotten mysteries, and the events that shaped our world.\n"
        "Which historical mystery do you want me to cover next?"
    ),
    "history_mystery": (
        "Subscribe to {ch_name} for daily deep dives into untold history, "
        "forgotten mysteries, and the events that shaped our world.\n"
        "Which historical mystery do you want me to cover next?"
    ),
    "science": (
        "Subscribe to {ch_name} for daily mind-blowing science facts, experiments, "
        "and discoveries that will change the way you see the world.\n"
        "What science topic blew your mind recently? Comment below!"
    ),
    "science_facts": (
        "Subscribe to {ch_name} for daily mind-blowing science facts, experiments, "
        "and discoveries that will change the way you see the world.\n"
        "What science topic blew your mind recently? Comment below!"
    ),
}

_DEFAULT_CTA = (
    "Subscribe to {ch_name} for daily videos - new content every single day.\n"
    "What would you like to see next? Drop a comment below!"
)

_NICHE_DISCLAIMER = {
    "finance": (
        "DISCLAIMER: This video is for educational and entertainment purposes only "
        "and does not constitute financial, investment, or legal advice. Always do your own "
        "research and consult a qualified financial advisor before making any investment decisions."
    ),
}

# Channel-posted engagement question seeded as a comment on every upload.
# (The Data API can't PIN it, but a channel-owner comment still surfaces and
# kick-starts the comment activity the algorithm reads as engagement.)
_ENGAGEMENT_Q = {
    "tech_ai": "Which AI tool should I break down next? 👇",
    "finance": "What's your #1 money goal right now? Drop it below 👇",
    "motivation": "What's the ONE goal you're chasing this week? 👇",
    "history": "Which mystery should I cover next? 👇",
    "history_mystery": "Which mystery should I cover next? 👇",
    "science": "What science fact blew your mind recently? 👇",
    "science_facts": "What science fact blew your mind recently? 👇",
}
_ENGAGEMENT_Q_DEFAULT = "What should I cover next? Drop your idea below 👇"


def build_description(script_data, channel, audio_sections, seo_data=None):
    """Build a YouTube description with timestamps, per-niche CTA, and hashtags."""
    title = (seo_data or {}).get("optimized_title", script_data.get("title", ""))
    description = (seo_data or {}).get("optimized_description", script_data.get("description", title))
    # Safety: coerce description to string if Ollama returned a dict/list
    if not isinstance(description, str):
        description = str(description) if description else title
    sections = script_data.get("script_sections", [])
    hashtags = (seo_data or {}).get("hashtags", [])
    ch_name = channel.get("youtube_channel_title") or channel["name"]
    niche = channel.get("niche", "")

    # Use SEO-optimised description up to 800 chars (first 125 are visible in search)
    # Pull out any CHAPTERS/TIMESTAMPS block BEFORE truncating, then re-append it.
    # run_pipeline appends chapters to the END of a 1200-2200 char description, so
    # description[:800] was amputating them -- every long-form shipped with NO chapters.
    import re as _re_cd
    _cm = _re_cd.search(
        r"(?im)^[^\n]{0,3}(?:CHAPTERS|TIMESTAMPS)\s*:\s*\n"
        r"(?:\s*\d{1,2}:\d{2}(?::\d{2})?[^\n]*\n?)+", description)
    _chap_block = _cm.group(0).strip() if _cm else ""
    _body = (description[:_cm.start()] + description[_cm.end():]).strip() if _cm else description
    parts = [_body[:800] if len(_body) > 800 else _body, ""]
    if _chap_block:
        parts += [_chap_block, ""]

    # Timestamps from actual audio durations. Only REAL section titles are
    # emitted ("Section 3" placeholders are dropped), and if the SEO
    # description already contains a chapter block we do not add a second
    # one (doubled blocks shipped live on 2026-06/07 uploads).
    _head_lower = (description or "").lower()
    _already_has_chapters = ("chapters:" in _head_lower) or ("timestamps:" in _head_lower)
    if audio_sections and sections and not _already_has_chapters:
        chapter_lines = []
        cumulative = 0.0
        for a in audio_sections:
            sec_title = str(a.get("section_title") or "").strip()
            _tw = sec_title.split()
            _generic = (not sec_title) or (
                len(_tw) == 2
                and _tw[0].lower() in ("section", "part", "chapter")
                and _tw[1].rstrip(":.").isdigit()
            )
            mins = int(cumulative // 60)
            secs = int(cumulative % 60)
            if not _generic:
                chapter_lines.append(f"{mins:02d}:{secs:02d} {sec_title}")
            try:
                dur = _get_audio_duration(a["path"])
                cumulative += dur
            except Exception:
                cumulative += 90  # estimate 90s per section if ffprobe unavailable
        # YouTube needs a 0:00 first chapter and 3+ entries to render chapters
        if len(chapter_lines) >= 3 and chapter_lines[0].startswith("00:00"):
            parts.append("CHAPTERS:")
            parts.extend(chapter_lines)
            parts.append("")

    # Per-niche CTA (specific benefit promise, not generic)
    cta_template = _NICHE_CTA.get(niche, _DEFAULT_CTA)
    parts.append(cta_template.format(ch_name=ch_name))
    parts.append("")

    # Niche-specific disclaimer (finance, etc.)
    disclaimer = _NICHE_DISCLAIMER.get(niche)
    if disclaimer:
        parts.append(disclaimer)
        parts.append("")

    # Hashtags â€” prefer SEO-generated, fall back to base tags
    if hashtags:
        # Filter and format: ensure each starts with #, take best 5
        ht_formatted = []
        for h in hashtags[:5]:
            if isinstance(h, str) and h.strip():
                ht = h.strip()
                if not ht.startswith("#"):
                    ht = f"#{ht}"
                ht_formatted.append(ht.replace(" ", ""))
        if ht_formatted:
            parts.append(" ".join(ht_formatted))
    else:
        tags = channel.get("tags_base", [])[:5]
        if tags:
            parts.append(" ".join(f"#{t.replace(' ', '')}" for t in tags))

    return "\n".join(parts)[:5000]


RETENTION_DAYS = 14   # keep audio/ + images/ this long so a late-discovered bug is REPAIRABLE


def _prune_old_outputs(days: int = RETENTION_DAYS):
    """Delete output/<channel>/<date>/ dirs older than `days`.

    We now KEEP the raw narration wavs + source images after upload so a systemic bug
    found weeks later can be repaired in place (see _cleanup_channel_output). This prune
    is what stops that retention from eating the disk.
    """
    import shutil as _sh
    from datetime import datetime as _dt, timedelta as _td
    cutoff = _dt.now() - _td(days=days)
    freed = 0
    root = ROOT / "output"
    if not root.exists():
        return
    for ch_dir in root.iterdir():
        if not ch_dir.is_dir():
            continue
        for day_dir in ch_dir.iterdir():
            if not day_dir.is_dir() or not day_dir.name.isdigit() or len(day_dir.name) != 8:
                continue
            try:
                d = _dt.strptime(day_dir.name, "%Y%m%d")
            except ValueError:
                continue
            if d >= cutoff:
                continue
            try:
                size = sum(f.stat().st_size for f in day_dir.rglob("*") if f.is_file())
                _sh.rmtree(day_dir)
                freed += size
            except Exception as e:
                log(f"  [PRUNE] could not remove {day_dir}: {e}")
    if freed:
        log(f"  [PRUNE] removed outputs older than {days}d ({freed / 1024**3:.2f} GB)")


def _cleanup_channel_output(ch_output: Path, ch_id: str, keep_metadata: bool = True):
    """Delete all large generated files for a channel's daily output after successful upload.

    Keeps small metadata files (script.json, seo.json, checkpoint.json, upload_result.json)
    so the pipeline's resume and analytics systems still work. Deletes:
      - All .mp4 video files (long-form, enhanced, captioned, final, intro, outro)
      - All .srt caption files (already uploaded to YouTube)
      - audio/ directory (WAV files â€” largest disk consumer)
      - images/ directory (PNG source frames)
      - Thumbnail images (uploaded; no longer needed locally)
      - shorts/ directory contents (each file deleted after individual upload)

    Parameters
    ----------
    ch_output : Path
        The channel's daily output directory, e.g. output/tech-ai/20260503/
    ch_id : str
        Pipeline channel id (e.g. 'tech-ai') â€” used for log messages.
    keep_metadata : bool
        If True (default), preserve .json metadata files. Set False to wipe everything.
    """
    if not ch_output.exists():
        return

    import shutil
    freed_bytes = 0

    # -- 1. Delete all .mp4 files in the top-level output dir --
    for mp4 in list(ch_output.glob("*.mp4")):
        try:
            size = mp4.stat().st_size
            mp4.unlink()
            freed_bytes += size
            log(f"  [CLEANUP] Deleted {mp4.name} ({size / 1024**2:.1f} MB)")
        except Exception as e:
            log(f"  [CLEANUP] Could not delete {mp4.name}: {e}")

    # -- 2. Delete .srt caption files (already pushed to YouTube) --
    for srt in list(ch_output.glob("*.srt")):
        try:
            size = srt.stat().st_size
            srt.unlink()
            freed_bytes += size
            log(f"  [CLEANUP] Deleted {srt.name}")
        except Exception as e:
            log(f"  [CLEANUP] Could not delete {srt.name}: {e}")

    # -- 3. Delete thumbnail images (uploaded to YouTube already) --
    for thumb in list(ch_output.glob("thumbnail*.jpg")) + list(ch_output.glob("thumbnail*.png")):
        try:
            size = thumb.stat().st_size
            thumb.unlink()
            freed_bytes += size
            log(f"  [CLEANUP] Deleted {thumb.name}")
        except Exception as e:
            log(f"  [CLEANUP] Could not delete {thumb.name}: {e}")

    # -- 4. audio/ -- KEEP the raw narration WAVs (RETENTION_DAYS window).
    # These are the ONLY thing that makes a systemic audio bug repairable without
    # regenerating scripts/voice. When the sound_design [narr] bug muted narration for
    # ~10 weeks, every affected video was UNRECOVERABLE precisely because this
    # directory had already been rmtree'd. The 12 Jul batch survived only by luck, and
    # that is the sole reason it could be repaired. Raw section wavs are small
    # (~25 MB/channel); the 450 MB mp4s above are the real disk hog and still go.
    # Only the derived enhanced/ copies are dropped here; _prune_old_outputs() bounds disk.
    audio_dir = ch_output / "audio"
    enhanced_dir = audio_dir / "enhanced"
    if enhanced_dir.exists():
        try:
            size = sum(f.stat().st_size for f in enhanced_dir.rglob("*") if f.is_file())
            shutil.rmtree(enhanced_dir)
            freed_bytes += size
            log(f"  [CLEANUP] Deleted audio/enhanced/ ({size / 1024**2:.1f} MB); kept raw narration wavs")
        except Exception as e:
            log(f"  [CLEANUP] Could not delete audio/enhanced/: {e}")

    # -- 5. images/ -- KEEP (RETENTION_DAYS window). Together with audio/ this makes a
    # broken video re-assemblable without re-running Ollama/Fooocus/TTS. ~22 MB/channel.
    pass

    # -- 6. Wipe clips/ directory (animated LTX-Video clips) --
    clips_dir = ch_output / "clips"
    if clips_dir.exists():
        try:
            size = sum(f.stat().st_size for f in clips_dir.rglob("*") if f.is_file())
            shutil.rmtree(clips_dir)
            freed_bytes += size
            log(f"  [CLEANUP] Deleted clips/ ({size / 1024**2:.1f} MB)")
        except Exception as e:
            log(f"  [CLEANUP] Could not delete clips/: {e}")

    log(f"  [CLEANUP] {ch_id}: freed {freed_bytes / 1024**2:.0f} MB total")


def _cleanup_short_file(sf: Path, ch_id: str):
    """Delete a single Short mp4 + its companion script JSON after successful upload."""
    freed = 0
    for f in [sf, sf.with_suffix(".json"), sf.parent / f"{sf.stem}_script.json"]:
        if f.exists():
            try:
                freed += f.stat().st_size
                f.unlink()
            except Exception as e:
                log(f"  [CLEANUP] Could not delete {f.name}: {e}")
    if freed:
        log(f"  [CLEANUP] Deleted short {sf.name} ({freed / 1024**2:.1f} MB)")


def _get_audio_duration(audio_path):
    """Get duration of an audio file via ffprobe."""
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "json", audio_path],
        capture_output=True, text=True, timeout=30,
    )
    data = json.loads(result.stdout)
    return float(data["format"]["duration"])


def _write_studio_checklist(today, tasks):
    """Write the daily list of manual YouTube Studio actions the Data API can't do
    (pin comment, end screens) plus the off-platform syndication handoff. The
    pipeline already did everything the API allows; this is the ~1-min-per-video
    manual remainder, collected into one file. Returns the path or None."""
    if not tasks:
        return None
    lines = [
        f"# YouTube Studio — manual tasks for {today}",
        "",
        "_Everything the API allows is already done. These steps are Studio-only "
        "(pinning, end screens) or off-platform — roughly 1 minute per video._",
        "",
    ]
    for t in tasks:
        lines.append(f"## {t['channel']}")
        lines.append(f"- Video: {t['url']}")
        lines.append(f"- PIN the comment we posted: \"{t['comment']}\"")
        lines.append("- END SCREEN: add a video element -> your previous upload on this "
                     "channel (best for session watch-time) + a Subscribe element")
        lines.append(f"- SYNDICATE the Shorts to TikTok / Reels / Facebook / X — ready-made "
                     f"clips + captions + hashtags here: `{t['syndicate_dir']}` "
                     f"(see manifest_{today.replace('-', '')}.json)")
        lines.append("")
    path = ROOT / "output" / f"STUDIO_TASKS_{today}.md"
    try:
        path.write_text("\n".join(lines), encoding="utf-8")
        # 2026-07-11: also surface the newest checklist at the repo ROOT —
        # buried in output/<ch>/<date>/ it was never seen, so the 10-minute
        # manual distribution (pin comment, end screens, syndication) never
        # happened even though the assets are packaged daily.
        try:
            (ROOT / "STUDIO_TASKS_TODAY.md").write_text("\n".join(lines), encoding="utf-8")
        except Exception:
            pass
        return str(path)
    except Exception as e:
        log(f"  studio checklist write failed: {e}")
        return None


def main():
    """Main pipeline: generate videos for all channels, then upload."""
    sys.path.insert(0, str(ROOT))

    from analytics.monetization_tracker import MonetizationTracker
    tracker = MonetizationTracker(str(ROOT / "analytics"))

    config = json.loads((ROOT / "config" / "channels.json").read_text(encoding="utf-8"))
    niche_prompts = json.loads((ROOT / "config" / "niche_prompts.json").read_text(encoding="utf-8"))
    global_cfg = config["global"]

    # -- Config-clobber guard (2026-07-11) -----------------------------
    # channels.json was once overwritten by a stale June-23 snapshot
    # (_audit_src copy): LTX back on, B-roll off, wrong ollama model.
    # Fail LOUD if modern keys are missing so a reverted config can
    # never silently degrade a production run.
    _guard_missing = [_k for _k in (
        "ltx_video_enabled", "stock_video_enabled", "tts_speed",
        "images_per_section", "llm_provider",
    ) if _k not in global_cfg]
    if _guard_missing:
        log(f"  ABORT: channels.json looks REVERTED/stale - missing keys: {_guard_missing}")
        log("  Restore config/channels.json from the newest channels.json.bak_* and re-run.")
        sys.exit(3)
    _bad_models = ("llama3.2:3b", "glm4:latest", "qwen3.6:latest", "qwen3.6-smaller:latest", "gemma4:latest")
    if global_cfg.get("ollama_model") in _bad_models:
        log(f"  ABORT: ollama_model={global_cfg.get('ollama_model')!r} is a known-bad/stale model (empty scripts or VRAM wedge).")
        log("  Expected qwen2.5:7b - edit this guard in run_rerun.py if the change is deliberate.")
        sys.exit(3)

    all_channel_ids = [ch["id"] for ch in config["channels"]]
    CHANNELS = sys.argv[1:] if len(sys.argv) > 1 else all_channel_ids

    log(f"=== YOUTUBE EMPIRE DAILY RUN: {CHANNELS} ===")
    try:
        _prune_old_outputs()
    except Exception as _pe:
        log(f"  [PRUNE] skipped: {_pe}")
    log(f"  GPU Music: MUSICGEN_DEVICE={os.environ.get('MUSICGEN_DEVICE', 'NOT SET')}")
    try:
        _prefetch_trends([c for c in config["channels"] if c["id"] in CHANNELS])  # === PREMIUM_REVENUE_PATCH === trends-hook
    except Exception as _e:
        log(f"  trends prefetch outer-error: {_e}")
    try:
        _prefetch_winners([c for c in config["channels"] if c["id"] in CHANNELS], global_cfg)  # best-performer feedback loop
    except Exception as _e:
        log(f"  winners prefetch outer-error: {_e}")

    from pipeline.ollama_writer import OllamaWriter
    from pipeline.kokoro_tts import KokoroTTS
    from pipeline.ffmpeg_assembler import FFmpegAssembler

    writer = OllamaWriter(global_cfg)
    from pipeline.elevenlabs_tts import make_tts
    tts = make_tts(global_cfg)
    assembler = FFmpegAssembler(global_cfg)

    log("Checking Ollama...")
    ensure_ollama()
    log("Checking Fooocus API...")
    ensure_fooocus()

    # Pre-flight OAuth token health check â€” warn early if uploads will fail.
    try:
        token_status = check_channel_tokens(global_cfg, CHANNELS)
    except Exception as e:
        log(f"  [TOKEN] Pre-flight check failed: {e} (continuing â€” generation unaffected)")
        token_status = {}

    # Channel routing diagnostic â€” log exactly where each channel uploads to
    log("")
    log("=" * 50)
    log("CHANNEL ROUTING MAP")
    log("=" * 50)
    _yt_ids_seen = {}
    for _ch in config["channels"]:
        if _ch["id"] not in CHANNELS:
            continue
        _yt_id = _ch.get("youtube_channel_id", "").strip()
        _yt_title = _ch.get("youtube_channel_title", "â€”")
        _playlist = _ch.get("playlist_title", f"{_ch['name']} Daily")
        _enabled = _ch.get("upload_enabled", True)
        _status = "âœ“ UPLOAD" if (_enabled and _yt_id) else "âœ— SKIP (no auth)"
        log(f"  {_ch['id']:20s} â†’ YT:{_yt_id or 'NOT BOUND':30s} ({_yt_title})")
        log(f"  {'':20s}   playlist: {_playlist!r}   [{_status}]")
        if _yt_id:
            _yt_ids_seen.setdefault(_yt_id, []).append(_ch["id"])
    # Warn if multiple channels share the same YouTube channel ID
    for _yt_id, _chs in _yt_ids_seen.items():
        if len(_chs) > 1:
            log(f"  âš ï¸  NOTE: {len(_chs)} pipeline channels share YouTube ID {_yt_id}: {', '.join(_chs)}")
            log(f"      Content will be organised into separate playlists on that channel.")
    log("=" * 50)
    log("")

    # -- PHASE 1: Long-form video generation (premium 12-step pipeline) ------
    today = datetime.now().strftime("%Y%m%d")  # compute once; used by all phases (fixes UnboundLocalError + cross-midnight drift)
    channel_results = {}
    channel_generation_failed = set()

    def _generate_channel(ch_id: str) -> bool:
        """Spawn the OOM-isolated channel subprocess and record its result.

        Returns True when the subprocess exits 0 and its final video exists.
        Used by the main PHASE 1 loop and the retry sweep — one code path.
        GPU/module cleanup always runs so a crashed child can't poison the
        next spawn.
        """
        _result_path = ROOT / "output" / ch_id / today / "channel_result.json"
        if _result_path.exists():
            _result_path.unlink()
        try:
            _proc = subprocess.run(
                [sys.executable, str(ROOT / "run_channel_isolated.py"), ch_id, today],
                cwd=str(ROOT),
                timeout=7200,
            )
            if _proc.returncode == 0 and _result_path.exists():
                _res_data = json.loads(_result_path.read_text(encoding="utf-8"))
                _vp = _res_data.get("video_path", "")
                if _vp and Path(_vp).exists():
                    channel_results[ch_id] = _res_data
                    channel_results[ch_id]["video_path"] = _vp
                    log(f"  ✓ {ch_id}: video ready — {_vp}")
                    return True
                log(f"  ✗ {ch_id}: subprocess OK but video_path missing: {_vp!r}")
            else:
                log(f"  ✗ {ch_id}: subprocess exited {_proc.returncode} (OOM or error)")
        except subprocess.TimeoutExpired:
            log(f"  ✗ {ch_id}: TIMEOUT after 7200s — subprocess killed")
        except Exception as e:
            log(f"  ✗ {ch_id}: subprocess launch FAILED — {e}")
            traceback.print_exc(file=open(LOG, "a"))
        finally:
            # Release GPU memory between channels to prevent OOM on subsequent runs.
            # LTX-Video and Fooocus both hold VRAM; explicit cleanup keeps the RTX 4070 Ti
            # from accumulating state across 5 channels (known OOM crash after finance).
            _clear_gpu_memory()
            _purge_module_caches()
        return False

    for ch_id in CHANNELS:
        log(f"\n--- Channel: {ch_id} ---")
        # RAM guard: warn early if available RAM is low before starting a heavy channel
        try:
            import psutil as _psutil
            _vm = _psutil.virtual_memory()
            if _vm.available < 4 * 1024 ** 3:
                log(f"  [RAM] WARNING: only {_vm.available / 1024**3:.1f} GB free before {ch_id} -- OOM risk is high")
            else:
                log(f"  [RAM] {_vm.available / 1024**3:.1f} GB free before {ch_id}")
        except Exception as _ram_e:
            log(f"  [RAM] psutil check skipped: {_ram_e}")
        ensure_ollama()
        # Heal a dead image server BEFORE spawning (probe + fresh start) rather
        # than waiting up to 5 min for the watchdog — a channel spawned against
        # a dead Fooocus burns its whole step-4 retry budget on placeholders.
        try:
            ensure_fooocus()
        except Exception as _ff_e:
            log(f"  [FOOOCUS] pre-spawn probe failed: {_ff_e} (step-4 gate will protect)")
        log(f"  [ISOLATED] Spawning subprocess for {ch_id} (OOM-isolated)...")
        if not _generate_channel(ch_id):
            channel_generation_failed.add(ch_id)

    # -- RETRY SWEEP: one second chance for crashed channels ------------------
    # Transient Fooocus/GPU outages kill channel subprocesses mid-run
    # (2026-07-04: finance exit -1, history-mystery 0xC000013A). By the end
    # of the first pass the FooocusAPI-Watchdog has healed the image server,
    # and per-channel checkpoints make a retry cheap (completed steps
    # fast-forward). One sweep only — a channel that fails twice stays failed.
    if channel_generation_failed:
        _retry_list = sorted(channel_generation_failed)
        log(f"\n=== RETRY SWEEP: {len(_retry_list)} failed channel(s): {', '.join(_retry_list)} ===")
        for ch_id in _retry_list:
            log(f"\n--- RETRY {ch_id} ---")
            ensure_ollama()
            try:
                ensure_fooocus()
            except Exception as _ff_e:
                log(f"  [FOOOCUS] pre-retry probe failed: {_ff_e} (step-4 gate will protect)")
            log(f"  [ISOLATED] Re-spawning subprocess for {ch_id} (checkpoint resume)...")
            if _generate_channel(ch_id):
                channel_generation_failed.discard(ch_id)
                log(f"  ✓ {ch_id}: recovered on retry")

    log(f"\n=== LONG-FORM VIDEOS COMPLETE ===")

    # -- PHASE 2: YouTube Shorts (premium pipeline via generate_shorts.py) ----
    log(f"\n{'='*50}")
    log(f"PHASE 2: YOUTUBE SHORTS GENERATION")
    log(f"{'='*50}")

    ensure_ollama()
    total_shorts = 0

    for ch_id in CHANNELS:
        channel = next((c for c in config["channels"] if c["id"] == ch_id), None)
        if not channel or not channel.get("shorts_enabled", True):
            continue

        # `today` is pinned at PHASE 1 start — do NOT recompute here, or a run
        # that crosses midnight generates Shorts for a date with no content.
        ch_output = ROOT / "output" / ch_id / today
        shorts_dir = ch_output / "shorts"

        script_file = ch_output / "script.json"
        if not script_file.exists():
            log(f"  {ch_id}: No script found, skipping shorts")
            continue

        log(f"\n  {ch_id}: Running premium shorts pipeline (LTX + hook overlay + captions)...")
        try:
            # generate_shorts.py is the fully overhauled premium pipeline:
            #   LTX animation 576x1024, hook text overlay, faster-whisper captions,
            #   quality gate (15-60s, >50KB). Pass channel ID to process just this channel.
            shorts_proc = subprocess.run(
                [sys.executable, str(ROOT / "generate_shorts.py"), ch_id],
                cwd=str(ROOT),
                capture_output=True,
                text=True,
                timeout=1800,  # 30 min â€” LTX per short ~2 min on RTX 4070 Ti Super
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
            )
            for line in shorts_proc.stdout.splitlines():
                log(f"    [shorts] {line}")
            if shorts_proc.returncode != 0:
                log(f"  {ch_id}: generate_shorts.py exited rc={shorts_proc.returncode}")
                for line in shorts_proc.stderr.splitlines()[-10:]:
                    log(f"    [shorts-err] {line}")
        except subprocess.TimeoutExpired:
            log(f"  {ch_id}: Shorts generation timed out after 30 min")
        except Exception as e:
            log(f"  {ch_id}: Shorts generation error: {e}")
        finally:
            _clear_gpu_memory()
            try:
                _run_syndicator(channel, ch_id, channel.get("niche",""), channel.get("name",ch_id), shorts_dir=shorts_dir)  # === PREMIUM_REVENUE_PATCH === post-shorts
            except Exception as _e:
                log(f"  syndicator hook outer-err: {_e}")

        # Count successfully generated shorts
        if shorts_dir.exists():
            ready = [f for f in shorts_dir.glob(f"short_*_{ch_id}.mp4") if f.stat().st_size > 50_000]
            total_shorts += len(ready)
            log(f"  {ch_id}: {len(ready)} shorts ready")

    log(f"\n=== GENERATION COMPLETE: {len(CHANNELS)} channels, {total_shorts} Shorts ===")

    # -- PHASE 3: YouTube Upload ---------------------------------------------
    log(f"\n{'='*50}")
    log(f"PHASE 3: YOUTUBE UPLOAD")
    log(f"{'='*50}")

    # ── GPU + RAM cleanup before upload ──────────────────────────────────────
    # PHASE 1+2 keep LTX, Whisper, Kokoro, MusicGen, Fooocus subprocesses, and
    # 5 large MP4 buffers alive in the same Python process. Today's run died
    # mid-upload — most likely OOM. Free everything we can before the upload
    # loop opens an HTTPS resumable session.
    try:
        import gc
        gc.collect()
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
                free, total = torch.cuda.mem_get_info()
                log(f"  GPU cleanup: {free/1e9:.1f} GB free of {total/1e9:.1f} GB")
        except Exception:
            pass
        try:
            import psutil
            mem = psutil.virtual_memory()
            log(f"  RAM cleanup: {mem.available/1e9:.1f} GB free of {mem.total/1e9:.1f} GB")
        except Exception:
            pass
    except Exception as _ce:
        log(f"  Pre-upload cleanup non-fatal error: {_ce}")
    # --- premium-grade: quality gate before any uploads ---
    log("")
    log("=" * 50)
    log("QUALITY GATE: validating today's batch before publish")
    log("=" * 50)
    _qg_blocked = False
    qg_stdout = ""
    try:
        qg_proc = subprocess.run(
            [sys.executable, str(ROOT / "quality_gate.py"), today],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
        )
        qg_stdout = qg_proc.stdout or ""
        for _line in qg_stdout.splitlines():
            log(f"  qg> {_line}")
        _blocker = ROOT / "QUALITY_GATE_BLOCKED.txt"
        if qg_proc.returncode != 0 or _blocker.exists():
            log("QUALITY GATE: FAILED â€” refusing to upload today's batch.")
            log(f"  Blocker file: {_blocker} (delete + re-run quality_gate.py to re-validate)")
            _qg_blocked = True
        else:
            log("QUALITY GATE: PASSED")
    except Exception as _qg_e:
        log(f"QUALITY GATE: error invoking quality_gate.py: {_qg_e}")
        log("  FAIL-CLOSED: gate could not run, refusing ungated uploads (no-placeholder policy).")
        _qg_blocked = True
    # --- end quality gate ---


    from uploaders.youtube_uploader import YouTubeUploader

    import re as _re
    _qg_passed = set(_re.findall(r"\[PASS(?:_WITH_SKIPS)?\]\s+(\S+)", qg_stdout))
    if _qg_passed:
        log(f"  Quality gate: {len(_qg_passed)} channel(s) passed and will publish: {sorted(_qg_passed)}")
    if _qg_blocked and not _qg_passed:
        log("  Quality gate blocked uploads â€” skipping uploader init.")
        uploader = None
    else:
        try:
            uploader = YouTubeUploader(global_cfg)
        except Exception as e:
            log(f"  YouTube uploader init FAILED: {e}")
            log(f"  Skipping all uploads. Run setup_youtube_auth.py to configure.")
            uploader = None

    NICHE_CATEGORY = {
        "tech_ai":        "28",  # Science & Technology
        "finance":        "27",  # Education
        "motivation":     "26",  # Howto & Style
        "history":        "27",  # Education
        "science":        "28",  # Science & Technology
        "history_mystery": "27",  # Education
        "science_facts":  "28",  # Science & Technology
    }

    # Retry any uploads that failed with quota errors in previous runs
    if uploader:
        try:
            _retry_failed_uploads(uploader)
        except Exception as _rq_e:
            log(f"  [RETRY-Q] retry_failed_uploads outer error (non-fatal): {_rq_e}")
    # Purge any lingering model caches before the upload loop
    _purge_module_caches()

    total_uploads = 0
    total_short_uploads = 0
    upload_warnings = []  # aggregated sub-step failures (thumbnail 403s, captions, playlists)
    studio_tasks = []     # per-video manual Studio steps (pin / end screen / syndicate)

    # ---- QUOTA BUDGET ----------------------------------------------------
    # 10,000 units/day, shared by all 5 channels (one Cloud project). Real costs
    # (verified 2026-07-14): videos.insert=100 (NOT the old 1600 -- Google cut it
    # ~16x in Dec 2025), captions.insert=400, thumbnails.set=50,
    # playlistItems.insert=50. So long-form=601u, short=201u and ONE full night
    # (~6,770u) fits easily.
    #
    # But TWO runs in a Pacific day = ~13,540u and blows it. That happened on
    # 2026-07-14: uploads go long-form-first, so the long-forms went through and
    # EVERY Short 403'd -- "Long-form uploaded: 5/5, Shorts uploaded: 0."
    #
    # That is the worst way to run out. Shorts are ~99.5% of all views this
    # business earns (99 Shorts = 9,880 views; 18 long-form = 54). So when the
    # budget is short, Shorts are funded FIRST and long-form yields.
    _budget = None
    _plan = None
    try:
        from pipeline.quota_budget import QuotaBudget, LONGFORM_COST, SHORT_COST
        _budget = QuotaBudget(global_cfg, state_dir=str(ROOT / "state"))
        _shorts_avail = {}
        for _c in CHANNELS:
            _sd = ROOT / "output" / _c / today / "shorts"
            _shorts_avail[_c] = ([str(p) for p in sorted(_sd.glob(f"short_*_{_c}.mp4"))]
                                 if _sd.exists() else [])
        _plan = _budget.plan(list(CHANNELS), _shorts_avail)
        _plan_lf = set(_plan["longform"])
        _plan_sh = {Path(p).name for _c, p in _plan["shorts"]}
        log("")
        for _line in QuotaBudget.describe(_plan).splitlines():
            log("  " + _line)
        log("")
    except Exception as _e:
        # LOUD, not silent. If the budget breaks we upload unbudgeted -- which is
        # the old behaviour and will 403 the Shorts on a second run of the day.
        # A quiet "budget unavailable" line in a 3,000-line log is how a fix dies.
        log("")
        log("  " + "!" * 66)
        log(f"  !! QUOTA BUDGET FAILED TO LOAD: {type(_e).__name__}: {_e}")
        log("  !! Uploading WITHOUT a budget. If the pipeline already ran today")
        log("  !! (Pacific), Shorts will 403 -- and Shorts are 99.5% of all views.")
        log("  " + "!" * 66)
        log("")
        _plan_lf = set(CHANNELS)
        _plan_sh = None          # None = no filtering

    if uploader:
        for ch_id in CHANNELS:
            channel = next((c for c in config["channels"] if c["id"] == ch_id), None)
            if not channel:
                continue

            # Skip channels that haven't been bound to a YouTube account yet.
            if not channel.get("upload_enabled", True):
                log(f"  {ch_id}: upload_enabled=false â€” skipping upload (run auth_manual.py {ch_id} to bind)")
                continue
            if not channel.get("youtube_channel_id", "").strip():
                log(f"  {ch_id}: No youtube_channel_id in channels.json â€” skipping upload")
                continue

            # `today` is pinned at PHASE 1 start — do NOT recompute here, or a
            # post-midnight PHASE 3 looks in the wrong date dir and uploads nothing
            # (last night's upload at 00:00:46 only worked because the phase began
            # pre-midnight).
            ch_output = ROOT / "output" / ch_id / today
            niche = channel["niche"]
            category = NICHE_CATEGORY.get(niche, "22")

            # Use video_path from the generation result dict; fall back to the
            # conventional output path so run_upload.py re-runs still work.
            ch_result = channel_results.get(ch_id, {})
            video_path_str = ch_result.get("video_path") or str(ch_output / f"{ch_id}_{today}.mp4")
            video_file = Path(video_path_str)
            if not video_file.exists() or video_file.stat().st_size < 1000:
                log(f"  {ch_id}: No long-form video found, skipping upload")
                continue

            # Quota: long-form YIELDS to Shorts. See the budget block above --
            # long-form has earned a median of 2 views, Shorts a median of 39
            # (max 1,191). If there isn't room for both, the Short wins.
            if ch_id not in _plan_lf:
                log(f"  {ch_id}: long-form DEFERRED by quota budget "
                    f"(Shorts funded first; they are 99.5% of all views). "
                    f"The video is rendered and will upload on the next run.")
                continue

            if _qg_blocked and ch_id not in _qg_passed:
                log(f"  {ch_id}: failed batch quality gate, skipping upload")
                continue

            # Double-upload guard (long-form): if this channel was already uploaded
            # for `today` (sentinel written only after a successful upload), skip so a
            # same-day re-run (manual + scheduled collision) cannot post a duplicate.
            if (ch_output / "UPLOADED_LONGFORM").exists():
                log(f"  {ch_id}: long-form already uploaded today (UPLOADED_LONGFORM sentinel) -- skipping to avoid duplicate")
                continue
            log(f"\n  Uploading {ch_id} long-form...")

            seo_data = {}
            seo_file = ch_output / "seo.json"
            if seo_file.exists():
                try:
                    seo_data = json.loads(seo_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            script_data = {}
            script_file = ch_output / "script.json"
            if script_file.exists():
                try:
                    script_data = json.loads(script_file.read_text(encoding="utf-8"))
                except Exception:
                    pass

            final_title = seo_data.get("optimized_title", script_data.get("title", f"{channel['name']} - {today}"))
            final_tags = seo_data.get("tags", script_data.get("tags", [])) + channel.get("tags_base", [])
            seen_tags = set()
            unique_tags = []
            for t in final_tags:
                if not isinstance(t, str):
                    t = str(t) if t else ""
                tl = t.lower().strip()
                if tl and tl not in seen_tags:
                    seen_tags.add(tl)
                    unique_tags.append(t)
            final_tags = unique_tags[:30]

            audio_dir = ch_output / "audio"
            audio_sections = []
            sections = script_data.get("script_sections", [])
            if audio_dir.exists():
                for i, sec in enumerate(sections):
                    audio_path = audio_dir / f"section_{i:03d}.wav"
                    if audio_path.exists():
                        audio_sections.append({
                            "index": i,
                            "section_title": sec.get("section_title", f"Section {i+1}"),
                            "path": str(audio_path),
                        })

            description = build_description(script_data, channel, audio_sections, seo_data)
            description = _inject_affiliates(description, channel.get("niche", ""))  # === PREMIUM_REVENUE_PATCH ===
            # Use thumbnail_path from generation result; fall back to on-disk locations.
            # run_pipeline.py writes thumbnail.jpg; old pipeline wrote thumbnail.png.
            thumbnail_path = ch_result.get("thumbnail_path")
            # Homegrown thumbnail A/B: rotate the 3 rendered styles and record the
            # choice so the analytics pass can learn the best style by CTR.
            _ab_style = None
            try:
                from pipeline import thumb_ab as _thumb_ab
                _ab_path, _ab_style = _thumb_ab.choose_variant(str(ROOT), ch_id, ch_result)
                if _ab_path and os.path.exists(_ab_path) and 20_000 <= os.path.getsize(_ab_path) <= 2 * 1024**2:
                    thumbnail_path = _ab_path
                    log(f"  {ch_id}: A/B thumbnail style = {_thumb_ab.STYLE_NAMES.get(_ab_style, _ab_style or 'main')}")
                else:
                    # Variant rejected -> the MAIN thumbnail ships. Do NOT attribute
                    # this video to a variant style, or the A/B dataset is poisoned.
                    _ab_style = None
            except Exception as _abe:
                log(f"  {ch_id}: thumb A/B select skipped: {_abe}")
            if not thumbnail_path or not os.path.exists(thumbnail_path) or os.path.getsize(thumbnail_path) < 20_000:
                for _thumb_name in ("thumbnail.jpg", "thumbnail.png"):
                    _tp = str(ch_output / _thumb_name)
                    if os.path.exists(_tp) and os.path.getsize(_tp) >= 20_000:  # align w/ QG MIN_THUMB_BYTES
                        thumbnail_path = _tp
                        break
                else:
                    thumbnail_path = None

            # SRT captions â€” from result dict or on-disk fallback
            captions_path = ch_result.get("srt_path")
            if not captions_path or not os.path.exists(captions_path):
                # Look for any captioned .srt in output dir
                for _srt in sorted(ch_output.glob(f"{ch_id}_*_captioned.srt")):
                    if _srt.exists():
                        captions_path = str(_srt)
                        break
                else:
                    captions_path = None

            # Playlist â€” use playlist_title from channels.json (set during config);
            # each niche has its own named playlist so content is organised on Matrix monster.
            playlist_title = (
                channel.get("playlist_title")
                or f"{channel['name']} Daily"
            )

            # Optimal publish scheduling â€” schedule for peak traffic window.
            # Returns None if we're already inside the optimal window (publish now).
            from uploaders.youtube_uploader import get_optimal_publish_time
            niche_for_timing = channel.get("niche", "tech_ai")
            publish_at = get_optimal_publish_time(niche_for_timing)
            if publish_at:
                log(f"  {ch_id}: scheduling for peak window â†’ {publish_at}")
            else:
                log(f"  {ch_id}: inside optimal window â€” publishing immediately")

            _longform_vid_id = None
            try:
                _upload_params = dict(
                    channel_id=ch_id,
                    video_path=str(video_file),
                    title=final_title[:100],
                    description=description,
                    tags=final_tags,
                    category_id=category,
                    privacy="public" if not publish_at else "private",
                    thumbnail_path=thumbnail_path,
                    captions_path=captions_path,
                    playlist_title=playlist_title,
                    default_audio_language="en-US",
                    scheduled_time=publish_at,
                )
                result = uploader.upload_video(**_upload_params)
                vid_id = result.get("video_id", "unknown")
                _longform_vid_id = vid_id
                log(f"  UPLOADED: {ch_id} -> https://youtube.com/watch?v={vid_id}")
                # Surface any silent sub-step failures (thumbnail 403, captions, playlist)
                for _w in result.get("warnings", []):
                    upload_warnings.append(_w)
                    log(f"  ⚠  {_w}")
                # Record A/B thumbnail style for CTR learning next run.
                try:
                    from pipeline import thumb_ab as _thumb_ab
                    _thumb_ab.record_choice(str(ROOT), ch_id, vid_id, _ab_style)
                except Exception:
                    pass
                # Seed engagement with a channel comment (API can't auto-pin it).
                try:
                    uploader.post_top_comment(ch_id, vid_id, _ENGAGEMENT_Q.get(niche, _ENGAGEMENT_Q_DEFAULT))
                except Exception as _ce:
                    log(f"  {ch_id}: engagement comment failed: {_ce}")
                # Queue the manual Studio steps the API can't do (pin / end screen / syndicate).
                studio_tasks.append({
                    "channel": channel.get("name", ch_id),
                    "url": f"https://youtube.com/watch?v={vid_id}",
                    "comment": _ENGAGEMENT_Q.get(niche, _ENGAGEMENT_Q_DEFAULT),
                    "syndicate_dir": str((ROOT / "syndicate" / ch_id).resolve()),
                })
                # Localize title/description into major languages so the video
                # surfaces in non-English search & browse (big reach lever).
                try:
                    _langs = global_cfg.get("localization_languages", [])
                    if _langs:
                        _loc = writer.translate_metadata(final_title, description, _langs)
                        if _loc:
                            uploader.set_localizations(ch_id, vid_id, _loc, default_language="en")
                            log(f"  {ch_id}: localized into {len(_loc)} languages ({', '.join(sorted(_loc))})")
                except Exception as _le:
                    log(f"  {ch_id}: localization skipped: {_le}")
                # Sentinel: safety-net run_upload.py skips re-upload
                try:
                    (ROOT / "output" / ch_id / today / "UPLOADED_LONGFORM").write_text(vid_id)
                except Exception:
                    pass
                total_uploads += 1
                # Persist the spend so a SECOND run in the same Pacific day knows
                # what this one burned. Without the ledger, run two thinks it has
                # a fresh 10,000 and 403s halfway through -- which is exactly how
                # every Short died on 2026-07-14.
                if _budget:
                    try:
                        _budget.spend(LONGFORM_COST, f"long-form {ch_id}")
                    except Exception:
                        pass
                (ch_output / "upload_result.json").write_text(json.dumps(result, indent=2))
                try:
                    tracker.log_video_performance({
                        "video_id": vid_id,
                        "channel_id": ch_id,
                        "title": final_title,
                        "niche": niche,
                        "uploaded_at": datetime.now().isoformat(),
                        "playlist_id": result.get("playlist_id", ""),
                        "has_thumbnail": bool(thumbnail_path),
                        "has_captions": bool(captions_path),
                        "tags_count": len(final_tags),
                        "duration_seconds": ch_result.get("video_duration", 0),
                        "is_short": False,
                    })
                except Exception as _te:
                    log(f"  Analytics log_video_performance failed (non-fatal): {_te}")

                # â”€â”€ POST-UPLOAD CLEANUP â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
                # Upload confirmed (video_id received). Delete all large generated
                # files to reclaim disk space. Metadata JSON files are preserved.
                try:
                    _cleanup_channel_output(ch_output, ch_id)
                except Exception as _ce:
                    log(f"  [CLEANUP] Non-fatal cleanup error for {ch_id}: {_ce}")

            except FileNotFoundError as e:
                log(f"  UPLOAD SKIPPED ({ch_id}): {e}")
                log(f"  Run: python setup_youtube_auth.py")
            except Exception as e:
                _err_str = str(e)
                _is_quota = False
                try:
                    from googleapiclient.errors import HttpError as _HttpError
                    if isinstance(e, _HttpError):
                        _is_quota = (e.resp.status == 429 or "quota" in _err_str.lower())
                except Exception:
                    pass
                if _is_quota:
                    log(f"  UPLOAD QUOTA EXCEEDED ({ch_id}): queuing for retry next run")
                    _enqueue_failed_upload(_upload_params)
                else:
                    log(f"  UPLOAD FAILED ({ch_id}): {type(e).__name__}: {e}")

            # Shorts -> long-form funnel link.
            #
            # `_longform_vid_id` is only set when TODAY's long-form uploaded in
            # THIS run. It is now legitimately possible for that not to happen:
            # the quota budget can defer long-form so Shorts get funded first, and
            # the double-upload sentinel can skip it. Left as None, every Short
            # silently loses its "Full breakdown: youtu.be/..." link and falls
            # back to a generic CTA -- a real downgrade to the funnel.
            #
            # So recover the channel's MOST RECENT published long-form from the
            # UPLOADED_LONGFORM sentinels (which store the video id). Linking a
            # Short to an already-indexed long-form is arguably better than
            # linking to one uploaded sixty seconds ago anyway.
            if not _longform_vid_id:
                try:
                    _sentinels = sorted(
                        (ROOT / "output" / ch_id).glob("*/UPLOADED_LONGFORM"),
                        key=lambda p: p.parent.name, reverse=True)
                    for _s in _sentinels:
                        _vid = _s.read_text(encoding="utf-8").strip()
                        if _vid and _vid != "uploaded":
                            _longform_vid_id = _vid
                            log(f"  {ch_id}: funnel link -> most recent long-form "
                                f"{_vid} (today's was deferred/skipped)")
                            break
                except Exception as _fe:
                    log(f"  {ch_id}: could not recover a funnel link ({_fe})")

            # Upload Shorts
            shorts_dir = ch_output / "shorts"
            if shorts_dir.exists():
                short_files = sorted(shorts_dir.glob(f"short_*_{ch_id}.mp4"))
                for sf in short_files:
                    if sf.stat().st_size < 50_000:  # align w/ QG MIN_SHORT_BYTES (was 500)
                        continue
                    # Quota: only the Shorts the budget actually funded. The plan
                    # allocated them ROUND-ROBIN across channels, so one channel
                    # can't drain the day before another gets a single Short.
                    if _plan_sh is not None and sf.name not in _plan_sh:
                        log(f"    {sf.name}: deferred (quota) -- will upload next run")
                        continue
                    # Double-upload guard: skip Shorts already published
                    # (sentinel written post-upload) so a rerun or the
                    # safety-net uploader cannot re-publish the same Short.
                    short_sentinel = sf.with_suffix(".uploaded")
                    if short_sentinel.exists():
                        log(f"  SHORT SKIP: {sf.name} (already uploaded -- sentinel)")
                        continue
                    try:
                        short_num = sf.stem.split("_")[1]
                        # Try to get hook text for a better title
                        hook_text = ""
                        # Check if generate_shorts.py wrote a script file
                        script_candidate = shorts_dir / f"short_{short_num}_script.json"
                        if script_candidate.exists():
                            try:
                                s = json.loads(script_candidate.read_text(encoding="utf-8"))
                                hook_text = s.get("hook", s.get("title", ""))[:80]
                            except Exception:
                                pass
                        # Fall back to shorts_hooks from main script
                        if not hook_text:
                            try:
                                idx = int(short_num) - 1
                                hooks = script_data.get("shorts_hooks", [])
                                if 0 <= idx < len(hooks):
                                    hook_text = str(hooks[idx])[:80]
                            except Exception:
                                pass

                        _real_ch_title = channel.get("youtube_channel_title") or channel["name"]
                        # 2026-07-14: hook_text is a SPOKEN/on-screen teaser and the
                        # writer loves to end it on an ellipsis ("Black holes are...").
                        # That is fine on the video and fatal in the title: metadata_gate
                        # rejects any title ending in "..." as generator-truncated, so
                        # those Shorts were built, encoded, captioned -- and then silently
                        # never uploaded (2 lost on science-facts this run alone).
                        # Also: the raw [:80] slice above can cut mid-word.
                        short_title = _clean_short_title(hook_text, _real_ch_title)
                        _human_niche = (channel.get("niche", "").replace("_", " ")
                                        .replace("history mystery", "history")
                                        .replace("science facts", "science").strip()) or "videos"
                        _funnel_line = (  # _funnel_line: direct Shorts->long-form link
                            f"Full breakdown: https://youtu.be/{_longform_vid_id}\n\n"
                            if _longform_vid_id else
                            "Watch the FULL breakdown on the channel.\n\n"
                        )
                        short_desc = (
                            f"SUBSCRIBE to {_real_ch_title} for daily {_human_niche} - a new one every single day.\n"
                            + _funnel_line
                            + "Like + drop a comment if this helped - it genuinely pushes the channel to more people.\n\n"
                        )
                        if niche == "finance":
                            short_desc += "DISCLAIMER: Educational only. Not financial advice.\n\n"
                        # Shorts used to ship the SAME 10 channel tags on every
                        # upload -- thousands of identically-packaged videos with
                        # zero per-video keywords. The long-form's SEO data (which
                        # now carries the target search query as tag #1, plus the
                        # topic writer's secondary keywords) applies just as well
                        # to the Short cut from the same topic. Lead with it.
                        _seo_tags = []
                        try:
                            _seo_tags = [str(t) for t in (seo_data.get("tags") or []) if t]
                        except Exception:
                            _seo_tags = []
                        short_tags = []
                        _seen_st = set()
                        for _t in (_seo_tags[:12]
                                   + channel.get("tags_base", [])[:6]
                                   + ["Shorts", "short"]):
                            _tn = str(_t).strip()
                            _tk = _tn.lower()
                            if _tn and _tk not in _seen_st:
                                _seen_st.add(_tk)
                                short_tags.append(_tn)
                        short_tags = short_tags[:20]

                        # Give the Short its own searchable first line instead of
                        # opening every one with the same "SUBSCRIBE to ..." boilerplate.
                        _st_hook = ""
                        try:
                            _st_hook = str(seo_data.get("shorts_hook") or "").strip()
                        except Exception:
                            _st_hook = ""
                        if not _st_hook:
                            _st_hook = str(hook_text or "").strip()
                        if _st_hook:
                            short_desc = _st_hook.rstrip(".") + ".\n\n" + short_desc
                        _st_ht = []
                        try:
                            _st_ht = [str(h) for h in (seo_data.get("hashtags") or []) if h][:3]
                        except Exception:
                            _st_ht = []
                        if _st_ht:
                            short_desc = short_desc.rstrip() + "\n\n" + " ".join(_st_ht) + "\n"

                        # Stagger the 3 Shorts across the day (15 min after the
                        # long-form, then +4h, +8h) so each hits a different audience
                        # wave instead of clustering into one burst. The long-form
                        # still indexes first; each Short keeps funnelling traffic to it.
                        short_publish_at = get_optimal_publish_time(niche_for_timing)
                        if short_publish_at:
                            try:
                                _k = max(0, int(short_num) - 1)
                            except Exception:
                                _k = 0
                            _dt = datetime.strptime(short_publish_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                            _dt = _dt + timedelta(minutes=15) + timedelta(hours=4 * _k)
                            short_publish_at = _dt.strftime("%Y-%m-%dT%H:%M:%SZ")

                        short_desc = _inject_affiliates(short_desc, channel.get("niche", ""))  # === PREMIUM_REVENUE_PATCH === shorts-desc
                        _short_upload_params = dict(
                            channel_id=ch_id,
                            video_path=str(sf),
                            title=short_title,
                            description=short_desc,
                            tags=short_tags,
                            category_id=category,
                            privacy="public" if not short_publish_at else "private",
                            is_short=True,
                            playlist_title=f"{channel['name']} Shorts",
                            default_audio_language="en-US",
                            scheduled_time=short_publish_at,
                        )
                        result = uploader.upload_video(**_short_upload_params)
                        log(f"  SHORT UPLOADED: {sf.name} -> https://youtube.com/watch?v={result.get('video_id', '?')}")
                        for _w in result.get("warnings", []):
                            upload_warnings.append(f"[short] {_w}")
                            log(f"  ⚠  {_w}")
                        total_short_uploads += 1
                        if _budget:
                            try:
                                _budget.spend(SHORT_COST, f"short {sf.name}")
                            except Exception:
                                pass
                        try:
                            sf.with_suffix(".uploaded").write_text(result.get("video_id", "uploaded"))
                        except Exception:
                            pass
                        try:
                            tracker.log_video_performance({
                                "video_id": result.get("video_id", "unknown"),
                                "channel_id": ch_id,
                                "title": short_title,
                                "niche": niche,
                                "uploaded_at": datetime.now().isoformat(),
                                "playlist_id": result.get("playlist_id", ""),
                                "has_thumbnail": False,
                                "has_captions": False,
                                "tags_count": len(short_tags),
                                "duration_seconds": 0,
                                "is_short": True,
                            })
                        except Exception as _te:
                            log(f"  Analytics log_video_performance (short) failed (non-fatal): {_te}")

                        # Delete this Short's mp4 + companion files immediately after upload
                        try:
                            _cleanup_short_file(sf, ch_id)
                        except Exception as _ce:
                            log(f"  [CLEANUP] Short cleanup non-fatal: {_ce}")

                    except Exception as e:
                        _err_str = str(e)
                        _is_quota = False
                        try:
                            from googleapiclient.errors import HttpError as _HttpError
                            if isinstance(e, _HttpError):
                                _is_quota = (e.resp.status == 429 or "quota" in _err_str.lower())
                        except Exception:
                            pass
                        if _is_quota:
                            log(f"  SHORT UPLOAD QUOTA EXCEEDED ({sf.name}): queuing for retry next run")
                            _enqueue_failed_upload(_short_upload_params)
                        else:
                            log(f"  SHORT UPLOAD FAILED ({sf.name}): {e}")

        log(f"\n=== UPLOADS COMPLETE: {total_uploads} videos, {total_short_uploads} shorts ===")
    else:
        log(f"  Uploader not available. Skipping all uploads.")

    # -- Analytics: daily report + monetization summary ----------------------
    try:
        report = tracker.generate_daily_report(today)
        summary = tracker.get_monetization_summary()
        log(f"\n=== MONETIZATION SUMMARY ===")
        for line in summary.strip().split('\n'):
            log(f"  {line}")
        (ROOT / "analytics" / f"daily_report_{today}.json").write_text(
            json.dumps(report, indent=2)
        )
    except Exception as e:
        log(f"  Analytics report failed (non-fatal): {e}")

    # -- Real channel stats collection (post-upload) --------------------------
    try:
        if uploader is not None:
            for ch_id in CHANNELS:
                try:
                    stats = uploader.get_channel_stats(ch_id)
                except Exception as _se:
                    log(f"  [STATS] {ch_id}: fetch failed (non-fatal): {_se}")
                    stats = {}
                watch_hours = -1.0
                try:
                    watch_hours = uploader.get_watch_hours(ch_id)
                except Exception as _we:
                    log(f"  [STATS] {ch_id}: watch hours fetch failed (non-fatal): {_we}")
                if stats.get("subscribers", 0):
                    tracker.update_channel_stats(ch_id, stats, watch_hours=watch_hours)
                    log(f"  [STATS] {ch_id}: {stats.get('subscribers', 0)} subs | {watch_hours:.0f}h watch")
                else:
                    log(f"  [STATS] {ch_id}: no subscriber data returned (skipping update)")
            ypp_summary = tracker.get_monetization_summary()
            for line in ypp_summary.strip().split('\n'):
                log(f"  [YPP] {line}")
    except Exception as e:
        log(f"  Real channel stats collection failed (non-fatal): {e}")

    # ── Upload health: elevate silent sub-step failures to the run summary ───
    # Thumbnail 403s, caption/playlist errors used to hide as quiet log lines.
    # Now they're front-and-center so regressions (e.g. an unverified Brand
    # Account silently falling back to auto thumbnails) can't go unnoticed.
    try:
        if upload_warnings:
            log("")
            log("!" * 52)
            log(f"  ⚠  UPLOAD WARNINGS ({len(upload_warnings)}) — ACTION MAY BE NEEDED:")
            for _w in upload_warnings:
                log(f"     • {_w}")
            log("!" * 52)
        elif uploader and (total_uploads or total_short_uploads):
            log("  ✓ Upload health: thumbnails / captions / playlists all applied — no warnings.")
    except Exception as _uw_e:
        log(f"  (upload-warning summary error, non-fatal: {_uw_e})")

    # ── Manual Studio remainder (pin / end screen / off-platform syndication) ──
    try:
        _ck = _write_studio_checklist(today, studio_tasks)
        if _ck:
            log("")
            log(f"  📋 STUDIO TASKS ({len(studio_tasks)} video(s), ~1 min each): {_ck}")
            log("     → pin the seeded comment, set end screens, and post the packaged "
                "Shorts to TikTok/Reels/FB/X (clips ready in syndicate/).")
    except Exception as _cke:
        log(f"  studio checklist error: {_cke}")

    log(f"\n{'='*50}")
    log(f"=== PIPELINE COMPLETE ===")
    log(f"  Channels: {len(CHANNELS)}")
    log(f"  Shorts generated: {total_shorts}")
    log(f"  Videos uploaded: {total_uploads if uploader else 0}")
    log(f"  Shorts uploaded: {total_short_uploads if uploader else 0}")
    log(f"  Log: {LOG}")
    log(f"{'='*50}")

# -- Entry Point ---------------------------------------------------------
if __name__ == "__main__":
    if not acquire_lock():
        sys.exit(1)
    try:
        main()
    except Exception as e:
        log(f"\n=== PIPELINE CRASHED ===")
        log(f"  Error: {type(e).__name__}: {e}")
        log(f"  Traceback:\n{traceback.format_exc()}")
        sys.exit(1)
    finally:
        release_lock()
        log(f"Lock released. Pipeline finished at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
