"""
botcontrol/models.py — всё, чем панель управляет ботом.

  BotAccount  — токены Telegram-ботов (шифрованные), активный один;
  ProviderKey — ключи ИИ-провайдеров (шифрованные) и выбранная модель;
  BotSetting  — единственная запись с текстами и выключателем;
  QuickAnswer — готовые ответы, отдаются без обращения к модели;
  Broadcast   — рассылки жителям;
  Outbox      — очередь всего, что бот должен отправить в Telegram;
  Heartbeat   — последние сигналы «я жив» от бота.

Секреты в интерфейсе никогда не показываются целиком — только последние
4 символа (поле tail4). Ввод работает как «заменить значение».
"""

from django.conf import settings as dj_settings
from django.db import models

from .crypto import decrypt, encrypt


class SecretFieldMixin(models.Model):
    """Общее для моделей с шифрованным секретом."""

    secret_encrypted = models.TextField("секрет (шифр)", editable=False)
    tail4 = models.CharField("последние 4 символа", max_length=4, editable=False, default="")

    class Meta:
        abstract = True

    def set_secret(self, value: str) -> None:
        value = value.strip()
        self.secret_encrypted = encrypt(value)
        self.tail4 = value[-4:]

    def get_secret(self) -> str:
        return decrypt(self.secret_encrypted)

    @property
    def masked(self) -> str:
        return f"…{self.tail4}" if self.tail4 else "не задан"


class BotAccount(SecretFieldMixin):
    """Telegram-бот. Токен шифрован; активный бот — один."""

    name = models.CharField("название", max_length=100)
    username = models.CharField("@username", max_length=100, blank=True)
    is_active = models.BooleanField("активный", default=False)
    checked_at = models.DateTimeField("проверен", null=True, blank=True)
    check_ok = models.BooleanField("проверка прошла", null=True, blank=True)

    class Meta:
        verbose_name = "Telegram-бот"
        verbose_name_plural = "Telegram-боты"

    def __str__(self):
        return f"{self.name} (@{self.username})" if self.username else self.name

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        # Активный бот один: включили этого — выключаем остальных.
        if self.is_active:
            BotAccount.objects.exclude(pk=self.pk).update(is_active=False)


class ProviderKey(SecretFieldMixin):
    """Ключ ИИ-провайдера и выбранная модель."""

    class Provider(models.TextChoices):
        OPENAI = "openai", "GPT (OpenAI)"
        GEMINI = "gemini", "Gemini (Google)"
        CLAUDE = "claude", "Claude (Anthropic)"
        OPENROUTER = "openrouter", "OpenRouter"

    provider = models.CharField(
        "провайдер", max_length=16, choices=Provider.choices, unique=True,
    )
    model = models.CharField("модель", max_length=100, blank=True)
    is_active = models.BooleanField("используется", default=True)
    last_check_ok = models.BooleanField("проверка прошла", null=True, blank=True)
    last_check_at = models.DateTimeField("проверен", null=True, blank=True)

    class Meta:
        verbose_name = "ключ провайдера"
        verbose_name_plural = "ключи провайдеров"

    def __str__(self):
        return f"{self.get_provider_display()} {self.masked}"


class BotSetting(models.Model):
    """
    Единственная запись с настройками поведения бота.

    version увеличивается при каждом сохранении — по нему бот понимает,
    что конфигурация изменилась, не сравнивая тексты целиком.
    """

    prompt_detailed = models.TextField(
        "промпт развёрнутого режима", blank=True,
        help_text="Пусто — бот использует свой встроенный текст",
    )
    prompt_chat = models.TextField("промпт диалогового режима", blank=True)
    invent_facts = models.BooleanField(
        "разрешать додумывать факты", default=True,
        help_text="Режим показа. Для настоящих обращений выключить: "
                  "бот будет опираться только на справочник контактов",
    )
    enabled = models.BooleanField("бот включён", default=True)
    maintenance_text = models.CharField(
        "текст при выключенном боте", max_length=300,
        default="Бот временно недоступен: идут технические работы. "
                "Пожалуйста, напишите позже.",
    )
    default_provider = models.CharField(
        "провайдер по умолчанию", max_length=16,
        choices=ProviderKey.Provider.choices, default=ProviderKey.Provider.OPENAI,
    )
    version = models.PositiveIntegerField("версия", default=1, editable=False)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "настройки бота"
        verbose_name_plural = "настройки бота"

    def __str__(self):
        return f"Настройки (версия {self.version})"

    def save(self, *args, **kwargs):
        self.pk = 1  # запись всегда одна
        self.version = (self.version or 0) + 1
        super().save(*args, **kwargs)

    @classmethod
    def get(cls) -> "BotSetting":
        obj = cls.objects.first()
        if obj is None:
            obj = cls()
            obj.save()
        return obj

    @classmethod
    def bump_version(cls) -> None:
        """Поднять версию, когда изменились смежные данные (контакты, ответы)."""
        cls.get().save()


