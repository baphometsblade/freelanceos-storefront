"""
Monetization Tracker - Tracks channel growth toward YouTube Partner Program thresholds.
"""
import json
import logging
from collections import Counter
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("analytics.monetization")


class MonetizationTracker:
    def __init__(self, data_dir="./analytics"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.channels_file = self.data_dir / "channels_status.json"
        self.uploads_log = self.data_dir / "uploads_log.json"
        self.daily_stats = self.data_dir / "daily_stats.json"
        self._ensure_files()

    def _ensure_files(self):
        for f in [self.channels_file, self.uploads_log, self.daily_stats]:
            if not f.exists():
                f.write_text("[]" if "log" in f.name or "stats" in f.name else "{}")

    def _load(self, filepath):
        try:
            return json.loads(filepath.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return {} if "status" in filepath.name else []

    def _save(self, filepath, data):
        filepath.write_text(json.dumps(data, indent=2, default=str))

    def log_upload(self, upload_info):
        log = self._load(self.uploads_log)
        if not isinstance(log, list):
            log = []
        upload_info["logged_at"] = datetime.now().isoformat()
        log.append(upload_info)
        self._save(self.uploads_log, log)

    def update_channel_stats(self, channel_id, stats, watch_hours: float = None):
        """Update channel stats and monetization progress.

        Args:
            channel_id: Channel identifier.
            stats: Dict with subscribers, total_views, video_count, channel_name.
            watch_hours: Real watch hours from YouTube Analytics API.
                         If None or negative, falls back to estimation (views * 0.05).
        """
        channels = self._load(self.channels_file)
        if not isinstance(channels, dict):
            channels = {}
        current = channels.get(channel_id, {})
        current.update({
            "channel_id": channel_id,
            "channel_name": stats.get("channel_name", current.get("channel_name", "")),
            "subscribers": stats.get("subscribers", 0),
            "total_views": stats.get("total_views", 0),
            "video_count": stats.get("video_count", 0),
            "last_updated": datetime.now().isoformat(),
        })
        subs = current["subscribers"]

        # Use real watch hours from Analytics API if available, otherwise estimate
        if watch_hours is not None and watch_hours >= 0:
            actual_watch_hours = watch_hours
            hours_source = "analytics_api"
        else:
            actual_watch_hours = current["total_views"] * 0.05
            hours_source = "estimated"

        current["monetization"] = {
            "subscribers_progress": min(subs / 1000 * 100, 100),
            "subscribers_remaining": max(1000 - subs, 0),
            "watch_hours": actual_watch_hours,
            "watch_hours_source": hours_source,
            "watch_hours_progress": min(actual_watch_hours / 4000 * 100, 100),
            "watch_hours_remaining": max(4000 - actual_watch_hours, 0),
            "eligible": subs >= 1000 and actual_watch_hours >= 4000,
            "status": "ELIGIBLE" if (subs >= 1000 and actual_watch_hours >= 4000) else "GROWING",
        }
        channels[channel_id] = current
        self._save(self.channels_file, channels)

    def log_daily_stats(self, channel_id, video_stats):
        daily = self._load(self.daily_stats)
        if not isinstance(daily, list):
            daily = []
        daily.append({"date": datetime.now().strftime("%Y-%m-%d"), "channel_id": channel_id, "videos_analyzed": len(video_stats), "total_views_today": sum(v.get("views", 0) for v in video_stats), "total_likes_today": sum(v.get("likes", 0) for v in video_stats), "top_video": max(video_stats, key=lambda v: v.get("views", 0)) if video_stats else None})
        self._save(self.daily_stats, daily)

    def get_dashboard_data(self):
        channels = self._load(self.channels_file)
        uploads = self._load(self.uploads_log)
        daily = self._load(self.daily_stats)
        total_videos = len(uploads) if isinstance(uploads, list) else 0
        total_channels = len(channels) if isinstance(channels, dict) else 0
        eligible_channels = 0
        total_subs = 0
        if isinstance(channels, dict):
            for ch in channels.values():
                total_subs += ch.get("subscribers", 0)
                if ch.get("monetization", {}).get("eligible"):
                    eligible_channels += 1
        return {"summary": {"total_channels": total_channels, "total_videos_uploaded": total_videos, "total_subscribers": total_subs, "eligible_for_monetization": eligible_channels, "generated_at": datetime.now().isoformat()}, "channels": channels if isinstance(channels, dict) else {}, "recent_uploads": (uploads[-20:] if isinstance(uploads, list) else []), "daily_stats": (daily[-30:] if isinstance(daily, list) else [])}

    def log_video_performance(self, video_info: dict):
        """Append a video performance record to analytics/video_performance.json."""
        perf_file = self.data_dir / "video_performance.json"
        if not perf_file.exists():
            perf_file.write_text("[]")
        try:
            records = json.loads(perf_file.read_text())
            if not isinstance(records, list):
                records = []
        except (json.JSONDecodeError, FileNotFoundError):
            records = []

        record = {
            "video_id": str(video_info.get("video_id", "")),
            "channel_id": str(video_info.get("channel_id", "")),
            "title": str(video_info.get("title", "")),
            "niche": str(video_info.get("niche", "")),
            "uploaded_at": str(video_info.get("uploaded_at", datetime.now().isoformat())),
            "playlist_id": str(video_info.get("playlist_id", "")),
            "has_thumbnail": bool(video_info.get("has_thumbnail", False)),
            "has_captions": bool(video_info.get("has_captions", False)),
            "tags_count": int(video_info.get("tags_count", 0)),
            "duration_seconds": float(video_info.get("duration_seconds", 0)),
            "is_short": bool(video_info.get("is_short", False)),
        }
        records.append(record)
        perf_file.write_text(json.dumps(records, indent=2, default=str))
        logger.info(f"Logged video performance: {record['video_id']} ({record['channel_id']})")

    def generate_daily_report(self, date_str: str = None) -> dict:
        """Read video_performance.json and channels_status.json to produce a daily report.

        Saves the report to analytics/daily_report_{date_str}.json and returns it.
        """
        if date_str is None:
            date_str = datetime.now().strftime("%Y-%m-%d")

        perf_file = self.data_dir / "video_performance.json"
        try:
            all_records = json.loads(perf_file.read_text()) if perf_file.exists() else []
            if not isinstance(all_records, list):
                all_records = []
        except (json.JSONDecodeError, FileNotFoundError):
            all_records = []

        channels_data = self._load(self.channels_file)
        if not isinstance(channels_data, dict):
            channels_data = {}

        # Filter to records uploaded today (date_str prefix match on uploaded_at)
        today_records = [
            r for r in all_records
            if str(r.get("uploaded_at", "")).startswith(date_str)
        ]

        shorts_today = [r for r in today_records if r.get("is_short")]
        long_form_today = [r for r in today_records if not r.get("is_short")]

        # Per-channel breakdown
        channels_report = {}
        ch_ids = set(r.get("channel_id", "") for r in today_records)
        for ch_id in ch_ids:
            if not ch_id:
                continue
            ch_records = [r for r in today_records if r.get("channel_id") == ch_id]
            niche_counter = Counter(r.get("niche", "") for r in ch_records if r.get("niche"))
            best_niche = niche_counter.most_common(1)[0][0] if niche_counter else ""
            mon_progress = channels_data.get(ch_id, {}).get("monetization", {})
            channels_report[ch_id] = {
                "uploads": len(ch_records),
                "best_niche": best_niche,
                "monetization_progress": mon_progress,
            }

        # Recommendations
        recommendations = []
        for ch_id, ch in channels_data.items():
            mon = ch.get("monetization", {})
            ch_name = ch.get("channel_name", ch_id)
            subs_remaining = mon.get("subscribers_remaining", 0)
            hours_remaining = mon.get("watch_hours_remaining", 0)
            if subs_remaining > 0 and hours_remaining > 0:
                recommendations.append(
                    f"{ch_name} needs {int(subs_remaining)} more subscribers "
                    f"and {hours_remaining:.0f}h more watch time for YPP"
                )
            elif subs_remaining > 0:
                recommendations.append(
                    f"{ch_name} needs {int(subs_remaining)} more subscribers for YPP"
                )
            elif hours_remaining > 0:
                recommendations.append(
                    f"{ch_name} needs {hours_remaining:.0f} more watch hours for YPP"
                )
            elif mon.get("eligible"):
                recommendations.append(f"{ch_name} is eligible — apply for YPP now!")

        report = {
            "date": date_str,
            "total_uploads": len(today_records),
            "shorts_count": len(shorts_today),
            "long_form_count": len(long_form_today),
            "channels": channels_report,
            "recommendations": recommendations,
        }

        report_file = self.data_dir / f"daily_report_{date_str}.json"
        report_file.write_text(json.dumps(report, indent=2, default=str))
        logger.info(f"Daily report saved: {report_file}")
        return report

    def get_monetization_summary(self) -> str:
        """Return a human-readable string summarizing YPP progress.

        Deduplicated by underlying YouTube channel name: when multiple pipeline
        IDs share one Brand Account (e.g. all 5 niches point to "Matrix monster")
        only ONE line per real YouTube destination is emitted.

        Example line:
            Matrix monster: 847 subs (84.7%) | 1,203h watch (30.1%) — NEEDS WATCH HOURS
        """
        channels = self._load(self.channels_file)
        if not isinstance(channels, dict) or not channels:
            return "No channel data available."

        # Dedupe: pick the entry with the highest stats per channel_name
        # (highest = freshest, since stats only ever go up over time).
        deduped: dict[str, dict] = {}
        for ch_id, ch in channels.items():
            name = ch.get("channel_name", ch_id)
            existing = deduped.get(name)
            if existing is None or ch.get("subscribers", 0) >= existing.get("subscribers", 0):
                deduped[name] = ch

        lines = []
        for name in sorted(deduped):
            ch = deduped[name]
            mon = ch.get("monetization", {})
            subs = ch.get("subscribers", 0)
            subs_pct = mon.get("subscribers_progress", 0)
            hours = mon.get("watch_hours", 0)
            hours_pct = mon.get("watch_hours_progress", 0)

            if mon.get("eligible"):
                status = "ELIGIBLE FOR YPP"
            elif subs_pct < 100 and hours_pct < 100:
                status = "NEEDS SUBS + WATCH HOURS"
            elif subs_pct < 100:
                status = "NEEDS SUBSCRIBERS"
            else:
                status = "NEEDS WATCH HOURS"

            lines.append(
                f"{name}: {subs:,} subs ({subs_pct:.1f}%) | "
                f"{hours:,.0f}h watch ({hours_pct:.1f}%) — {status}"
            )

        return "\n".join(lines)

    def generate_report(self):
        data = self.get_dashboard_data()
        lines = ["=" * 60, "  YOUTUBE CHANNEL EMPIRE - MONETIZATION REPORT", f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}", "=" * 60, "", f"  Total Channels: {data['summary']['total_channels']}", f"  Total Videos Uploaded: {data['summary']['total_videos_uploaded']}", f"  Combined Subscribers: {data['summary']['total_subscribers']}", f"  Channels Eligible for YPP: {data['summary']['eligible_for_monetization']}", "", "-" * 60]
        for ch_id, ch in data["channels"].items():
            mon = ch.get("monetization", {})
            hours_label = "Watch Hours" if mon.get("watch_hours_source") == "analytics_api" else "Est. Watch Hours"
            lines.extend([
                f"\n  Channel: {ch.get('channel_name', ch_id)}",
                f"    Subscribers: {ch.get('subscribers', 0):,} / 1,000 ({mon.get('subscribers_progress', 0):.1f}%)",
                f"    {hours_label}: {mon.get('watch_hours', 0):,.0f} / 4,000 ({mon.get('watch_hours_progress', 0):.1f}%)",
                f"    Videos: {ch.get('video_count', 0)}",
                f"    Status: {'ELIGIBLE' if mon.get('eligible') else 'GROWING'}",
                "-" * 40,
            ])
        return "\n".join(lines)
