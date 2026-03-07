from datetime import datetime, timedelta, timezone
import discord

TZ = timezone(timedelta(hours=1))


def parse_event_time(time_str: str):
    time_str = time_str.strip()
    if time_str == "-1":
        return "-1"
    for fmt in ["%d.%m.%Y %H:%M", "%d.%m. %H:%M"]:
        try:
            parsed = datetime.strptime(time_str, fmt)
            if "%Y" not in fmt:
                parsed = parsed.replace(year=datetime.now(TZ).year)
            return parsed.replace(tzinfo=TZ)
        except ValueError:
            continue
    try:
        parts = time_str.split(":")
        now = datetime.now(TZ)
        target = now.replace(hour=int(parts[0]), minute=int(parts[1]), second=0, microsecond=0)
        if target < now:
            target += timedelta(days=1)
        return target
    except Exception:
        return None


def build_event_embed(event: dict) -> discord.Embed:
    status   = event.get("status", "upcoming")
    start_dt = None
    end_dt   = None
    if event.get("start_time"):
        start_dt = datetime.fromisoformat(event["start_time"]).replace(tzinfo=TZ)
    if event.get("end_time"):
        end_dt = datetime.fromisoformat(event["end_time"]).replace(tzinfo=TZ)
    now = datetime.now(TZ)

    color_map = {
        "upcoming":  0x3498DB, "tba": 0x9B59B6,   "live":      0x2ECC71,
        "open_end":  0x1ABC9C, "delayed": 0xE67E22, "cancelled": 0xED4245, "ended": 0x95A5A6,
    }
    status_labels = {
        "upcoming":  "📅 Geplant", "tba": "❓ Datum noch unbekannt", "live": "🟢 Läuft gerade",
        "open_end":  "🟢 Läuft gerade (Ende unbekannt)", "delayed": "⏸️ Verschoben",
        "cancelled": "❌ Abgesagt", "ended": "✅ Beendet",
    }

    embed = discord.Embed(title=f"🎉 {event['title']}", description=event.get("description", ""),
                          color=color_map.get(status, 0x3498DB))
    embed.add_field(name="📊 Status",   value=status_labels.get(status, status), inline=True)
    embed.add_field(name="👥 Follower", value=str(len(event.get("followers") or [])), inline=True)
    embed.add_field(name="\u200b",      value="\u200b", inline=True)

    if status == "tba":
        embed.add_field(name="🕐 Start", value="❓ Wird bekannt gegeben", inline=True)
        embed.add_field(name="🏁 Ende",  value="❓ Wird bekannt gegeben", inline=True)
        embed.add_field(name="\u200b",   value="\u200b", inline=True)
    else:
        embed.add_field(name="🕐 Start",
                        value=f"<t:{int(start_dt.timestamp())}:F>" if start_dt else "❓ Unbekannt", inline=True)
        if end_dt:
            embed.add_field(name="🏁 Ende", value=f"<t:{int(end_dt.timestamp())}:F>", inline=True)
        elif status in ("live", "open_end"):
            embed.add_field(name="🏁 Ende", value="⏳ Ende nicht festgelegt", inline=True)
        else:
            embed.add_field(name="🏁 Ende", value="❓ Unbekannt", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)

        if status == "upcoming" and start_dt and start_dt > now:
            embed.add_field(name="⏳ Startet in", value=f"<t:{int(start_dt.timestamp())}:R>", inline=False)
        elif status == "live" and end_dt and end_dt > now:
            embed.add_field(name="⏱️ Endet in",  value=f"<t:{int(end_dt.timestamp())}:R>", inline=False)
        elif status == "open_end":
            embed.add_field(name="ℹ️ Hinweis", value="Läuft bis manuell beendet.", inline=False)
        elif status == "delayed":
            embed.add_field(name="ℹ️ Hinweis", value="Auf unbestimmte Zeit verschoben.", inline=False)
        elif status == "cancelled":
            embed.add_field(name="ℹ️ Hinweis", value="Dieses Event wurde abgesagt.", inline=False)

    embed.set_footer(text=f"Event-ID: {event.get('id', '?')}")
    return embed