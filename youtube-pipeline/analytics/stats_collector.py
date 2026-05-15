"""
stats_collector.py — Pull real YouTube channel stats and update YPP progress.

Fetches subscriber count, total views, and watch hours from the YouTube Data API
and YouTube Analytics API for all 5 channels, then updates MonetizationTracker
and prints a human-readable progress report toward YouTube Partner Program (YPP)
thresholds (1,000 subscribers and 4,000 watch hours).

Usage:
    python analytics/stats_collector.py              # all channels
    python analytics/stats_collector.py tech_ai      # single channel
    python analytics/stats_collector.py --report-only # print last saved stats, no API calls
"""
import json
import sys
from datetime import datetime
from pathlib import Path

# Allow running from the pipeline root or from analytics/
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _bar(pct: float, width: int = 20) -> str:
    """Render a simple ASCII progress bar for a percentage 0-100."""
    filled = min(width, max(0, int(pct / 100 * width)))
    return "█" * filled + "░" * (width - filled)


def _format_progress(label: str, value: float, threshold: float, unit: str, width: int = 20) -> str:
    pct = min(100.0, value / threshold * 100) if threshold > 0 else 0.0
    bar = _bar(pct, width)
    remaining = max(0.0, threshold - value)
    status = "✓ DONE" if pct >= 100.0 else f"{remaining:,.0f} {unit} to go"
    return f"    {label}: {value:>8,.0f} / {threshold:,.0f} {unit}  [{bar}] {pct:5.1f}%  ({status})"


def _print_channel_report(ch_id: str, name: str, subs: int, views: int, hours: float, eligible: bool):
    """Print a formatted YPP progress block for one channel."""
    print(f"\n  ┌─ {name} ({ch_id})")
    print(_format_progress("Subscribers", subs, 1000, "subs"))
    if hours >= 0:
        print(_format_progress("Watch Hours ", hours, 4000, "hours"))
    else:
        print(f"    Watch Hours: N/A (YouTube Analytics API unavailable)")
    if eligible:
        print(f"  │  🎉 ELIGIBLE FOR YPP — apply at studio.youtube.com")
    print(f"  └─ Total views: {views:,}")


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------

