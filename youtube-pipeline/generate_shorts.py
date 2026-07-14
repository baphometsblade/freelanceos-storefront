"""
YouTube Shorts Generator — Production-grade Shorts pipeline.

Per short:
  1. Script via generate_short_script() (new) or generate_shorts_script() (fallback)
  2. TTS narration → WAV
  3. Source image (reuse from long-form or generate a solid colour plate)
  4. Smart-crop landscape image to 9:16 via FFmpeg
  5. Animate the 9:16 crop with LTXAnimator (576×1024, 8 steps)
  6. Loop animated clip to audio duration
  7. Burn animated hook text overlay (first 3 seconds, fade in/out)
  8. Burn captions with CaptionGenerator
  9. Quality gate: 15-60 s, file > 50 KB
"""

import sys
import json
import os
import subprocess
import traceback
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

LOG = ROOT / "logs" / "shorts_generation.log"
LOG.parent.mkdir(parents=True, exist_ok=True)


def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
    print(line, flush=True)


# Video-encode args for Shorts ffmpeg calls — set per-run from global_cfg so the
# GPU NVENC encoder is used when configured (falls back to libx264).
_VENC_ARGS = ["-c:v", "libx264", "-crf", "23"]


def _set_short_encoder(global_cfg: dict):
    global _VENC_ARGS
    enc = global_cfg.get("video_encoder", "libx264")
    if "nvenc" in enc:
        _mr = global_cfg.get("nvenc_maxrate", "12M")
        _VENC_ARGS = ["-c:v", enc, "-preset", global_cfg.get("nvenc_preset", "p6"), "-rc", "vbr",
                      "-b:v", global_cfg.get("nvenc_bitrate", "8M"), "-maxrate", _mr, "-bufsize", _mr]
    else:
        _VENC_ARGS = ["-c:v", enc, "-crf", "23"]


# ---------------------------------------------------------------------------
# Helper: smart-crop landscape image to 9:16 vertical
# ---------------------------------------------------------------------------

def _smart_crop_to_vertical(image_path: str, output_path: str, ffmpeg: str = "ffmpeg") -> str:
    """
    Crop a potentially landscape image to 9:16 (1080×1920) using center crop.
    If the image is already taller than wide it is simply scaled to 1080×1920.

    Returns output_path on success, image_path unchanged on failure.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        # Detect dimensions
        probe_cmd = [
            "ffprobe", "-v", "quiet",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height",
            "-of", "json",
            image_path,
        ]
        probe_result = subprocess.run(
            probe_cmd, capture_output=True, text=True, timeout=15
        )
        dims = {}
        if probe_result.returncode == 0:
            try:
                dims = json.loads(probe_result.stdout).get("streams", [{}])[0]
            except Exception:
                dims = {}

        w = dims.get("width", 1920)
        h = dims.get("height", 1080)

        if w > h:
            # Landscape → center-crop to portrait: keep full height, crop width
            # FFmpeg filter: crop=ih*9/16:ih:(iw-ow)/2:0
            vf = (
                "crop=ih*9/16:ih:(iw-ow)/2:0,"
                "scale=1080:1920:flags=lanczos"
            )
        else:
            # Already portrait or square — just scale/pad to 1080×1920
            vf = (
                "scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black"
            )

        cmd = [
            ffmpeg, "-y",
            "-i", image_path,
            "-vf", vf,
            "-frames:v", "1",
            "-q:v", "2",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0 and Path(output_path).exists():
            return output_path
        log(f"    _smart_crop_to_vertical ffmpeg error: {result.stderr[:200]}")
        return image_path
    except Exception as e:
        log(f"    _smart_crop_to_vertical exception: {e}")
        return image_path


# ---------------------------------------------------------------------------
# Helper: build a looped animated short from a clip + audio
# ---------------------------------------------------------------------------

def _build_looped_video(clip_path: str, audio_path: str, duration: float, output_path: str, ffmpeg: str = "ffmpeg") -> str:
    """
    Loop clip_path to fill duration seconds, mixed with audio_path.
    Returns output_path on success, empty string on failure.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        fade_d = min(0.4, duration * 0.08)
        cmd = [
            ffmpeg, "-y",
            "-stream_loop", "-1",
            "-i", clip_path,
            "-i", audio_path,
            "-filter_complex",
            (
                f"[0:v]"
                f"scale=1080:1920:force_original_aspect_ratio=decrease:flags=lanczos,"
                f"pad=1080:1920:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,"
                f"fade=t=in:st=0:d={fade_d:.3f},"
                f"fade=t=out:st={max(0.0, duration - fade_d):.3f}:d={fade_d:.3f}"
                f"[v]"
            ),
            "-map", "[v]", "-map", "1:a",
            "-t", f"{duration:.3f}",
            *_VENC_ARGS,
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-vsync", "cfr",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and Path(output_path).exists():
            return output_path
        log(f"    _build_looped_video error: {result.stderr[:200]}")
        return ""
    except Exception as e:
        log(f"    _build_looped_video exception: {e}")
        return ""


# ---------------------------------------------------------------------------
# Helper: hook text overlay (first 3 seconds, fade in/out)
# ---------------------------------------------------------------------------

def _escape_drawtext(text: str) -> str:
    """
    Escape text for FFmpeg drawtext filter value.
    Order matters: backslash first, then single-quote, then colon.
    """
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "\\'")
    text = text.replace(":", "\\:")
    return text


