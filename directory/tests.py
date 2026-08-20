"""Тесты страницы справочников."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from botcontrol.models import BotSetting

from .models import Category, Contact, District, Executor

User = get_user_model()


class DirectoryPageTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email="adm@meriya.kg",
                                             password="pass12345", role="admin")
        cls.operator = User.objects.create_user(email="op@meriya.kg",
                                                password="pass12345")

    def setUp(self):
        self.client.login(email="adm@meriya.kg", password="pass12345")

    def test_operator_denied(self):
        self.client.login(email="op@meriya.kg", password="pass12345")
        self.assertEqual(self.client.get("/directory/").status_code, 302)

    def test_add_and_edit_category(self):
        executor = Executor.objects.create(name="МП «Тазалык»")
        self.client.post("/directory/", {"action": "add_category",
                                         "name": "Мусор", "sla_hours": "48",
                                         "executor": executor.pk})
        cat = Category.objects.get()
        self.assertEqual((cat.name, cat.sla_hours, cat.default_executor),
                         ("Мусор", 48, executor))

        self.client.post("/directory/", {"action": "save_category", "id": cat.pk,
                                         "name": "Мусор и уборка",
                                         "sla_hours": "24", "is_active": "on"})
        cat.refresh_from_db()
        self.assertEqual((cat.name, cat.sla_hours), ("Мусор и уборка", 24))

    def test_add_district_and_toggle(self):
        self.client.post("/directory/", {"action": "add_district", "name": "Спутник"})
        d = District.objects.get()
        self.assertTrue(d.slug)
        self.client.post("/directory/", {"action": "toggle_district", "id": d.pk})
        d.refresh_from_db()
        self.assertFalse(d.is_active)

    def test_contact_bumps_bot_config_version(self):
        v1 = BotSetting.get().version
        self.client.post("/directory/", {"action": "add_contact",
                                         "title": "Приёмная", "phone": "5-00-00",
                                         "kind": "office", "is_public": "on"})
        self.assertTrue(Contact.objects.filter(title="Приёмная").exists())
        self.assertGreater(BotSetting.get().version, v1,
                           "бот должен узнать о новом контакте")

    def test_cyrillic_slug_fallback(self):
        self.client.post("/directory/", {"action": "add_district", "name": "Тоолос"})
        self.assertTrue(District.objects.get().slug)