def collect_stats(channel_ids: list = None, report_only: bool = False) -> dict:
    """Collect stats for the given channel IDs (or all channels if None).

    Returns a summary dict mapping channel_id → {subs, hours, eligible}.
    """
    config_path = ROOT / "config" / "channels.json"
    if not config_path.exists():
        print(f"ERROR: channels.json not found at {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    channels = config.get("channels", [])
    if channel_ids:
        channels = [c for c in channels if c["id"] in channel_ids]
    if not channels:
        print("No channels to process.")
        return {}

    # Lazy imports so the script starts fast even when libraries aren't loaded
    from analytics.monetization_tracker import MonetizationTracker
    tracker = MonetizationTracker(str(ROOT / "analytics"))

    if report_only:
        print("\n=== YPP PROGRESS REPORT (saved data) ===")
        summary = tracker.get_monetization_summary()
        print(summary if summary.strip() else "  No channel data saved yet. Run without --report-only to fetch live stats.")
        return {}

    from uploaders.youtube_uploader import YouTubeUploader
    uploader = YouTubeUploader(config.get("global", {}))

    print(f"\n=== COLLECTING YOUTUBE STATS ({datetime.now().strftime('%Y-%m-%d %H:%M:%S')}) ===")
    print(f"  Channels: {[c['id'] for c in channels]}")
    print()

    results = {}

    for ch in channels:
        ch_id = ch["id"]
        ch_name = ch.get("name", ch_id)
        print(f"  Fetching {ch_name} ({ch_id})...", end=" ", flush=True)

        # --- Subscriber count + views ---
        stats = {}
        try:
            stats = uploader.get_channel_stats(ch_id)
            print(f"{stats.get('subscribers', 0):,} subs", end=" ", flush=True)
        except Exception as e:
            print(f"STATS FAILED: {e}", flush=True)

        # --- Watch hours from Analytics API ---
        watch_hours = -1.0
        try:
            watch_hours = uploader.get_watch_hours(ch_id, days=365)
            if watch_hours >= 0:
                print(f"| {watch_hours:,.0f}h watch", end=" ", flush=True)
            else:
                print("| watch hours N/A", end=" ", flush=True)
        except Exception as e:
            print(f"| watch hours FAILED: {e}", end=" ", flush=True)

        print()  # newline after the status line

        # --- Update tracker ---
        if stats.get("subscribers", 0) or stats.get("total_views", 0):
            try:
                tracker.update_channel_stats(ch_id, stats, watch_hours=watch_hours)
            except Exception as e:
                print(f"    WARNING: tracker.update_channel_stats failed: {e}")

        subs = stats.get("subscribers", 0)
        views = stats.get("total_views", 0)
        eligible = subs >= 1000 and watch_hours >= 4000
        results[ch_id] = {
            "channel_name": ch_name,
            "subscribers": subs,
            "total_views": views,
            "watch_hours": watch_hours,
            "eligible": eligible,
        }

    # --- Print detailed report ---
    print("\n=== YPP PROGRESS REPORT ===")
    for ch_id, r in results.items():
        _print_channel_report(
            ch_id,
            r["channel_name"],
            r["subscribers"],
            r["total_views"],
            r["watch_hours"],
            r["eligible"],
        )

    # --- Print tracker summary ---
    print("\n=== MONETIZATION SUMMARY ===")
    summary = tracker.get_monetization_summary()
    for line in summary.strip().split("\n"):
        print(f"  {line}")

    # --- Save JSON report ---
    today = datetime.now().strftime("%Y%m%d")
    report_path = ROOT / "analytics" / f"stats_report_{today}.json"
    report = {
        "date": datetime.now().isoformat(),
        "channels": results,
    }
    report_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"\n  Report saved: {report_path}")

    # --- YPP callout ---
    eligible_channels = [ch_id for ch_id, r in results.items() if r["eligible"]]
    if eligible_channels:
        print(f"\n{'='*50}")
        print(f"  🎉 YPP-ELIGIBLE: {', '.join(eligible_channels)}")
        print(f"  Apply at: https://studio.youtube.com")
        print(f"{'='*50}")
    else:
        total_subs = sum(r["subscribers"] for r in results.values())
        total_hours = sum(max(0, r["watch_hours"]) for r in results.values())
        print(f"\n  Combined empire: {total_subs:,} subs | {total_hours:,.0f}h watch")
        closest = max(
            results.items(),
            key=lambda kv: min(
                kv[1]["subscribers"] / 1000,
                max(0, kv[1]["watch_hours"]) / 4000,
            ) if kv[1]["subscribers"] > 0 else 0,
            default=None,
        )
        if closest:
            print(f"  Closest to YPP: {closest[1]['channel_name']} ({closest[0]})")

    # --- Generate HTML dashboard ---
    try:
        _write_dashboard(results, tracker, ROOT)
        print(f"  Dashboard: {ROOT / 'analytics' / 'dashboard.html'}")
    except Exception as _de:
        print(f"  Dashboard generation failed (non-fatal): {_de}")

    return results


# ---------------------------------------------------------------------------
# Dashboard generator
# ---------------------------------------------------------------------------

