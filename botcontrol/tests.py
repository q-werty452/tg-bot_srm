"""Тесты управления ботом: шифрование, настройки, готовые ответы."""

from django.test import TestCase

from botcontrol.crypto import decrypt, encrypt
from botcontrol.models import BotAccount, BotSetting, ProviderKey, QuickAnswer


class CryptoTests(TestCase):
    def test_roundtrip(self):
        secret = "sk-proj-very-secret-key-1234"
        self.assertEqual(decrypt(encrypt(secret)), secret)

    def test_ciphertext_differs_from_plaintext(self):
        secret = "AIzaSyExample"
        token = encrypt(secret)
        self.assertNotIn(secret, token)


class ProviderKeyTests(TestCase):
    def test_secret_stored_encrypted_and_masked(self):
        key = ProviderKey(provider="openai", model="gpt-4o")
        key.set_secret("sk-proj-abcdef123456")
        key.save()
        key.refresh_from_db()
        self.assertEqual(key.get_secret(), "sk-proj-abcdef123456")
        self.assertEqual(key.tail4, "3456")
        self.assertEqual(key.masked, "…3456")
        self.assertNotIn("sk-proj", key.secret_encrypted)

    def test_str_never_leaks_secret(self):
        key = ProviderKey(provider="gemini")
        key.set_secret("AIzaSecretValue9999")
        key.save()
        self.assertNotIn("AIzaSecretValue", str(key))


class BotAccountTests(TestCase):
    def test_single_active_bot(self):
        a = BotAccount(name="Основной"); a.set_secret("111:AAA"); a.is_active = True; a.save()
        b = BotAccount(name="Резервный"); b.set_secret("222:BBB"); b.is_active = True; b.save()
        a.refresh_from_db()
        self.assertFalse(a.is_active, "активным должен остаться только последний включённый")
        self.assertTrue(BotAccount.objects.get(pk=b.pk).is_active)


class BotSettingTests(TestCase):
    def test_singleton_and_version_bump(self):
        s1 = BotSetting.get()
        v1 = s1.version
        s1.enabled = False
        s1.save()
        s2 = BotSetting.get()
        self.assertEqual(s2.pk, 1)
        self.assertEqual(BotSetting.objects.count(), 1)
        self.assertGreater(s2.version, v1)

    def test_bump_version_helper(self):
        v = BotSetting.get().version
        BotSetting.bump_version()
        self.assertGreater(BotSetting.get().version, v)


class QuickAnswerTests(TestCase):
    def test_trigger_list_parsing(self):
        qa = QuickAnswer.objects.create(
            triggers="График работы\n  во сколько работает  \n\nиш убактысы",
            answer="Мэрия работает с 8:30 до 17:30.",
        )
        self.assertEqual(
            qa.trigger_list(),
            ["график работы", "во сколько работает", "иш убактысы"],
        )
