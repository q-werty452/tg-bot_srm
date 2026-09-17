"""Тесты API бота: приём обращений, классификация, история, оценки."""

import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from directory.models import Category, District
from tickets.models import Attachment, Channel, Citizen, Event, Message, Ticket

TOKEN = "test-bot-token-123"
HDR = {"HTTP_X_BOT_TOKEN": TOKEN}
MEDIA = tempfile.mkdtemp(prefix="manas-test-media-")


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class ApiAuthTests(APITestCase):
    def test_no_token_rejected(self):
        r = self.client.post("/api/v1/tickets/incoming/", {"tg_user_id": 1, "text": "hi"})
        self.assertEqual(r.status_code, 403)

    def test_wrong_token_rejected(self):
        r = self.client.post("/api/v1/tickets/incoming/",
                             {"tg_user_id": 1, "text": "hi"},
                             HTTP_X_BOT_TOKEN="wrong")
        self.assertEqual(r.status_code, 403)


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class IncomingTests(APITestCase):
    def incoming(self, **extra):
        data = {"tg_user_id": 500, "chat_id": 500, "first_name": "Айбек",
                "text": "Не вывозят мусор на Токтогула 12"}
        data.update(extra)
        return self.client.post("/api/v1/tickets/incoming/", data, **HDR)

    def test_creates_citizen_ticket_message(self):
        r = self.incoming()
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["created"])
        self.assertEqual(body["answer_mode"], "ai")
        ticket = Ticket.objects.get(pk=body["ticket_id"])
        self.assertEqual(ticket.citizen.first_name, "Айбек")
        self.assertEqual(ticket.messages.count(), 1)
        self.assertTrue(ticket.number.endswith("-0001"))
        self.assertEqual(ticket.title, "Не вывозят мусор на Токтогула 12")

    def test_second_message_joins_open_ticket(self):
        t1 = self.incoming().json()
        t2 = self.incoming(text="Уже неделю не вывозят!").json()
        self.assertFalse(t2["created"])
        self.assertEqual(t1["ticket_id"], t2["ticket_id"])
        self.assertEqual(Ticket.objects.count(), 1)
        self.assertEqual(Message.objects.count(), 2)

    def test_last_message_preview_tracks_latest_text_not_first(self):
        self.incoming()
        ticket = Ticket.objects.get()
        self.assertEqual(ticket.last_message_preview, "Не вывозят мусор на Токтогула 12")
        self.incoming(text="Уже неделю не вывозят!")
        ticket.refresh_from_db()
        self.assertEqual(ticket.last_message_preview, "Уже неделю не вывозят!")
        # description (первое сообщение) остаётся прежним
        self.assertEqual(ticket.description, "Не вывозят мусор на Токтогула 12")

    def test_last_message_preview_for_photo_without_text(self):
        photo = SimpleUploadedFile("p.jpg", b"JPG", content_type="image/jpeg")
        self.client.post("/api/v1/tickets/incoming/",
                         {"tg_user_id": 503, "files": photo},
                         format="multipart", **HDR)
        self.assertEqual(Ticket.objects.get().last_message_preview, "[Вложение]")

    def test_closed_ticket_spawns_new_one(self):
        first = self.incoming().json()
        Ticket.objects.filter(pk=first["ticket_id"]).update(status="done")
        second = self.incoming(text="Новая проблема").json()
        self.assertTrue(second["created"])
        self.assertNotEqual(first["ticket_id"], second["ticket_id"])

    def test_photo_saved_as_attachment(self):
        photo = SimpleUploadedFile("yama.jpg", b"\xff\xd8\xffFAKEJPEG",
                                   content_type="image/jpeg")
        r = self.client.post("/api/v1/tickets/incoming/",
                             {"tg_user_id": 501, "chat_id": 501,
                              "text": "Вот фото ямы", "files": photo},
                             format="multipart", **HDR)
        self.assertEqual(r.status_code, 200)
        att = Attachment.objects.get()
        self.assertTrue(att.is_image)
        self.assertIn(r.json()["number"], att.file.name)

    def test_photo_without_text_ok_but_empty_rejected(self):
        photo = SimpleUploadedFile("p.jpg", b"JPG", content_type="image/jpeg")
        ok = self.client.post("/api/v1/tickets/incoming/",
                              {"tg_user_id": 502, "files": photo},
                              format="multipart", **HDR)
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(Ticket.objects.get().title, "Фото от жителя")
        bad = self.client.post("/api/v1/tickets/incoming/",
                               {"tg_user_id": 502}, **HDR)
        self.assertEqual(bad.status_code, 400)

    def test_hints_applied_on_create(self):
        Category.objects.create(name="Мусор", slug="cleanup", sla_hours=10)
        District.objects.create(name="Центр", slug="center")
        r = self.incoming(title="Мусор на Токтогула", category="cleanup",
                          district="center", address="Токтогула 12")
        ticket = Ticket.objects.get(pk=r.json()["ticket_id"])
        self.assertEqual(ticket.category.slug, "cleanup")
        self.assertEqual(ticket.district.slug, "center")
        self.assertEqual(ticket.address, "Токтогула 12")
        self.assertIsNotNone(ticket.due_at)

    def test_waiting_ticket_reopens_on_reply(self):
        first = self.incoming().json()
        Ticket.objects.filter(pk=first["ticket_id"]).update(status="waiting")
        self.incoming(text="отвечаю на вопрос")
        self.assertEqual(Ticket.objects.get().status, "in_progress")

    def test_default_channel_is_telegram_when_omitted(self):
        self.incoming()
        self.assertEqual(Citizen.objects.get().channel, Channel.TELEGRAM)
        self.assertEqual(Ticket.objects.get().channel, Channel.TELEGRAM)


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class IncomingWhatsAppTests(APITestCase):
    def incoming(self, **extra):
        data = {"channel": "whatsapp", "chat_id": 996700123456,
                "first_name": "Айгуль", "text": "Не работает уличный свет"}
        data.update(extra)
        return self.client.post("/api/v1/tickets/incoming/", data, **HDR)

    def test_creates_whatsapp_citizen_without_tg_user_id(self):
        r = self.incoming()
        self.assertEqual(r.status_code, 200)
        citizen = Citizen.objects.get()
        self.assertEqual(citizen.channel, Channel.WHATSAPP)
        self.assertIsNone(citizen.tg_user_id)
        self.assertEqual(citizen.chat_id, 996700123456)
        ticket = Ticket.objects.get(pk=r.json()["ticket_id"])
        self.assertEqual(ticket.channel, Channel.WHATSAPP)

    def test_missing_chat_id_rejected(self):
        r = self.incoming(chat_id="")
        self.assertEqual(r.status_code, 400)

    def test_second_message_updates_last_inbound_at(self):
        self.incoming()
        first_seen = Citizen.objects.get().last_inbound_at
        self.incoming(text="Ещё вопрос")
        second_seen = Citizen.objects.get().last_inbound_at
        self.assertGreater(second_seen, first_seen)

    def test_telegram_and_whatsapp_citizen_independent_with_same_chat_id(self):
        self.incoming(chat_id=7)
        self.client.post("/api/v1/tickets/incoming/",
                         {"tg_user_id": 7, "chat_id": 7, "text": "Telegram-сообщение"}, **HDR)
        self.assertEqual(Citizen.objects.filter(chat_id=7).count(), 2)


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA,
                   STAFF_CHAT_ID="-100200")