def _write_dashboard(results: dict, tracker, root: Path):
    """Write analytics/dashboard.html with embedded data."""
    channels_data = tracker._load(tracker.channels_file) if hasattr(tracker, "channels_file") else {}
    perf_file = root / "analytics" / "video_performance.json"
    try:
        perf_records = json.loads(perf_file.read_text(encoding="utf-8")) if perf_file.exists() else []
    except Exception:
        perf_records = []

    recent = sorted(perf_records, key=lambda r: r.get("uploaded_at", ""), reverse=True)[:20]
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    channel_cards_js = json.dumps(results, indent=2, default=str)
    channels_status_js = json.dumps(channels_data, indent=2, default=str)
    perf_js = json.dumps(recent, indent=2, default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>YouTube Empire — Monetization Dashboard</title>
<style>
  :root {{
    --bg: #0d0d1a; --card: #161628; --border: #2a2a45;
    --accent: #6c63ff; --green: #22c55e; --yellow: #eab308;
    --red: #ef4444; --text: #e2e8f0; --muted: #94a3b8;
    --bar-bg: #1e1e35; --bar-fill: #6c63ff;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ background: var(--bg); color: var(--text); font-family: 'Segoe UI', system-ui, sans-serif; padding: 24px; }}
  h1 {{ font-size: 1.5rem; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: var(--muted); font-size: 0.85rem; margin-bottom: 24px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 16px; margin-bottom: 32px; }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; }}
  .card-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 4px; }}
  .card-id {{ color: var(--muted); font-size: 0.75rem; margin-bottom: 16px; }}
  .metric {{ margin-bottom: 14px; }}
  .metric-label {{ display: flex; justify-content: space-between; font-size: 0.8rem; color: var(--muted); margin-bottom: 5px; }}
  .metric-value {{ font-weight: 600; color: var(--text); }}
  .bar-track {{ background: var(--bar-bg); border-radius: 4px; height: 8px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 4px; background: var(--bar-fill); transition: width 0.6s ease; }}
  .bar-fill.done {{ background: var(--green); }}
  .badge {{ display: inline-block; padding: 3px 10px; border-radius: 20px; font-size: 0.72rem; font-weight: 600; margin-top: 8px; }}
  .badge.eligible {{ background: #166534; color: #86efac; }}
  .badge.needs-subs {{ background: #1e3a5f; color: #93c5fd; }}
  .badge.needs-hours {{ background: #3b2b00; color: #fde68a; }}
  .badge.needs-both {{ background: #2a1a1a; color: #fca5a5; }}
  .section-title {{ font-size: 1rem; font-weight: 600; margin-bottom: 12px; color: var(--text); }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.82rem; }}
  th {{ text-align: left; padding: 8px 12px; color: var(--muted); border-bottom: 1px solid var(--border); font-weight: 500; }}
  td {{ padding: 8px 12px; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  .chip {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 0.7rem; font-weight: 600; }}
  .chip.short {{ background: #1a2040; color: #93c5fd; }}
  .chip.long {{ background: #1a3020; color: #86efac; }}
  .empty {{ color: var(--muted); text-align: center; padding: 40px; font-size: 0.9rem; }}
  .summary-bar {{ display: flex; gap: 24px; background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 16px 24px; margin-bottom: 24px; flex-wrap: wrap; }}
  .summary-item {{ text-align: center; }}
  .summary-item .num {{ font-size: 1.6rem; font-weight: 700; color: var(--accent); }}
  .summary-item .lbl {{ font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.05em; }}
  .footer {{ color: var(--muted); font-size: 0.75rem; margin-top: 32px; text-align: center; }}
  a {{ color: var(--accent); text-decoration: none; }}
  a:hover {{ text-decoration: underline; }}
</style>
</head>
<body>
<h1>📺 YouTube Empire — Monetization Dashboard</h1>
<p class="subtitle">Generated {generated_at} · Run <code>python analytics/stats_collector.py</code> to refresh</p>

<div id="summary" class="summary-bar"></div>
<div class="grid" id="channel-grid"></div>

<div class="card">
  <div class="section-title">Recent Uploads</div>
  <div id="uploads-table"></div>
</div>

<p class="footer">
  YPP thresholds: 1,000 subscribers + 4,000 watch hours in 12 months ·
  <a href="https://studio.youtube.com" target="_blank">YouTube Studio</a>
</p>

<script>
const CHANNELS = {channel_cards_js};
const STATUS   = {channels_status_js};
const RECENT   = {perf_js};

function bar(pct, done) {{
  const p = Math.min(100, Math.max(0, pct));
  return `<div class="bar-track"><div class="bar-fill${{done ? ' done' : ''}}" style="width:${{p}}%"></div></div>`;
}}

function badge(mon) {{
  if (!mon) return '';
  if (mon.eligible) return '<span class="badge eligible">✓ YPP Eligible</span>';
  const ns = (mon.subscribers_progress || 0) < 100;
  const nh = (mon.watch_hours_progress || 0) < 100;
  if (ns && nh) return '<span class="badge needs-both">Needs Subs + Hours</span>';
  if (ns)       return '<span class="badge needs-subs">Needs Subscribers</span>';
  return         '<span class="badge needs-hours">Needs Watch Hours</span>';
}}

function renderChannels() {{
  const grid = document.getElementById('channel-grid');
  const entries = Object.entries(CHANNELS);
  if (!entries.length) {{
    grid.innerHTML = '<div class="card empty">No channel data yet.<br>Run <code>python analytics/stats_collector.py</code></div>';
    return;
  }}

  let totalSubs = 0, totalHours = 0, eligible = 0;
  entries.forEach(([id, r]) => {{
    const st = STATUS[id] || {{}};
    const mon = st.monetization || {{}};
    const subs = r.subscribers || 0;
    const hours = r.watch_hours >= 0 ? r.watch_hours : (mon.watch_hours || 0);
    totalSubs += subs;
    totalHours += hours;
    if (mon.eligible) eligible++;

    const subsPct  = Math.min(100, subs / 10);
    const hoursPct = Math.min(100, hours / 40);

    grid.innerHTML += `
      <div class="card">
        <div class="card-title">${{r.channel_name || id}}</div>
        <div class="card-id">${{id}}</div>
        <div class="metric">
          <div class="metric-label"><span>Subscribers</span><span class="metric-value">${{subs.toLocaleString()}} / 1,000</span></div>
          ${{bar(subsPct, subs >= 1000)}}
        </div>
        <div class="metric">
          <div class="metric-label"><span>Watch Hours</span><span class="metric-value">${{hours >= 0 ? hours.toLocaleString(undefined, {{maximumFractionDigits:0}}) + ' / 4,000h' : 'N/A'}}</span></div>
          ${{hours >= 0 ? bar(hoursPct, hours >= 4000) : '<div class="bar-track"></div>'}}
        </div>
        ${{badge(mon)}}
      </div>`;
  }});

  // Summary bar
  document.getElementById('summary').innerHTML = `
    <div class="summary-item"><div class="num">${{entries.length}}</div><div class="lbl">Channels</div></div>
    <div class="summary-item"><div class="num">${{totalSubs.toLocaleString()}}</div><div class="lbl">Total Subs</div></div>
    <div class="summary-item"><div class="num">${{Math.round(totalHours).toLocaleString()}}</div><div class="lbl">Watch Hours</div></div>
    <div class="summary-item"><div class="num">${{eligible}}</div><div class="lbl">YPP Eligible</div></div>
    <div class="summary-item"><div class="num">${{RECENT.length}}</div><div class="lbl">Recent Uploads</div></div>`;
}}

function renderUploads() {{
  const el = document.getElementById('uploads-table');
  if (!RECENT.length) {{
    el.innerHTML = '<div class="empty">No uploads recorded yet.</div>';
    return;
  }}
  let rows = RECENT.map(r => `
    <tr>
      <td>${{r.uploaded_at ? r.uploaded_at.substring(0,16) : '—'}}</td>
      <td>${{r.channel_id || '—'}}</td>
      <td style="max-width:280px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${{r.title}}">${{r.title || '—'}}</td>
      <td><span class="chip ${{r.is_short ? 'short' : 'long'}}">${{r.is_short ? 'Short' : 'Long'}}</span></td>
      <td>${{r.video_id ? `<a href="https://youtube.com/watch?v=${{r.video_id}}" target="_blank">${{r.video_id}}</a>` : '—'}}</td>
    </tr>`).join('');
  el.innerHTML = `<table>
    <thead><tr><th>Date</th><th>Channel</th><th>Title</th><th>Type</th><th>Video ID</th></tr></thead>
    <tbody>${{rows}}</tbody></table>`;
}}

renderChannels();
renderUploads();
</script>
</body>
</html>"""

    dash_path = root / "analytics" / "dashboard.html"
    dash_path.write_text(html, encoding="utf-8")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    report_only = "--report-only" in flags
    channel_ids = args if args else None
    collect_stats(channel_ids=channel_ids, report_only=report_only)


if __name__ == "__main__":
    main()
