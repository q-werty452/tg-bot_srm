"""Тесты журнала действий."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from .models import Entry, log

User = get_user_model()


class AuditTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email="adm@meriya.kg",
                                             password="pass12345", role="admin")
        cls.operator = User.objects.create_user(email="op@meriya.kg",
                                                password="pass12345")

    def test_log_helper(self):
        log(self.admin, "тестовое действие", "объект", "детали")
        entry = Entry.objects.get()
        self.assertEqual(entry.user, self.admin)
        self.assertEqual(entry.action, "тестовое действие")

    def test_admin_sees_log_operator_does_not(self):
        log(self.admin, "смена ключа", "openai")
        self.client.login(email="adm@meriya.kg", password="pass12345")
        response = self.client.get("/audit/")
        self.assertContains(response, "смена ключа")

        self.client.login(email="op@meriya.kg", password="pass12345")
        self.assertEqual(self.client.get("/audit/").status_code, 302)

    def test_search(self):
        log(self.admin, "рассылка отправлена", "", "субботник")
        log(self.admin, "ключ заменён", "gemini")
        self.client.login(email="adm@meriya.kg", password="pass12345")
        html = self.client.get("/audit/?q=рассылка").content.decode()
        self.assertIn("рассылка отправлена", html)
        self.assertNotIn("ключ заменён", html)
