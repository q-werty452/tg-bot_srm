"""Тесты данных, которые бот извлекает из переписки: ФИО, место, тип, исполнитель."""

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from directory.models import Category, District, Executor
from tickets.models import Citizen, Event, Ticket

TOKEN = "test-bot-token-123"
HDR = {"HTTP_X_BOT_TOKEN": TOKEN}
User = get_user_model()
FIRST_TEXT = "Нет воды в селе Кашка-Терек уже неделю, помогите"


class ModelFieldsTests(TestCase):
    def test_new_field_defaults(self):
        citizen = Citizen.objects.create(tg_user_id=1, chat_id=1)
        ticket = Ticket.objects.create(citizen=citizen, title="x")
        self.assertEqual(citizen.middle_name, "")
        self.assertFalse(citizen.name_confirmed)
        self.assertEqual(ticket.settlement, "")
        self.assertEqual(ticket.kind, "")
        self.assertEqual(Ticket.Kind.values, ["appeal", "question", "other"])

    def test_full_name_order_and_fallbacks(self):
        c = Citizen(tg_user_id=5, chat_id=5, first_name="Айбек", last_name="Иванов",
                    middle_name="Маратович")
        self.assertEqual(c.full_name, "Иванов Айбек Маратович")
        self.assertEqual(str(c), "Иванов Айбек Маратович")
        self.assertEqual(str(Citizen(tg_user_id=6, chat_id=6, username="bek")), "@bek")
        self.assertEqual(str(Citizen(tg_user_id=7, chat_id=7)), "id7")

    def test_executor_external_id_unique_but_many_nulls(self):
        Executor.objects.create(name="Один")
        Executor.objects.create(name="Два")  # external_id=None у обоих — можно
        Executor.objects.create(name="Три", external_id="org-1")
        from django.db import IntegrityError, transaction
        with self.assertRaises(IntegrityError), transaction.atomic():
            Executor.objects.create(name="Четыре", external_id="org-1")

    def test_district_kind_blank_allowed(self):
        d = District.objects.create(name="Центр", slug="center")
        self.assertEqual(d.kind, "")


