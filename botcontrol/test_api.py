"""Тесты API управления: исходящие, конфигурация, здоровье."""

from django.test import override_settings
from rest_framework.test import APITestCase

from directory.models import Category, Contact, District
from tickets.models import Channel, Citizen

from .models import (
    BotAccount, BotSetting, Broadcast, Heartbeat, Outbox, ProviderKey, QuickAnswer,
)

TOKEN = "test-bot-token-123"
HDR = {"HTTP_X_BOT_TOKEN": TOKEN}


@override_settings(BOT_API_TOKEN=TOKEN)
class OutboxTests(APITestCase):
    def setUp(self):
        self.citizen = Citizen.objects.create(tg_user_id=42, chat_id=42)
        self.row = Outbox.objects.create(chat_id=42, text="Ответ отдела", kind="reply")

    def test_pending_listing_and_sent(self):
        items = self.client.get("/api/v1/outbox/?channel=telegram", **HDR).json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["text"], "Ответ отдела")

        self.client.post(f"/api/v1/outbox/{self.row.pk}/sent/", **HDR)
        self.row.refresh_from_db()
        self.assertEqual(self.row.status, "sent")
        self.assertIsNotNone(self.row.sent_at)
        self.assertEqual(
            self.client.get("/api/v1/outbox/?channel=telegram", **HDR).json()["items"], [])

    def test_outbox_requires_channel_param(self):
        response = self.client.get("/api/v1/outbox/", **HDR)
        self.assertEqual(response.status_code, 400)
        response = self.client.get("/api/v1/outbox/?channel=carrier-pigeon", **HDR)
        self.assertEqual(response.status_code, 400)

    def test_outbox_filters_by_channel(self):
        Outbox.objects.create(chat_id=996700123456, text="Здравствуйте",
                              kind="reply", channel=Channel.WHATSAPP)
        telegram_items = self.client.get(
            "/api/v1/outbox/?channel=telegram", **HDR).json()["items"]
        whatsapp_items = self.client.get(
            "/api/v1/outbox/?channel=whatsapp", **HDR).json()["items"]
        self.assertEqual([i["text"] for i in telegram_items], ["Ответ отдела"])
        self.assertEqual([i["text"] for i in whatsapp_items], ["Здравствуйте"])

    def test_failed_with_block_marks_citizen(self):
        self.client.post(f"/api/v1/outbox/{self.row.pk}/failed/",
                         {"error": "bot was blocked", "blocked": True}, **HDR)
        self.row.refresh_from_db()
        self.citizen.refresh_from_db()
        self.assertEqual(self.row.status, "failed")
        self.assertTrue(self.citizen.is_blocked)
        self.assertFalse(self.citizen.subscribed)

    def test_failed_block_does_not_touch_other_channel_same_chat_id(self):
        """chat_id=42 у Telegram-жителя и у WhatsApp-жителя — разные люди."""
        whatsapp_citizen = Citizen.objects.create(channel=Channel.WHATSAPP, chat_id=42)
        row = Outbox.objects.create(chat_id=42, text="x", kind="reply", channel=Channel.WHATSAPP)
        self.client.post(f"/api/v1/outbox/{row.pk}/failed/",
                         {"error": "unreachable", "blocked": True}, **HDR)
        self.citizen.refresh_from_db()
        whatsapp_citizen.refresh_from_db()
        self.assertFalse(self.citizen.is_blocked)
        self.assertTrue(whatsapp_citizen.is_blocked)

    def test_broadcast_counters_and_completion(self):
        b = Broadcast.objects.create(text="Отключение воды", status="sending", total=2)
        r1 = Outbox.objects.create(chat_id=1, text="x", kind="broadcast", broadcast=b)
        r2 = Outbox.objects.create(chat_id=2, text="x", kind="broadcast", broadcast=b)
        self.client.post(f"/api/v1/outbox/{r1.pk}/sent/", **HDR)
        b.refresh_from_db()
        self.assertEqual((b.sent, b.status), (1, "sending"))
        self.client.post(f"/api/v1/outbox/{r2.pk}/failed/", {"error": "x"}, **HDR)
        b.refresh_from_db()
        self.assertEqual((b.sent, b.failed, b.status), (1, 1, "done"))

    def test_subscription_toggle(self):
        self.client.post("/api/v1/citizens/subscription/",
                         {"chat_id": 42, "subscribed": False}, **HDR)
        self.citizen.refresh_from_db()
        self.assertFalse(self.citizen.subscribed)


