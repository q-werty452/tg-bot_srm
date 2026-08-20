"""Тесты страницы сотрудников и входа."""

from django.contrib.auth import get_user_model
from django.test import TestCase

User = get_user_model()


class StaffPageTests(TestCase):
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
        self.assertEqual(self.client.get("/staff/").status_code, 302)

    def test_create_staff_shows_password_once(self):
        response = self.client.post("/staff/", {
            "action": "add", "email": "new@meriya.kg",
            "first_name": "Айбек", "role": "operator"}, follow=True)
        user = User.objects.get(email="new@meriya.kg")
        self.assertEqual(user.role, "operator")
        self.assertContains(response, "Пароль (показывается один раз)")

    def test_duplicate_email_rejected(self):
        self.client.post("/staff/", {"action": "add", "email": "op@meriya.kg"})
        self.assertEqual(User.objects.filter(email="op@meriya.kg").count(), 1)

    def test_cannot_disable_self(self):
        self.client.post("/staff/", {"action": "toggle", "id": self.admin.pk})
        self.admin.refresh_from_db()
        self.assertTrue(self.admin.is_active)

    def test_toggle_other(self):
        self.client.post("/staff/", {"action": "toggle", "id": self.operator.pk})
        self.operator.refresh_from_db()
        self.assertFalse(self.operator.is_active)

    def test_reset_password(self):
        response = self.client.post("/staff/", {
            "action": "reset_password", "id": self.operator.pk}, follow=True)
        self.assertContains(response, "Новый пароль")


class LogoutTests(TestCase):
    def test_logout_via_post(self):
        User.objects.create_user(email="x@meriya.kg", password="pass12345")
        self.client.login(email="x@meriya.kg", password="pass12345")
        response = self.client.post("/logout/")
        self.assertEqual(response.status_code, 302)
