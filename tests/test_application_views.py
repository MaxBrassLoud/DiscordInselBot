"""Regressionstests für das Bewerbungs-System.

Hintergrund: Nach einem Neustart waren die Buttons auf den bestehenden
Control-Nachrichten teilweise ohne Funktion, weil

  * nur Bewerbungen mit status="open" wiederhergestellt wurden – der
    "🔒 Ticket schließen"-Button angenommener Bewerbungen ("accepted") also
    nie registriert wurde,
  * claimed_by erst NACH dem Aufbau der View gesetzt wurde (registriert wurde
    "Übernehmen" statt "Abgeben"),
  * eine einzige fehlerhafte Bewerbung die gesamte Wiederherstellung abbrach.

Diese Tests laufen ohne Datenbank und ohne Discord-Verbindung.
"""

import sys
import types
import unittest
from unittest import mock

import discord

# Datenbank-/Discord-Integration wird nicht benötigt.
supabase_client = types.ModuleType("bot.core.supabase_client")
supabase_client.get_supabase = lambda: None
sys.modules["bot.core.supabase_client"] = supabase_client

logger_module = types.ModuleType("bot.utils.logger")
logger_module.get_logger = lambda _name: __import__("logging").getLogger(_name)
sys.modules["bot.utils.logger"] = logger_module

permissions_module = types.ModuleType("bot.utils.permissions")
permissions_module.has_admin_rights = lambda _interaction: True
sys.modules["bot.utils.permissions"] = permissions_module

from bot.features.applications import cog as app_cog
from bot.features.applications import views as app_views
from bot.features.applications.views import ApplicationChannelView, CloseConfirmView


def _custom_ids(view):
    return [child.custom_id for child in view.children]


def _member(administrator=False, role_ids=(), manage_guild=False, user_id=777):
    return types.SimpleNamespace(
        id=user_id,
        mention=f"<@{user_id}>",
        guild_permissions=types.SimpleNamespace(administrator=administrator,
                                                manage_guild=manage_guild),
        roles=[types.SimpleNamespace(id=rid) for rid in role_ids],
    )


class ApplicationChannelViewTests(unittest.IsolatedAsyncioTestCase):
    def _view(self, **kwargs):
        kwargs.setdefault("app_id", 7)
        kwargs.setdefault("server_id", "111")
        kwargs.setdefault("applicant_id", "42")
        kwargs.setdefault("cfg", {"staff_role_ids": "5"})
        kwargs.setdefault("bot", None)
        return ApplicationChannelView(**kwargs)

    async def test_open_application_registers_claim_accept_reject(self):
        self.assertEqual(
            _custom_ids(self._view(status="open")),
            ["app_claim_7", "app_accept_7", "app_reject_7"],
        )

    async def test_claimed_application_registers_unclaim(self):
        """claimed_by muss vor dem Aufbau der Buttons bekannt sein."""
        ids = _custom_ids(self._view(status="open", claimed_by="99"))
        self.assertIn("app_unclaim_7", ids)
        self.assertNotIn("app_claim_7", ids)

    async def test_accepted_application_registers_close(self):
        self.assertEqual(_custom_ids(self._view(status="accepted")), ["app_close_7"])

    async def test_tolerant_view_registers_every_possible_custom_id(self):
        for status, claimed_by in (("open", None), ("open", "99"), ("accepted", None)):
            with self.subTest(status=status, claimed_by=claimed_by):
                ids = _custom_ids(self._view(status=status, claimed_by=claimed_by, tolerant=True))
                self.assertEqual(
                    set(ids),
                    {
                        "app_claim_7", "app_unclaim_7", "app_accept_7",
                        "app_reject_7", "app_close_7",
                    },
                )

    async def test_staff_check_uses_given_config(self):
        view = self._view(cfg={"staff_role_ids": "5,6"})
        self.assertTrue(view._is_staff(_member(administrator=True), {"staff_role_ids": ""}))
        self.assertTrue(view._is_staff(_member(role_ids=[6]), {"staff_role_ids": "5,6"}))
        self.assertFalse(view._is_staff(_member(role_ids=[6]), {"staff_role_ids": "5"}))
        self.assertFalse(view._is_staff(_member(role_ids=[6]), {}))

    async def test_manage_guild_counts_as_staff(self):
        member = types.SimpleNamespace(
            guild_permissions=types.SimpleNamespace(administrator=False, manage_guild=True),
            roles=[],
        )
        self.assertTrue(ApplicationChannelView._is_staff_member(member, {}))