@override_settings(BOT_API_TOKEN=TOKEN, STAFF_CHAT_ID="")
class ClassifyEnrichmentTests(APITestCase):
    def setUp(self):
        self.cat = Category.objects.create(name="Вода", slug="water", sla_hours=24)
        self.mayor = Executor.objects.create(name="Аппарат мэрии", short_name="Мэрия")
        self.cat.default_executor = self.mayor
        self.cat.save()
        self.vodokanal = Executor.objects.create(
            name="Водоканал Кашка-Терек", external_id="org-vodokanal")
        self.staff = User.objects.create_user(email="op@meriya.kg", password="pass12345")
        r = self.client.post("/api/v1/tickets/incoming/", {
            "tg_user_id": 9, "chat_id": 9, "first_name": "bek_77", "text": FIRST_TEXT,
        }, **HDR)
        self.ticket = Ticket.objects.get(pk=r.json()["ticket_id"])
        self.citizen = self.ticket.citizen

    def classify(self, **data):
        return self.client.post(f"/api/v1/tickets/{self.ticket.pk}/classify/", data, **HDR)

    def reload(self):
        self.ticket.refresh_from_db()
        self.citizen.refresh_from_db()

    def staff_event(self, kind):
        Event.objects.create(ticket=self.ticket, user=self.staff, kind=kind)

    # --- ФИО -------------------------------------------------------------
    def test_name_replaces_messenger_nick_and_confirms(self):
        r = self.classify(last_name="Иванов", first_name="Айбек", middle_name="Маратович")
        self.assertIn("name", r.json()["applied"])
        self.reload()
        self.assertEqual(
            (self.citizen.last_name, self.citizen.first_name, self.citizen.middle_name),
            ("Иванов", "Айбек", "Маратович"))
        self.assertTrue(self.citizen.name_confirmed)

    def test_first_stated_name_replaces_whole_profile_name(self):
        # Ник из мессенджера («Mama») не должен остаться в имени, если житель
        # назвал только фамилию: иначе в карточке «Асанова Mama».
        Citizen.objects.filter(pk=self.citizen.pk).update(
            first_name="Mama", last_name="Профильная")
        self.classify(first_name="", last_name="Асанова")
        self.reload()
        self.assertEqual(self.citizen.last_name, "Асанова")
        self.assertEqual(self.citizen.first_name, "")
        self.assertTrue(self.citizen.name_confirmed)

    def test_confirmed_name_only_fills_empty_parts(self):
        Citizen.objects.filter(pk=self.citizen.pk).update(
            first_name="Айбек", last_name="Иванов", name_confirmed=True)
        r = self.classify(last_name="Петров", first_name="Другой", middle_name="Маратович")
        self.assertIn("name", r.json()["applied"])
        self.reload()
        self.assertEqual(self.citizen.last_name, "Иванов")
        self.assertEqual(self.citizen.first_name, "Айбек")
        self.assertEqual(self.citizen.middle_name, "Маратович")

    def test_confirmed_name_untouched_when_nothing_empty(self):
        Citizen.objects.filter(pk=self.citizen.pk).update(
            first_name="Айбек", last_name="Иванов", middle_name="М", name_confirmed=True)
        r = self.classify(last_name="Петров", first_name="Другой", middle_name="Иной")
        self.assertNotIn("name", r.json()["applied"])
        self.reload()
        self.assertEqual(self.citizen.last_name, "Иванов")

    def test_middle_name_alone_does_not_confirm(self):
        self.classify(middle_name="Маратович")
        self.reload()
        self.assertFalse(self.citizen.name_confirmed)
        self.assertEqual(self.citizen.middle_name, "")

    def test_phone_only_if_empty(self):
        self.classify(phone="+996700111222")
        self.reload()
        self.assertEqual(self.citizen.phone, "+996700111222")
        r = self.classify(phone="+996555000000")
        self.assertNotIn("phone", r.json()["applied"])
        self.reload()
        self.assertEqual(self.citizen.phone, "+996700111222")

    # --- место -----------------------------------------------------------
    def test_settlement_only_if_empty(self):
        r = self.classify(settlement="Кашка-Терек")
        self.assertIn("settlement", r.json()["applied"])
        self.classify(settlement="Другое село")
        self.reload()
        self.assertEqual(self.ticket.settlement, "Кашка-Терек")

    def test_settlement_edited_by_staff_not_overwritten(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(settlement="Правка сотрудника")
        r = self.classify(settlement="Кашка-Терек")
        self.assertEqual(r.json()["applied"], [])

    # --- тип обращения ---------------------------------------------------
    def test_kind_set_and_updated_by_bot(self):
        self.classify(kind="other")
        self.reload()
        self.assertEqual(self.ticket.kind, "other")
        self.classify(kind="appeal")
        self.reload()
        self.assertEqual(self.ticket.kind, "appeal")

    def test_unknown_kind_ignored(self):
        r = self.classify(kind="выдумка")
        self.assertEqual(r.json()["applied"], [])
        self.reload()
        self.assertEqual(self.ticket.kind, "")

    def test_kind_not_changed_after_staff_edit(self):
        self.client.force_login(self.staff)
        self.client.post(f"/tickets/{self.ticket.number}/action/",
                         {"action": "kind", "kind": "question"})
        self.client.logout()
        r = self.classify(kind="appeal")
        self.assertNotIn("kind", r.json()["applied"])
        self.reload()
        self.assertEqual(self.ticket.kind, "question")

    # --- исполнитель -----------------------------------------------------
    def test_profile_executor_replaces_category_default(self):
        r = self.classify(category="water", executor="org-vodokanal")
        self.assertEqual(r.json()["applied"].count("executor"), 1)
        self.reload()
        self.assertEqual(self.ticket.executor, self.vodokanal)

    def test_profile_executor_replaces_earlier_automatic_one(self):
        self.classify(category="water")
        self.reload()
        self.assertEqual(self.ticket.executor, self.mayor)
        self.classify(executor="org-vodokanal")
        self.reload()
        self.assertEqual(self.ticket.executor, self.vodokanal)

    def test_executor_chosen_by_staff_not_overwritten(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(executor=self.mayor)
        self.staff_event("executor")
        r = self.classify(executor="org-vodokanal")
        self.assertNotIn("executor", r.json()["applied"])
        self.reload()
        self.assertEqual(self.ticket.executor, self.mayor)

    def test_unknown_or_inactive_executor_ignored(self):
        self.assertEqual(self.classify(executor="org-nope").json()["applied"], [])
        Executor.objects.filter(pk=self.vodokanal.pk).update(is_active=False)
        self.assertEqual(self.classify(executor="org-vodokanal").json()["applied"], [])
        self.reload()
        self.assertIsNone(self.ticket.executor)

    # --- описание --------------------------------------------------------
    def test_description_replaced_when_equal_to_first_message(self):
        r = self.classify(description="Нет воды в с. Кашка-Терек неделю")
        self.assertIn("description", r.json()["applied"])
        self.reload()
        self.assertEqual(self.ticket.description, "Нет воды в с. Кашка-Терек неделю")

    def test_description_can_be_refined_again_by_bot(self):
        self.classify(description="Первая версия")
        self.classify(description="Вторая, точнее")
        self.reload()
        self.assertEqual(self.ticket.description, "Вторая, точнее")

    def test_manually_edited_description_kept(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(description="Написал сотрудник")
        r = self.classify(description="Версия ИИ")
        self.assertNotIn("description", r.json()["applied"])
        self.reload()
        self.assertEqual(self.ticket.description, "Написал сотрудник")

    def test_staff_description_event_blocks_replacement(self):
        self.staff_event("description")
        r = self.classify(description="Версия ИИ")
        self.assertNotIn("description", r.json()["applied"])

    # --- событие ---------------------------------------------------------
    def test_classified_event_lists_applied(self):
        self.classify(settlement="Кашка-Терек", kind="appeal")
        event = Event.objects.filter(kind="classified").latest("id")
        self.assertEqual(set(event.payload["applied"]), {"settlement", "kind"})


@override_settings(BOT_API_TOKEN=TOKEN, STAFF_CHAT_ID="")
class IncomingConfirmedNameTests(APITestCase):
    def incoming(self, **extra):
        data = {"tg_user_id": 31, "chat_id": 31, "first_name": "bek_77",
                "last_name": "", "username": "bek", "text": "Здравствуйте"}
        data.update(extra)
        return self.client.post("/api/v1/tickets/incoming/", data, **HDR)

    def test_profile_name_overwrites_while_unconfirmed(self):
        self.incoming()
        self.incoming(first_name="Новый ник")
        self.assertEqual(Citizen.objects.get().first_name, "Новый ник")

    def test_confirmed_name_survives_messenger_profile(self):
        self.incoming()
        Citizen.objects.update(first_name="Айбек", last_name="Иванов",
                               name_confirmed=True)
        self.incoming(first_name="bek_77", last_name="nick", username="bek_new",
                      phone="+996700000001")
        citizen = Citizen.objects.get()
        self.assertEqual((citizen.first_name, citizen.last_name), ("Айбек", "Иванов"))
        self.assertEqual(citizen.username, "bek_new")
        self.assertEqual(citizen.phone, "+996700000001")


class ConfigDistrictsTests(APITestCase):
    @override_settings(BOT_API_TOKEN=TOKEN)
    def test_config_lists_only_active_districts_with_slug_and_name(self):
        District.objects.create(name="Старый", slug="d01", is_active=False)
        District.objects.create(name="Сузакский район", slug="suzak", kind="district")
        r = self.client.get("/api/v1/config/", **HDR)
        self.assertEqual(r.json()["districts"],
                         [{"slug": "suzak", "name": "Сузакский район"}])
