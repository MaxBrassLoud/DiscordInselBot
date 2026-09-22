from __future__ import annotations

import os


def _is_mbl(user_id: int) -> bool:
    mbl_ids = {uid.strip() for uid in os.getenv("MBL", "").split(",") if uid.strip()}
    return str(user_id) in mbl_ids


CONFIG = {
    # Bestehende Werte
    "total_mention_threshold": 6,
    "message_time_window": 60,
    "new_member_days": 2,
    "timeout_hours": 168,
    "log_channel_override": None,

    # Duplikaterkennung
    "text_duplicate_threshold": 8,
    "image_duplicate_threshold": 3,
    "image_similarity_threshold": 5,
    "duplicate_time_window": 60,

    # Datei mit verbotenen Bildern (SHA1 -> phash)
    "forbidden_images_path": os.getenv(
        "RAID_FORBIDDEN_IMAGES_PATH", "data/forbidden_images.json"
    ),
}


RAID_ACTIONS = {
    "approve": {
        "label": "Nachrichten waren zulaessig",
        "confirm_label": "Ja, Timeout aufheben",
        "style": 3,  # discord.ButtonStyle.success
        "color": "green",
        "done": "Fall abgeschlossen: Timeout aufgehoben.",
        "feedback": "Timeout aufgehoben.",
        "dm_title": "Entschuldigung - Timeout aufgehoben",
        "dm_description": "Deine Nachrichten wurden von einem Moderator als zulaessig eingestuft. Der Timeout wurde aufgehoben.",
        "required_perm": "moderate_members",
    },
    "warn_release": {
        "label": "Unzulaessig - Timeout aufheben",
        "confirm_label": "Ja, verwarnen",
        "style": 1,  # primary
        "color": "gold",
        "done": "Fall abgeschlossen: Verwarnung, Timeout aufgehoben.",
        "feedback": "Verwarnung gesetzt und Timeout aufgehoben.",
        "dm_title": "Verwarnung - Timeout aufgehoben",
        "dm_description": "Deine Nachrichten wurden als unzulaessig eingestuft. Du erhaeltst eine Verwarnung, der Timeout wurde aufgehoben.",
        "required_perm": "moderate_members",
    },
    "keep_timeout": {
        "label": "Unzulaessig - Timeout behalten",
        "confirm_label": "Ja, Timeout behalten",
        "style": 2,  # secondary
        "color": "red",
        "done": "Fall abgeschlossen: Timeout bleibt bestehen.",
        "feedback": "Timeout bleibt bestehen.",
        "dm_title": "Timeout bleibt bestehen",
        "dm_description": "Deine Nachrichten wurden als unzulaessig eingestuft. Der Timeout bleibt bestehen.",
        "required_perm": "moderate_members",
    },
    "ban": {
        "label": "Unzulaessig - User bannen",
        "confirm_label": "Ja, User bannen",
        "style": 4,  # danger
        "color": "dark_red",
        "done": "Fall abgeschlossen: User gebannt.",
        "feedback": "User gebannt.",
        "dm_title": "Du wurdest gebannt",
        "dm_description": "Deine Nachrichten wurden als schwerwiegend unzulaessig eingestuft. Du wurdest vom Server verbannt.",
        "required_perm": "ban_members",
    },
}

# Diese Aktionen bedeuten "Nicht erlaubt" -> Review-Trigger fuer verbotene Bilder
NOT_ALLOWED_ACTIONS = {"warn_release", "keep_timeout", "ban"}