@override_settings(BOT_API_TOKEN=TOKEN)
class ConfigTests(APITestCase):
    def test_version_short_circuit(self):
        v = BotSetting.get().version
        r = self.client.get(f"/api/v1/config/?version={v}", **HDR).json()
        self.assertEqual(r, {"changed": False, "version": v})

    def test_full_payload(self):
        key = ProviderKey(provider="openai", model="gpt-4o")
        key.set_secret("sk-proj-secret-xyz")
        key.save()
        bot = BotAccount(name="Манас Бот", username="it_ran_studio_bot", is_active=True)
        bot.set_secret("890:AAA-token")
        bot.save()
        QuickAnswer.objects.create(triggers="график работы", answer="с 8:30 до 17:30")
        Contact.objects.create(title="Приёмная мэрии", phone="5-00-00",
                               work_hours="пн-пт 8:30-17:30")
        Contact.objects.create(title="Скрытый", phone="000", is_public=False)

        r = self.client.get("/api/v1/config/?version=0", **HDR).json()
        self.assertTrue(r["changed"])
        self.assertTrue(r["enabled"])
        self.assertEqual(r["bot_token"], "890:AAA-token")
        self.assertEqual(r["providers"], [
            {"provider": "openai", "key": "sk-proj-secret-xyz", "model": "gpt-4o"}])
        self.assertEqual(r["quick_answers"][0]["triggers"], ["график работы"])
        self.assertIn("Приёмная мэрии", r["facts"])
        self.assertIn("5-00-00", r["facts"])
        self.assertNotIn("Скрытый", r["facts"])

    def test_config_includes_reference_lists(self):
        Category.objects.create(name="Дороги", slug="roads")
        Category.objects.create(name="Старое", slug="old", is_active=False)
        District.objects.create(name="Центр", slug="center")
        r = self.client.get("/api/v1/config/?version=0", **HDR).json()
        self.assertEqual(r["categories"], [{"slug": "roads", "name": "Дороги"}])
        self.assertEqual(r["districts"], [{"slug": "center", "name": "Центр"}])

    def test_related_changes_bump_version(self):
        v1 = self.client.get("/api/v1/config/?version=0", **HDR).json()["version"]
        QuickAnswer.objects.create(triggers="тест", answer="ответ")
        v2 = self.client.get("/api/v1/config/?version=0", **HDR).json()["version"]
        self.assertGreater(v2, v1)
        # и бот, сидящий на v1, получит полный ответ, а не «не изменилось»
        r = self.client.get(f"/api/v1/config/?version={v1}", **HDR).json()
        self.assertTrue(r["changed"])


@override_settings(BOT_API_TOKEN=TOKEN)
class HealthTests(APITestCase):
    def test_heartbeat_stored_and_pruned(self):
        for i in range(105):
            self.client.post("/api/v1/health/",
                             {"providers": {"openai": True}, "counters": {"n": i}},
                             format="json", **HDR)
        self.assertLessEqual(Heartbeat.objects.count(), 100)
        self.assertEqual(Heartbeat.objects.latest().counters["n"], 104)

    def test_answer_hit_counter(self):
        qa = QuickAnswer.objects.create(triggers="т", answer="о")
        self.client.post(f"/api/v1/answers/{qa.pk}/hit/", **HDR)
        self.client.post(f"/api/v1/answers/{qa.pk}/hit/", **HDR)
        qa.refresh_from_db()
        self.assertEqual(qa.hits, 2)
