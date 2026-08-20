"""Тесты автоопределения провайдера по ключу."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from . import keydetect
from .models import ProviderKey

User = get_user_model()


class ShapeTests(TestCase):
    """Догадка по виду ключа — без сети."""

    def test_anthropic(self):
        provider, _ = keydetect.guess("sk-ant-api03-" + "x" * 40)
        self.assertEqual(provider, "claude")

    def test_google_old_format(self):
        provider, _ = keydetect.guess("AIzaSyB" + "x" * 32)
        self.assertEqual(provider, "gemini")

    def test_google_new_format(self):
        # Реальный формат из AI Studio 2026 года — на AIza он не похож,
        # ровно из-за этого случая и понадобилось автоопределение.
        provider, _ = keydetect.guess("AQ.ExampleFakeKeyForTests_0123456789abcdef")
        self.assertEqual(provider, "gemini")

    def test_openai_project_key(self):
        provider, _ = keydetect.guess("sk-proj-" + "x" * 40)
        self.assertEqual(provider, "openai")

    def test_bare_sk_guessed_as_openai(self):
        provider, hint = keydetect.guess("sk-" + "aB3-_" * 10)
        self.assertEqual(provider, "openai")
        self.assertIn("сторонних", hint, "человеку сказано, что это лишь догадка")

    def test_unknown_shape(self):
        provider, hint = keydetect.guess("совершенно-непонятная-строка")
        self.assertIsNone(provider)
        self.assertIn("проверю у всех", hint)

    def test_telegram_token_recognised(self):
        self.assertTrue(keydetect.looks_like_telegram_token(
            "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"))
        self.assertFalse(keydetect.looks_like_telegram_token("sk-proj-" + "x" * 40))

    def test_probe_order_puts_guess_first(self):
        order = keydetect.probe_order("AQ.ExampleFakeKeyForTests_0123")
        self.assertEqual(order[0], "gemini")
        self.assertEqual(sorted(order), sorted(keydetect.ALL_PROVIDERS))

    def test_probe_order_covers_all_when_unknown(self):
        order = keydetect.probe_order("непонятно")
        self.assertEqual(sorted(order), sorted(keydetect.ALL_PROVIDERS))


class DetectTests(TestCase):
    """Определение с обращением к провайдерам — сеть подменена."""

    @staticmethod
    def fake_fetch(accepts: str, models=("model-a", "model-b")):
        """Подставной опрос: ключ принимает только провайдер `accepts`."""
        def fetch(provider, key):
            if provider != accepts:
                raise RuntimeError("401 Unauthorized")
            return list(models)
        return fetch

    def test_finds_owner_even_if_guess_wrong(self):
        # Ключ выглядит как OpenAI, а принимает его Anthropic.
        provider, models, problem = keydetect.detect(
            "sk-" + "z" * 40, self.fake_fetch("claude"))
        self.assertEqual(provider, "claude")
        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(problem, "")

    def test_guess_checked_first(self):
        asked = []

        def fetch(provider, key):
            asked.append(provider)
            if provider != "gemini":
                raise RuntimeError("нет")
            return ["gemini-3.6-flash"]

        provider, _, _ = keydetect.detect("AQ.ExampleFakeKeyForTests", fetch)
        self.assertEqual(provider, "gemini")
        self.assertEqual(asked, ["gemini"], "лишних запросов быть не должно")

    def test_nobody_accepts(self):
        def fetch(provider, key):
            raise RuntimeError("401 Unauthorized")

        provider, models, problem = keydetect.detect("sk-" + "q" * 40, fetch)
        self.assertIsNone(provider)
        self.assertEqual(models, [])
        self.assertIn("Ни один провайдер не принял", problem)

    def test_empty_key(self):
        provider, _, problem = keydetect.detect("   ", self.fake_fetch("openai"))
        self.assertIsNone(provider)
        self.assertIn("пустой", problem)

    def test_telegram_token_gets_explanation(self):
        provider, _, problem = keydetect.detect(
            "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw",
            self.fake_fetch("openai"))
        self.assertIsNone(provider)
        self.assertIn("токен Telegram-бота", problem)


class DetectPageTests(TestCase):
    """Страница «Ключи и токены»: кнопка «Определить и сохранить»."""

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email="adm@meriya.kg",
                                             password="pass12345", role="admin")

    def setUp(self):
        self.client.login(email="adm@meriya.kg", password="pass12345")

    def test_detects_saves_and_offers_models(self):
        from unittest.mock import patch
        with patch("botcontrol.views._fetch_models",
                   side_effect=lambda p, k: ["gemini-3.6-flash", "gemini-3.5-flash"]
                   if p == "gemini" else (_ for _ in ()).throw(RuntimeError("401"))):
            response = self.client.post(
                "/bot/keys/", {"action": "detect_key",
                               "key": "AQ.ExampleFakeKeyForTests_0123456"},
                follow=True)

        row = ProviderKey.objects.get()
        self.assertEqual(row.provider, "gemini")
        self.assertTrue(row.last_check_ok)
        self.assertEqual(row.model, "gemini-3.6-flash", "модель подставилась сама")
        self.assertContains(response, "Gemini")
        self.assertNotContains(response, "AQ.ExampleFakeKeyForTests",
                               msg_prefix="ключ не должен попадать на страницу")

    def test_unknown_key_reports_problem_and_saves_nothing(self):
        from unittest.mock import patch
        with patch("botcontrol.views._fetch_models",
                   side_effect=RuntimeError("401 Unauthorized")):
            response = self.client.post("/bot/keys/",
                                        {"action": "detect_key", "key": "sk-" + "x" * 40},
                                        follow=True)
        self.assertFalse(ProviderKey.objects.exists())
        self.assertContains(response, "Ни один провайдер не принял")

    def test_operator_cannot_detect(self):
        User.objects.create_user(email="op@meriya.kg", password="pass12345")
        self.client.login(email="op@meriya.kg", password="pass12345")
        self.client.post("/bot/keys/", {"action": "detect_key", "key": "sk-x"})
        self.assertFalse(ProviderKey.objects.exists())


class SmokeTestModelTests(TestCase):
    """
    Проверка модели диалогом перед сохранением.

    Появилась после случая в бою: в списке моделей Google оказалась
    antigravity-preview, которая принимает только одиночный вопрос. Бот слал
    ей историю переписки и получал 400 на каждой второй реплике жителя.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = User.objects.create_user(email="adm@meriya.kg",
                                             password="pass12345", role="admin")

    def setUp(self):
        self.client.login(email="adm@meriya.kg", password="pass12345")
        self.key = ProviderKey(provider="gemini", model="gemini-3.6-flash")
        self.key.set_secret("AQ.testkey1234567890")
        self.key.save()

    def test_multiturn_failure_is_explained(self):
        from botcontrol.views import smoke_test_model
        with patch("botcontrol.views.httpx.post", side_effect=RuntimeError(
                "400 Multiturn chat is not enabled for models/antigravity-preview")):
            problem = smoke_test_model("gemini", "k", "antigravity-preview")
        self.assertIn("не умеет вести переписку", problem)

    def test_quota_failure_is_explained(self):
        from botcontrol.views import smoke_test_model
        with patch("botcontrol.views.httpx.post", side_effect=RuntimeError(
                "429 RESOURCE_EXHAUSTED quota exceeded")):
            problem = smoke_test_model("gemini", "k", "gemini-3.6-flash")
        self.assertIn("квота", problem)

    def test_good_model_passes(self):
        from botcontrol.views import smoke_test_model
        with patch("botcontrol.views.httpx.post") as post:
            post.return_value.raise_for_status.return_value = None
            self.assertEqual(smoke_test_model("gemini", "k", "gemini-3.6-flash"), "")
            self.assertIn("generateContent", post.call_args.args[0])

    def test_openai_model_checked_with_two_turns(self):
        from botcontrol.views import smoke_test_model
        with patch("botcontrol.views.httpx.post") as post:
            post.return_value.raise_for_status.return_value = None
            smoke_test_model("openai", "sk-x", "gpt-4o")
        messages = post.call_args.kwargs["json"]["messages"]
        self.assertEqual(len(messages), 3, "проверять надо именно переписку")
        self.assertEqual(messages[1]["role"], "assistant")

    def test_bad_model_not_saved(self):
        with patch("botcontrol.views.smoke_test_model",
                   return_value="модель отвечает только на одиночные вопросы"):
            response = self.client.post("/bot/keys/", {
                "action": "set_model", "provider": "gemini",
                "model": "antigravity-preview-05-2026"}, follow=True)
        self.key.refresh_from_db()
        self.assertEqual(self.key.model, "gemini-3.6-flash", "прежняя модель осталась")
        self.assertContains(response, "не подошла")

    def test_good_model_saved(self):
        with patch("botcontrol.views.smoke_test_model", return_value=""):
            response = self.client.post("/bot/keys/", {
                "action": "set_model", "provider": "gemini",
                "model": "gemini-3.5-flash"}, follow=True)
        self.key.refresh_from_db()
        self.assertEqual(self.key.model, "gemini-3.5-flash")
        self.assertContains(response, "проверена диалогом")
