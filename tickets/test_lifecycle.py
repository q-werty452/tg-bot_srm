"""Новое обращение с того же номера: закрытие справочных заявок и разделение."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from tickets.models import Event, Message, Ticket

TOKEN = "test-bot-token"
HDR = {"HTTP_X_BOT_TOKEN": TOKEN}


@override_settings(BOT_API_TOKEN=TOKEN, STAFF_CHAT_ID="", GEOCODE_ENABLED=False)
class LifecycleTests(APITestCase):
    def incoming(self, text, **extra):
        return self.client.post("/api/v1/tickets/incoming/",
                                {"tg_user_id": 5, "chat_id": 5, "text": text, **extra},
                                **HDR).json()

    def age(self, ticket_id, hours):
        Ticket.objects.filter(pk=ticket_id).update(
            last_message_at=timezone.now() - timedelta(hours=hours))

    def test_incoming_returns_message_id(self):
        r = self.incoming("Где ЦОН?")
        self.assertEqual(Message.objects.get(pk=r["message_id"]).text, "Где ЦОН?")

    def test_idle_question_closed_and_new_ticket_opened(self):
        first = self.incoming("Где ЦОН?")
        Ticket.objects.filter(pk=first["ticket_id"]).update(kind="question")
        self.age(first["ticket_id"], 13)
        second = self.incoming("У нас нет воды")
        self.assertNotEqual(second["ticket_id"], first["ticket_id"])
        self.assertTrue(second["created"])
        self.assertEqual(second["closed_ticket"], first["number"])
        old = Ticket.objects.get(pk=first["ticket_id"])
        self.assertEqual(old.status, Ticket.Status.DONE)
        self.assertTrue(old.events.filter(kind="status", payload__by="consultation_idle").exists())

    def test_recent_question_continues(self):
        first = self.incoming("Где ЦОН?")
        Ticket.objects.filter(pk=first["ticket_id"]).update(kind="question")
        self.age(first["ticket_id"], 2)
        self.assertEqual(self.incoming("а часы работы?")["ticket_id"], first["ticket_id"])

    def test_appeal_never_closed_by_silence(self):
        first = self.incoming("Нет воды")
        Ticket.objects.filter(pk=first["ticket_id"]).update(kind="appeal")
        self.age(first["ticket_id"], 24 * 7)
        second = self.incoming("Когда сделают?")
        self.assertEqual(second["ticket_id"], first["ticket_id"])
        self.assertTrue(Ticket.objects.get(pk=first["ticket_id"]).is_open)

    def test_staff_touched_question_not_closed(self):
        first = self.incoming("Где ЦОН?")
        user = get_user_model().objects.create_user("op", password="x")
        Ticket.objects.filter(pk=first["ticket_id"]).update(kind="question")
        Event.objects.create(ticket_id=first["ticket_id"], user=user, kind="status",
                             payload={"to": "in_progress"})
        self.age(first["ticket_id"], 30)
        self.assertEqual(self.incoming("ещё вопрос")["ticket_id"], first["ticket_id"])

    def test_split_moves_new_topic(self):
        first = self.incoming("Нет воды в Масы")
        self.client.post(f"/api/v1/tickets/{first['ticket_id']}/messages/",
                         {"text": "Передали в водоканал"}, **HDR)
        second = self.incoming("И ещё у школы яма на дороге")
        self.client.post(f"/api/v1/tickets/{first['ticket_id']}/messages/",
                         {"text": "Записал"}, **HDR)
        r = self.client.post(f"/api/v1/tickets/{first['ticket_id']}/split/",
                             {"from_message_id": second["message_id"], "title": "Яма у школы"},
                             format="json", **HDR)
        self.assertEqual(r.status_code, 200)
        new = Ticket.objects.get(pk=r.json()["ticket_id"])
        old = Ticket.objects.get(pk=first["ticket_id"])
        self.assertEqual(new.title, "Яма у школы")
        self.assertEqual([m.text for m in new.messages.all()],
                         ["И ещё у школы яма на дороге", "Записал"])
        self.assertEqual([m.text for m in old.messages.all()],
                         ["Нет воды в Масы", "Передали в водоканал"])
        self.assertEqual(old.last_message_preview, "Передали в водоканал")
        self.assertTrue(old.is_open)
        self.assertTrue(old.events.filter(kind="split").exists())
        # следующие сообщения идут уже в новую заявку
        self.assertEqual(self.incoming("Это возле школы №3")["ticket_id"], new.pk)

    def test_split_rejects_first_message_and_foreign(self):
        first = self.incoming("Нет воды")
        r = self.client.post(f"/api/v1/tickets/{first['ticket_id']}/split/",
                             {"from_message_id": first["message_id"]}, format="json", **HDR)
        self.assertEqual(r.status_code, 409)
        r = self.client.post(f"/api/v1/tickets/{first['ticket_id']}/split/",
                             {"from_message_id": 999999}, format="json", **HDR)
        self.assertEqual(r.status_code, 400)

    def test_pin_from_messenger_puts_ticket_on_map(self):
        first = self.incoming("Яма тут")
        r = self.client.post(f"/api/v1/tickets/{first['ticket_id']}/classify/",
                             {"lat": 40.95, "lon": 72.98}, format="json", **HDR)
        self.assertIn("pin", r.json()["applied"])
        t = Ticket.objects.get(pk=first["ticket_id"])
        self.assertAlmostEqual(t.lat, 40.95)
        self.assertEqual(t.geo_source, "pin")
        # точка за пределами области не принимается
        second = self.incoming("ещё")
        r = self.client.post(f"/api/v1/tickets/{second['ticket_id']}/classify/",
                             {"lat": 55.75, "lon": 37.61}, format="json", **HDR)
        self.assertNotIn("pin", r.json()["applied"])


@override_settings(BOT_API_TOKEN=TOKEN, STAFF_CHAT_ID="", GEOCODE_ENABLED=False)
class CardCitizenTests(APITestCase):
    def test_staff_edits_name_and_sees_other_tickets(self):
        HDRS = HDR
        first = self.client.post("/api/v1/tickets/incoming/",
                                 {"tg_user_id": 8, "text": "Нет воды"}, **HDRS).json()
        Ticket.objects.filter(pk=first["ticket_id"]).update(status=Ticket.Status.DONE)
        second = self.client.post("/api/v1/tickets/incoming/",
                                  {"tg_user_id": 8, "text": "Яма"}, **HDRS).json()
        user = get_user_model().objects.create_user("op2", password="x", is_staff=True)
        self.client.force_login(user)
        page = self.client.get(f"/tickets/{second['number']}/")
        self.assertContains(page, f"#{first['number']}")
        self.client.post(f"/tickets/{second['number']}/action/",
                         {"action": "citizen", "last_name": "Асанов", "first_name": "Бакыт",
                          "middle_name": "Асанович", "phone": "996555123456"})
        t = Ticket.objects.select_related("citizen").get(pk=second["ticket_id"])
        self.assertEqual(t.citizen.full_name, "Асанов Бакыт Асанович")
        self.assertTrue(t.citizen.name_confirmed)
        # ник мессенджера после этого не возвращается
        self.client.post("/api/v1/tickets/incoming/",
                         {"tg_user_id": 8, "text": "ещё", "first_name": "bek_77"}, **HDRS)
        t.citizen.refresh_from_db()
        self.assertEqual(t.citizen.first_name, "Бакыт")
