"""
Ollama Script Writer — Generates video scripts, titles, descriptions, and SEO tags
using a local Ollama LLM instance.
"""

import json
import logging
import re
from datetime import datetime

import requests

logger = logging.getLogger("pipeline.ollama_writer")


class OllamaWriter:
    def __init__(self, config: dict):
        self.host = config.get("ollama_host", "http://localhost:11434")
        self.timeout = 1200
        # Provider: "gemini" (Google AI Studio API) or "ollama" (local). Gemini is far
        # faster + more reliable for JSON than the local models; Ollama stays as fallback.
        self.provider = str(config.get("llm_provider", "ollama")).lower()
        self.gemini_model = config.get("gemini_model", "gemini-2.0-flash")
        self._gemini = None
        self._gemini_exhausted = False  # set True once Gemini's daily quota is hit -> Ollama backup
        if self.provider == "gemini":
            self._init_gemini(config)
        # Only probe Ollama when we're actually using it (avoid disturbing other GPU work).
        if self.provider == "ollama":
            self.model = self._best_available_model(config.get("ollama_model", "qwen2.5:7b"))
        else:
            self.model = config.get("ollama_model", "qwen2.5:7b")

    def _init_gemini(self, config: dict):
        """Set up the Google Gemini client. API key resolved in order:
        config 'gemini_api_key' -> config/gemini_api_key.txt -> GEMINI_API_KEY/GOOGLE_API_KEY env."""
        import os
        from pathlib import Path as _P
        key = (config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
               or os.environ.get("GOOGLE_API_KEY"))
        if not key:
            kf = _P(__file__).resolve().parent.parent / "config" / "gemini_api_key.txt"
            try:
                if kf.exists():
                    key = kf.read_text(encoding="utf-8").strip()
            except Exception:
                pass
        if not key:
            logger.error("OllamaWriter: llm_provider=gemini but no API key found "
                         "(set GEMINI_API_KEY, config 'gemini_api_key', or "
                         "config/gemini_api_key.txt). Falling back to Ollama.")
            self.provider = "ollama"
            return
        try:
            from google import genai
            self._gemini = genai.Client(api_key=key)
            logger.info(f"OllamaWriter: Gemini provider, model '{self.gemini_model}'")
        except Exception as e:
            logger.error(f"Gemini init failed ({e}); falling back to Ollama provider")
            self.provider = "ollama"

    def _best_available_model(self, preferred: str) -> str:
        """Probe Ollama /api/tags and return the best available model.

        Preference order: exact preferred ? largest llama3.x ? any model.
        Falls back to preferred string if Ollama is unreachable.
        """
        RANKED = [
            "qwen2.5:14b",
            "qwen2.5:7b",
            "llama3.1:8b",
            "llama3.2:latest",
            "llama3.2:3b",
            "glm-4.7-flash:latest",
            "mistral",
        ]
        try:
            r = requests.get(f"{self.host}/api/tags", timeout=3)
            r.raise_for_status()
            models = [m.get("name", "") for m in r.json().get("models", [])]
            if preferred in models:
                logger.info(f"OllamaWriter: using preferred model '{preferred}'")
                return preferred
            for candidate in RANKED:
                if candidate in models:
                    logger.info(f"OllamaWriter: preferred '{preferred}' not found; using '{candidate}'")
                    return candidate
            if models:
                chosen = models[0]
                logger.info(f"OllamaWriter: no ranked model found; using first available '{chosen}'")
                return chosen
        except Exception as e:
            logger.warning(f"OllamaWriter: could not probe Ollama models ({e}); defaulting to '{preferred}'")
        return preferred

    def _call(self, prompt: str, system: str = "", temperature: float = 0.8, num_predict: int = 800) -> str:
        """Generate text via the configured provider (Gemini primary, Ollama backup)."""
        if self.provider == "gemini" and self._gemini is not None and not self._gemini_exhausted:
            try:
                return self._call_gemini(prompt, system=system, temperature=temperature, num_predict=num_predict)
            except Exception as e:
                msg = str(e)
                # Daily free-tier quota exhausted: don't waste retries — permanently
                # fall back to local Ollama for the rest of this run. (Per-minute caps
                # are transient, so re-raise those and let _call_with_retry back off.)
                daily_quota = ("PerDay" in msg or
                               ("RESOURCE_EXHAUSTED" in msg and "PerMinute" not in msg))
                server_down = ("503" in msg or "UNAVAILABLE" in msg or "500" in msg or "INTERNAL" in msg or "overloaded" in msg.lower() or "high demand" in msg.lower())
                if daily_quota or server_down:
                    logger.warning("Gemini daily quota exhausted — falling back to Ollama for the rest of this run.")
                    self._gemini_exhausted = True
                    return self._call_ollama(prompt, system=system, temperature=temperature, num_predict=num_predict)
                raise
        return self._call_ollama(prompt, system=system, temperature=temperature, num_predict=num_predict)

    def _call_gemini(self, prompt: str, system: str = "", temperature: float = 0.8, num_predict: int = 800) -> str:
        """Generate text via the Gemini API (google-genai SDK). Returns the text content."""
        from google.genai import types
        max_tokens = max(256, min(8192, int(num_predict)))
        cfg = types.GenerateContentConfig(
            temperature=max(0.0, min(2.0, float(temperature))),  # Gemini range is 0-2
            max_output_tokens=max_tokens,
        )
        if system:
            cfg.system_instruction = system
        # Gemini 2.5 models "think" by default, which silently eats the output-token
        # budget and can return empty/truncated JSON. Disable it for reliable, cheap
        # script generation (we don't need chain-of-thought for templated prompts).
        if "2.5" in self.gemini_model:
            try:
                cfg.thinking_config = types.ThinkingConfig(thinking_budget=0)
            except Exception:
                pass
        try:
            resp = self._gemini.models.generate_content(
                model=self.gemini_model, contents=prompt, config=cfg,
            )
            return (getattr(resp, "text", "") or "")
        except Exception as e:
            logger.error(f"Gemini call failed: {e}")
            raise

    def _call_ollama(self, prompt: str, system: str = "", temperature: float = 0.8, num_predict: int = 800) -> str:
        """Send a prompt to Ollama and return the response text.

        Auto JSON mode (2026-07-11 fix): when the prompt asks for JSON, use
        Ollama structured output (format=json) so markdown fences / preamble
        can never break parsing, and raise the token ceiling so verbose models
        (gemma4) cannot truncate mid-JSON — the truncation that killed every
        channel run on 2026-07-10. num_predict is a MAX, not a target: short
        outputs are unaffected by the higher cap.
        """
        wants_json = "json" in prompt.lower()
        if wants_json:
            num_predict = max(num_predict, 1600)
        payload = {
            "model": self.model,
            "prompt": prompt,
            "system": system,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": num_predict},
        }
        if wants_json:
            payload["format"] = "json"
        try:
            r = requests.post(
                f"{self.host}/api/generate", json=payload, timeout=self.timeout
            )
            r.raise_for_status()
            return r.json().get("response", "")
        except Exception as e:
            logger.error(f"Ollama call failed: {e}")
            raise

    def _call_with_retry(self, prompt: str, system: str = "", temperature: float = 0.8, retries: int = 3, num_predict: int = 800) -> str:
        """Call Ollama with retry on empty/timeout. Falls back to lower temperature on retry."""
        result = ""
        for attempt in range(retries + 1):
            try:
                result = self._call(prompt, system=system, temperature=temperature, num_predict=num_predict)
                if result and len(result.strip()) > 10:
                    return result
                if attempt < retries:
                    logger.warning(f"Empty response on attempt {attempt+1}, retrying...")
                    temperature = max(0.4, temperature - 0.15)
            except Exception as e:
                if attempt < retries:
                    import time, re
                    msg = str(e)
                    is_rate = ("429" in msg or "RESOURCE_EXHAUSTED" in msg
                               or "rate" in msg.lower() or "quota" in msg.lower())
                    if is_rate:
                        # Honor Gemini's suggested retry delay ("retry in 5s" / retryDelay: '5s'),
                        # else back off generously so a per-minute free-tier cap clears.
                        m = re.search(r"(\d+(?:\.\d+)?)\s*s", msg)
                        delay = (float(m.group(1)) + 1.5) if m else 12.0
                        logger.warning(f"LLM rate-limited (attempt {attempt+1}); sleeping {delay:.0f}s")
                    else:
                        delay = 2.0
                        logger.warning(f"LLM call failed attempt {attempt+1}: {e}; retrying...")
                    time.sleep(delay)
                else:
                    raise
        return result  # return whatever we got

    @staticmethod
    def _find_json_by_braces(text: str) -> str | None:
        """Find the first balanced JSON object in text using brace counting."""
        start = text.find("{")
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"' and not escape:
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
        return None

    @staticmethod
    def _json_repair(candidate: str) -> str:
        """Repair the JSON-almost-valid output local models actually emit.

        qwen2.5 regularly writes \\' inside strings — an escape that is NOT
        legal JSON — which made every extraction strategy fail and (until
        2026-07-05) dumped raw JSON into narration that Kokoro then read
        aloud. Also strips trailing commas and raw control chars.
        """
        fixed = candidate.replace("\\'", "'")
        fixed = re.sub(r",\s*([}\]])", r"\1", fixed)
        fixed = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", " ", fixed)
        return fixed

    def _extract_json(self, text: str) -> dict:
        """Try to extract a JSON object from LLM output with multiple strategies."""
        if not text or not text.strip():
            return {}

        # Strategy 1: Direct parse
        try:
            return json.loads(text.strip())
        except json.JSONDecodeError:
            pass

        # Strategy 2: Strip markdown code fences first
        cleaned = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("```")
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

        # Strategy 3: Brace-counting extraction (handles LLM wrapping text around JSON)
        for source in [cleaned, text]:
            candidate = self._find_json_by_braces(source)
            if candidate:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    # Strategy 4: Repair common LLM errors (invalid \' escapes,
                    # trailing commas, raw control chars) and retry
                    try:
                        return json.loads(self._json_repair(candidate))
                    except json.JSONDecodeError:
                        pass

        # Strategy 4.5: repair TRUNCATED JSON (model hit the num_predict cap
        # mid-generation — the 2026-07-10 gemma4 failure mode). Close an
        # unterminated string, drop a dangling partial key, close open braces.
        for source in [cleaned, text]:
            repaired = self._close_truncated_json(source)
            if repaired:
                try:
                    return json.loads(repaired)
                except json.JSONDecodeError:
                    try:
                        return json.loads(self._json_repair(repaired))
                    except json.JSONDecodeError:
                        pass

        # Strategy 5: Greedy regex as last resort
        json_match = re.search(r"\{[\s\S]*\}", cleaned)
        if json_match:
            try:
                return json.loads(json_match.group())
            except json.JSONDecodeError:
                try:
                    return json.loads(self._json_repair(json_match.group()))
                except json.JSONDecodeError:
                    pass

        logger.warning("Could not extract JSON from LLM response, using fallback")
        return {}

    @staticmethod
    def _close_truncated_json(text: str) -> str:
        """Best-effort close of JSON that was cut off mid-generation.

        Walks from the first '{' tracking string/escape state and open
        braces/brackets. If the JSON is unbalanced (truncated): closes an
        unterminated string, strips a dangling partial key (e.g. ', "b_') or
        trailing comma/colon, then appends the missing closers.
        Returns '' when the text is balanced or doesn't look like JSON.
        """
        start = text.find("{")
        if start == -1:
            return ""
        s = text[start:].rstrip()
        if s.endswith("```"):
            s = s[:-3].rstrip()
        stack = []
        in_str = False
        esc = False
        for ch in s:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
        if not stack:
            return ""  # balanced — not a truncation problem
        if in_str:
            s += '"'
        # Drop a dangling partial key ', "visu' / ', "key":' left at the end.
        s = re.sub(r',\s*"[^"\n]*"?\s*(:\s*)?$', "", s.rstrip())
        s = re.sub(r"[,:]\s*$", "", s.rstrip())
        for opener in reversed(stack):
            s += "]" if opener == "[" else "}"
        return s

    # Stage directions gemma-style models embed in narration. If they reach
    # Kokoro they get read ALOUD ("Fast-paced, urgent tone. Stop scrolling...").
    # Conservative on purpose: any parenthetical at the very start, plus inline
    # parentheticals that BEGIN with a known direction word.
    _LEADING_DIRECTION_RE = re.compile(r"^\s*[\(\[][^)\]]{0,120}[\)\]]\s*")
    _INLINE_DIRECTION_RE = re.compile(
        r"[\(\[]\s*(?:pause|beat|music|sfx|silence|breath|tone|voice[:\s]|"
        r"narrator|whisper|dramatic|urgent|upbeat|fast-paced|slow|cut to|"
        r"sound of)[^)\]]{0,80}[\)\]]\s*",
        re.IGNORECASE,
    )

    @staticmethod
    def _cap_narration(text: str, max_words: int = 350) -> str:
        """Hard-cap a single section's narration so a misbehaving model cannot emit
        a runaway multi-thousand-word section (which produces ~19 min of TTS audio
        and times out FFmpeg Ken Burns assembly). Truncates at the last sentence
        boundary at or before max_words. Normal sections (~228 words) pass through.
        Also strips stage directions ('(Fast-paced, urgent tone)') so TTS never
        reads them aloud."""
        if not text:
            return text
        text = OllamaWriter._LEADING_DIRECTION_RE.sub("", str(text), count=1)
        text = OllamaWriter._INLINE_DIRECTION_RE.sub("", text).strip() or str(text)
        words = str(text).split()
        if len(words) <= max_words:
            return text
        truncated = " ".join(words[:max_words])
        cut = max(truncated.rfind(". "), truncated.rfind("! "), truncated.rfind("? "))
        if cut > len(truncated) * 0.5:
            return truncated[: cut + 1]
        return truncated.rstrip(",;: ") + "."

    @staticmethod
    def _salvage_narration(raw: str) -> str:
        """Return usable narration text from raw LLM output, or '' if none.

        Plain prose passes through (minus a leading 'Here is…' preamble line).
        JSON-ish output is NEVER returned verbatim — we try to pull just the
        "narration" string field; failing that, return '' so the caller can
        retry or abort. Guards the TTS from ever reading JSON aloud.
        """
        if not raw or not raw.strip():
            return ""
        text = raw.strip().strip('"').strip("'")
        if text.startswith("Here"):
            lines = text.split("\n", 1)
            if len(lines) > 1:
                text = lines[1].strip()
        looks_json = text.lstrip().startswith("{") or '"narration"' in text[:400]
        if not looks_json:
            return text
        m = re.search(r'"narration"\s*:\s*"((?:[^"\\]|\\.)*)"', text, re.S)
        if m:
            val = m.group(1)
            try:
                val = json.loads('"' + val + '"')  # proper unescape (\n, \", \uXXXX)
            except Exception:
                val = (val.replace('\\"', '"').replace("\\n", "\n")
                          .replace("\\'", "'").replace("\\t", " "))
            val = str(val).strip()
            if len(val.split()) >= 25:  # sanity: a real section, not a stub
                return val
        return ""

    def _expand_narration(self, narration: str, target_words: int, section_title: str,
                          video_title: str, system: str = "") -> str:
        """Lengthen a too-short section toward target_words with genuine value.

        Local models routinely under-deliver on a '{N}-word' instruction, which
        leaves videos shorter than intended (less watch time and ad inventory).
        This rewrites the section to land near target by ADDING concrete detail —
        not filler, repetition, or fabricated studies/stats. Returns the original
        narration unchanged if expansion fails or doesn't actually make it longer."""
        if not narration:
            return narration
        cur = len(narration.split())
        prompt = (
            f'This narration for the section "{section_title}" of the video "{video_title}" '
            f'is too short ({cur} words). Rewrite it to about {target_words} words.\n'
            "Keep the same opening line, speaking voice, and meaning. ADD genuine value: a concrete "
            "detail, a vivid real-world example, a clear explanation of how or why it works. Do NOT pad "
            "with filler, do NOT repeat sentences, and do NOT invent studies, statistics, institutions, "
            "or quotes.\n"
            "Return ONLY the rewritten spoken narration — no preamble, no headings, no notes.\n\n"
            f"CURRENT NARRATION:\n{narration}"
        )
        try:
            num_predict = min(1600, int(target_words * 2.2) + 120)
            raw = self._call_with_retry(prompt, system=system, temperature=0.7, num_predict=num_predict)
        except Exception as e:
            logger.warning(f"_expand_narration failed: {e}")
            return narration
        expanded = self._strip_preamble(raw.strip().strip('"').strip("'")).strip()
        if not expanded or expanded.startswith("{"):  # model returned junk/JSON — keep original
            return narration
        return expanded if len(expanded.split()) > cur else narration

    # --- premium-grade response sanitizers (added by apply_pipeline_patches) ---
    @staticmethod
    def _strip_rating_artifacts(text: str) -> str:
        """Remove LLM self-rating artifacts that leak into a title.

        The topic/SEO prompts ask the model for a `viral_score`, and the model
        sometimes prepends or embeds that score in the title text, e.g.
        '4.5/10**: The Best Roth IRA Strategy', 'Viral Score: 9/10 - Real Title',
        or 'Real Title (viral score: 8)'. These wreck click-through rate and look
        broken to a viewer scrolling the feed, so we strip them before publish.

        Only explicit rating shapes match (a '/10' ratio, or an explicit
        score/rating label). Legitimate numeric titles like
        '7 AI Tools That Replace Your Job' are left untouched.
        """
        if not text:
            return text
        t = str(text)
        # Embedded parenthetical/bracketed rating anywhere: (viral score: 8), [rating 7/10]
        t = re.sub(
            r"[\(\[]\s*(?:viral\s*score|score|rating)\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*/\s*10)?\s*[\)\]]",
            "",
            t,
            flags=re.IGNORECASE,
        )
        # Leading labeled rating: 'Viral Score: 9/10 -', 'Rating 7:', 'Score = 8'
        t = re.sub(
            r"^\s*(?:viral\s*score|score|rating)\s*[:=]?\s*\d+(?:\.\d+)?(?:\s*/\s*10)?\s*[\*\)\]]*\s*[-–—:|.]*\s*",
            "",
            t,
            flags=re.IGNORECASE,
        )
        # Leading bare ratio rating: '4.5/10**:', '8 / 10 -'
        t = re.sub(
            r"^\s*\d+(?:\.\d+)?\s*/\s*10\b\s*[\*\)\]]*\s*[-–—:|.]*\s*",
            "",
            t,
        )
        return t.strip()

    @staticmethod
    def _clean_title(text: str) -> str:
        """Strip markdown leaders, surrounding quotes, rating artifacts, junk."""
        if not text:
            return text
        t = str(text).strip()
        t = re.sub(r"^[\*#>\-\s]+", "", t)
        t = OllamaWriter._strip_rating_artifacts(t)
        t = re.sub(r"[\*#>\s]+$", "", t)
        if (t.startswith('"') and t.endswith('"')) or (
            t.startswith("'") and t.endswith("'")
        ):
            t = t[1:-1].strip()
        # Removing a rating can expose a leftover leading colon/dash/markdown — clean again.
        t = re.sub(r"^[\*#>\-:|\s]+", "", t)
        # 2026-07-11: repair dangling percent placeholders (model drops the
        # number) — live Shorts shipped titled "% of historians skip these
        # 2026 finds". "Most historians skip..." keeps the hook w/o the glitch.
        t = re.sub(r"^\s*%\s*of\s+(?=(?:you|us|them)\b)", "Most of ", t, flags=re.IGNORECASE)
        t = re.sub(r"^\s*%\s*(?:of\s+)?", "Most ", t)
        t = re.sub(r"(?<!\d)\s%\s+of\s+", " most ", t)
        return t.strip()

    _PREAMBLE_RE = re.compile(
        r"^(here(?:\'s| is| are)|sure[,!]?|here we go|alright|okay|"
        r"description|title|tags?)\b[^\n]*",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_preamble(cls, text: str) -> str:
        """Remove a leading LLM preamble line ('Here are three hooks for...')."""
        if not text:
            return text
        return cls._PREAMBLE_RE.sub("", text, count=1).strip()

    @classmethod
    def _filter_preamble_lines(cls, lines):
        """Drop short lines whose first 120 chars match a known preamble."""
        out = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if cls._PREAMBLE_RE.match(line) and ":" in line[:120] and len(line) < 140:
                continue
            out.append(line)
        return out

    # Words that carry zero information scent on a thumbnail by themselves.
    _HYPE_ONLY_WORDS = {
        "shocking", "shocked", "exposed", "expose", "secret", "secrets",
        "truth", "nobody", "never", "insane", "crazy", "unbelievable",
        "revealed", "reveal", "this", "that", "yet", "explained", "everyone",
        "right", "now", "the", "a", "an", "of", "is", "it", "you", "your",
        "wont", "won't", "believe", "must", "see", "watch", "wow", "omg",
        "fail", "warning", "big", "huge", "massive", "one", "no",
    }

    @classmethod
    def _informative_thumb_text(cls, thumb_text: str, title: str) -> str:
        """Guarantee thumbnail text names something concrete (2026 packaging:
        specificity beats hype). Hype-only text ('SHOCKING TRUTH') is rebuilt
        from the title's number + content nouns ('3 AI TOOLS')."""
        t = re.sub(r"\s+", " ", str(thumb_text or "")).strip()
        words = re.findall(r"[A-Za-z$%#0-9'-]+", t)
        has_digit = any(re.search(r"\d", w) for w in words)
        informative = has_digit or any(
            re.sub(r"'s$", "", w.lower()) not in cls._HYPE_ONLY_WORDS
            for w in words
        )
        if words and informative and len(words) <= 4:
            return t.upper()
        title_words = re.findall(r"[A-Za-z0-9$%-]+", str(title or ""))
        num = next((w for w in title_words if re.search(r"\d", w)), "")
        skip = cls._HYPE_ONLY_WORDS | {
            "why", "how", "what", "when", "and", "or", "to", "in", "for",
            "are", "will", "with", "most", "people", "things", "ways",
        }
        content = [w for w in title_words
                   if w.lower() not in skip and not re.search(r"\d", w)]
        parts = ([num] if num else []) + content[: (2 if num else 3)]
        out = " ".join(parts).upper().strip()
        return out or (t.upper() if t else "THE REAL STORY")

    @staticmethod
    def _score_title(title: str) -> int:
        """Score a title for YouTube CTR potential (2026 algorithm signals)."""
        POWER_WORDS = {
            "secret", "truth", "real", "hidden", "actually", "why", "how",
            "reveal", "shocking", "never", "always", "most", "best",
            "exposed", "nobody", "everyone", "stop", "warning", "mistake",
            "wrong", "finally", "instantly", "proven", "lied",
            "tested", "tried", "free", "fast", "results", "before",
        }
        CONTRAST_WORDS = {"vs", "versus", "actually", "wrong", "lied", "not"}
        STRONG_STARTERS = {"why", "how", "the", "stop", "never", "this", "what"}
        score = 0
        t = title.strip()
        t_lower = t.lower()
        words = t.split()
        first_word = words[0].lower() if words else ""

        # Length 40-60 chars (2026: search truncates ~60-70, mobile ~45-50;
        # large-scale studies show shorter titles outperform — front-load payoff)
        if 40 <= len(t) <= 60:
            score += 4
        elif 30 <= len(t) <= 65:
            score += 2
        elif len(t) > 70:
            score -= 5
        elif len(t) > 65:
            score -= 3

        # Contains a specific number (big CTR signal)
        if re.search(r"\d", t):
            score += 3

        # Power words (max +6)
        power_hits = sum(1 for w in POWER_WORDS if w in t_lower.split())
        score += min(power_hits * 2, 6)

        # Starts with a high-CTR word
        if first_word in STRONG_STARTERS:
            score += 2

        # Contains a contrast/curiosity word
        if any(w in t_lower.split() for w in CONTRAST_WORDS):
            score += 2

        # Question mark (question hook format)
        if "?" in t:
            score += 2

        # Year reference (recency signal)
        if re.search(r"\b202[5-9]\b", t):
            score += 2

        # First-person proof format (I tested..., I tried...) - strong 2026 CTR
        if re.search(r"\bi (tested|tried|spent|made|used|built|ran|did)\b", t_lower):
            score += 2

        # Parenthetical or bracket (secondary CTR boost)
        if re.search(r"[\(\[\|]", t):
            score += 2

        # Ends with "..." — looks truncated to viewers AND the metadata gate
        # blocks these at upload (2026-07-05). Penalize so we never pick one.
        if t.endswith("...") or t.endswith("…"):
            score -= 4

        # Exactly ONE all-caps word = emphasis (good); too many = spammy
        caps_words = [w for w in words if w.isupper() and len(w) > 1 and w.isalpha()]
        if len(caps_words) == 1:
            score += 1
        elif len(caps_words) > 2:
            score -= 2

        # Penalize overly generic openers
        WEAK_OPENERS = ("a ", "an ", "some ", "here ", "in this")
        if any(t_lower.startswith(s) for s in WEAK_OPENERS):
            score -= 2

        # 2026 SEO blend: reward named-tool specificity, penalize hollow
        # clickbait words YouTube now demotes. Guarded so title generation
        # never breaks if the helper is unavailable.
        try:
            from pipeline import seo_optimizer as _seo_qc
            score += _seo_qc.title_ctr_delta(t)
        except Exception:
            pass

        return score

    @staticmethod
    def _score_hook(narration: str) -> int:
        """Score an opening section's retention pull — higher = stickier first 30s.

        Used for in-video hook A/B: generate two opening sections and keep the one
        that scores higher on the signals that actually drive early retention."""
        if not narration or not isinstance(narration, str):
            return 0
        t = narration.strip()
        low = t.lower()
        words = t.split()
        if not words:
            return 0
        score = 0
        # First sentence short and punchy (a long opener loses people).
        first_sentence = re.split(r"(?<=[.!?])\s+", t, maxsplit=1)[0]
        fs_words = len(first_sentence.split())
        if fs_words <= 8:
            score += 4
        elif fs_words <= 12:
            score += 2
        elif fs_words > 22:
            score -= 2
        # Strong opening word.
        first_word = low.split(" ", 1)[0].strip(".,!?")
        if first_word in {"what", "why", "how", "imagine", "stop", "your",
                          "this", "here's", "heres", "i", "picture", "what's"}:
            score += 2
        # A number in the first 12 words.
        if re.search(r"\d", " ".join(low.split()[:12])):
            score += 2
        # Curiosity / FOMO markers (capped).
        FOMO = ("secret", "nobody", "most people", "what if", "never", "truth",
                "actually", "here's why", "before you", "you're doing", "wrong",
                "hidden", "quietly", "shocking", "stop ")
        score += min(sum(1 for m in FOMO if m in low) * 2, 6)
        # Direct second-person address.
        if re.search(r"\byou(r|'re|'ll)?\b", low):
            score += 2
        # Stakes / tension.
        if any(w in low for w in ("mistake", "costing", "losing", "waste", "risk", "broke", "fail")):
            score += 1
        # Generic/weak openers tank retention.
        if any(low.startswith(s) for s in ("in this video", "today we", "hello", "welcome", "hi ", "in today")):
            score -= 4
        # Sentence-rhythm variation (mix of very short and long = momentum).
        sents = [len(s.split()) for s in re.split(r"(?<=[.!?])\s+", t) if s.strip()]
        if sents and min(sents) <= 5 and max(sents) >= 14:
            score += 2
        return score

    @classmethod
    def _parse_description_and_tags(cls, blob: str) -> tuple[str, list[str]]:
        """Robust parser for 'Description:\n"..."\n\nTAGS: a, b, c' style output."""
        if not blob:
            return "", []
        text = blob.strip()
        # Split on TAGS: marker (case-insensitive). If no marker: treat all as desc.
        m = re.search(r"\n*TAGS\s*:\s*", text, flags=re.IGNORECASE)
        if not m:
            return cls._strip_preamble(text).strip().strip('"').strip("'"), []
        desc_blob = text[: m.start()]
        tags_blob = text[m.end():]
        # Drop "Description:" prefix and surrounding quotes
        desc_blob = re.sub(r"^\s*Description\s*:\s*", "", desc_blob, flags=re.IGNORECASE)
        desc_blob = cls._strip_preamble(desc_blob).strip().strip('"').strip("'").strip()
        # Tags: split on comma, drop empties, cap length
        tags = [t.strip().strip('"').strip("'") for t in tags_blob.split(",")]
        tags = [t for t in tags if t and len(t) <= 100][:30]
        return desc_blob, tags
    # --- end premium-grade sanitizers ---

    def _load_trend_seed(self, niche: str) -> str:
      """Load viral-signal seed prompt from state/trends_{niche}.txt if present.
      Written by run_rerun.py via TrendHunter.to_ollama_seed() at start of pipeline run."""
      try:
        from pathlib import Path as _P
        f = _P(__file__).resolve().parent.parent / "state" / f"trends_{niche}.txt"
        if f.exists():
          seed = f.read_text(encoding="utf-8").strip()
          if seed:
            return seed
      except Exception:
        pass
      return ""

    def _load_winner_seed(self, niche: str) -> str:
      """Load best-performer seed from state/winners_{niche}.txt if present.
      Written by run_rerun.py::_prefetch_winners — biases new topics toward the
      angles/formats that have already earned the most views on this channel."""
      try:
        from pathlib import Path as _P
        f = _P(__file__).resolve().parent.parent / "state" / f"winners_{niche}.txt"
        if f.exists():
          seed = f.read_text(encoding="utf-8").strip()
          if seed:
            return seed
      except Exception:
        pass
      return ""

    # --- generation memory + continuity helpers (major-improvement patch) ---
    @staticmethod
    def _state_dir():
        from pathlib import Path as _P
        d = _P(__file__).resolve().parent.parent / "state"
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _load_used_titles(self, niche: str, limit: int = 60) -> list:
        """Recent titles already produced for this niche (newest last).

        Used to stop a daily multi-channel pipeline from regenerating near-identical
        videos, which YouTube treats as repetitious/low-value content."""
        try:
            f = self._state_dir() / f"used_titles_{niche or 'general'}.json"
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    return [str(t) for t in data if t][-limit:]
        except Exception as e:
            logger.warning(f"_load_used_titles failed: {e}")
        return []

    def _record_used_title(self, niche: str, title: str, limit: int = 120) -> None:
        if not title:
            return
        try:
            f = self._state_dir() / f"used_titles_{niche or 'general'}.json"
            titles = []
            if f.exists():
                data = json.loads(f.read_text(encoding="utf-8"))
                if isinstance(data, list):
                    titles = [str(t) for t in data if t]
            titles.append(title.strip())
            f.write_text(json.dumps(titles[-limit:], ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception as e:
            logger.warning(f"_record_used_title failed: {e}")

    @staticmethod
    def _norm_title(t: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", str(t).lower()).strip()

    def _is_duplicate_title(self, title: str, recent: list, threshold: float = 0.6) -> bool:
        """True if `title` substantially repeats a recent one (Jaccard token overlap)."""
        a = set(self._norm_title(title).split())
        if not a:
            return False
        for r in recent:
            b = set(self._norm_title(r).split())
            if not b:
                continue
            if len(a & b) / max(len(a | b), 1) >= threshold:
                return True
        return False

    @staticmethod
    def _continuity_gist(narration: str, max_words: int = 28) -> str:
        """Cheap one-line gist of a section for the next section's context (no extra LLM call)."""
        if not narration:
            return ""
        sentences = re.split(r"(?<=[.!?])\s+", str(narration).strip())
        first = sentences[0] if sentences else ""
        return " ".join(first.split()[:max_words]).strip()

    @staticmethod
    def _last_sentence(narration: str) -> str:
        if not narration:
            return ""
        sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(narration).strip()) if s.strip()]
        return sentences[-1].strip() if sentences else ""

# === PREMIUM_REVENUE_PATCH ===

    def generate_topic(self, niche_config: dict) -> dict:
        """Generate a video topic with title, hook, and section outline."""
        base_prompt = niche_config["topic_generation"]
        today = datetime.now().strftime("%B %d, %Y")

        _trend_seed = self._load_trend_seed(niche_config.get("niche", ""))  # === PREMIUM_REVENUE_PATCH ===
        _winner_seed = self._load_winner_seed(niche_config.get("niche", ""))  # best-performer feedback loop
        _recent_titles = self._load_used_titles(niche_config.get("niche", ""))
        _avoid_block = ""
        if _recent_titles:
            _avoid_lines = "\n".join(f"- {t}" for t in _recent_titles[-25:])
            _avoid_block = (
                "ALREADY COVERED — do NOT repeat, reword, or lightly re-angle any of these recent videos. "
                "Pick a clearly distinct sub-topic, subject, or format:\n" + _avoid_lines + "\n\n"
            )
        prompt = f"""{_trend_seed}

{_winner_seed}

{_avoid_block}{base_prompt}

Today's date is {today}. Make the topic timely and relevant.

REQUIREMENTS — your topic JSON must score high on all four:
1. PATTERN INTERRUPT: The title must stop the scroll. Use a number, a bold contrast, or a named entity. Bad: "AI is changing things." Good: "97% of AI users skip this one setting that triples output quality."
2. CURIOSITY GAP: What do most people NOT know about this topic? The hook sentence must tease that gap without answering it. e.g. "The reason this works has nothing to do with what experts tell you..."
3. VIRAL ANGLE: Could this be shared? Does it trigger emotion (surprise, fear of missing out, pride)? Rate it 1-10.
4. FIRST-SENTENCE HOOK: Write the literal opening sentence of the video — must create FOMO or a curiosity gap in the first 15 words.
5. SEARCH TARGET: Pick ONE specific query a real person types into YouTube search when they need exactly this video (3-7 words, natural phrasing — e.g. "best ai email assistant 2026", "how do treasury bond ladders work", "why do objects fall at the same speed"). Small channels are discovered through SEARCH, not the home feed: this video must be the best possible answer to that query, and the title must contain the query's core words naturally.

TITLE RULES (mandatory):
- The title MUST contain either a specific number (e.g. "7 Ways", "The #1 Reason", "3 Things") OR a year reference (2025, 2026).
- The title MUST start with a number, "Why", "How", "The", or "What".
- Viral score must be 8 or higher. A low viral score indicates the topic is too generic.

SECTIONS RULE (mandatory): the 3rd and 4th entries in "sections" MUST be real, specific titles describing actual content of THIS video (e.g. "How the Pricing Trick Actually Works", "The Setting Most People Never Change"). NEVER output the literal words "Section 3 title", "Section 4 title", angle brackets, or any placeholder.

Return ONLY valid JSON:
{{
  "title": "compelling title with curiosity gap, 8-12 words",
  "thumb_text": "2-4 word thumbnail text naming the CONCRETE payoff — a named tool, a number+noun, an object, or a sharp question. NEVER hype-only words with no subject (SHOCKING, EXPOSED, SECRET, NOBODY, TRUTH, INSANE). Good examples: '3 AI EMAIL TOOLS', 'ROTH IRA TRAP', '$500/MO LEAK', 'GRAVITY IS WRONG?'. It must add information NEXT TO the title, not repeat it.",
  "search_query": "the exact 3-7 word YouTube search query this video targets (from requirement 5)",
  "hook": "opening sentence of the video, first 15 words create FOMO or curiosity gap",
  "sections": ["Hook & Pattern Interrupt", "The Problem Most People Face", "<<invent a specific content-section title about THIS topic>>", "<<invent a second specific content-section title about THIS topic>>", "The Key Insight Nobody Talks About", "CTA & Subscribe"],
  "seo_keywords": ["keyword1", "keyword2", "keyword3", "keyword4", "keyword5"],
  "viral_score": 8,
  "curiosity_angle": "one sentence: what most people don't know about this topic",
  "series_hook": "optional: 1-sentence tease for a follow-up video on a related angle"
}}"""

        response = self._call_with_retry(prompt, temperature=0.9, num_predict=1600)
        topic = self._extract_json(response)

        # Retry once if viral_score is below 8
        if isinstance(topic, dict) and int(topic.get("viral_score", 0)) < 8:
            logger.info(f"viral_score={topic.get('viral_score')} < 8, retrying topic generation at higher temperature")
            response = self._call_with_retry(prompt, temperature=0.95, num_predict=1600)
            retry_topic = self._extract_json(response)
            if isinstance(retry_topic, dict) and int(retry_topic.get("viral_score", 0)) >= int(topic.get("viral_score", 0)):
                topic = retry_topic

        # Reject a topic that just repeats a recent video; regenerate once for novelty.
        if _recent_titles and isinstance(topic, dict) and topic.get("title") \
                and self._is_duplicate_title(topic.get("title", ""), _recent_titles):
            logger.info(f"generate_topic: '{topic.get('title')}' duplicates a recent video; regenerating for novelty")
            response = self._call_with_retry(
                prompt + "\n\nYour previous idea was too similar to a past video. Choose a COMPLETELY different sub-topic and angle.",
                temperature=0.97, num_predict=1600,
            )
            alt = self._extract_json(response)
            if isinstance(alt, dict) and alt.get("title") and not self._is_duplicate_title(alt.get("title", ""), _recent_titles):
                topic = alt

        if not topic.get("title"):
            # Secondary LLM fallback (simpler prompt, lower temperature)
            niche = niche_config.get("niche", "general")
            fallback_prompt = (
                f"Write one viral YouTube video title for the {niche} niche. "
                "It must start with Why/How/The/What or a number and contain a specific number or year. "
                'Return ONLY JSON: {"title": "...", "hook": "opening sentence", '
                '"sections": ["Hook", "Problem", "Main Point", "Key Insight", "CTA"], '
                '"seo_keywords": [], "viral_score": 8, "curiosity_angle": "what most miss"}'
            )
            response = self._call_with_retry(fallback_prompt, temperature=0.7, num_predict=400, retries=1)
            topic = self._extract_json(response)

        if not topic.get("title"):
            # GUARANTEED HARDCODED FALLBACK — never let the pipeline die for lack of a title
            import random
            _year = datetime.now().year
            _FALLBACK_BANK = {
                "tech_ai": [
                    f"7 AI Tools Replacing Entire Job Roles in {_year}",
                    f"How I Made $3,000 Using Free AI Tools in 30 Days",
                    f"The #1 AI Mistake Costing You 10 Hours a Week",
                    f"Why 97% of People Use ChatGPT Wrong (And How to Fix It)",
                    f"3 AI Side Hustles Earning $5,000/Month in {_year}",
                ],
                "finance": [
                    f"Why Your Savings Account Is Costing You $20,000 in {_year}",
                    f"7 Money Mistakes Keeping You Broke (Fix #3 First)",
                    f"How to Turn $1,000 Into $10,000 in 12 Months",
                    f"The Recession Playbook Nobody Is Talking About in {_year}",
                    f"Why 90% of People Retire Broke (And the 3 Habits That Fix It)",
                ],
                "motivation": [
                    f"The ONE Daily Habit That Separates Winners From Everyone Else",
                    f"Why Discipline Beats Motivation Every Single Time (Science Proves It)",
                    f"How to Rewire Your Brain for Success in 21 Days",
                    f"7 Morning Habits of People Who Never Fail",
                    f"The Real Reason You Keep Self-Sabotaging (And How to Stop)",
                ],
                "history": [
                    f"15 Mysteries Science CANNOT Explain in {_year}",
                    f"The Ancient Secret Hidden in Plain Sight for 3,000 Years",
                    f"Why Historians Still Can't Explain These 7 Events",
                    f"The Discovery That Rewrote Everything We Know About Ancient Egypt",
                    f"7 Historical 'Facts' That Were Actually Propaganda",
                ],
                "science": [
                    f"7 Things Your Science Teacher Lied to You About",
                    f"Why Everything You Know About Black Holes Is Wrong",
                    f"The Scientific Discovery That Changes How You See Reality",
                    f"How Your Brain Is Tricking You Right Now (Neuroscience Explained)",
                    f"7 Mind-Blowing Facts About the Universe Nobody Talks About",
                ],
            }
            niche_key = niche_config.get("niche", "tech_ai")
            titles = _FALLBACK_BANK.get(niche_key, _FALLBACK_BANK["tech_ai"])
            chosen_title = random.choice(titles)
            logger.warning(f"[generate_topic] Using hardcoded fallback title: {chosen_title}")
            topic = {
                "title": chosen_title,
                "thumb_text": "SHOCKING TRUTH",
                "hook": "What if everything you believed about this was completely wrong?",
                "sections": [
                    "Hook & Pattern Interrupt",
                    "The Problem Most People Don't Know About",
                    "The Key Insight That Changes Everything",
                    "Real-World Proof and Examples",
                    "What This Means for You in 2026",
                    "The Action Plan and CTA",
                ],
                "seo_keywords": [],
                "viral_score": 8,
                "curiosity_angle": "The surprising truth most people never discover.",
                "series_hook": "Next week: the follow-up nobody expected.",
            }

        if isinstance(topic, dict) and topic.get("title"):
            topic["title"] = self._clean_title(topic["title"])
        # Pass through bonus CTR fields if present
        if isinstance(topic, dict):
            for _k in ("series_hook", "thumbnail_direction", "thumb_text"):
                if _k in topic:
                    topic[_k] = str(topic[_k]).strip()
        # 2026-07-11: thumbnail text must carry information scent. Live thumbs
        # shipped as "SHOCKING" / "NOBODY EXPLAINED THIS YET" — hollow hype the
        # 2026 audience skips. If the model still returns hype-only text,
        # rebuild concrete text from the title. Also surface the search query
        # into seo_keywords so it flows into tags/description wiring.
        if isinstance(topic, dict):
            topic["thumb_text"] = self._informative_thumb_text(
                topic.get("thumb_text", ""), topic.get("title", ""))
            _sq = str(topic.get("search_query", "") or "").strip().lower().rstrip("?.!")
            if _sq and 2 <= len(_sq.split()) <= 9:
                _kws = topic.get("seo_keywords") or []
                if isinstance(_kws, list) and _sq not in [str(k).lower() for k in _kws]:
                    topic["seo_keywords"] = [_sq] + [str(k) for k in _kws][:7]
        # Remember this title so future runs don't regenerate the same video.
        if isinstance(topic, dict) and topic.get("title"):
            self._record_used_title(niche_config.get("niche", ""), topic["title"])
        return topic

    # Cinematic shot-type vocabulary injected into visual_prompt instructions
    _SHOT_TYPES = [
        "extreme close-up",
        "close-up",
        "medium shot",
        "wide shot",
        "aerial drone shot",
        "low-angle heroic shot",
        "over-the-shoulder shot",
        "Dutch angle tilt",
        "tracking shot",
        "rack focus",
    ]

    _LIGHTING_VOCAB = [
        "golden hour rim lighting",
        "harsh neon underlighting",
        "soft diffused studio lighting",
        "dramatic chiaroscuro (high contrast)",
        "cool blue moonlight",
        "warm candlelight glow",
        "clinical fluorescent overheads",
        "sunrise silhouette backlight",
    ]

    # --- structural-variation pools (anti-"Inauthentic Content" patch) ---
    # YouTube's channel-level Inauthentic Content policy (eff. 2025-07-15) demonetizes
    # templated output with little variation across videos. These pools are SHUFFLED
    # per video so transitions, the hook bridge, and the CTA differ from upload to
    # upload instead of repeating the same literal phrasing every time.
    _TRANSITION_OPENERS = [
        "Now here's the part most people completely overlook...",
        "But wait — because this is where it gets genuinely surprising.",
        "And this is the moment everything starts to click.",
        "Here's what the evidence actually shows — and it contradicts what you've heard.",
        "So let's go one level deeper, because this is where the real insight lives.",
        "Now, most people stop here. But that's exactly the mistake.",
        "Here's where it flips from interesting to genuinely useful.",
        "Pay close attention, because this next part changes how you'll think about it.",
        "And that leads straight into the thing nobody seems to mention.",
        "This is the part I wish someone had told me years ago.",
    ]
    _HOOK_BRIDGES = [
        "But first — here's something that might surprise you...",
        "Before we get there, you need to see this one thing...",
        "Stick with me, because what comes next reframes everything...",
        "And it starts with a detail almost everyone gets wrong...",
        "But to understand why, we have to back up for just a second...",
        "Here's the twist that makes the rest of this make sense...",
    ]
    _CTA_STYLES = [
        'If you want more breakdowns like this, subscribe — new videos drop regularly. '
        'Then tell me in the comments: [specific question tied to the video topic].',
        'Hit subscribe if this changed how you see it — there\'s a lot more where this came from. '
        'And drop your take below: [specific question tied to the video topic].',
        'Subscribe so the next one finds you — I go deep on this niche every week. '
        'Comment your answer to this: [specific question tied to the video topic].',
        'If this was useful, the subscribe button keeps these coming. '
        'I read the comments — so tell me: [specific question tied to the video topic].',
    ]

    # Distinctive per-niche narration personas. Faceless-channel research (2026) found
    # that a recognizable narration *style* + research-heavy original analysis is what
    # separates monetized breakouts (ColdFusion, Kurzgesagt, MagnatesMedia) from the
    # templated "AI slop" that gets channel-level demonetized. A consistent persona
    # also builds brand/audience loyalty across uploads.
    _NICHE_PERSONA = {
        "tech_ai": "a calm, authoritative tech-documentary narrator (ColdFusion-style): measured pacing, "
                   "cinematic gravity, and genuine insight into WHY a development matters — not hype.",
        "finance": "a sharp, analytical money explainer who gives a clear, opinionated, practical take and "
                   "names real trade-offs — never generic 'save more money' filler.",
        "motivation": "a direct, intense, emotionally resonant narrator who challenges the viewer head-on "
                      "and earns the payoff with conviction, not platitudes.",
        "history": "an investigative documentary storyteller who builds intrigue through vivid, specific, "
                   "concrete detail and a strong narrative throughline.",
        "history_mystery": "an investigative documentary storyteller who builds intrigue through vivid, specific, "
                           "concrete detail and a strong narrative throughline.",
        "science": "a wonder-driven science communicator (Kurzgesagt-style) who makes the abstract vivid and "
                   "intuitive with sharp analogies and a sense of awe.",
        "science_facts": "a wonder-driven science communicator (Kurzgesagt-style) who makes the abstract vivid "
                         "and intuitive with sharp analogies and a sense of awe.",
    }

    # Human-readable niche labels — injected into prompts instead of the raw slug
    # so narration/hooks never say "tech_ai creators" (a visible bot-tell).
    _HUMAN_NICHE = {
        "tech_ai": "AI and technology",
        "finance": "personal finance and investing",
        "motivation": "motivation and self-improvement",
        "history": "history and unsolved mysteries",
        "history_mystery": "history and unsolved mysteries",
        "science": "science",
        "science_facts": "science",
    }

    # Varied closing forward-hooks (anti-repetition). The prompt used to hardcode
    # ONE sentence, so the model parroted it verbatim every section.
    _MICROHOOK_POOL = [
        "But the next part is where it actually clicks.",
        "What comes next is the piece that ties this together.",
        "Stay with me — the next part changes how you see this.",
        "And that sets up the thing most people get wrong.",
        "The next section is where the real shift happens.",
        "Here's where it gets genuinely useful — keep watching.",
        "What I show you next is the part that sticks.",
        "And that leads straight into what nobody mentions.",
    ]

    # Real content-section titles used to repair leaked placeholders.
    _SECTION_FALLBACKS = [
        "What's Really Going On Here", "The Part Most People Get Wrong",
        "How It Actually Works", "The Mistake Hiding in Plain Sight",
        "Why the Usual Advice Fails", "The Step Everyone Skips",
        "Breaking Down the Real Mechanism", "The Detail That Changes Everything",
    ]

    def _human_niche(self, niche: str) -> str:
        return self._HUMAN_NICHE.get(niche, (niche or "general").replace("_", " "))

    def _clean_section_title(self, title, idx: int = 0) -> str:
        """Strip a leaked 'Section N title:' prefix (keeping the real remainder) or
        replace a pure placeholder with a real content title."""
        import re as _re
        if not isinstance(title, str) or not title.strip():
            return self._SECTION_FALLBACKS[idx % len(self._SECTION_FALLBACKS)]
        t = title.strip()
        m = _re.match(r"^section\s*\d+\s*(?:title)?\s*[:\-]\s*(.+)$", t, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
        if _re.match(r"^(section\s*\d+\s*title|<.*>|.*\bREPLACE\b.*)$", t, _re.IGNORECASE):
            return self._SECTION_FALLBACKS[idx % len(self._SECTION_FALLBACKS)]
        return t

    # Named research/authority bodies the model likes to invent data from.
    _FAKE_SOURCE_ORGS = (
        r"stanford|harvard|mit|oxford|cambridge|yale|princeton|berkeley|stack overflow|"
        r"mckinsey|gartner|forrester|pew research|gallup|nielsen|statista|deloitte|pwc|"
        r"goldman sachs|morgan stanley|the economist|harvard business review"
    )

    def _is_fabricated_claim(self, sentence: str) -> bool:
        """True if a sentence is a fabricated *attributed statistic* — the kind this
        generator hallucinates ("according to a Stack Overflow survey, 70% of
        developers…", "a 2024 Stanford study of 8,000 people found a 340% gain").

        HIGH PRECISION by design: a cited source or named institution only trips the
        filter when the sentence ALSO contains a number. This spares legitimate
        qualitative content ("Scientists discovered a new exoplanet", "a study found
        that black holes slow time") and intentional rhetoric ("9 out of 10 people").
        """
        import re as _re
        low = sentence.lower()
        has_number = bool(_re.search(r"\d", sentence))

        # 1) Attributed share of a population — almost always invented here.
        if _re.search(
            r"\b\d{1,3}(?:\.\d+)?\s*%\s+of\s+(?:people|users|developers|programmers|"
            r"investors|traders|americans|adults|respondents|companies|businesses|"
            r"students|professionals|workers|consumers|men|women|millennials)\b", low):
            return True

        # 2) Cited study/survey/report + a number in the same sentence.
        source = (
            r"according to\s+(?:a|an|the|one|recent|this)?[\w\s]*?\b"
            r"(?:study|survey|report|poll|research|analysis|paper)\b"
            r"|\b(?:a|an|one|recent|the)\s+(?:\d{4}\s+)?(?:[a-z]+\s+)?"
            r"(?:study|survey|report|poll|analysis)\s+"
            r"(?:by|from|of|conducted|published|found|showed|revealed|concluded|"
            r"suggests?|indicates?|reports?)\b"
            r"|\b(?:study|survey|research|report|poll|analysis)\s+"
            r"(?:found|shows?|showed|reveals?|revealed|suggests?|concluded|indicates?|reports?)\s+that\b"
        )
        if has_number and _re.search(source, low):
            return True

        # 3) Named research/authority body presented as a data source (+ a number).
        if has_number and _re.search(rf"\b(?:{self._FAKE_SOURCE_ORGS})\b", low):
            return True

        return False

    def _scrub_fabrications(self, text: str):
        """Drop fabricated attributed-statistic sentences. Returns (clean, n_removed).
        Fail-safe: never returns empty text (keeps original if everything matched)."""
        import re as _re
        if not isinstance(text, str) or not text.strip():
            return text, 0
        kept, removed = [], 0
        for sent in _re.split(r"(?<=[.!?])\s+", text):
            if sent.strip() and self._is_fabricated_claim(sent):
                removed += 1
                continue
            kept.append(sent)
        cleaned = " ".join(kept).strip()
        return (cleaned or text), removed

    def _sanitize_script(self, script: dict, niche: str = "") -> dict:
        """Deterministic backstop run after generation: strip the raw niche slug
        ('tech_ai' -> 'AI and technology') from viewer-facing text and drop any
        long sentence repeated verbatim across sections. Guarantees viewers never
        see the artifacts even when the model ignores the prompt rules."""
        import re as _re
        human = self._human_niche(niche)
        use_slug = bool(niche) and "_" in niche

        def _slug(text):
            if not use_slug or not isinstance(text, str) or not text:
                return text
            return text.replace(niche, human).replace(niche.replace("_", " "), human)

        seen = set()
        fabricated_removed = 0
        for sec in script.get("script_sections", []):
            if not isinstance(sec, dict):
                continue
            if isinstance(sec.get("title"), str):
                sec["title"] = _slug(sec["title"])
            nar = _slug(sec.get("narration", ""))
            if isinstance(nar, str) and nar:
                kept = []
                for sent in _re.split(r"(?<=[.!?])\s+", nar):
                    if sent.strip() and self._is_fabricated_claim(sent):
                        fabricated_removed += 1
                        continue  # fabricated attributed statistic — strip it
                    key = _re.sub(r"\s+", " ", sent.strip().lower())
                    if len(key) > 40 and key in seen:
                        continue  # verbatim repeat across sections — drop it
                    if len(key) > 40:
                        seen.add(key)
                    kept.append(sent)
                # fail-safe: never emit empty narration
                nar = " ".join(kept).strip() or nar
            sec["narration"] = nar
        if fabricated_removed:
            logger.info(f"_sanitize_script: scrubbed {fabricated_removed} fabricated stat/citation sentence(s)")
        script["title"] = _slug(script.get("title", ""))
        script["description"] = _slug(script.get("description", ""))
        # Hooks become Shorts titles verbatim — run them through the title
        # cleaner so dangling-% and markdown junk never reach a live title.
        script["shorts_hooks"] = [self._clean_title(_slug(h)) for h in script.get("shorts_hooks", [])]
        return script

    def translate_metadata(self, title: str, description: str, target_langs: list) -> dict:
        """Translate a video's title + description into each target language for
        YouTube localizations — this surfaces the video in those languages' search
        and browse, expanding reach well beyond the English audience.

        Returns {lang_code: {"title": str, "description": str}}; best-effort ({} on
        failure). Uses ONE LLM call for all languages.
        """
        langs = [l for l in (target_langs or []) if l]
        if not langs or not title:
            return {}
        langs_str = ", ".join(langs)
        prompt = (
            f"Translate this YouTube video title and description into each of these "
            f"language codes: {langs_str}.\n"
            "Make each translation natural and idiomatic (NOT word-for-word), keep the "
            "curiosity/hook intact, keep any #hashtags and numbers as-is, and keep each "
            "translated title under 100 characters.\n\n"
            f"TITLE: {title}\n"
            f"DESCRIPTION: {description[:1500]}\n\n"
            "Return ONLY JSON mapping each language code to an object, e.g. "
            '{"es": {"title": "...", "description": "..."}}. '
            f"Use exactly these codes: {langs_str}."
        )
        try:
            resp = self._call(prompt, temperature=0.4, num_predict=2000)
            data = self._extract_json(resp)
        except Exception as e:
            logger.warning(f"translate_metadata failed: {e}")
            return {}
        out = {}
        if isinstance(data, dict):
            for lang in langs:
                v = data.get(lang)
                if isinstance(v, dict) and v.get("title"):
                    out[lang] = {
                        "title": str(v.get("title", ""))[:100],
                        "description": str(v.get("description", ""))[:4900],
                    }
        return out

    def generate_script(
        self,
        topic: dict,
        niche_config: dict,
        channel_config: dict,
        target_minutes: int = 10,
    ) -> dict:
        """Generate a full video script using a multi-step approach for reliability."""
        words_target = target_minutes * 160  # ~160 words per minute for TTS narration
        # Scale section count with video length: 5 for =6 min, 6 for 7-9 min, 7 for 10+ min
        num_sections = 5 if target_minutes <= 6 else (6 if target_minutes <= 9 else 7)
        words_per_section = words_target // num_sections

        system = niche_config.get(
            "script_system",
            "You are a senior YouTube scriptwriter with 10+ years crafting viral, "
            "high-retention scripts. You write conversational spoken prose that keeps "
            "viewers watching past 70% — measured, varied sentence rhythm, concrete "
            "specifics over vague generalities, and irresistible micro-hooks between sections.",
        )
        title = topic.get("title", "Untitled")
        hook = topic.get("hook", "")
        curiosity_angle = topic.get("curiosity_angle", "")
        niche = niche_config.get("niche", channel_config.get("niche", "general"))

        base_sections = topic.get("sections", [])
        # If the topic returned fewer sections than we need, extend with generic titles
        if len(base_sections) < num_sections:
            extras = [
                "The Hidden Mechanism",
                "What The Data Actually Shows",
                "Why Most People Get This Wrong",
                "The Counter-Intuitive Truth",
                "Real-World Proof",
            ]
            base_sections = base_sections + extras
        sections_outline = base_sections[:num_sections]

        # Determine section type for each index
        total_sections = len(sections_outline)

        script_sections = []
        story_so_far = []          # running continuity notes (cheap, no extra LLM calls)
        prev_last_sentence = ""    # last line of the previous section, for a smooth handoff
        # Open-loop payoff promised in the hook and resolved in the finale (boosts AVD).
        open_loop_payoff = (topic.get("curiosity_angle") or "").strip()
        if len(open_loop_payoff) > 160:
            open_loop_payoff = open_loop_payoff[:160].rsplit(" ", 1)[0]

        # Per-video structural variation so phrasing differs from upload to upload
        # (defends against YouTube's channel-level Inauthentic Content policy).
        import random as _random
        _vary = _random.Random()
        _transition_pool = self._TRANSITION_OPENERS[:]
        _vary.shuffle(_transition_pool)
        _microhook_pool = self._MICROHOOK_POOL[:]
        _vary.shuffle(_microhook_pool)
        _hook_bridge = _vary.choice(self._HOOK_BRIDGES)
        _cta_style = _vary.choice(self._CTA_STYLES)
        # Human-readable niche label for prompt injection (never the raw slug).
        niche_label = self._human_niche(niche)
        # Apply a distinctive per-niche narration persona (brand/loyalty + the
        # "recognizable style survives" finding), plus the central monetization rule:
        # AI content stays monetized only when it adds genuine ORIGINAL value, and is
        # demonetized at the channel level when it's templated/low-variation.
        _persona = self._NICHE_PERSONA.get(niche, "")
        if _persona:
            system = system + f" Narrate as {_persona}"
        system = (
            system
            + " CRITICAL FOR MONETIZATION: every video must add genuine ORIGINAL value — a specific "
            "point of view, a non-obvious connection, your own analysis or synthesis — not just a "
            "list of facts anyone could regurgitate. Deliver materially different substance from any "
            "previous video; vary the structure, examples, and phrasing so the channel clearly differs "
            "upload to upload. Templated, interchangeable scripts get the whole channel demonetized."
        )

        for i, section_title in enumerate(sections_outline):
            section_title = self._clean_section_title(section_title, i)
            is_first = i == 0
            is_last = i == total_sections - 1
            is_second = i == 1

            if is_first:
                section_prompt = f"""Write the HOOK section for a YouTube video titled: "{title}"
Niche: {niche_label}

RULES for the hook (first 30 words are CRITICAL — this determines whether the viewer stays):
- Open with this exact sentence or a strong variation: "{hook}"
- The first sentence must be 10 words or fewer and feel urgent or provocative.
- NEVER open with a greeting, a channel intro, or "in this video" — lead straight with a concrete outcome or scenario. The hook's core job must land within the first 15 seconds (~30 spoken words).
- Create immediate FOMO or a curiosity gap: "What I'm about to show you changes everything most people believe about this."
- Use punchy sentence rhythm: alternate 3-5 word sentences with 12-18 word sentences for pace variation.
- Tease the payoff without giving it away: "By the end of this video, you'll know the exact reason 9 out of 10 people get this completely wrong — and how to fix it in under a day."
- Weave in this curiosity angle naturally: {curiosity_angle}
- End the hook section with a retention bridge like: "{_hook_bridge}"
- Write exactly {words_per_section} words of spoken narration. No headings, no stage directions, no parenthetical notes.

Return ONLY this JSON (no preamble, no markdown fences):
{{
  "index": {i},
  "title": "{section_title}",
  "narration": "full {words_per_section}-word spoken narration here",
  "visual_prompt": "CINEMATOGRAPHY: [shot type, e.g. extreme close-up / aerial shot / low-angle]. SUBJECT: [specific person, object, or environment directly related to the hook topic]. LIGHTING: [specific lighting style — e.g. dramatic chiaroscuro, neon underlighting, golden hour]. MOOD: [one adjective]. CAMERA: [lens effect — shallow depth of field / wide angle distortion / rack focus]. QUALITY: photorealistic, 8K, cinematic color grade, no text.",
  "b_roll_cue": "[8-word max visual description for AI image generation]"
}}"""

            elif is_last:
                section_prompt = f"""Write the PAYOFF + CTA conclusion section for: "{title}"
Niche: {niche_label}

RULES:
- Open with the reveal the viewer has been waiting for: "Here's the thing nobody actually talks about: [specific insight]."
- Deliver a concrete, actionable takeaway — not vague advice but a specific step or fact.
- Use a reframe close: "Most people spend [time/money] trying to [common wrong approach]. But now you know the real reason it works — and that changes everything."
- End with a benefit-driven CTA tied to THIS video's topic (not generic). Adapt this phrasing in your own words, filling in the bracket with a real question about the video: "{_cta_style}"
- Write exactly {words_per_section} words of spoken narration. No headings, no stage directions.

Return ONLY this JSON:
{{
  "index": {i},
  "title": "{section_title}",
  "narration": "full {words_per_section}-word spoken narration here",
  "visual_prompt": "CINEMATOGRAPHY: [shot type for the triumphant close — e.g. medium shot, slow pull-back]. SUBJECT: [person in confident, empowered pose OR symbolic object of success in this niche]. LIGHTING: [warm golden hour or soft studio — conveys resolution and satisfaction]. MOOD: triumphant. CAMERA: slight slow zoom out to reveal scale. QUALITY: photorealistic, 8K, cinematic color grade, no text.",
  "b_roll_cue": "[8-word max visual for AI image generation]"
}}"""

            elif is_second:
                section_prompt = f"""Write the PROBLEM/TENSION section for: "{title}"
Section title: "{section_title}"
Niche: {niche_label}

RULES:
- Open by making the viewer feel seen: "If you've ever [specific frustrating situation in this niche], you know exactly how demoralising that is."
- Name the enemy — the system, misconception, or common advice that makes the problem worse.
- Escalate tension with a concrete, real consequence the viewer can feel — a vivid everyday scenario or a clear cost in time, money, or effort. Do NOT invent statistics, studies, or institutions; only cite a number if it is real and widely known, otherwise frame it as a plain observation ("most people never realise how much this quietly costs them").
- Use a pivot: "And here's the part that's going to make you rethink everything you thought you knew..."
- Alternate punchy 4-6 word sentences with explanatory 14-20 word sentences. Varied rhythm = retention.
- Write exactly {words_per_section} words of spoken narration only. No headings.

Return ONLY this JSON:
{{
  "index": {i},
  "title": "{section_title}",
  "narration": "full {words_per_section}-word spoken narration here",
  "visual_prompt": "CINEMATOGRAPHY: [shot type showing tension — e.g. Dutch angle tilt, low-angle oppressive shot]. SUBJECT: [person or environment showing the problem — frustrated, overwhelmed, failing]. LIGHTING: [harsh overhead fluorescents OR cold blue shadows — conveys stress and difficulty]. MOOD: tense. CAMERA: slow push-in for unease. QUALITY: photorealistic, 8K, cinematic color grade, no text.",
  "b_roll_cue": "[8-word max visual for AI image generation]"
}}"""

            else:
                # Per-video shuffled pool — different videos get different transitions
                # for the same section index (anti-templated-content variation).
                _transition = _transition_pool[i % len(_transition_pool)]
                _microhook = _microhook_pool[i % len(_microhook_pool)]
                section_prompt = f"""Write section {i+1} of {total_sections} for a YouTube video titled: "{title}"
Section title: "{section_title}"
Niche: {niche_label}

RULES:
- Open with this transition or a strong variation: "{_transition}"
- Make the viewer feel they're learning something most people don't know. Be specific and concrete, not vague — but NEVER fabricate studies, statistics, institutions, or expert quotes.
  Bad (vague): "This technique is very effective."
  Bad (fabricated — do NOT do this): "A 2024 Stanford study of 8,000 people found a 340% improvement." Do not invent sources or numbers.
  Good (honest specificity): "Here's the mechanism almost everyone misses — and once you see it, you can't unsee it: [concrete, real explanation tied directly to the topic]."
- Include a relatable analogy or real, everyday example to anchor the abstract concept.
- Vary sentence length: 3-5 word punchy sentences mixed with 15-20 word explanatory ones.
- End with a SHORT forward-hook that makes skipping the next section feel risky. Write it FRESH in your own words — do NOT reuse wording from any other section, and never repeat the same sentence twice in this video. For tone only (rephrase, don't copy): "{_microhook}"
- Write exactly {words_per_section} words of spoken narration only. No headings, no stage directions.

Return ONLY this JSON:
{{
  "index": {i},
  "title": "{section_title}",
  "narration": "full {words_per_section}-word spoken narration here",
  "visual_prompt": "CINEMATOGRAPHY: [specific shot type — e.g. aerial drone, rack focus, tracking shot]. SUBJECT: [specific environment or person relevant to '{section_title}' in the {niche} niche — be concrete]. LIGHTING: [specific lighting style matching the emotional tone of this section]. MOOD: [one-word emotion]. CAMERA: [lens movement or effect]. QUALITY: photorealistic, 8K, cinematic color grade, no text, no watermarks.",
  "b_roll_cue": "[8-word max visual description for AI image generation]"
}}"""

            # --- continuity + assigned cinematography (major-improvement patch) ---
            shot_type = self._SHOT_TYPES[i % len(self._SHOT_TYPES)]
            lighting = self._LIGHTING_VOCAB[(i * 3 + 1) % len(self._LIGHTING_VOCAB)]
            if story_so_far:
                _cont = "\n".join(f"- {g}" for g in story_so_far[-6:])
                continuity_block = (
                    "CONTINUITY — earlier sections already covered the points below. Do NOT repeat them; "
                    "advance the argument instead, and open by flowing naturally from the previous line.\n"
                    f"{_cont}\n"
                    f'The previous section ended on: "{prev_last_sentence}"\n\n'
                )
            else:
                continuity_block = ""
            section_prompt = continuity_block + section_prompt + (
                f"\n\nVISUAL DIRECTION (use these EXACT values inside visual_prompt, do not substitute): "
                f"shot type = {shot_type}; lighting = {lighting}."
            )

            # --- open-loop coordination: plant in the hook, re-hook mid-way, pay off at the end.
            #     A promised-but-unresolved payoff is one of the strongest average-view-duration levers. ---
            is_rehook = (not is_first and not is_last and i == total_sections // 2)
            if open_loop_payoff:
                if is_first:
                    section_prompt += (
                        f"\n\nOPEN LOOP — plant this and do NOT resolve it yet: explicitly promise that by the END "
                        f"of the video you'll reveal {open_loop_payoff!r}. Tease it as a specific, can't-skip payoff "
                        f'(e.g. "stick around — by the end you\'ll see exactly why...").'
                    )
                elif is_last:
                    section_prompt += (
                        f"\n\nPAY OFF THE OPEN LOOP: the intro promised a reveal about {open_loop_payoff!r}. Deliver it "
                        f"explicitly and concretely now, so that promise feels fully satisfied — then go into the CTA."
                    )
                elif is_rehook:
                    section_prompt += (
                        f"\n\nRE-HOOK (one sentence only): briefly remind the viewer the big reveal about "
                        f"{open_loop_payoff!r} is still coming, then keep moving — do NOT resolve it here."
                    )

            raw = self._call_with_retry(section_prompt, system=system, temperature=0.75, num_predict=800)

            # Try JSON extraction first
            sec_data = self._extract_json(raw)
            if sec_data and sec_data.get("narration"):
                sec_data["narration"] = self._cap_narration(sec_data.get("narration", ""))
                # Ensure required fields present
                sec_data.setdefault("index", i)
                sec_data.setdefault("title", section_title if isinstance(section_title, str) else f"Section {i+1}")
                sec_data.setdefault("visual_prompt", f"cinematic visual for: {section_title}, related to {title}")
                sec_data.setdefault("b_roll_cue", f"{section_title} visual")
                script_sections.append(sec_data)
            else:
                # Fallback: treat raw output as narration text — but NEVER raw
                # JSON. On 2026-07-05 unparseable JSON (\' escapes) fell through
                # here and Kokoro read '{ "index": 1, "title": ...' aloud for
                # 5/6 finance and 4/6 history sections.
                narration = self._salvage_narration(raw)
                if not narration:
                    logger.warning(f"section {i}: unusable output, one strict retry…")
                    retry_raw = self._call_with_retry(
                        section_prompt
                        + "\n\nIMPORTANT: reply with ONLY the JSON object. Inside JSON strings "
                          "never use \\' — write the apostrophe bare (').",
                        system=system, temperature=0.5, num_predict=800,
                    )
                    retry_data = self._extract_json(retry_raw)
                    if retry_data and retry_data.get("narration"):
                        retry_data["narration"] = self._cap_narration(str(retry_data["narration"]))
                        retry_data.setdefault("index", i)
                        retry_data.setdefault("title", section_title if isinstance(section_title, str) else f"Section {i+1}")
                        retry_data.setdefault("visual_prompt", f"cinematic visual for: {section_title}, related to {title}")
                        retry_data.setdefault("b_roll_cue", f"{section_title} visual")
                        script_sections.append(retry_data)
                        continue
                    narration = self._salvage_narration(retry_raw)
                if not narration:
                    # No-garbage policy: fail the section (and the channel) loudly
                    # rather than ship a video that reads JSON aloud.
                    raise RuntimeError(
                        f"section {i} ('{section_title}'): LLM output unusable after retry — aborting script"
                    )
                narration = self._cap_narration(narration)
                script_sections.append({
                    "index": i,
                    "title": section_title if isinstance(section_title, str) else f"Section {i+1}",
                    "narration": narration,
                    "visual_prompt": f"cinematic visual for: {section_title}, related to {title}",
                    "b_roll_cue": f"{section_title} scene",
                })

            # --- enforce target length: expand a section that came in too short ---
            # (Short sections shrink the video's runtime, watch time, and ad inventory.)
            _sec = script_sections[-1]
            _nar = _sec.get("narration", "") if isinstance(_sec, dict) else ""
            if isinstance(_nar, str) and 0 < len(_nar.split()) < int(words_per_section * 0.8):
                _expanded = self._expand_narration(
                    _nar, words_per_section, _sec.get("title", section_title), title, system
                )
                if _expanded and len(_expanded.split()) > len(_nar.split()):
                    _sec["narration"] = self._cap_narration(_expanded)
                    logger.info(
                        f"section {i} expanded {len(_nar.split())}->{len(_sec['narration'].split())} words "
                        f"(target {words_per_section})"
                    )

            # --- hook A/B: the first section decides whether the viewer stays, so it
            #     is worth one extra roll. Generate a second hook and keep the stronger. ---
            if is_first and isinstance(script_sections[-1], dict):
                _primary = script_sections[-1]
                _primary_nar = _primary.get("narration", "") or ""
                try:
                    _alt_raw = self._call_with_retry(section_prompt, system=system, temperature=0.95, num_predict=800)
                    _alt = self._extract_json(_alt_raw)
                    _alt_nar = (_alt.get("narration") if isinstance(_alt, dict) else "") or ""
                    if isinstance(_alt_nar, str) and _alt_nar.strip():
                        _alt_nar = self._cap_narration(_alt_nar)
                        if 0 < len(_alt_nar.split()) < int(words_per_section * 0.8):
                            _alt_nar = self._cap_narration(
                                self._expand_narration(_alt_nar, words_per_section,
                                                       _primary.get("title", section_title), title, system)
                            )
                        if self._score_hook(_alt_nar) > self._score_hook(_primary_nar):
                            _primary["narration"] = _alt_nar
                            if isinstance(_alt, dict):
                                if _alt.get("visual_prompt"):
                                    _primary["visual_prompt"] = _alt["visual_prompt"]
                                if _alt.get("b_roll_cue"):
                                    _primary["b_roll_cue"] = _alt["b_roll_cue"]
                            logger.info(
                                f"hook A/B: kept alternate hook "
                                f"(score {self._score_hook(_alt_nar)} > {self._score_hook(_primary_nar)})"
                            )
                except Exception as _e:
                    logger.warning(f"hook A/B failed (keeping primary): {_e}")

            # --- update running continuity from whatever we just appended ---
            _last_narr = script_sections[-1].get("narration", "") if script_sections else ""
            _gist = self._continuity_gist(_last_narr)
            if _gist:
                story_so_far.append(f"{script_sections[-1].get('title', '')}: {_gist}")
            prev_last_sentence = self._last_sentence(_last_narr)

        # Step 2: Generate description and tags
        meta_prompt = f"""Write a YouTube video description and 20 comma-separated tags for: "{title}"
Niche: {niche_label}

DESCRIPTION RULES (every line matters for search ranking):
- Line 1 (shown in search — first 125 chars): restate the video's core promise + primary keyword. Create urgency. End with "..." to invite the click. Example: "Most {niche} creators get this completely wrong — here's the one thing that changes everything..."
- Blank line.
- Paragraph 2 (2-3 sentences): expand what viewers will specifically learn/gain. Name concrete techniques, tools, or facts from the video. Use secondary keywords naturally.
- Blank line.
- Paragraph 3 (CTA — 2-3 sentences): subscribe prompt tied to the ongoing channel value (not generic — specify what future videos cover). Add a comment prompt: "Comment below: [specific question from the video topic]."
- Blank line.
- Timestamps (CRITICAL — YouTube algorithm boosts videos with chapters): Generate REAL estimated timestamps based on {num_sections} sections of ~{target_minutes // num_sections} min each. Format exactly as:\n  ? CHAPTERS:\n  00:00 Introduction\n  01:30 [Section 2 title from video]\n  03:00 [Section 3 title from video]\n  (continue for all {num_sections} sections, spacing evenly across {target_minutes} minutes)
- Blank line.
- Final line: exactly 3 hashtags (e.g. #{niche.replace("_", "")} #youtube #[niche-specific trend tag]).
- Total: 180-250 words.

Format: write the full description text, then on a new line write TAGS: followed by 20 comma-separated tags.
Tags: first tag = exact-match primary keyword. Tags 2-8 = long-tail variants (3-5 words, highly specific). Remaining = related broader terms. All lowercase, no # symbols, no duplicates."""

        meta_response = self._call_with_retry(meta_prompt, temperature=0.6)

        description, tags = self._parse_description_and_tags(meta_response)
        if not description:
            description = self._strip_preamble(meta_response).strip()

        # Step 3: Generate shorts hooks
        shorts_prompt = f"""Write 3 ultra-viral YouTube Shorts hooks for: "{title}"
Niche: {niche_label}

RULES — every hook must hit ALL of these:
- UNDER 15 WORDS (Shorts viewers decide in under 2 seconds)
- Start with: shocking number, direct challenge, provocative counter-intuitive claim, or "I did X and..."
- Creates instant FOMO or "wait, WHAT?" reaction
- NICHE-SPECIFIC and concrete — no generic language
- Uses "you" OR starts with "I" for immediate personal relevance

HIGH-CTR FORMATS for {niche}:
  "97% of {niche} people skip this — it costs them everything"
  "I did [specific action] for 30 days — the results shocked me"
  "The {niche} secret that experts don't want you to know"
  "Stop [common {niche} mistake] — do this instead (3x faster)"
  "This one {niche} change outperforms 6 months of normal effort"

BANNED (never write):
  "You need to know this important information..."
  "Here are some great tips for..."
  Any hook over 15 words

Write exactly 3 hooks, numbered 1-3. One per line. No preamble, no explanation."""

        shorts_response = self._call(shorts_prompt, temperature=0.8)
        raw_lines = [line.strip().lstrip("0123456789.-) ") for line in shorts_response.strip().split("\n") if line.strip() and len(line.strip()) > 5]
        # Enforce 15-word limit on Shorts hooks
        cleaned_hooks = []
        for h in self._filter_preamble_lines(raw_lines):
            words = h.split()
            if len(words) > 15:
                h = " ".join(words[:14]) + "..."
            cleaned_hooks.append(h)
        shorts_hooks = cleaned_hooks[:3]

        script = {
            "title": self._clean_title(title),
            "description": description,
            "tags": tags + topic.get("seo_keywords", []),
            "script_sections": script_sections,
            "shorts_hooks": shorts_hooks,
        }

        # Deterministic backstop: strip any leaked niche slug / placeholder and
        # drop verbatim sentence repeats the model may have produced anyway.
        script = self._sanitize_script(script, niche)

        total_words = sum(len(s["narration"].split()) for s in script.get("script_sections", []) if isinstance(s.get("narration"), str))
        logger.info(f"Script generated: {len(script.get('script_sections', []))} sections, ~{total_words} words ({total_words/150:.1f} min)")

        return script

    # Words that carry no search intent -- a title "covering" the query only
    # because it happens to share the word "the" is not covering it.
    _QUERY_STOPWORDS = {
        "a", "an", "and", "are", "as", "at", "be", "but", "by", "can", "did",
        "do", "does", "for", "from", "had", "has", "have", "how", "i", "in",
        "is", "it", "its", "of", "on", "or", "our", "that", "the", "their",
        "then", "there", "these", "they", "this", "to", "was", "were", "what",
        "when", "where", "which", "who", "why", "will", "with", "you", "your",
    }

    @classmethod
    def _query_core_words(cls, query: str) -> list:
        """The content words of a search query, lowercased."""
        words = re.findall(r"[a-z0-9']+", (query or "").lower())
        return [w for w in words if w not in cls._QUERY_STOPWORDS and len(w) > 2]

    @classmethod
    def _covers_query(cls, title: str, query: str, min_ratio: float = 0.6) -> bool:
        """Does `title` actually answer `query`?

        True when the title contains at least `min_ratio` of the query's content
        words. Substring matching on the whole phrase would be far too strict --
        "best ai tools for small business" vs a title that says "AI Tools Every
        Small Business Should Use in 2026" is a hit, not a miss -- while requiring
        ALL words would reject perfectly good natural phrasing. 60% of content
        words is the practical line: enough shared surface for YouTube to match
        the query, loose enough that the title still reads like a human wrote it.
        """
        core = cls._query_core_words(query)
        if not core:
            return True  # no query to satisfy -> nothing to fail
        t = (title or "").lower()
        hits = sum(1 for w in core if w in t)
        return (hits / len(core)) >= min_ratio

    def generate_seo_metadata(self, title: str, description: str, niche: str,
                              base_tags: list, search_query: str = "",
                              seo_keywords: list = None) -> dict:
        """Generate optimized SEO metadata for a video.

        search_query / seo_keywords come from generate_topic(), which explicitly
        picks "ONE specific query a real person types into YouTube search when
        they need exactly this video" -- because small channels are discovered
        through SEARCH, not the home feed.

        They used NOT to be passed here. This function rewrote the title from
        scratch having never seen the target query, and run_rerun shipped that
        rewrite; the query's other escape route (script["tags"]) was also never
        read, because seo_data["tags"] always existed and won the fallback. So
        the entire search-discovery mechanism was inert: the published title and
        tags had no deliberate relationship to any query a human types. That is
        the most likely single reason four of five channels sit at 2-11 subs.

        Tuned for 2026 YouTube ranking signals:
          - Title: 60 chars max (mobile truncation), primary keyword in first 45 chars,
            one curiosity-gap device (number, contrast, specificity, callout). No ALL CAPS.
          - Description: first 150 chars are the critical snippet shown in search/suggested.
            Those 150 chars must restate the promise + include the primary keyword again.
            Rest of description: 3 short paragraphs, timestamps placeholder, 3-5 hashtags
            on the last line.
          - Tags: 25-30 tags, mix of broad (1-2 words) and long-tail (3-5 words). First tag
            must be the exact-match primary keyword.
          - Hashtags: exactly 3, one broad one niche one time-relevant.
          - Title variants: 3 click-through-rate-optimized options.
          - Shorts hook: 15-word version optimized for Shorts title.
        """
        # Truncate the input description so the prompt doesn't bloat for long scripts.
        desc_snippet = description[:500] if description else ""
        niche_label = self._human_niche(niche)
        search_query = (search_query or "").strip()
        seo_keywords = [str(k).strip() for k in (seo_keywords or []) if str(k).strip()]

        if search_query:
            query_block = (
                f"  TARGET SEARCH QUERY (the exact phrase a real person types into\n"
                f"  YouTube to find this video): \"{search_query}\"\n"
                f"  Secondary keywords: {json.dumps(seo_keywords[:8])}\n"
            )
            query_rule = (
                f"  0. THE MOST IMPORTANT RULE: optimized_title MUST naturally contain the "
                f"core words of the TARGET SEARCH QUERY (\"{search_query}\"). This channel is "
                f"discovered through SEARCH, not the home feed — if the title does not answer "
                f"that query, the video is invisible. Do not paraphrase the query away. Every "
                f"title_variant must also contain those core words. Tag #1 must be the target "
                f"search query, verbatim and lowercase.\n"
            )
        else:
            query_block = ""
            query_rule = ""

        prompt = f"""You are a senior YouTube SEO specialist optimizing for 2026 algorithm signals (session time, CTR, suggested-feed pickup). Your output drives title + description + tags that will be shipped verbatim.

SOURCE MATERIAL
  Working title: {title}
  Script excerpt: {desc_snippet}
  Niche: {niche_label}
{query_block}  Base tags (always include at least the first 5, verbatim): {json.dumps(base_tags[:10])}

HARD RULES — violating any of these ruins ranking:
{query_rule}  1. optimized_title: 50-60 chars. Primary keyword must appear in the first 45 chars. Include ONE curiosity device: a number, a bold contrast, a named entity, OR a specific claim. No clickbait that the video cannot deliver. No ALL CAPS. Title case or sentence case only.
  2. title_variants: 3 alternative title options, each 50-60 chars, each using a different CTR device (e.g. one uses a number, one uses a "how to", one uses a contrast/reveal). These give the channel A/B testing options.
  3. optimized_description: 200-350 words. STRUCTURE:
       - Line 1 (first 125 chars shown in search): restate the value proposition and repeat the primary keyword. This is the hook — make it compelling. End with "..." or a full stop.
       - Blank line.
       - Paragraph 2 (2-3 sentences): expand the promise, add secondary keywords naturally, name specific items/people/tools the video covers.
       - Blank line.
       - Paragraph 3 (2-3 sentences): CTA — subscribe with a specific benefit promise (e.g. "Subscribe if you want [specific ongoing value] — new videos drop every day."), comment prompt tied to the video topic, link placeholder "(resources in pinned comment)".
       - Blank line.
       - Blank line.
       - Final line: exactly 3 hashtags separated by spaces (these must also appear in the hashtags array).
  4. tags: 25-30 items. Tag #1 is the exact-match primary keyword. Tags 2-8 are secondary keywords and long-tail variants (3-5 words each). Remaining tags are related broader terms. Lowercase. No # symbols. No duplicates. No tag over 30 chars. Tags must be highly specific — e.g. not just "AI" but "AI productivity tools 2026".
  5. hashtags: exactly 3. One broad category tag (e.g. #ai), one niche-specific (e.g. #llm2026), one evergreen topic tag (e.g. #automation). Do NOT put a month name or a calendar date in any hashtag - dated tags go stale and get flagged. Lowercase. Include the # prefix.
  6. shorts_hook: a 15-word version of the title optimized for Shorts. Must create FOMO or curiosity gap. Never begin with a bare '%' or leave a statistic unfilled - write a real number or rephrase. e.g. "I tested this for 30 days and the results shocked me — here's what happened"

QUALITY BAR
  - Every string is publish-ready; no placeholders like "[keyword]" or "TBD".
  - No emoji in the title or tags. Emoji allowed in description paragraphs 2 and 3, max one per paragraph.
  - No disclaimers, meta-commentary, or "here is the optimized content" preamble.

OUTPUT FORMAT
Return ONE valid JSON object and NOTHING else. Start with {{ and end with }}. Schema:
{{
  "optimized_title": "string, 50-60 chars",
  "title_variants": ["variant 1, 50-60 chars", "variant 2, 50-60 chars", "variant 3, 50-60 chars"],
  "optimized_description": "string, 200-350 words, with blank lines between paragraphs",
  "tags": ["tag1", "tag2", ... 25-30 strings],
  "hashtags": ["#one", "#two", "#three"],
  "shorts_hook": "15-word Shorts title with FOMO or curiosity gap"
}}"""

        try:
            response = self._call_with_retry(prompt, temperature=0.55)
        except Exception as _e:
            logger.warning(f"SEO LLM call failed: {_e}; using deterministic fallback")
            response = ""
        result = self._extract_json(response) or {}

        # Sanitize titles BEFORE scoring/promotion so rating artifacts (e.g. a
        # leaked '4.5/10:' prefix from the viral_score field) never reach YouTube.
        if isinstance(result, dict):
            if result.get("optimized_title"):
                result["optimized_title"] = self._clean_title(str(result["optimized_title"]))
            if isinstance(result.get("title_variants"), list):
                result["title_variants"] = [
                    self._clean_title(str(v)) for v in result["title_variants"] if v
                ]

        # Pick the best title_variant by CTR score and promote it to optimized_title.
        #
        # A title that does not contain the target search query cannot be found by
        # the query, no matter how high it scores on CTR heuristics. So: among the
        # candidates, if ANY carry the query's core words, only those are eligible.
        # CTR score then breaks the tie WITHIN that eligible set. Reach is a
        # precondition for clicks -- a great title on an unfindable video earns 0.
        if isinstance(result, dict):
            variants = result.get("title_variants") or []
            if isinstance(variants, list) and variants:
                all_titles = variants[:]
                existing_opt = result.get("optimized_title", "")
                if existing_opt and existing_opt not in all_titles:
                    all_titles.append(existing_opt)

                eligible = all_titles
                if search_query:
                    on_query = [t for t in all_titles
                                if self._covers_query(t, search_query)]
                    if on_query:
                        eligible = on_query
                        if len(on_query) < len(all_titles):
                            logger.info(
                                "SEO: %d/%d title candidates carry the target query "
                                "%r; discarding the rest",
                                len(on_query), len(all_titles), search_query)
                    else:
                        logger.warning(
                            "SEO: NO title candidate contains the target search query "
                            "%r -- this video will not be findable by it. "
                            "Candidates: %s", search_query, all_titles)

                best = max(eligible, key=self._score_title)
                if best and best != result.get("optimized_title"):
                    logger.info(f"_score_title promoted '{best}' over '{result.get('optimized_title')}'")
                    result["optimized_title"] = best

        # Light post-validation: guarantee the returned fields meet the shape the
        # downstream pipeline assumes (tags is a list of strings etc). Fix cheap
        # violations silently; log louder ones for later review.
        if isinstance(result, dict):
            title_out = str(result.get("optimized_title", "") or "").strip()
            if len(title_out) > 70:
                # Hard-trim rather than blow up the whole pipeline.
                title_out = title_out[:67].rstrip() + "..."
                result["optimized_title"] = title_out
                logger.warning("SEO title was >70 chars, trimmed")

            tags_out = result.get("tags") or []
            if isinstance(tags_out, list):
                cleaned = []
                seen = set()
                for t in tags_out:
                    if not isinstance(t, str):
                        continue
                    t = t.strip().lstrip("#").lower()
                    if not t or len(t) > 30 or t in seen:
                        continue
                    seen.add(t)
                    cleaned.append(t)
                # Ensure base tags are always present so channel consistency is preserved.
                for bt in base_tags[:5]:
                    bt_norm = str(bt).strip().lstrip("#").lower()
                    if bt_norm and bt_norm not in seen:
                        cleaned.insert(0, bt_norm)
                        seen.add(bt_norm)

                # The topic writer's secondary keywords are real search intent --
                # union them in rather than letting the LLM's guesses crowd them out.
                for kw in seo_keywords[:10]:
                    kw_norm = kw.strip().lstrip("#").lower()
                    if kw_norm and kw_norm not in seen and len(kw_norm) <= 30:
                        cleaned.append(kw_norm)
                        seen.add(kw_norm)

                # Tag #1 must be the exact-match target query.
                # NOTE: do NOT apply the 30-char cap used for the other tags here.
                # It is a self-imposed convention (YouTube's real limit is 500
                # chars across ALL tags), and truncating mid-word turns the single
                # most important tag into garbage -- "best ai tools for small
                # business" became "best ai tools for small busine", which matches
                # nothing. The exact-match query tag ships whole.
                if search_query:
                    q = search_query.strip().lstrip("#").lower()
                    if q and len(q) <= 100:
                        cleaned = [t for t in cleaned if t != q]
                        cleaned.insert(0, q)

                result["tags"] = cleaned[:30]

            hashtags_out = result.get("hashtags") or []
            if isinstance(hashtags_out, list):
                ht_clean = []
                for h in hashtags_out:
                    if not isinstance(h, str):
                        continue
                    h = h.strip()
                    if not h.startswith("#"):
                        h = "#" + h
                    h = h.lower()
                    if h and h not in ht_clean:
                        ht_clean.append(h)
                result["hashtags"] = ht_clean[:3]

        # ----------------------------------------------------------------
        # Failsoft: if the LLM returned nothing usable, synthesize SEO
        # from the inputs so the pipeline never ships an empty title and
        # we never waste a retry. Fallback quality is intentionally good
        # enough to publish — not just a placeholder.
        # ----------------------------------------------------------------
        if not (result.get("optimized_title") and result.get("optimized_description")):
            logger.warning("SEO LLM returned incomplete result; using deterministic fallback")
            fb_title = title.strip() if title else "Untitled"
            if len(fb_title) > 60:
                fb_title = fb_title[:57].rstrip() + "..."

            primary_kw = base_tags[0] if base_tags else (niche.replace("_", " ") if niche else "video")
            snippet = (description or "").strip().split("\n", 1)[0][:140]

            fb_desc_parts = []
            # Search-snippet line (first 150 chars matter most)
            lead = f"{fb_title}. {snippet}".strip()
            if len(lead) > 240:
                lead = lead[:237].rstrip() + "..."
            fb_desc_parts.append(lead)
            fb_desc_parts.append("")
            fb_desc_parts.append(
                f"In this video we break down {primary_kw} with concrete examples, the trade-offs, and what to do next. "
                f"If you want more deep-dives like this, subscribe and tap the bell — new videos drop every day."
            )
            fb_desc_parts.append("")
            fb_desc_parts.append("")
            # 3 hashtags (deterministic from base tags + niche)
            ht_seed = []
            for src in [base_tags[:1], [niche or "ai"], base_tags[1:2] or ["daily"]]:
                for s in src:
                    tag = "#" + re.sub(r"[^A-Za-z0-9]+", "", str(s)).lower()
                    if len(tag) > 1 and tag not in ht_seed:
                        ht_seed.append(tag)
                        break
            while len(ht_seed) < 3:
                ht_seed.append("#daily")
            fb_desc_parts.append(" ".join(ht_seed[:3]))

            # Tags fallback: base tags first, then niche-derived, dedup, lower, <=30
            fb_tags = []
            seen = set()
            for src in (base_tags or []) + [niche, "daily", "2026", primary_kw]:
                if not src:
                    continue
                t = str(src).strip().lstrip("#").lower()
                if t and t not in seen and len(t) <= 30:
                    seen.add(t)
                    fb_tags.append(t)
                if len(fb_tags) >= 25:
                    break

            result.setdefault("optimized_title", fb_title)
            result.setdefault("optimized_description", "\n".join(fb_desc_parts))
            result.setdefault("title_variants", [fb_title, fb_title, fb_title])
            result.setdefault("shorts_hook", fb_title[:100])
            if not result.get("tags"):
                result["tags"] = fb_tags
            if not result.get("hashtags"):
                result["hashtags"] = ht_seed[:3]

        return result

    def generate_visual_prompts(self, sections: list, channel_config: dict) -> list:
        """For each section that lacks a detailed visual_prompt, generate a cinematic one.

        Prompts are optimized for LTX-Video animation: specific camera angles,
        lighting conditions, subject descriptions, and mood. Returns the updated
        sections list with enriched visual_prompt fields.
        """
        channel_style = channel_config.get("visual_style", "cinematic, high-quality, professional")
        color_palette = channel_config.get("color_palette", "rich blues, deep blacks, electric accents")
        updated = []

        for sec in sections:
            if not isinstance(sec, dict):
                updated.append(sec)
                continue

            existing_vp = sec.get("visual_prompt", "")
            # Only regenerate if the visual_prompt is generic/short (under 50 chars)
            needs_upgrade = not existing_vp or len(existing_vp) < 50 or existing_vp.startswith("cinematic visual for:")

            if needs_upgrade:
                sec_title = sec.get("title", "")
                narration_snippet = (sec.get("narration", "") or "")[:200]
                b_roll = sec.get("b_roll_cue", "")

                vp_prompt = f"""Generate a single cinematic visual prompt for LTX-Video animation.

Section: "{sec_title}"
Narration context: "{narration_snippet}"
B-roll cue: "{b_roll}"
Channel visual style: {channel_style}
Color palette: {color_palette}

The visual prompt must be:
- Specific: name camera angle (e.g. "low-angle tracking shot"), lens type (e.g. "85mm portrait"), lighting (e.g. "golden hour rim lighting"), and subject
- Cinematic: include depth of field, color grading, mood
- 30-60 words
- Optimized for LTX-Video: describe motion, atmosphere, and visual metaphor

Example good prompt: "Low-angle tracking shot of a lone developer at a glowing terminal in a dark server room, green code reflections on face, shallow depth of field, dramatic side lighting, slow dolly forward, cinematic 4K, teal-orange color grade"

Return ONLY the visual prompt text, nothing else."""

                try:
                    vp_response = self._call(vp_prompt, temperature=0.7)
                    vp_text = vp_response.strip().strip('"').strip("'")
                    # Strip any preamble the LLM might add
                    vp_text = self._strip_preamble(vp_text)
                    if vp_text:
                        sec = dict(sec)  # don't mutate original
                        sec["visual_prompt"] = vp_text
                except Exception as e:
                    logger.warning(f"Visual prompt generation failed for section '{sec_title}': {e}")

            updated.append(sec)

        logger.info(f"generate_visual_prompts: processed {len(updated)} sections")
        return updated

    def generate_short_script(self, topic: dict, niche_config: dict, channel_config: dict) -> dict:
        """Generate a 40-55 second script specifically structured for YouTube Shorts.

        Structure (2026 retention architecture):
          Hook (0-3s) -> Tension/Curiosity (3-15s) -> Payload (15-40s) -> Loop/CTA (last 5s)
        Target: 115-140 words (~42-52s at the channel's narration pace).

        2026-07-14 length note: an earlier pass tuned this to 55-70 words (~22-28s) to
        chase raw completion rate. Per updated 2026 research, the algorithm's sweet spot
        is 40-55s (hold 50%+ watch-through on 30-60s Shorts), so the target is retuned
        upward here with retention bridges between beats to defend the longer runtime.

        Returns:
            dict with keys: narration, visual_prompt, title, sections
            sections is a list of dicts: [{narration, visual_prompt, section_type}]
        """
        system = niche_config.get("script_system", "You are a YouTube Shorts expert who writes viral 40-55 second scripts that loop.")
        title = topic.get("title", "Untitled")
        hook_sentence = topic.get("hook", "")
        curiosity_angle = topic.get("curiosity_angle", "")
        channel_style = channel_config.get("visual_style", "cinematic, high-quality")

        # Proven, niche-specific hook templates (config/niche_prompts.json -> hook_styles).
        # The model adapts ONE to this exact topic; it never copies a template verbatim.
        hook_styles = niche_config.get("hook_styles", [])
        hook_examples = ""
        if hook_styles:
            _sample = "\n".join(f"   - {h}" for h in hook_styles[:12])
            hook_examples = (
                "\n\nPROVEN HOOK PATTERNS for this niche (adapt ONE to THIS exact topic, "
                "never copy verbatim, keep it under 12 words, first five words carry the claim):\n"
                + _sample
            )

        shorts_prompt = f"""Write a 40-55 second YouTube Shorts script for: "{title}"

HOOK sentence to use or adapt: "{hook_sentence}"
Curiosity angle: "{curiosity_angle}"{hook_examples}

STRICT STRUCTURE (total 115-140 words -> ~42-52s of speech at narration pace).
2026 ranking reality: the FIRST 3 SECONDS decide 50-60% of all swipe-away, and the
sweet spot for the 2026 Shorts algorithm is 40-55 seconds - long enough to satisfy,
short enough to hold 50%+ watch-through. Every replay still counts as a new view, so
the script must also LOOP. Build it as FOUR beats with a retention bridge between each:

1. HOOK (0-3s, ~10 words): The FIRST FIVE WORDS must already carry the claim. No warm-up,
   no "in this video", no scene-setting, no "have you ever". Use a bold claim, a curiosity
   gap, or a pattern interrupt. Open mid-thought, as if the viewer walked in on the most
   interesting sentence.
   Good: "This 300-year-old map shows a city that never existed."
   Bad:  "Today we're going to look at a very strange old map."

2. TENSION / CURIOSITY (3-15s, ~30-40 words): Widen the gap the hook opened. Raise the
   stakes and add ONE concrete detail, but keep the answer just out of reach. END this beat
   on a retention bridge that makes skipping feel risky - e.g. "But here's where it gets
   crazy...", "And almost nobody notices the next part...", "Nobody talks about this, but...".

3. PAYLOAD (15-40s, ~55-70 words): Deliver the answer, but earn it. Three to four punchy
   sentences. Concrete and specific - name the exact thing, place, step or mechanism
   (NEVER invent a statistic, study or quote). Land the genuine reveal here, and shift the
   energy at least once so the middle never flattens.

4. LOOP / CTA (last ~5s, ~10-15 words): The final sentence must send the viewer straight
   back to the first sentence - re-pose the hook now that they know the answer, so the
   replay feels intentional and the Short loops seamlessly.
   Good: "Which is exactly why nobody can explain the city on that map."
   BANNED in narration: "subscribe", "like this video", "follow for more", "comment
   below", "next video drops tomorrow", or any other channel-promo line. The follow-ask
   lives in the on-screen text and description, NOT in the audio - it wastes the
   highest-value seconds of the Short and breaks the loop.

Also: no trailing filler, no sign-off, no "thanks for watching". The audio must END on
the loop line.

HARD BANS (these get the upload blocked by YouTube's misleading-metadata rules, so the
whole Short is wasted):
- NO specific money/income claims anywhere in the title or narration. Never "I turned
  $1k into $10k", "make $500 a day", "$10,000 per month". Describe the METHOD and the
  MECHANISM, never a dollar outcome. "The pricing mistake that caps most freelancers"
  is fine; "How I made $10k freelancing" is not.
- NO invented statistics, studies, experts or quotes. If you don't know a real number,
  don't use a number.
- The title must be a COMPLETE thought. It must NOT end in "..." or trail off, and must
  not end on a dangling word like "and", "the", "into", "is".

VISUAL STYLE: {channel_style}

Return ONLY valid JSON:
{{
  "title": "Shorts title under 60 chars with curiosity gap",
  "narration": "complete narration joining all 4 beats, 115-140 words total",
  "visual_prompt": "cinematic vertical 9:16 prompt for LTX-Video: specific camera angle, lighting, subject, motion, mood — 30-50 words",
  "sections": [
    {{
      "section_type": "hook",
      "narration": "hook narration, ~10 words, claim lands in the first five words",
      "visual_prompt": "vertical 9:16 cinematic close-up visual for the hook moment"
    }},
    {{
      "section_type": "tension",
      "narration": "tension/curiosity narration, 30-40 words, ends on a retention bridge",
      "visual_prompt": "vertical 9:16 cinematic visual that escalates the stakes"
    }},
    {{
      "section_type": "key_insight",
      "narration": "payload narration, 55-70 words, the concrete reveal",
      "visual_prompt": "vertical 9:16 cinematic visual showing the key insight in action"
    }},
    {{
      "section_type": "cta",
      "narration": "loop line, 10-15 words - re-poses the hook so the replay is seamless. No subscribe/follow ask.",
      "visual_prompt": "vertical 9:16 cinematic closing visual that visually rhymes with the opening shot"
    }}
  ]
}}"""

        response = self._call(shorts_prompt, system=system, temperature=0.8)
        result = self._extract_json(response)

        if not isinstance(result, dict):
            result = {}

        # Validate word count and retry if too short
        narration = result.get("narration", "")
        if isinstance(narration, list):
            narration = " ".join(str(x) for x in narration)
        narration = str(narration or "")
        result["narration"] = narration  # guarantee str from here on
        word_count = len(narration.split())

        # 2026-07-14 (retune): target is now 115-140 words (~42-52s), the 40-55s
        # sweet spot for the 2026 Shorts algorithm. Floor of 100 keeps every Short
        # comfortably in the 40-55s band while staying clear of the 60s quality-gate
        # ceiling in generate_shorts.py. (Superseded the same-day 55-70-word / 22-28s
        # completion-first tuning per updated retention research.)
        if word_count < 100:
            logger.info(f"Shorts narration too short ({word_count} words), retrying")
            retry_prompt = (
                shorts_prompt
                + f"\n\nPrevious attempt was only {word_count} words — TOO SHORT. "
                "Total narration must be 115-140 words. Expand the TENSION and PAYLOAD beats with "
                "more concrete detail and a retention bridge. Do NOT pad it with a subscribe/follow line."
            )
            response2 = self._call(retry_prompt, system=system, temperature=0.7)
            result2 = self._extract_json(response2)
            if isinstance(result2, dict):
                narration2 = result2.get("narration", "")
                if isinstance(narration2, list):
                    narration2 = " ".join(str(x) for x in narration2)
                narration2 = str(narration2 or "")
                if len(narration2.split()) > word_count:
                    result2["narration"] = narration2  # guarantee str
                    result = result2

        # Harden visual_prompt to str (LLM may return a dict or None)
        if not isinstance(result.get("visual_prompt"), str):
            result["visual_prompt"] = f"cinematic vertical 9:16 visual for: {title}"

        # Clean up title
        if result.get("title"):
            result["title"] = self._clean_title(result["title"])

        # Ensure sections list exists with correct structure
        if not result.get("sections"):
            result["sections"] = [
                {
                    "section_type": "hook",
                    "narration": result.get("narration", "")[:100],
                    "visual_prompt": result.get("visual_prompt", f"vertical 9:16 cinematic hook visual for: {title}"),
                },
                {
                    "section_type": "key_insight",
                    "narration": result.get("narration", ""),
                    "visual_prompt": result.get("visual_prompt", f"vertical 9:16 cinematic insight visual for: {title}"),
                },
                {
                    "section_type": "cta",
                    "narration": "And that's the part almost nobody sees coming.",
                    "visual_prompt": f"vertical 9:16 energetic closing shot related to: {title}",
                },
            ]

        final_wc = len(str(result.get("narration", "")).split())
        logger.info(f"generate_short_script: {final_wc} words, {len(result.get('sections', []))} sections")
        return result

    def generate_music_prompt(self, topic: dict, niche: str, script_mood: str = "") -> str:
        """Generate a MusicGen-compatible music description tailored to the video topic.

        Returns a comma-separated string of 15-25 words describing the music style,
        e.g. "electronic ambient, futuristic synths, driven beat, tech documentary, energetic".

        Falls back to the niche base style string if Ollama fails — never returns empty.
        """
        NICHE_BASE = {
            "tech_ai":         "electronic ambient, futuristic synths, driven beat, tech documentary",
            "finance":         "corporate jazz, piano, subtle tension, professional, calm focus",
            "motivation":      "uplifting orchestral, epic strings, building energy, inspiring",
            "history_mystery": "mysterious orchestral, dark ambient, ancient, cinematic score",
            "science_facts":   "curious electronic, scientific documentary, wonder, clean beats",
        }
        base_style = NICHE_BASE.get(niche, "cinematic background music, documentary style")

        title = topic.get("title", "")
        hook = topic.get("hook", "")
        mood_hint = f" Mood hint: {script_mood}." if script_mood else ""

        prompt = (
            f"You are a music supervisor choosing background music for a YouTube video.\n\n"
            f"Video title: \"{title}\"\n"
            f"Opening hook: \"{hook}\"\n"
            f"Channel niche: {niche}\n"
            f"Base musical style: {base_style}\n"
            f"{mood_hint}\n\n"
            f"Write a MusicGen music description for the background track. "
            f"It must be 15-25 words, comma-separated music descriptors only — no sentences, no explanation. "
            f"Examples of the correct format:\n"
            f"  \"cinematic orchestral, tense building, documentary style, strings and brass, emotional\"\n"
            f"  \"dark electronic, pulsing bass, mysterious, atmospheric synths, slow tension build\"\n\n"
            f"Base the description on the actual topic and mood of the video title above. "
            f"Start from the base style but adapt it to match the video's specific content. "
            f"Return ONLY the comma-separated descriptor string, nothing else."
        )

        try:
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": 0.6, "num_predict": 50},
            }
            r = requests.post(
                f"{self.host}/api/generate", json=payload, timeout=60
            )
            r.raise_for_status()
            raw = r.json().get("response", "").strip()

            # Strip any preamble the model might add before the actual descriptors
            raw = self._strip_preamble(raw).strip().strip('"').strip("'")

            # Keep only lines that look like descriptor lists (contain commas, no full sentences)
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            for line in lines:
                if "," in line and len(line) < 250:
                    # Remove any trailing explanation after a sentence-ending period
                    clean = line.split(".")[0].strip()
                    word_count = len(clean.split())
                    if 8 <= word_count <= 40:
                        logger.info(f"generate_music_prompt: '{clean[:80]}'")
                        return clean

            # If no well-formed line found, use the first non-empty line clipped to safety
            if lines:
                candidate = lines[0].split(".")[0].strip()
                if candidate:
                    logger.info(f"generate_music_prompt (fallback line): '{candidate[:80]}'")
                    return candidate

        except Exception as e:
            logger.warning(f"generate_music_prompt: Ollama call failed ({e}), using niche base")

        # Hard fallback — always return the niche base style
        logger.info(f"generate_music_prompt: using niche base fallback '{base_style}'")
        return base_style

    def select_best_shorts_from_script(self, script: dict, count: int = 3, words_target: int = 120) -> list[dict]:
        """Select the N highest-retention passages from a long-form script for Shorts.

        Heuristic-based (fast, no extra LLM call). Scores each contiguous `words_target`-word
        window inside every section's narration for retention signals:
          - opens with a question, number, or strong imperative
          - contains surprise markers ("but", "actually", "turns out", "here's why")
          - has high concrete-noun density (crude proxy: capitalized words mid-sentence)
          - ends on a curiosity loop or pivot

        Returns up to `count` dicts with keys: hook, narration, source_section,
        visual_prompt. These are drop-in replacements for the LLM-generated shorts_hooks.

        This is a signal-preserving strategy: shorts derived from the long-form let
        YouTube see the same topic & narration pattern landing across both surfaces,
        which helps the algorithm suggest the long-form after the short hits.
        """
        sections = script.get("script_sections") or []
        if not sections:
            return []

        import re as _re

        surprise_markers = (
            "but ", "actually", "turns out", "here's why", "here is why",
            "the truth is", "the twist", "wait ", "suddenly", "what they found",
            "what we found", "it gets", "most people", "few know", "the catch",
        )
        imperative_openers = (
            "stop", "start", "imagine", "picture", "think", "consider",
            "remember", "listen", "look", "watch", "forget",
        )

        def score_window(text: str) -> float:
            low = text.lower().strip()
            if not low:
                return 0.0
            score = 0.0
            first_word = low.split(" ", 1)[0]
            if first_word in imperative_openers:
                score += 3.0
            if low.startswith(("what ", "why ", "how ", "did you", "have you")):
                score += 2.5
            # number in the first 12 words
            first_chunk = " ".join(low.split()[:12])
            if _re.search(r"\d", first_chunk):
                score += 1.5
            # surprise markers anywhere
            for m in surprise_markers:
                if m in low:
                    score += 1.0
            # curiosity loop ending
            if low.rstrip(".!?").endswith(("but", "next", "however", "yet", "though")):
                score += 1.0
            # concrete-noun density proxy: mid-sentence capitalized words
            words = text.split()
            mid_caps = sum(1 for w in words[1:] if w[:1].isupper() and w[:1].isalpha())
            score += min(mid_caps * 0.15, 2.0)
            # length penalty if far from target
            wc = len(words)
            score -= abs(wc - words_target) * 0.02
            return score

        candidates = []
        for s_idx, sec in enumerate(sections):
            narration = sec.get("narration", "") if isinstance(sec, dict) else ""
            if not narration or not isinstance(narration, str):
                continue
            words = narration.split()
            if len(words) < max(30, words_target // 2):
                continue
            # Slide a window of `words_target` words across the section; step ~1/3 window.
            step = max(10, words_target // 3)
            for i in range(0, max(1, len(words) - words_target + 1), step):
                window_words = words[i : i + words_target]
                if len(window_words) < max(30, words_target // 2):
                    continue
                window = " ".join(window_words)
                candidates.append(
                    {
                        "narration": window,
                        "source_section": sec.get("section_title", sec.get("title", f"Section {s_idx+1}")),
                        "score": score_window(window),
                        "section_index": s_idx,
                    }
                )

        if not candidates:
            return []

        # Prefer diversity — dedupe by source section when possible.
        candidates.sort(key=lambda c: c["score"], reverse=True)
        selected: list[dict] = []
        used_sections: set = set()
        for cand in candidates:
            if cand["section_index"] in used_sections and len(used_sections) < len(sections):
                continue
            used_sections.add(cand["section_index"])
            # First sentence of the window is the hook.
            first_sentence = _re.split(r"(?<=[.!?])\s+", cand["narration"], maxsplit=1)[0]
            visual_prompt = (
                sections[cand["section_index"]].get("visual_prompt")
                if isinstance(sections[cand["section_index"]], dict)
                else None
            ) or f"cinematic vertical 9:16 visual for: {cand['source_section']}"
            selected.append(
                {
                    "hook": first_sentence[:160],
                    "narration": cand["narration"],
                    "source_section": cand["source_section"],
                    "visual_prompt": visual_prompt,
                    "score": round(cand["score"], 2),
                }
            )
            if len(selected) >= count:
                break

        logger.info(
            f"select_best_shorts_from_script: picked {len(selected)} clips from "
            f"{len(sections)} sections (candidates: {len(candidates)})"
        )
        return selected

    def generate_shorts_script(self, hook: str, niche_config: dict, min_words: int = 75, max_words: int = 120) -> dict:
        """Generate a 30-48 second YouTube Shorts script from a hook.

        2026-07-11: retuned from 100-150 words to 75-120. Shorts can run to 3
        minutes now, but 2025-26 completion data converges on ~25-50s as the
        top-performing band — watched-% and replays are what scale a Short.
        Kokoro TTS ~150 wpm (2.5 wps) → 75-120 words = 30-48s. If the LLM
        returns under `min_words`, retries once with a more directive prompt.
        """
        system = niche_config.get("script_system", "You are a YouTube Shorts expert.")

        def _build_prompt(min_w, max_w):
            return f"""Write a YouTube Shorts script from this hook:
"{hook}"

STRICT REQUIREMENTS:
- Narration MUST be between {min_w} and {max_w} words. Shorter scripts underperform.
- Grab attention in the first 2 seconds with a pattern interrupt or bold claim.
- Pack in 2-3 concrete, surprising facts or insights.
- End with a line that flows naturally back into the opening (seamless loop — replays boost reach) or a curiosity question.
- Write conversational spoken-word narration, no headings or stage directions.
- Vertical 9:16 format.

Return ONLY valid JSON, nothing else:
{{
  "title": "Shorts title with #shorts",
  "narration": "the full narration text (between {min_w} and {max_w} words)",
  "visual_prompts": ["visual for each 10-second segment"],
  "description": "shorts description with hashtags"
}}"""

        response = self._call(_build_prompt(min_words, max_words), system=system, temperature=0.8)
        result = self._extract_json(response)

        # Retry once if narration is too short — shorts under ~15s tank in the algorithm.
        narration = result.get("narration", "") if isinstance(result, dict) else ""
        if isinstance(narration, list):
            narration = " ".join(str(x) for x in narration)
        narration = str(narration or "")
        word_count = len(narration.split())

        if word_count < min_words:
            logger.info(
                f"Shorts narration too short ({word_count} words, need >={min_words}), retrying with stricter prompt"
            )
            retry_prompt = (
                _build_prompt(min_words, max_words)
                + f"\n\nYour previous attempt returned only {word_count} words. That is TOO SHORT. "
                f"Write a LONGER narration, at LEAST {min_words} words, packed with specific facts and insights."
            )
            response2 = self._call(retry_prompt, system=system, temperature=0.7)
            result2 = self._extract_json(response2)
            if isinstance(result2, dict):
                narration2 = result2.get("narration", "")
                if isinstance(narration2, list):
                    narration2 = " ".join(str(x) for x in narration2)
                narration2 = str(narration2 or "")
                if len(narration2.split()) > word_count:
                    return result2

        return result
