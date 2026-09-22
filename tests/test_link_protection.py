import unittest
import sys
import types

# URL-policy tests do not need a database connection.  Keeping the Supabase
# integration out of this module also makes these tests runnable with only the
# bot's Discord dependency installed.
supabase_client = types.ModuleType("bot.core.supabase_client")
supabase_client.get_supabase = lambda: None
sys.modules["bot.core.supabase_client"] = supabase_client

logger_module = types.ModuleType("bot.utils.logger")
logger_module.get_logger = lambda _name: __import__("logging").getLogger(_name)
sys.modules["bot.utils.logger"] = logger_module

permissions_module = types.ModuleType("bot.utils.permissions")
permissions_module.has_admin_rights = lambda _interaction: True
sys.modules["bot.utils.permissions"] = permissions_module

from bot.features.moderation.link_protection import (
    URL_PATTERN,
    _extract_twitch_channel,
    _extract_youtube_channel,
    _is_allowed_url,
    _normalise_url,
)


class LinkProtectionUrlTests(unittest.TestCase):
    def test_normalises_case_idn_and_trailing_punctuation(self):
        self.assertEqual(_normalise_url("HTTPS://B\u00dcCHER.example./shop/!"), ("xn--bcher-kva.example", "/shop"))

    def test_rejects_non_web_and_malformed_urls(self):
        self.assertEqual(_normalise_url("javascript:alert(1)"), ("", ""))
        self.assertEqual(_normalise_url("https://example.com:99999"), ("", ""))

    def test_detects_bare_domains_but_not_email_addresses(self):
        content = "Besuche example.com/path und https://www.example.org. mail@example.net bleibt Text."
        self.assertEqual(URL_PATTERN.findall(content), ["example.com/path", "https://www.example.org."])

    def test_whitelist_respects_domain_boundaries_and_paths(self):
        allowed = [{"url": "example.com/safe", "channel_id": None, "user_id": None}]
        self.assertTrue(_is_allowed_url("https://cdn.example.com/safe/file", allowed, "1", "2"))
        self.assertFalse(_is_allowed_url("https://evil-example.com/safe", allowed, "1", "2"))
        self.assertFalse(_is_allowed_url("https://example.com/unsafe", allowed, "1", "2"))

    def test_platform_matches_require_the_real_platform_host(self):
        self.assertEqual(_extract_youtube_channel("https://youtube.com/@OpenAI"), "openai")
        self.assertIsNone(_extract_youtube_channel("https://evil.test/youtube.com/@OpenAI"))
        self.assertEqual(_extract_twitch_channel("https://twitch.tv/OpenAI"), "openai")
        self.assertIsNone(_extract_twitch_channel("https://evil.test/twitch.tv/OpenAI"))


if __name__ == "__main__":
    unittest.main()