class NotifyTests(APITestCase):
    def test_new_ticket_creates_staff_notification(self):
        from botcontrol.models import Outbox
        self.client.post("/api/v1/tickets/incoming/",
                         {"tg_user_id": 1, "text": "Прорвало трубу"}, **HDR)
        row = Outbox.objects.get(kind="notify")
        self.assertEqual(row.chat_id, -100200)
        self.assertIn("Прорвало трубу", row.text)
        # второе сообщение в ту же заявку — без второго уведомления
        self.client.post("/api/v1/tickets/incoming/",
                         {"tg_user_id": 1, "text": "и ещё"}, **HDR)
        self.assertEqual(Outbox.objects.filter(kind="notify").count(), 1)

    def test_staff_notification_always_telegram_even_for_whatsapp_ticket(self):
        from botcontrol.models import Outbox
        self.client.post("/api/v1/tickets/incoming/",
                         {"channel": "whatsapp", "chat_id": 996700123456,
                          "text": "Прорвало трубу"}, **HDR)
        row = Outbox.objects.get(kind="notify")
        self.assertEqual(row.channel, Channel.TELEGRAM)


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class DialogueTests(APITestCase):
    def setUp(self):
        r = self.client.post("/api/v1/tickets/incoming/",
                             {"tg_user_id": 7, "chat_id": 7, "text": "Вопрос"}, **HDR)
        self.ticket_id = r.json()["ticket_id"]

    def test_ai_message_and_history(self):
        r = self.client.post(f"/api/v1/tickets/{self.ticket_id}/messages/",
                             {"text": "Ответ ИИ"}, **HDR)
        self.assertEqual(r.status_code, 200)
        h = self.client.get(f"/api/v1/tickets/{self.ticket_id}/history/", **HDR).json()
        self.assertEqual([m["author"] for m in h["messages"]], ["citizen", "ai"])
        self.assertEqual(Ticket.objects.get(pk=self.ticket_id).last_message_preview, "Ответ ИИ")

    def test_history_limit(self):
        for i in range(5):
            self.client.post(f"/api/v1/tickets/{self.ticket_id}/messages/",
                             {"text": f"ответ {i}"}, **HDR)
        h = self.client.get(f"/api/v1/tickets/{self.ticket_id}/history/?limit=3",
                            **HDR).json()
        self.assertEqual(len(h["messages"]), 3)
        self.assertEqual(h["messages"][-1]["text"], "ответ 4")

    def test_rating(self):
        mid = self.client.post(f"/api/v1/tickets/{self.ticket_id}/messages/",
                               {"text": "Ответ"}, **HDR).json()["message_id"]
        r = self.client.post(f"/api/v1/messages/{mid}/rating/", {"rating": "up"}, **HDR)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(Message.objects.get(pk=mid).rating, "up")
        bad = self.client.post(f"/api/v1/messages/{mid}/rating/",
                               {"rating": "meh"}, **HDR)
        self.assertEqual(bad.status_code, 400)

    def test_rating_only_for_ai_messages(self):
        citizen_msg = Message.objects.get(author="citizen")
        r = self.client.post(f"/api/v1/messages/{citizen_msg.pk}/rating/",
                             {"rating": "up"}, **HDR)
        self.assertEqual(r.status_code, 404)

    def test_chat_context_after_restart(self):
        self.client.post(f"/api/v1/tickets/{self.ticket_id}/messages/",
                         {"text": "Ответ ИИ"}, **HDR)
        ctx = self.client.get("/api/v1/chats/7/context/", **HDR).json()
        self.assertEqual(ctx["ticket_id"], self.ticket_id)
        self.assertEqual(len(ctx["messages"]), 2)
        missing = self.client.get("/api/v1/chats/999/context/", **HDR)
        self.assertEqual(missing.status_code, 404)

    def test_close_by_chat(self):
        r = self.client.post("/api/v1/chats/7/close/", **HDR).json()
        self.assertTrue(r["closed"])
        self.assertEqual(Ticket.objects.get().status, "done")
        again = self.client.post("/api/v1/chats/7/close/", **HDR).json()
        self.assertFalse(again["closed"])


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class ChannelDisambiguationTests(APITestCase):
    """Telegram и WhatsApp могут случайно иметь одинаковый числовой chat_id —
    ручки, которые ищут жителя только по chat_id, обязаны спрашивать канал."""

    def setUp(self):
        self.tg = self.client.post(
            "/api/v1/tickets/incoming/",
            {"tg_user_id": 7, "chat_id": 7, "text": "Telegram-вопрос"}, **HDR).json()
        self.wa = self.client.post(
            "/api/v1/tickets/incoming/",
            {"channel": "whatsapp", "chat_id": 7, "text": "WhatsApp-вопрос"}, **HDR).json()

    def test_chat_context_disambiguates_by_channel(self):
        tg_ctx = self.client.get("/api/v1/chats/7/context/?channel=telegram", **HDR).json()
        wa_ctx = self.client.get("/api/v1/chats/7/context/?channel=whatsapp", **HDR).json()
        self.assertEqual(tg_ctx["ticket_id"], self.tg["ticket_id"])
        self.assertEqual(wa_ctx["ticket_id"], self.wa["ticket_id"])
        # без указания канала — по умолчанию Telegram (обратная совместимость)
        default_ctx = self.client.get("/api/v1/chats/7/context/", **HDR).json()
        self.assertEqual(default_ctx["ticket_id"], self.tg["ticket_id"])

    def test_close_by_chat_disambiguates_by_channel(self):
        self.client.post("/api/v1/chats/7/close/", {"channel": "whatsapp"}, **HDR)
        self.assertEqual(Ticket.objects.get(pk=self.wa["ticket_id"]).status, "done")
        self.assertEqual(Ticket.objects.get(pk=self.tg["ticket_id"]).status, "new")

    def test_subscription_disambiguates_by_channel(self):
        self.client.post("/api/v1/citizens/subscription/",
                         {"chat_id": 7, "channel": "whatsapp", "subscribed": False}, **HDR)
        self.assertFalse(Citizen.objects.get(channel="whatsapp", chat_id=7).subscribed)
        self.assertTrue(Citizen.objects.get(channel="telegram", chat_id=7).subscribed)


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class ClassifyTests(APITestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="Дороги", slug="roads", sla_hours=24)
        self.dist = District.objects.create(name="Центр", slug="center")
        r = self.client.post("/api/v1/tickets/incoming/",
                             {"tg_user_id": 9, "text": "На Ленина большая яма уже месяц, "
                              "машины ломаются, сделайте что-нибудь"}, **HDR)
        self.ticket_id = r.json()["ticket_id"]

    def classify(self, **data):
        return self.client.post(f"/api/v1/tickets/{self.ticket_id}/classify/",
                                data, **HDR)

    def test_fills_empty_fields(self):
        r = self.classify(title="Яма на Ленина", category="roads",
                          district="center", address="ул. Ленина")
        applied = r.json()["applied"]
        self.assertIn("title", applied)
        self.assertIn("category", applied)
        t = Ticket.objects.get()
        self.assertEqual(t.title, "Яма на Ленина")
        self.assertEqual(t.category, self.cat)
        self.assertEqual(t.district, self.dist)
        self.assertIsNotNone(t.due_at)

    def test_does_not_overwrite_staff_edits(self):
        Ticket.objects.filter(pk=self.ticket_id).update(
            title="Правка сотрудника", category=self.cat, address="уточнённый адрес")
        r = self.classify(title="Другой заголовок", category="roads",
                          address="другой адрес")
        self.assertEqual(r.json()["applied"], [])
        t = Ticket.objects.get()
        self.assertEqual(t.title, "Правка сотрудника")
        self.assertEqual(t.address, "уточнённый адрес")

    def test_unknown_slug_ignored(self):
        r = self.classify(category="nesuschestvuet")
        self.assertNotIn("category", r.json()["applied"])