def _normalize_punct(text: str) -> str:
    """Map unicode punctuation to ASCII for clean Impact-font rendering."""
    _m = {
        chr(0x2026): "...",
        chr(0x2018): "'", chr(0x2019): "'",
        chr(0x201C): '"', chr(0x201D): '"',
        chr(0x2014): "-", chr(0x2013): "-",
        chr(0x00A0): " ",
    }
    for _k, _v in _m.items():
        text = text.replace(_k, _v)
    return text


def _wrap_hook_text(text: str, max_chars: int = 21) -> str:
    """
    Wrap the hook to up to THREE lines of ~max_chars, splitting on word
    boundaries. Returns text with literal \\n separators for drawtext.

    2026-07-14: was a hard 50-char truncate + 2 lines at fontsize 60, which
    guillotined most hooks mid-word ("The 300-year-old map that shows a c").
    At fontsize 76 a 21-char line is the widest that clears 1080px with the
    border box, so we wrap to 3 lines and only truncate past ~63 chars --
    and we truncate on a WORD boundary with an ellipsis, never mid-word.
    """
    text = " ".join(text.split()).strip()

    words = text.split(" ")
    lines, cur = [], ""
    for w in words:
        cand = (cur + " " + w).strip() if cur else w
        if len(cand) <= max_chars or not cur:
            cur = cand
        else:
            lines.append(cur)
            cur = w
        if len(lines) == 3:
            break
    if cur and len(lines) < 3:
        lines.append(cur)

    # If we ran out of room, mark the truncation on a word boundary.
    consumed = len(" ".join(lines))
    if consumed < len(text) and lines:
        lines[-1] = (lines[-1].rstrip(",;:-") + "...")

    return "\\n".join(lines[:3])


