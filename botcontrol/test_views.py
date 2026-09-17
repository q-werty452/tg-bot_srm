"""Тесты страниц управления ботом: доступ, настройки, ключи."""

from datetime import timedelta
from unittest.mock import patch

import httpx
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from .models import BotAccount, BotSetting, Broadcast, Outbox, ProviderKey, QuickAnswer

User = get_user_model()


class BotPagesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email="adm@meriya.kg",
                                             password="pass12345", role="admin")
        cls.operator = User.objects.create_user(email="op@meriya.kg",
                                                password="pass12345", role="operator")

    def as_admin(self):
        self.client.login(email="adm@meriya.kg", password="pass12345")

    def as_operator(self):
        self.client.login(email="op@meriya.kg", password="pass12345")


class AccessTests(BotPagesTestCase):
    def test_operator_cannot_open_admin_sections(self):
        self.as_operator()
        for url in ("/bot/settings/", "/bot/keys/"):
            response = self.client.get(url)
            self.assertRedirects(response, "/", msg_prefix=url)

    def test_operator_cannot_post_key(self):
        self.as_operator()
        self.client.post("/bot/keys/", {"action": "save_key",
                                        "provider": "openai", "key": "sk-hack"})
        self.assertFalse(ProviderKey.objects.exists())

    def test_admin_opens_sections(self):
        self.as_admin()
        self.assertEqual(self.client.get("/bot/settings/").status_code, 200)
        self.assertEqual(self.client.get("/bot/keys/").status_code, 200)


class SettingsTests(BotPagesTestCase):
    def test_save_settings_bumps_version(self):
        self.as_admin()
        v1 = BotSetting.get().version
        self.client.post("/bot/settings/", {
            "enabled": "", "invent_facts": "on",
            "maintenance_text": "Технические работы до 15:00",
            "prompt_detailed": "", "prompt_chat": "",
        })
        setting = BotSetting.get()
        self.assertFalse(setting.enabled)
        self.assertTrue(setting.invent_facts)
        self.assertIn("15:00", setting.maintenance_text)
        self.assertGreater(setting.version, v1)

    def test_audit_written(self):
        self.as_admin()
        self.client.post("/bot/settings/", {"enabled": "on",
                                            "maintenance_text": "x"})
        from audit.models import Entry
        self.assertTrue(Entry.objects.filter(action="настройки бота изменены").exists())