@override_settings(BOT_API_TOKEN=TOKEN, MEDIA_ROOT=MEDIA, STAFF_CHAT_ID="")
class RetitleTests(APITestCase):
    def setUp(self):
        r = self.client.post("/api/v1/tickets/incoming/",
                             {"tg_user_id": 20, "text": "Привет"}, **HDR)
        self.ticket_id = r.json()["ticket_id"]

    def retitle(self, title):
        return self.client.post(f"/api/v1/tickets/{self.ticket_id}/retitle/",
                                {"title": title}, **HDR)

    def test_applies_once(self):
        r = self.retitle("Сломан лифт в доме 5")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["applied"])
        ticket = Ticket.objects.get(pk=self.ticket_id)
        self.assertEqual(ticket.title, "Сломан лифт в доме 5")
        self.assertTrue(ticket.events.filter(kind="retitled").exists())

    def test_second_call_ignored(self):
        self.retitle("Первое уточнение")
        r = self.retitle("Второе уточнение")
        self.assertFalse(r.json()["applied"])
        self.assertEqual(Ticket.objects.get(pk=self.ticket_id).title, "Первое уточнение")

    def test_skipped_if_staff_already_edited_title(self):
        Event.objects.create(ticket_id=self.ticket_id, kind="title", payload={"to": "Ручная правка"})
        Ticket.objects.filter(pk=self.ticket_id).update(title="Ручная правка")
        r = self.retitle("Автоматическое уточнение")
        self.assertFalse(r.json()["applied"])
        self.assertEqual(Ticket.objects.get(pk=self.ticket_id).title, "Ручная правка")

    def test_empty_title_rejected(self):
        r = self.retitle("   ")
        self.assertEqual(r.status_code, 400)

    def test_unknown_ticket_404(self):
        r = self.client.post("/api/v1/tickets/999999/retitle/", {"title": "х"}, **HDR)
        self.assertEqual(r.status_code, 404)

