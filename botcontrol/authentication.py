"""
botcontrol/authentication.py — как Telegram-бот подтверждает, что это он.

Бот присылает заголовок X-Bot-Token со служебным токеном из .env панели
(BOT_API_TOKEN). Сравнение — через secrets.compare_digest, чтобы длину
и содержимое токена нельзя было подобрать по времени ответа.

Это НЕ пользовательская авторизация: у ручек бота нет человека, нет прав
и нет сессии. Поэтому отдельный «принципал» BotPrincipal и отдельное
разрешение IsBot — ручки бота недоступны сотрудникам, а страницы
сотрудников недоступны боту.
"""

import secrets

from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.permissions import BasePermission


class BotPrincipal:
    """Минимальный «пользователь» для запросов бота."""

    is_authenticated = True
    is_anonymous = False
    is_active = True

    def __str__(self):
        return "telegram-bot"


class BotTokenAuthentication(BaseAuthentication):
    def authenticate(self, request):
        token = request.headers.get("X-Bot-Token", "")
        if not token:
            return None  # пусть отработают другие способы (и IsBot откажет)
        expected = settings.BOT_API_TOKEN
        if not expected:
            raise AuthenticationFailed(
                "В .env панели не задан BOT_API_TOKEN — доступ бота отключён."
            )
        if not secrets.compare_digest(token, expected):
            raise AuthenticationFailed("Неверный служебный токен бота.")
        return (BotPrincipal(), "bot")


class IsBot(BasePermission):
    """Пускает только запросы, прошедшие BotTokenAuthentication."""

    message = "Эта ручка доступна только Telegram-боту."

    def has_permission(self, request, view):
        return request.auth == "bot"
