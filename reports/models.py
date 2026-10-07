"""
reports/models.py — кэш геокодера.

Бесплатный Nominatim (OpenStreetMap) требует кэшировать ответы: один и тот
же «Кашка-Терек, Базар-Коргонский район…» не должен уходить в сервис
по разу на каждую заявку. Ключ — точная строка запроса, как её получил
сервис; так кэш не зависит от того, из какой заявки пришёл вопрос.
"""

from django.db import models
from django.utils import timezone


class GeocodeCache(models.Model):
    """Ответ геокодера на одну строку запроса (в том числе «ничего не нашлось»)."""

    query = models.CharField("строка запроса", max_length=500, unique=True)
    # found=False — сервис ответил, но места не знает; lat/lon тогда пустые.
    found = models.BooleanField("найдено", default=False)
    lat = models.FloatField("широта", null=True, blank=True)
    lon = models.FloatField("долгота", null=True, blank=True)
    # place_rank Nominatim: 26 и выше — улица или дом, ниже — только
    # населённый пункт или район. По нему видно, насколько точка точная.
    rank = models.PositiveSmallIntegerField("ранг места", null=True, blank=True)
    # Не auto_now_add: при повторном вопросе (истёк срок «не нашлось») время
    # обновляем руками, а auto_now_add такого не позволяет.
    created_at = models.DateTimeField("когда спросили", default=timezone.now)

    class Meta:
        verbose_name = "ответ геокодера"
        verbose_name_plural = "кэш геокодера"
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.query} → {'есть' if self.found else 'нет'}"
