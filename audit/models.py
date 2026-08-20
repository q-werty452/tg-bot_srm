"""
audit/models.py — журнал действий сотрудников.

Пишется всё существенное: вход, смена настроек и ключей, закрытие заявок,
рассылки. Значения секретов в журнал не попадают никогда.
"""

from django.conf import settings
from django.db import models


class Entry(models.Model):
    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                             on_delete=models.SET_NULL, verbose_name="кто")
    action = models.CharField("действие", max_length=60)
    obj = models.CharField("объект", max_length=120, blank=True)
    summary = models.CharField("что именно", max_length=300, blank=True)
    created_at = models.DateTimeField("когда", auto_now_add=True)

    class Meta:
        verbose_name = "запись журнала"
        verbose_name_plural = "журнал действий"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.user}: {self.action}"


def log(user, action: str, obj: str = "", summary: str = "") -> None:
    """Короткий помощник: audit.models.log(request.user, 'ключ заменён', 'openai')."""
    Entry.objects.create(
        user=user if getattr(user, "is_authenticated", False) else None,
        action=action, obj=obj, summary=summary[:300],
    )