# ── Fakes für Supabase / Discord ──────────────────────────────────────────────

class _FakeQuery:
    def __init__(self, rows):
        self._rows = [dict(row) for row in rows]

    def select(self, *args, **kwargs):
        return self

    def eq(self, field, value):
        self._rows = [r for r in self._rows if r.get(field) == value]
        return self

    def in_(self, field, values):
        self._rows = [r for r in self._rows if r.get(field) in list(values)]
        return self

    def limit(self, *args, **kwargs):
        return self

    def execute(self):
        return types.SimpleNamespace(data=self._rows)


class _FakeSupabase:
    def __init__(self, apps, servers=()):
        self._tables = {"applications": list(apps), "application_servers": list(servers)}

    def table(self, name):
        return _FakeQuery(self._tables.get(name, []))


class _FakeBot:
    def __init__(self, channels=None):
        self.user = types.SimpleNamespace(id=999)
        self.views = []
        self.channels = channels or {}

    def add_view(self, view, **kwargs):
        self.views.append(view)

    def get_channel(self, channel_id):
        return self.channels.get(channel_id)


class _FakeChild:
    def __init__(self, custom_id):
        self.custom_id = custom_id


class _FakeMessage:
    def __init__(self, message_id, custom_ids, author_id=999):
        self.id = message_id
        self.author = types.SimpleNamespace(id=author_id)
        self.components = [types.SimpleNamespace(children=[_FakeChild(c) for c in custom_ids])]
        self.edits = []

    async def edit(self, **kwargs):
        self.edits.append(kwargs)


class _FakeChannel:
    def __init__(self, channel_id, messages):
        self.id = channel_id
        self._messages = messages

    async def fetch_message(self, message_id):
        for message in self._messages:
            if message.id == message_id:
                return message
        raise RuntimeError(f"unbekannte Nachricht {message_id}")

    def history(self, **kwargs):
        messages = list(self._messages)

        class _History:
            def __aiter__(self):
                self._iter = iter(messages)
                return self

            async def __anext__(self):
                try:
                    return next(self._iter)
                except StopIteration:
                    raise StopAsyncIteration

        return _History()


class RestoreChannelViewsTests(unittest.IsolatedAsyncioTestCase):
    """_restore_channel_views muss ALLE laufenden Bewerbungen abdecken."""

    def setUp(self):
        self.apps = [
            {"server_id": "1", "app_id": 1, "status": "accepted", "creator_id": "u1",
             "channel_id": None, "claimed_by": None, "control_message_id": None},
            {"server_id": "1", "app_id": 2, "status": "open", "creator_id": "u2",
             "channel_id": None, "claimed_by": "99", "control_message_id": None},
            {"server_id": "1", "app_id": 3, "status": "rejected", "creator_id": "u3",
             "channel_id": None, "claimed_by": None, "control_message_id": None},
        ]
        self.servers = [{"server_id": "1", "staff_role_ids": "5"}]
        self.fake_sb = _FakeSupabase(self.apps, self.servers)
        supabase_client.get_supabase = lambda: self.fake_sb
        app_views._CFG_CACHE.clear()
        self.bot = _FakeBot()
        self.cog = app_cog.ApplicationsCog(self.bot)

    def _patches(self):
        return (
            mock.patch.object(
                app_cog, "load_application",
                side_effect=lambda sid, aid: next(
                    (dict(a) for a in self.apps if a["server_id"] == sid and a["app_id"] == aid), None
                ),
            ),
            mock.patch.object(app_cog, "update_application", lambda *a, **k: None),
            mock.patch.object(
                app_views.ApplicationManager, "get_server_config",
                side_effect=lambda sid: next(
                    (dict(s) for s in self.servers if s["server_id"] == sid), {}
                ),
            ),
        )

    async def test_accepted_and_open_are_restored(self):
        load_patch, update_patch, cfg_patch = self._patches()
        with load_patch, update_patch, cfg_patch:
            await self.cog._restore_channel_views()

        registered = {cid for view in self.bot.views for cid in _custom_ids(view)}
        # Angenommene Bewerbung: Schließen-Button muss wieder funktionieren
        self.assertIn("app_close_1", registered)
        # Offene, bereits übernommene Bewerbung: Abgeben + Annehmen/Ablehnen
        self.assertIn("app_unclaim_2", registered)
        self.assertIn("app_accept_2", registered)
        self.assertIn("app_reject_2", registered)
        # Abgelehnte Bewerbungen werden nicht registriert
        self.assertNotIn("app_close_3", registered)
        self.assertEqual(len(self.bot.views), 2)

    async def test_broken_application_does_not_stop_restore(self):
        load_patch, update_patch, cfg_patch = self._patches()

        def boom(sid, aid):
            if aid == 1:
                raise RuntimeError("kaputt")
            return next(a for a in self.apps if a["app_id"] == aid)

        with mock.patch.object(app_cog, "load_application", side_effect=boom), update_patch, cfg_patch:
            await self.cog._restore_channel_views()

        registered = {cid for view in self.bot.views for cid in _custom_ids(view)}
        self.assertIn("app_accept_2", registered)


