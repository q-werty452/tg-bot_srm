"""
botcontrol/signals.py — версия конфигурации.

Бот узнаёт об изменениях по номеру версии в ответе /api/v1/config/.
Версию хранит BotSetting и поднимает при каждом своём сохранении;
но конфигурация складывается и из смежных данных — готовых ответов,
контактов, ключей, ботов. Меняются они — версия тоже должна вырасти,
иначе бот не заметит правку. Эти сигналы как раз это и делают.
"""

from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from directory.models import Contact

from .models import BotAccount, BotSetting, ProviderKey, QuickAnswer

_WATCHED = (QuickAnswer, Contact, ProviderKey, BotAccount)


def _bump(sender, **kwargs):
    BotSetting.bump_version()


for model in _WATCHED:
    post_save.connect(_bump, sender=model, dispatch_uid=f"cfg-save-{model.__name__}")
    post_delete.connect(_bump, sender=model, dispatch_uid=f"cfg-del-{model.__name__}")
