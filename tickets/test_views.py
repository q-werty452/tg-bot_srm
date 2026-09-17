"""Тесты страниц сотрудников: вход, список, карточка, действия."""

from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from botcontrol.models import Outbox
from directory.models import Category, District, Executor
from tickets.models import Channel, Citizen, Message, Note, Ticket

User = get_user_model()


class PagesTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="op@meriya.kg", password="pass12345",
                                            first_name="Гульмира", role="operator")
        cls.category = Category.objects.create(name="Дороги", slug="roads", sla_hours=48)
        cls.district = District.objects.create(name="Центр", slug="center")
        cls.executor = Executor.objects.create(name="МП «Тазалык»", short_name="Тазалык")
        cls.citizen = Citizen.objects.create(tg_user_id=100, chat_id=100,
                                             first_name="Айбек")
        cls.ticket = Ticket.objects.create(
            citizen=cls.citizen, title="Яма на дороге",
            description="Большая яма", category=cls.category,
            last_message_at=timezone.now(),
        )
        Message.objects.create(ticket=cls.ticket, author="citizen", text="Большая яма")

    def login(self):
        self.client.login(email="op@meriya.kg", password="pass12345")


class AuthPagesTests(PagesTestCase):
    def test_anonymous_redirected_to_login(self):
        for url in ("/", f"/tickets/{self.ticket.number}/", "/tickets/new/",
                    "/bot/settings/", "/stats/"):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, url)
            self.assertIn("/login/", response["Location"])

    def test_login_by_email_works(self):
        response = self.client.post("/login/", {"username": "op@meriya.kg",
                                                "password": "pass12345"})
        self.assertRedirects(response, "/")

    def test_wrong_password_shows_error(self):
        response = self.client.post("/login/", {"username": "op@meriya.kg",
                                                "password": "wrong"})
        self.assertContains(response, "Неверная почта или пароль")

    def test_bot_api_not_open_to_logged_in_staff(self):
        """Сессия сотрудника не даёт доступа к ручкам бота."""
        self.login()
        response = self.client.post("/api/v1/tickets/incoming/",
                                    {"tg_user_id": 5, "text": "х"})
        self.assertIn(response.status_code, (401, 403))


class DashboardTests(PagesTestCase):
    def test_ticket_listed(self):
        self.login()
        response = self.client.get("/")
        self.assertContains(response, "Яма на дороге")
        self.assertContains(response, self.ticket.number)

    def test_filters(self):
        self.login()
        other_cat = Category.objects.create(name="Мусор", slug="cleanup")
        other = Ticket.objects.create(citizen=self.citizen, title="Не вывозят мусор",
                                      category=other_cat, status=Ticket.Status.DONE,
                                      last_message_at=timezone.now())
        # закрытая заявка нужна фильтру по статусу, поэтому open() тут не мешает
        response = self.client.get("/?status=new")
        self.assertContains(response, "Яма на дороге")
        self.assertNotContains(response, "Не вывозят мусор")

        response = self.client.get("/?category=cleanup")
        self.assertContains(response, "Не вывозят мусор")
        self.assertNotContains(response, "Яма на дороге")

        response = self.client.get("/?q=" + other.number)
        self.assertContains(response, "Не вывозят мусор")

    def test_channel_filter(self):
        self.login()
        wa_citizen = Citizen.objects.create(channel=Channel.WHATSAPP, chat_id=900)
        wa_ticket = Ticket.objects.create(
            citizen=wa_citizen, title="WhatsApp-обращение", channel=Channel.WHATSAPP,
            last_message_at=timezone.now(),
        )
        response = self.client.get("/?channel=whatsapp")
        self.assertContains(response, "WhatsApp-обращение")
        self.assertNotContains(response, "Яма на дороге")

        response = self.client.get("/?channel=telegram")
        self.assertContains(response, "Яма на дороге")
        self.assertNotContains(response, "WhatsApp-обращение")

    def test_empty_state(self):
        self.login()
        Ticket.objects.all().delete()
        response = self.client.get("/")
        self.assertContains(response, "Обращений пока нет")

    def test_operator_has_no_admin_nav(self):
        self.login()
        response = self.client.get("/")
        self.assertNotContains(response, "Ключи и токены")

    def test_admin_sees_admin_nav(self):
        User.objects.create_user(email="adm@meriya.kg", password="pass12345",
                                 role="admin")
        self.client.login(email="adm@meriya.kg", password="pass12345")
        response = self.client.get("/")
        self.assertContains(response, "Ключи и токены")


