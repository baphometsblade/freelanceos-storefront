#!/usr/bin/env python3
"""Quick preflight: checks lock, Fooocus port, Ollama, token ages, GPU VRAM."""
import json, sys, time, requests
from pathlib import Path
from datetime import datetime, timezone

ROOT = Path(__file__).parent

def check(name, ok, detail=""):
    status = "✓" if ok else "✗"
    print(f"  {status} {name}" + (f": {detail}" if detail else ""))
    return ok

results = []

# Lock file
lock = ROOT / "logs" / "pipeline.lock"
if lock.exists():
    pid = lock.read_text().strip()
    import psutil
    alive = psutil.pid_exists(int(pid)) if pid.isdigit() else False
    if alive:
        results.append(check("Lock file", False, f"PID {pid} still running - pipeline already active"))
    else:
        lock.unlink()
        results.append(check("Lock file", True, "stale lock removed"))
else:
    results.append(check("Lock file", True, "clear"))

# Fooocus
try:
    r = requests.get("http://127.0.0.1:7865/v1/", timeout=5)
    results.append(check("Fooocus API", True, f"HTTP {r.status_code}"))
except Exception as e:
    results.append(check("Fooocus API", False, str(e)[:60]))

# Ollama
try:
    r = requests.get("http://localhost:11434/api/tags", timeout=5)
    models = [m["name"] for m in r.json().get("models", [])]
    results.append(check("Ollama", True, f"{len(models)} models: {', '.join(models[:3])}"))
except Exception as e:
    results.append(check("Ollama", False, str(e)[:60]))

# OAuth tokens
tokens_dir = ROOT / "config" / "tokens"
for tf in sorted(tokens_dir.glob("*_token.json")):
    data = json.loads(tf.read_text())
    expiry = data.get("token_expiry") or data.get("expiry")
    channel = tf.stem.replace("_token", "")
    if expiry:
        try:
            exp_dt = datetime.fromisoformat(expiry.replace("Z","+00:00"))
            age_days = (datetime.now(timezone.utc) - exp_dt.replace(tzinfo=timezone.utc) if exp_dt.tzinfo is None else datetime.now(timezone.utc) - exp_dt).days
            results.append(check(f"Token {channel}", age_days < 6, f"age ~{age_days}d"))
        except:
            results.append(check(f"Token {channel}", True, "present"))
    else:
        results.append(check(f"Token {channel}", True, "present (no expiry field)"))

passed = sum(results)
total = len(results)
print(f"\n{'All checks passed' if passed == total else f'{total-passed} issue(s) found'} ({passed}/{total})")
sys.exit(0 if passed == total else 1)