class SyncControlMessageTests(unittest.IsolatedAsyncioTestCase):
    """Veraltete Buttons auf der Control-Nachricht werden korrigiert."""

    def setUp(self):
        self.message = _FakeMessage(
            message_id=555,
            custom_ids=["app_claim_4", "app_accept_4", "app_reject_4"],
        )
        self.channel = _FakeChannel(channel_id=777, messages=[self.message])
        self.bot = _FakeBot({777: self.channel})
        self.cog = app_cog.ApplicationsCog(self.bot)

    async def test_accepted_application_gets_close_button(self):
        app = {"server_id": "1", "app_id": 4, "status": "accepted", "creator_id": "u4",
               "channel_id": "777", "claimed_by": None, "control_message_id": None}
        writes = []
        with mock.patch.object(app_cog, "update_application",
                               lambda sid, aid, fields: writes.append((sid, aid, fields))):
            await self.cog._sync_control_message("1", 4, app, {"staff_role_ids": "5"})

        self.assertEqual(len(self.message.edits), 1)
        self.assertEqual(_custom_ids(self.message.edits[0]["view"]), ["app_close_4"])
        self.assertEqual(writes, [("1", 4, {"control_message_id": "555"})])

    async def test_correct_message_is_not_touched(self):
        accepted = _FakeMessage(message_id=556, custom_ids=["app_close_4"])
        self.channel._messages = [self.message, accepted]
        app = {"server_id": "1", "app_id": 4, "status": "accepted", "creator_id": "u4",
               "channel_id": "777", "claimed_by": None, "control_message_id": "556"}
        with mock.patch.object(app_cog, "update_application", lambda *a, **k: None):
            await self.cog._sync_control_message("1", 4, app, {})

        self.assertEqual(self.message.edits, [])
        self.assertEqual(accepted.edits, [])

    async def test_open_claimed_application_gets_unclaim_button(self):
        message = _FakeMessage(message_id=557, custom_ids=["app_claim_5", "app_accept_5"])
        self.channel._messages = [message]
        app = {"server_id": "1", "app_id": 5, "status": "open", "creator_id": "u5",
               "channel_id": "777", "claimed_by": "99", "control_message_id": "557"}
        with mock.patch.object(app_cog, "update_application", lambda *a, **k: None):
            await self.cog._sync_control_message("1", 5, app, {})

        self.assertEqual(len(message.edits), 1)
        ids = _custom_ids(message.edits[0]["view"])
        self.assertIn("app_unclaim_5", ids)
        self.assertNotIn("app_claim_5", ids)


# ── Interaktions-Verhalten (10062 "Unknown interaction" verhindern) ───────────

class _FakeResponse:
    def __init__(self):
        self.deferred = []
        self.messages = []
        self.modals = []
        self.done = False

    async def defer(self, **kwargs):
        self.deferred.append(kwargs)
        self.done = True

    async def send_message(self, *args, **kwargs):
        self.messages.append((args, kwargs))
        self.done = True

    async def send_modal(self, modal, **kwargs):
        self.modals.append(modal)
        self.done = True

    def is_done(self):
        return self.done


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, *args, **kwargs):
        self.sent.append((args, kwargs))


class _FakeInteraction:
    def __init__(self, user, guild_id="111", message=None, channel=None, guild=None):
        self.user = user
        self.guild_id = guild_id
        self.guild = guild
        self.message = message
        self.channel = channel
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