def _add_hook_text_overlay(video_path: str, hook_text: str, output_path: str, ffmpeg: str = "ffmpeg") -> str:
    """
    Burn animated hook text onto video_path for the first 3 seconds.
    Uses Impact font, large white bold text centred near the top (y=h*0.15),
    with a 0.3s fade-in and 0.3s fade-out, drop shadow + black border.

    Returns output_path on success, video_path unchanged on failure.
    """
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)

        wrapped = _wrap_hook_text(_normalize_punct(hook_text), max_chars=21)
        escaped = _escape_drawtext(wrapped)
        # Robust text rendering: write the hook line to a UTF-8 file and use
        # drawtext textfile= so an apostrophe or unicode char can never
        # terminate the filtergraph quote. Inline text= kept as fallback.
        # _wrap_hook_text emits a literal backslash-n; convert to a real
        # newline (chr 10) so the 2-line wrap still renders from the file.
        _overlay_txt = str(Path(output_path).parent / "hook_overlay_text.txt")
        _use_textfile = False
        try:
            Path(_overlay_txt).write_text(
                wrapped.replace(chr(92) + "n", chr(10)), encoding="utf-8"
            )
            _use_textfile = True
        except Exception as _e:
            log(f"    Hook overlay: textfile write failed ({_e}); inline text")

        # Locate Impact font (Windows); fall back to Arial
        font_path = None
        for candidate in ["C:/Windows/Fonts/impact.ttf", "C:/Windows/Fonts/arial.ttf"]:
            if os.path.exists(candidate.replace("/", os.sep)):
                font_path = candidate.replace("\\", "/")  # ensure forward slashes
                font_path = font_path.replace("C:", "C\\:")  # escape drive-letter colon for FFmpeg filtergraph
                break
        if not font_path:
            log("    Hook overlay: no suitable font found, skipping")
            return video_path

        # 2026-07-14 (research: swipe-away is decided in the first 1-1.5s, before the
        # viewer consciously processes the frame; on-screen text during the hook is
        # worth ~+18% watch time). The old expression faded the hook in over 0.3s --
        # i.e. the single most important text on the channel was still translucent
        # through a third of the decision window. Hook is now at FULL OPACITY on
        # frame 0, held for 4s, and fades out over the last 0.35s only.
        HOOK_HOLD = 4.0
        _fo = 0.35
        alpha_expr = (
            f"if(gt(t,{HOOK_HOLD - _fo:.2f}),({HOOK_HOLD:.2f}-t)/{_fo:.2f},1)"
        )

        if _use_textfile:
            _ff_txt = _overlay_txt.replace(chr(92), "/").replace(":", chr(92) + ":")
            _text_src = f"textfile='{_ff_txt}':expansion=none"
        else:
            _text_src = f"text='{escaped}'"
        drawtext_filter = (
            f"drawtext={_text_src}"
            f":fontfile='{font_path}'"
            f":fontsize=76"
            f":fontcolor=white"
            f":line_spacing=10"
            f":x=(w-text_w)/2"
            f":y=h*0.13"
            f":enable='between(t,0,{HOOK_HOLD:.2f})'"
            f":alpha='{alpha_expr}'"
            f":box=1:boxcolor=black@0.35:boxborderw=18"
            f":shadowx=3:shadowy=3:shadowcolor=black@0.8"
            f":borderw=4:bordercolor=black"
        )

        cmd = [
            ffmpeg, "-y",
            "-i", video_path,
            "-vf", drawtext_filter,
            *_VENC_ARGS,
            "-c:a", "copy",
            "-pix_fmt", "yuv420p",
            output_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode == 0 and Path(output_path).exists():
            return output_path
        log(f"    _add_hook_text_overlay ffmpeg error: {result.stderr[:300]}")
        return video_path
    except Exception as e:
        log(f"    _add_hook_text_overlay exception: {e}")
        return video_path


# ---------------------------------------------------------------------------
# Helper: fallback solid-colour vertical plate
# ---------------------------------------------------------------------------

def _make_fallback_image(output_path: str, ffmpeg: str = "ffmpeg") -> str:
    """Generate a dark solid-colour 1080×1920 PNG as a last resort."""
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        cmd = [
            ffmpeg, "-y",
            "-f", "lavfi",
            "-i", "color=c=0x1a1a2e:s=1080x1920:d=1",
            "-frames:v", "1",
            output_path,
        ]
        subprocess.run(cmd, capture_output=True, timeout=15)
        return output_path
    except Exception:
        return output_path


# ---------------------------------------------------------------------------
# Per-channel processing
# ---------------------------------------------------------------------------

def generate_for_channel(ch_id: str, config: dict, niche_prompts: dict) -> int:
    """Generate all shorts for a single channel. Returns count of successful shorts."""
    from pipeline.ollama_writer import OllamaWriter
    from pipeline.kokoro_tts import KokoroTTS
    from pipeline.fooocus_images import FooocusImages
    from pipeline.ffmpeg_assembler import FFmpegAssembler
    from pipeline.ltx_animator import LTXAnimator
    from pipeline.caption_generator import CaptionGenerator

    global_cfg = config["global"]
    _set_short_encoder(global_cfg)

    writer = OllamaWriter(global_cfg)
    from pipeline.elevenlabs_tts import make_tts
    tts = make_tts(global_cfg)
    assembler = FFmpegAssembler(global_cfg)
    captioner = CaptionGenerator(global_cfg)

    # LTX animator configured for vertical 9:16 shorts output
    ltx_config = {**global_cfg, "ltx_width": 576, "ltx_height": 1024, "ltx_steps": 8}
    animator = LTXAnimator(ltx_config)

    ffmpeg = global_cfg.get("ffmpeg_path", "ffmpeg")

    channel = next((c for c in config["channels"] if c["id"] == ch_id), None)
    if not channel:
        log(f"Channel {ch_id} not found, skipping")
        return 0

    niche = channel["niche"]
    niche_cfg = {**niche_prompts.get(niche, {}), "niche": niche}

    # Find the most recent script.json for this channel
    ch_output = ROOT / "output" / ch_id
    if not ch_output.exists():
        log(f"Output directory not found for {ch_id}, skipping")
        return 0

    dates = sorted(
        [d.name for d in ch_output.iterdir() if d.is_dir() and d.name.isdigit()],
        reverse=True,
    )
    if not dates:
        log(f"No output found for {ch_id}, skipping")
        return 0

    # Find the most recent date that has a script.json
    latest = None
    for d in dates:
        if (ch_output / d / "script.json").exists():
            latest = d
            break
    if not latest:
        log(f"No script.json found in any date folder for {ch_id}, skipping")
        return 0

    script_path = ch_output / latest / "script.json"
    script = json.loads(script_path.read_text())
    shorts_hooks = script.get("shorts_hooks", [])

    # Clean hooks — remove LLM preamble
    clean_hooks = []
    for h in shorts_hooks:
        h = h.strip().strip('"').strip("'")
        if h.lower().startswith("here are") or len(h) < 10:
            continue
        clean_hooks.append(h)

    _shorts_n = max(1, int(channel.get("shorts_per_run", 3)))  # configurable per channel; default 3
    if not clean_hooks:
        # Fallback: extract hooks from script sections
        sections = script.get("script_sections", [])
        for s in sections[:_shorts_n]:
            narration = s.get("narration", "")
            sentences = narration.split(".")
            hook = ". ".join(sentences[:2]).strip() + "."
            if len(hook) > 20:
                clean_hooks.append(hook[:150])

    log(f"\n--- {channel['name']} ({ch_id}) — {len(clean_hooks)} hooks ---")

    shorts_dir = ch_output / latest / "shorts"
    shorts_dir.mkdir(parents=True, exist_ok=True)

    # Collect available images from the long-form output
    images_dir = ch_output / latest / "images"
    image_files = sorted(images_dir.glob("*.png")) if images_dir.exists() else []

    channel_shorts = 0

    for i, hook in enumerate(clean_hooks[:_shorts_n]):
        log(f"  Short {i+1}/{_shorts_n}: {hook[:60]}...")
        try:
            # ------------------------------------------------------------------
            # Step 1: Generate short script
            # ------------------------------------------------------------------
            # Prefer the richer generate_short_script() which returns sections and
            # a proper visual_prompt; fall back to the simpler generate_shorts_script().
            visual_prompt = ""
            if hasattr(writer, "generate_short_script"):
                topic_dict = {
                    "title": script.get("title", hook[:60]),
                    "hook": hook,
                    "curiosity_angle": hook,
                }
                short_script = writer.generate_short_script(topic_dict, niche_cfg, channel)
                narration = short_script.get("narration", "")
                visual_prompt = short_script.get("visual_prompt", "")
            else:
                short_script = writer.generate_shorts_script(hook, niche_cfg)
                narration = short_script.get("narration", "")

            # Defensive coercion: LLM occasionally returns narration as list or dict
            if isinstance(narration, list):
                narration = " ".join(str(s) for s in narration)
            elif isinstance(narration, dict):
                # Flatten section dict: join all string values
                narration = " ".join(str(v) for v in narration.values() if v)
            narration = str(narration or "").strip()

            if not narration or not narration.strip():
                # Fallback A: richer generator returned empty -> try simpler generator
                try:
                    fb = writer.generate_shorts_script(hook, niche_cfg)
                    fb_narr = fb.get("narration", "")
                    if isinstance(fb_narr, list):
                        fb_narr = " ".join(str(s) for s in fb_narr)
                    elif isinstance(fb_narr, dict):
                        fb_narr = " ".join(str(v) for v in fb_narr.values() if v)
                    narration = str(fb_narr or "").strip()
                except Exception as _e:
                    log(f"    Short {i+1}: fallback generate_shorts_script failed: {_e}")
                # Fallback B: build a Short from the parent video's validated script
                if not narration:
                    for _s in (script.get("script_sections", []) or []):
                        _seed = str(_s.get("narration", "")).strip() if isinstance(_s, dict) else ""
                        if _seed:
                            # ~110 words of body → lands the fallback Short in the 40-55s
                            # sweet spot alongside the LLM path. No "Follow for more." — a
                            # channel-promo line in the audio is banned (wastes the loop).
                            _body = " ".join(_seed.split()[:110])
                            narration = f"{hook.strip().rstrip('.')}. {_body}"
                            break
                # Cap fallback narration to the Shorts sweet spot (~135 words, ~48-52s TTS,
                # under the 60s quality-gate ceiling so it is not skipped at the duration gate).
                if narration:
                    _wlist = narration.split()
                    if len(_wlist) > 135:
                        _trunc = " ".join(_wlist[:135])
                        _dot = _trunc.rfind(".")
                        narration = (_trunc[: _dot + 1] if _dot >= 40 else _trunc + ".").strip()
                if narration:
                    log(f"    Short {i+1}: recovered narration via fallback ({len(narration.split())} words)")
                else:
                    log(f"    Short {i+1}: empty narration after fallbacks, skipping")
                    continue

            narration = narration.strip().strip('"').strip("'")
            # Remove any preamble line
            if narration.lower().startswith("here"):
                lines = narration.split("\n", 1)
                if len(lines) > 1:
                    narration = lines[1].strip()

            word_count = len(narration.split())
            log(f"    Narration: {word_count} words")

            # ------------------------------------------------------------------
            # Step 2: TTS
            # ------------------------------------------------------------------
            audio_path = str(shorts_dir / f"short_{i:02d}.wav")
            tts.synthesize(narration, audio_path, channel.get("voice", "af_heart"))
            dur = tts.get_audio_duration(audio_path)
            log(f"    Audio: {dur:.1f}s")

            if dur > 65:
                log(f"    WARNING: Duration {dur:.1f}s > 65s, skipping")
                continue

            # Hard floor: YouTube's Shorts shelf requires >= 15s. Today's run
            # had 14 Shorts rejected as "too short" — pure compute waste.
            # Pad with end-silence (so the visual outro lingers) until we
            # clear 16s comfortably. Anything <8s gets re-narrated; below
            # 8s the LLM gave us a fragment, padding 8s of silence would
            # be obviously broken.
            MIN_SHORT_DUR = 16.0
            if dur < 8.0:
                log(f"    WARNING: narration only {dur:.1f}s ({word_count} words) — "
                    f"too fragmentary to pad, skipping")
                continue
            if dur < MIN_SHORT_DUR:
                pad_sec = (MIN_SHORT_DUR - dur) + 0.5
                padded_path = str(shorts_dir / f"short_{i:02d}_padded.wav")
                pad_cmd = [
                    ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
                    "-i", audio_path,
                    "-af", f"apad=pad_dur={pad_sec:.2f}",
                    "-c:a", "pcm_s16le",
                    padded_path,
                ]
                pad_res = subprocess.run(pad_cmd, capture_output=True, text=True, timeout=60)
                if pad_res.returncode == 0 and Path(padded_path).exists():
                    audio_path = padded_path
                    dur = tts.get_audio_duration(audio_path)
                    log(f"    Padded to {dur:.1f}s (was {dur - pad_sec:.1f}s, added {pad_sec:.1f}s silence)")
                else:
                    log(f"    WARNING: pad failed ({pad_res.stderr[-200:]}), shipping original")

            # ------------------------------------------------------------------
            # Step 3: Select / generate source image
            # ------------------------------------------------------------------
            # Take SEVERAL distinct stills, not one. assemble_short cuts between
            # them on a fast Shorts rhythm; handing it a single image is what made
            # every Short a motionless 15-60s slideshow of one slide (LTX, the
            # intended motion source, is permanently disabled). Stride through the
            # pool from a per-short offset so short #2 doesn't reuse short #1's
            # pictures.
            # 40-55s Shorts hold ~16-21 cut slots at the ~2.6s Shorts cadence; pull a
            # wider pool of distinct stills so the assembler cycles through more unique
            # visuals before repeating (static/repetitive frames kill retention).
            _WANT = 8
            if image_files:
                pool = [str(p) for p in image_files]
                start = (i * _WANT) % len(pool)
                source_imgs = [pool[(start + k) % len(pool)]
                               for k in range(min(_WANT, len(pool)))]
            else:
                log(f"    No long-form images found, generating fallback plate")
                fb = str(shorts_dir / f"short_bg_{i:02d}.png")
                _make_fallback_image(fb, ffmpeg)
                source_imgs = [fb]
            source_img = source_imgs[0]   # kept for the LTX path below

            # ------------------------------------------------------------------
            # Step 4: Smart-crop each to 9:16 vertical
            # ------------------------------------------------------------------
            cropped_imgs = []
            for k, src in enumerate(source_imgs):
                dst = str(shorts_dir / f"short_crop_{i:02d}_{k:02d}.png")
                c = _smart_crop_to_vertical(src, dst, ffmpeg)
                if c and Path(c).exists():
                    cropped_imgs.append(c)
            if not cropped_imgs:
                log(f"    Vertical crop failed for short {i+1}, skipping")
                continue
            cropped_img = cropped_imgs[0]
            log(f"    Cropped to vertical: {len(cropped_imgs)} visual(s)")

            # ------------------------------------------------------------------
            # Step 5: Animate the 9:16 image with LTXAnimator
            # ------------------------------------------------------------------
            clip_path = str(shorts_dir / f"short_clip_{i:02d}.mp4")
            if not visual_prompt:
                visual_prompt = (
                    f"cinematic vertical 9:16 short clip, smooth motion, "
                    f"{hook[:100]}, high quality, dynamic"
                )
            animation_prompt = (
                "cinematic vertical 9:16 camera movement, smooth motion, "
                f"{visual_prompt[:250]}, "
                "high quality, dynamic scene"
            )

            animated_clip = animator.animate_image(cropped_img, animation_prompt, clip_path)
            if animated_clip:
                log(f"    LTX clip generated: {Path(animated_clip).name}")
            else:
                log(f"    LTX animation failed, will use still image as loop source")
                animated_clip = None

            # ------------------------------------------------------------------
            # Step 6: Assemble looped video (animated clip or still fallback)
            # ------------------------------------------------------------------
            assembled_path = str(shorts_dir / f"short_assembled_{i:02d}.mp4")

            if animated_clip and Path(animated_clip).exists():
                assembled = _build_looped_video(animated_clip, audio_path, dur, assembled_path, ffmpeg)
            else:
                # assemble_short cuts between ALL of these (was: [cropped_img] --
                # a single still held for the whole Short).
                assembled = assembler.assemble_short(audio_path, cropped_imgs, assembled_path)

            if not assembled or not Path(assembled).exists():
                log(f"    Assembly failed for short {i+1}, skipping")
                continue
            log(f"    Assembled: {Path(assembled).name} ({dur:.1f}s)")

            # ------------------------------------------------------------------
            # Step 7: Burn hook text overlay (first 3 seconds)
            # ------------------------------------------------------------------
            hook_overlay_path = str(shorts_dir / f"short_hook_{i:02d}.mp4")
            # Pass the FULL hook: _wrap_hook_text now word-wraps to 3 lines and
            # truncates on a word boundary. Slicing to [:50] here was cutting
            # hooks mid-word before the wrapper ever saw them.
            video_with_hook = _add_hook_text_overlay(assembled, hook, hook_overlay_path, ffmpeg)
            if video_with_hook != assembled:
                log(f"    Hook overlay burned: {Path(video_with_hook).name}")
            else:
                log(f"    Hook overlay skipped (font not found or FFmpeg error)")

            # ------------------------------------------------------------------
            # Step 8: Burn captions
            # ------------------------------------------------------------------
            captioned_path = str(shorts_dir / f"short_{i+1}_{ch_id}.mp4")
            # CaptionGenerator.burn_subtitles needs an SRT; use transcribe+generate+burn
            srt_segments = captioner.transcribe_audio(audio_path)
            if srt_segments:
                srt_path = str(shorts_dir / f"short_{i+1}_{ch_id}.srt")
                srt_written = captioner.generate_srt(srt_segments, srt_path)
                if srt_written:
                    final_path = captioner.burn_subtitles(video_with_hook, srt_path, captioned_path)
                    if final_path != video_with_hook:
                        log(f"    Captions burned: {Path(final_path).name}")
                    else:
                        log(f"    Caption burn failed, using hook-overlay video as final")
                        final_path = video_with_hook
                else:
                    log(f"    SRT write failed, skipping captions")
                    final_path = video_with_hook
            else:
                log(f"    Transcription returned no segments, skipping captions")
                # Copy hook-overlay video to the expected final name
                import shutil
                shutil.copy2(video_with_hook, captioned_path)
                final_path = captioned_path

            # ------------------------------------------------------------------
            # Step 9: Quality gate
            # ------------------------------------------------------------------
            if not Path(final_path).exists():
                log(f"    Final file missing: {final_path}")
                continue

            final_dur = assembler.get_duration(final_path)
            file_size = Path(final_path).stat().st_size

            if final_dur < 15:
                log(f"    QUALITY GATE FAIL: duration {final_dur:.1f}s < 15s minimum")
                continue
            if final_dur > 60:
                log(f"    QUALITY GATE FAIL: duration {final_dur:.1f}s > 60s maximum")
                continue
            if file_size < 50 * 1024:
                log(f"    QUALITY GATE FAIL: file {file_size // 1024}KB < 50KB minimum")
                continue

            size_mb = file_size / (1024 * 1024)
            log(f"    SHORT COMPLETE: {final_path}")
            log(f"    Duration: {final_dur:.1f}s | Size: {size_mb:.2f} MB")
            channel_shorts += 1

            # Clean up intermediate working files (keep final)
            for tmp in [assembled_path, hook_overlay_path]:
                try:
                    if tmp != final_path and Path(tmp).exists():
                        Path(tmp).unlink()
                except Exception:
                    pass

        except Exception as e:
            log(f"    FAILED: {type(e).__name__}: {e}")
            log(f"    {traceback.format_exc()}")
            continue

    return channel_shorts


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    """Entry point: parse args, load config, run for each requested channel."""
    config = json.loads((ROOT / "config" / "channels.json").read_text())
    niche_prompts = json.loads((ROOT / "config" / "niche_prompts.json").read_text())

    filter_ids = sys.argv[1:] if len(sys.argv) > 1 else [ch["id"] for ch in config["channels"]]
    log(f"=== SHORTS GENERATOR: {filter_ids} ===")

    total = 0
    for ch_id in filter_ids:
        total += generate_for_channel(ch_id, config, niche_prompts)

    log(f"\n=== SHORTS COMPLETE: {total} shorts generated ===")


if __name__ == "__main__":
    main()