class KeysTests(BotPagesTestCase):
    def test_save_key_encrypted_and_masked_in_page(self):
        self.as_admin()
        self.client.post("/bot/keys/", {"action": "save_key",
                                        "provider": "openai",
                                        "key": "sk-proj-verysecret-abcd"})
        row = ProviderKey.objects.get(provider="openai")
        self.assertEqual(row.get_secret(), "sk-proj-verysecret-abcd")
        response = self.client.get("/bot/keys/")
        self.assertNotContains(response, "verysecret")
        self.assertContains(response, "…abcd")

    def test_check_success_offers_models(self):
        self.as_admin()
        row = ProviderKey(provider="openai"); row.set_secret("sk-x"); row.save()

        fake = httpx.Response(
            200, json={"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"},
                                {"id": "whisper-1"}, {"id": "gpt-4o-audio-preview"}]},
            request=httpx.Request("GET", "https://api.openai.com/v1/models"))
        with patch("botcontrol.views.httpx.get", return_value=fake):
            response = self.client.post("/bot/keys/", {"action": "check",
                                                       "provider": "openai"},
                                        follow=True)
        row.refresh_from_db()
        self.assertTrue(row.last_check_ok)
        self.assertContains(response, "gpt-4o-mini")
        self.assertNotContains(response, "whisper-1")

    def test_check_bad_key(self):
        self.as_admin()
        row = ProviderKey(provider="gemini"); row.set_secret("AIza-bad"); row.save()
        request_obj = httpx.Request("GET", "https://example")
        fake = httpx.Response(403, json={}, request=request_obj)
        with patch("botcontrol.views.httpx.get", return_value=fake):
            # raise_for_status бросит HTTPStatusError сам
            response = self.client.post("/bot/keys/", {"action": "check",
                                                       "provider": "gemini"},
                                        follow=True)
        row.refresh_from_db()
        self.assertFalse(row.last_check_ok)
        self.assertContains(response, "ключ отклонён")

    def test_check_network_down(self):
        self.as_admin()
        row = ProviderKey(provider="openai"); row.set_secret("sk-x"); row.save()
        with patch("botcontrol.views.httpx.get",
                   side_effect=httpx.ConnectError("no network")):
            response = self.client.post("/bot/keys/", {"action": "check",
                                                       "provider": "openai"},
                                        follow=True)
        self.assertContains(response, "нет связи")

    def test_set_model_and_default_provider(self):
        self.as_admin()
        row = ProviderKey(provider="openai"); row.set_secret("sk-x"); row.save()
        # Модель сохраняется только после проверки диалогом — её подменяем,
        # чтобы тест не ходил в сеть (сама проверка покрыта в test_keydetect).
        with patch("botcontrol.views.smoke_test_model", return_value=""):
            self.client.post("/bot/keys/", {"action": "set_model",
                                            "provider": "openai",
                                            "model": "gpt-4o-mini"})
        row.refresh_from_db()
        self.assertEqual(row.model, "gpt-4o-mini")

        self.client.post("/bot/keys/", {"action": "default_provider",
                                        "default_provider": "gemini"})
        self.assertEqual(BotSetting.get().default_provider, "gemini")

    def test_add_and_activate_bot(self):
        self.as_admin()
        self.client.post("/bot/keys/", {"action": "add_bot",
                                        "name": "Основной", "token": "111:AAA"})
        first = BotAccount.objects.get()
        self.assertTrue(first.is_active, "первый добавленный бот сразу активен")

        self.client.post("/bot/keys/", {"action": "add_bot",
                                        "name": "Резервный", "token": "222:BBB"})
        second = BotAccount.objects.get(name="Резервный")
        self.assertFalse(second.is_active)

        self.client.post("/bot/keys/", {"action": "activate_bot",
                                        "bot_id": second.pk})
        first.refresh_from_db(); second.refresh_from_db()
        self.assertTrue(second.is_active)
        self.assertFalse(first.is_active)

    def test_bad_token_rejected(self):
        self.as_admin()
        self.client.post("/bot/keys/", {"action": "add_bot",
                                        "name": "Кривой", "token": "без двоеточия"})
        self.assertFalse(BotAccount.objects.exists())


class QuickAnswersPageTests(BotPagesTestCase):
    def test_create_edit_delete(self):
        self.as_admin()
        self.client.post("/bot/answers/", {
            "action": "save", "triggers": "график работы\nво сколько",
            "answer": "С 8:30 до 17:30."})
        qa = QuickAnswer.objects.get()
        self.assertTrue(qa.is_active)

        self.client.post("/bot/answers/", {
            "action": "save", "id": qa.pk,
            "triggers": "график", "answer": "Новый текст", "is_active": ""})
        qa.refresh_from_db()
        self.assertEqual(qa.answer, "Новый текст")
        self.assertFalse(qa.is_active)

        self.client.post("/bot/answers/", {"action": "delete", "id": qa.pk})
        self.assertFalse(QuickAnswer.objects.exists())

    def test_empty_fields_rejected(self):
        self.as_admin()
        self.client.post("/bot/answers/", {"action": "save",
                                           "triggers": "", "answer": "х"})
        self.assertFalse(QuickAnswer.objects.exists())