class InteractionTimingTests(unittest.IsolatedAsyncioTestCase):
    """Die Interaktion muss VOR langsamer Arbeit bestätigt werden."""

    def setUp(self):
        app_views._CFG_CACHE.clear()
        self.non_staff = _member(role_ids=[999])

    def _view(self, **kwargs):
        kwargs.setdefault("app_id", 7)
        kwargs.setdefault("server_id", "111")
        kwargs.setdefault("applicant_id", "42")
        kwargs.setdefault("cfg", {"staff_role_ids": "5"})
        kwargs.setdefault("bot", None)
        return ApplicationChannelView(**kwargs)

    async def test_close_denies_after_defer_and_uses_followup(self):
        view = self._view(status="accepted")
        interaction = _FakeInteraction(self.non_staff, guild_id="111")

        await view._close(interaction)

        # 1. Sofort bestätigen (sonst 404/10062 bei langsamem DB-Zugriff)
        self.assertEqual(len(interaction.response.deferred), 1)
        # 2. Ablehnung als followup – nicht als (zu späte) Erstantwort
        self.assertEqual(interaction.response.messages, [])
        self.assertEqual(len(interaction.followup.sent), 1)
        self.assertIn("Nur Staff", interaction.followup.sent[0][0][0])

    async def test_close_sends_confirm_view_for_staff(self):
        view = self._view(status="accepted")
        interaction = _FakeInteraction(_member(role_ids=[5]), guild_id="111")

        await view._close(interaction)

        self.assertEqual(len(interaction.response.deferred), 1)
        self.assertEqual(len(interaction.followup.sent), 1)
        view_kwargs = interaction.followup.sent[0][1]
        self.assertIsInstance(view_kwargs["view"], CloseConfirmView)

    async def test_claim_defers_before_db_write(self):
        view = self._view(status="open")
        message = mock.AsyncMock()
        interaction = _FakeInteraction(_member(role_ids=[5]), guild_id="111",
                                       message=message, channel=mock.AsyncMock())

        with mock.patch("bot.features.applications.manager.update_application") as upd:
            await view._claim(interaction)

        self.assertEqual(len(interaction.response.deferred), 1)
        upd.assert_called_once()
        message.edit.assert_awaited()

    async def test_expired_interaction_is_swallowed(self):
        """Der gemeldete 10062-Fall darf keinen Traceback mehr erzeugen."""
        view = self._view(status="accepted")
        interaction = _FakeInteraction(_member(role_ids=[5]), guild_id="111")
        expired = discord.errors.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"),
            {"code": 10062, "message": "Unknown interaction"},
        )
        interaction.response.defer = mock.AsyncMock(side_effect=expired)

        await view._close(interaction)   # darf NICHT werfen

        self.assertEqual(interaction.response.messages, [])
        self.assertEqual(interaction.followup.sent, [])

    async def test_reject_sends_modal_without_db_read(self):
        """Ein Modal muss die Erstantwort sein – also kein DB-Zugriff davor."""
        view = self._view(status="open")
        interaction = _FakeInteraction(_member(role_ids=[5]), guild_id="111")

        with mock.patch.object(app_cog, "load_application",
                               side_effect=AssertionError("kein DB-Zugriff vor dem Modal")):
            await view._reject(interaction)

        self.assertEqual(len(interaction.response.modals), 1)
        self.assertEqual(interaction.response.messages, [])


class ServerContextTests(unittest.IsolatedAsyncioTestCase):
    """Config-Auflösung darf legitime Staffs nicht aussperren."""

    def setUp(self):
        app_views._CFG_CACHE.clear()

    async def test_cached_config_avoids_repeated_db_calls(self):
        calls = []

        async def fake_get(server_id):
            calls.append(server_id)
            return {"staff_role_ids": "5"}

        with mock.patch.object(app_views.ApplicationManager, "get_server_config",
                               side_effect=fake_get):
            first = await app_views.get_server_config_cached("111")
            second = await app_views.get_server_config_cached("111")

        self.assertEqual(calls, ["111"])
        self.assertEqual(first, second)

    async def test_db_error_keeps_known_config(self):
        """Bei DB-Fehler darf NICHT auf {} zurückgefallen werden (sonst 'Nur Staff')."""
        view = ApplicationChannelView(
            app_id=7, server_id="111", applicant_id="42",
            cfg={"staff_role_ids": "5"}, bot=None, status="accepted",
        )
        interaction = _FakeInteraction(_member(role_ids=[5]), guild_id="222")

        with mock.patch.object(app_views.ApplicationManager, "get_server_config",
                               side_effect=RuntimeError("DB offline")):
            server_id, cfg = await view._resolve(interaction)

        self.assertEqual(server_id, "222")
        self.assertEqual(cfg, {"staff_role_ids": "5"})
        self.assertTrue(view._is_staff(interaction.user, cfg))


if __name__ == "__main__":
    unittest.main()