class TicketDetailTests(PagesTestCase):
    def test_detail_shows_thread_and_marks_read(self):
        self.login()
        self.ticket.unread = True
        self.ticket.save(update_fields=["unread"])
        response = self.client.get(f"/tickets/{self.ticket.number}/")
        self.assertContains(response, "Большая яма")
        self.assertContains(response, "Айбек")
        self.ticket.refresh_from_db()
        self.assertFalse(self.ticket.unread)

    def test_unknown_number_404(self):
        self.login()
        self.assertEqual(self.client.get("/tickets/9999-9999/").status_code, 404)

    def act(self, **data):
        return self.client.post(f"/tickets/{self.ticket.number}/action/", data)

    def test_reply_creates_message_and_outbox(self):
        self.login()
        response = self.act(action="reply", text="Бригада выедет завтра.")
        self.assertRedirects(response, f"/tickets/{self.ticket.number}/")
        message = Message.objects.get(author="staff")
        self.assertEqual(message.staff_user, self.user)
        row = Outbox.objects.get()
        self.assertEqual((row.chat_id, row.kind), (100, Outbox.Kind.REPLY))
        self.assertEqual(row.text, "Бригада выедет завтра.")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, Ticket.Status.IN_PROGRESS)

    def test_empty_reply_rejected(self):
        self.login()
        self.act(action="reply", text="   ")
        self.assertFalse(Outbox.objects.exists())

    def _make_whatsapp_ticket(self, last_inbound_at):
        citizen = Citizen.objects.create(channel=Channel.WHATSAPP, chat_id=996700123456,
                                         last_inbound_at=last_inbound_at)
        return Ticket.objects.create(citizen=citizen, title="WhatsApp-обращение",
                                     channel=Channel.WHATSAPP,
                                     last_message_at=timezone.now())

    def test_whatsapp_reply_blocked_when_window_closed(self):
        self.login()
        ticket = self._make_whatsapp_ticket(timezone.now() - timedelta(hours=24))
        response = self.client.post(f"/tickets/{ticket.number}/action/",
                                    {"action": "reply", "text": "Разберёмся"})
        self.assertRedirects(response, f"/tickets/{ticket.number}/")
        self.assertFalse(Message.objects.filter(ticket=ticket).exists())
        self.assertFalse(Outbox.objects.filter(ticket=ticket).exists())

    def test_whatsapp_reply_allowed_when_window_open(self):
        self.login()
        ticket = self._make_whatsapp_ticket(timezone.now())
        response = self.client.post(f"/tickets/{ticket.number}/action/",
                                    {"action": "reply", "text": "Разберёмся"})
        self.assertRedirects(response, f"/tickets/{ticket.number}/")
        self.assertTrue(Message.objects.filter(ticket=ticket, author="staff").exists())
        row = Outbox.objects.get(ticket=ticket)
        self.assertEqual(row.channel, Channel.WHATSAPP)

    def test_telegram_reply_never_blocked_by_window(self):
        """У self.ticket (Telegram) last_inbound_at не задан вовсе — не должно мешать."""
        self.login()
        response = self.act(action="reply", text="Бригада выедет завтра.")
        self.assertRedirects(response, f"/tickets/{self.ticket.number}/")
        self.assertTrue(Outbox.objects.filter(ticket=self.ticket).exists())

    def test_mode_toggle(self):
        self.login()
        self.act(action="mode", mode="staff")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.answer_mode, "staff")
        self.act(action="mode", mode="ai")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.answer_mode, "ai")

    def test_status_change_logged(self):
        self.login()
        self.act(action="status", status="done")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "done")
        self.assertTrue(self.ticket.events.filter(kind="status").exists())
        from audit.models import Entry
        self.assertTrue(Entry.objects.filter(action="смена статуса").exists())

    def test_invalid_status_ignored(self):
        self.login()
        self.act(action="status", status="hacked")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.status, "new")

    def test_assign_executor_and_staff(self):
        self.login()
        self.act(action="executor", executor=str(self.executor.pk))
        self.act(action="assignee", assignee=str(self.user.pk))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.executor, self.executor)
        self.assertEqual(self.ticket.assignee, self.user)

    def test_title_address_note(self):
        self.login()
        self.act(action="title", title="Яма на Токтогула, 12")
        self.act(action="address", address="ул. Токтогула, 12")
        self.act(action="note", text="Позвонил в Тазалык, обещали завтра")
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.title, "Яма на Токтогула, 12")
        self.assertEqual(self.ticket.address, "ул. Токтогула, 12")
        self.assertEqual(Note.objects.get().user, self.user)


class TicketNewTests(PagesTestCase):
    def test_manual_ticket(self):
        self.login()
        response = self.client.post("/tickets/new/", {
            "name": "Бакыт", "phone": "0555 112233",
            "description": "Пришёл лично: не работает уличное освещение",
            "category": "roads", "district": "center",
            "address": "ул. Ленина, 5",
        })
        ticket = Ticket.objects.exclude(pk=self.ticket.pk).get()
        self.assertRedirects(response, f"/tickets/{ticket.number}/")
        self.assertEqual(ticket.answer_mode, "staff")
        self.assertLess(ticket.citizen.tg_user_id, 0)
        self.assertEqual(ticket.category, self.category)
        self.assertEqual(ticket.messages.count(), 1)

    def test_manual_requires_fields(self):
        self.login()
        self.client.post("/tickets/new/", {"name": "", "description": ""})
        self.assertEqual(Ticket.objects.count(), 1)
