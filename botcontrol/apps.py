from django.apps import AppConfig


class BotcontrolConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "botcontrol"
    verbose_name = "Управление ботом"

    def ready(self):
        from . import signals  # noqa: F401  подключение сигналов версии