class BroadcastPageTests(BotPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from directory.models import Category, District
        from tickets.models import Citizen, Ticket
        cls.district = District.objects.create(name="Центр", slug="center")
        cls.category = Category.objects.create(name="Дороги", slug="roads")
        cls.c1 = Citizen.objects.create(tg_user_id=1, chat_id=1,
                                        district=cls.district)
        cls.c2 = Citizen.objects.create(tg_user_id=2, chat_id=2)
        cls.blocked = Citizen.objects.create(tg_user_id=3, chat_id=3,
                                             is_blocked=True)
        cls.unsub = Citizen.objects.create(tg_user_id=4, chat_id=4,
                                           subscribed=False)
        cls.manual = Citizen.objects.create(tg_user_id=-1, chat_id=-1)
        Ticket.objects.create(citizen=cls.c2, category=cls.category)

    def test_broadcast_all_skips_blocked_unsubscribed_manual(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Отключение воды",
                                          "audience": "all"})
        broadcast = Broadcast.objects.get()
        self.assertEqual(broadcast.total, 2)
        chats = set(Outbox.objects.values_list("chat_id", flat=True))
        self.assertEqual(chats, {1, 2})
        self.assertEqual(broadcast.status, Broadcast.Status.SENDING)

    def test_broadcast_by_district(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Субботник", "audience": "district",
                                          "district": self.district.pk})
        self.assertEqual(set(Outbox.objects.values_list("chat_id", flat=True)), {1})

    def test_broadcast_by_category(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Ремонт дорог", "audience": "category",
                                          "category": self.category.pk})
        self.assertEqual(set(Outbox.objects.values_list("chat_id", flat=True)), {2})

    def test_empty_audience_finishes_immediately(self):
        self.as_admin()
        from tickets.models import Citizen
        Citizen.objects.filter(tg_user_id__gt=0).update(subscribed=False)
        self.client.post("/broadcasts/", {"text": "Никому", "audience": "all"})
        self.assertEqual(Broadcast.objects.get().status, Broadcast.Status.DONE)
        self.assertFalse(Outbox.objects.exists())

    def test_operator_cannot_broadcast(self):
        self.as_operator()
        self.client.post("/broadcasts/", {"text": "х", "audience": "all"})

    def test_broadcast_sets_channel_on_outbox_rows(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Отключение воды", "audience": "all"})
        self.assertEqual(
            set(Outbox.objects.values_list("channel", flat=True)), {"telegram"})


class BroadcastWhatsAppWindowTests(BotPagesTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        from tickets.models import Channel, Citizen
        cls.Channel = Channel
        cls.telegram_stale = Citizen.objects.create(
            tg_user_id=10, chat_id=10,
            last_inbound_at=timezone.now() - timedelta(hours=100))
        cls.whatsapp_fresh = Citizen.objects.create(
            channel=Channel.WHATSAPP, chat_id=996700111111,
            last_inbound_at=timezone.now())
        cls.whatsapp_stale = Citizen.objects.create(
            channel=Channel.WHATSAPP, chat_id=996700222222,
            last_inbound_at=timezone.now() - timedelta(hours=30))
        cls.whatsapp_never = Citizen.objects.create(
            channel=Channel.WHATSAPP, chat_id=996700333333)

    def test_broadcast_excludes_whatsapp_outside_window_includes_inside(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Рассылка", "audience": "all"})
        chats = set(Outbox.objects.values_list("chat_id", flat=True))
        self.assertIn(self.whatsapp_fresh.chat_id, chats)
        self.assertNotIn(self.whatsapp_stale.chat_id, chats)
        self.assertNotIn(self.whatsapp_never.chat_id, chats)

    def test_broadcast_never_excludes_telegram_by_window(self):
        self.as_admin()
        self.client.post("/broadcasts/", {"text": "Рассылка", "audience": "all"})
        chats = set(Outbox.objects.values_list("chat_id", flat=True))
        self.assertIn(self.telegram_stale.chat_id, chats)

    def test_broadcast_page_shows_excluded_count(self):
        self.as_admin()
        response = self.client.get("/broadcasts/")
        self.assertEqual(response.context["whatsapp_excluded"], 2)  # stale + never
        self.assertFalse(Broadcast.objects.exists())