class QuickAnswer(models.Model):
    """Готовый ответ: совпала фраза — бот отвечает дословно, модель не зовётся."""

    triggers = models.TextField(
        "фразы-триггеры",
        help_text="По одной на строку. Совпадение без учёта регистра, по вхождению",
    )
    answer = models.TextField("ответ")
    is_active = models.BooleanField("действует", default=True)
    hits = models.PositiveIntegerField("срабатываний", default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "готовый ответ"
        verbose_name_plural = "готовые ответы"

    def __str__(self):
        first = self.triggers.strip().splitlines()[0] if self.triggers.strip() else "?"
        return first

    def trigger_list(self) -> list[str]:
        return [t.strip().lower() for t in self.triggers.splitlines() if t.strip()]


class Broadcast(models.Model):
    """Рассылка жителям."""

    class Audience(models.TextChoices):
        ALL = "all", "Все, кто писал боту"
        DISTRICT = "district", "Жители района"
        CATEGORY = "category", "Обращавшиеся по категории"

    class Status(models.TextChoices):
        DRAFT = "draft", "Черновик"
        QUEUED = "queued", "В очереди"
        SENDING = "sending", "Отправляется"
        DONE = "done", "Завершена"

    text = models.TextField("текст")
    audience = models.CharField("кому", max_length=16, choices=Audience.choices,
                                default=Audience.ALL)
    district = models.ForeignKey("directory.District", null=True, blank=True,
                                 on_delete=models.SET_NULL, verbose_name="район")
    category = models.ForeignKey("directory.Category", null=True, blank=True,
                                 on_delete=models.SET_NULL, verbose_name="категория")
    status = models.CharField("состояние", max_length=16, choices=Status.choices,
                              default=Status.DRAFT)
    total = models.PositiveIntegerField("получателей", default=0)
    sent = models.PositiveIntegerField("отправлено", default=0)
    failed = models.PositiveIntegerField("не доставлено", default=0)
    created_by = models.ForeignKey(dj_settings.AUTH_USER_MODEL, null=True,
                                   on_delete=models.SET_NULL, verbose_name="кто создал")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "рассылка"
        verbose_name_plural = "рассылки"
        ordering = ["-created_at"]

    def __str__(self):
        return self.text[:50]


class Outbox(models.Model):
    """
    Очередь исходящих: всё, что бот должен отправить в Telegram.

    Панель кладёт сюда строки, бот забирает пачками, отправляет и помечает.
    Так ответы сотрудников и рассылки переживают перезапуск любой из сторон.
    """

    class Kind(models.TextChoices):
        REPLY = "reply", "Ответ жителю"
        BROADCAST = "broadcast", "Рассылка"
        NOTIFY = "notify", "Уведомление сотрудникам"

    class Status(models.TextChoices):
        PENDING = "pending", "Ожидает"
        SENT = "sent", "Отправлено"
        FAILED = "failed", "Не доставлено"

    chat_id = models.BigIntegerField("chat id получателя")
    text = models.TextField("текст")
    kind = models.CharField("вид", max_length=16, choices=Kind.choices)
    ticket = models.ForeignKey("tickets.Ticket", null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="outbox")
    broadcast = models.ForeignKey(Broadcast, null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="outbox")
    status = models.CharField("состояние", max_length=16, choices=Status.choices,
                              default=Status.PENDING)
    attempts = models.PositiveIntegerField("попыток", default=0)
    error = models.CharField("ошибка", max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    sent_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        verbose_name = "исходящее"
        verbose_name_plural = "исходящие"
        ordering = ["created_at"]
        indexes = [models.Index(fields=["status", "created_at"])]

    def __str__(self):
        return f"{self.get_kind_display()} -> {self.chat_id}"


class Heartbeat(models.Model):
    """Сигнал «я жив» от бота: раз в минуту, хранится последняя сотня."""

    created_at = models.DateTimeField(auto_now_add=True)
    providers = models.JSONField("состояние провайдеров", default=dict)
    counters = models.JSONField("счётчики", default=dict)

    class Meta:
        verbose_name = "сердцебиение"
        verbose_name_plural = "сердцебиения"
        ordering = ["-created_at"]
        get_latest_by = "created_at"
