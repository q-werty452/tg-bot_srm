"""
tickets/models.py — обращения жителей.

Главные объекты:
  Citizen — житель (кто пишет боту);
  Ticket  — карточка обращения: один открытый диалог = одна карточка;
  Message — реплики переписки (житель / ИИ / сотрудник / система);
  Attachment — фотографии и файлы к реплике;
  Note    — внутренние заметки отдела (житель их не видит);
  Event   — лента изменений карточки (кто что поменял).

«Просрочена» — НЕ статус, а вычисление: срок вышел, а заявка не закрыта.
Так просрочка всегда честная и не требует фоновой перепроверки таблицы.
"""

from datetime import timedelta

from django.conf import settings
from django.db import models, transaction
from django.db.models import Q
from django.utils import timezone


class Channel(models.TextChoices):
    """Канал, из которого пришло обращение."""

    TELEGRAM = "telegram", "Telegram"
    WHATSAPP = "whatsapp", "WhatsApp"


# Запас перед 24-часовым окном ответа Meta: если житель не писал в WhatsApp
# дольше этого срока, свободный ответ уже не уйдёт — только заново открытое
# окно после нового сообщения от него.
WHATSAPP_WINDOW_HOURS = 23


class CitizenQuerySet(models.QuerySet):
    def whatsapp_window_open(self):
        """Кому можно свободно писать в WhatsApp прямо сейчас.

        Telegram этим правилом не ограничен вообще — под условие попадают
        только каналы WhatsApp с недавним входящим сообщением.
        """
        cutoff = timezone.now() - timedelta(hours=WHATSAPP_WINDOW_HOURS)
        return self.filter(Q(channel=Channel.TELEGRAM) | Q(last_inbound_at__gte=cutoff))

    def whatsapp_window_closed(self):
        return self.exclude(pk__in=self.whatsapp_window_open())


class Citizen(models.Model):
    """Житель, писавший боту. Ключ — идентификатор аккаунта Telegram или
    номер телефона в WhatsApp (см. channel)."""

    channel = models.CharField(
        "канал", max_length=16, choices=Channel.choices, default=Channel.TELEGRAM,
    )
    tg_user_id = models.BigIntegerField("Telegram user id", unique=True, null=True, blank=True)
    chat_id = models.BigIntegerField("chat id / номер телефона")
    first_name = models.CharField("имя", max_length=120, blank=True)
    last_name = models.CharField("фамилия", max_length=120, blank=True)
    username = models.CharField("username", max_length=120, blank=True)
    phone = models.CharField("телефон", max_length=40, blank=True)
    district = models.ForeignKey(
        "directory.District", verbose_name="район", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="citizens",
    )
    # Заблокировал ли житель бота (узнаём при неудачной отправке) —
    # таких пропускаем при рассылках.
    is_blocked = models.BooleanField("заблокировал бота", default=False)
    # Согласен ли получать рассылки; /stop в боте снимает флаг.
    subscribed = models.BooleanField("подписан на рассылки", default=True)
    # Время последнего входящего сообщения — на нём строится проверка
    # 24-часового окна ответа WhatsApp.
    last_inbound_at = models.DateTimeField("последнее входящее", null=True, blank=True)
    created_at = models.DateTimeField("впервые написал", auto_now_add=True)

    objects = CitizenQuerySet.as_manager()

    class Meta:
        verbose_name = "житель"
        verbose_name_plural = "жители"
        constraints = [
            models.UniqueConstraint(
                fields=["channel", "chat_id"], name="tickets_citizen_unique_channel_chat_id",
            ),
        ]

    def __str__(self):
        name = f"{self.first_name} {self.last_name}".strip()
        return name or (f"@{self.username}" if self.username else f"id{self.tg_user_id}")


class TicketCounter(models.Model):
    """Счётчик номеров заявок в пределах года: 2026-0001, 2026-0002, …"""

    year = models.PositiveIntegerField(primary_key=True)
    value = models.PositiveIntegerField(default=0)

    @classmethod
    def next_number(cls) -> str:
        """Выдать следующий номер. Потокобезопасно: строка счётчика блокируется."""
        year = timezone.localdate().year
        with transaction.atomic():
            counter, _ = cls.objects.select_for_update().get_or_create(year=year)
            counter.value += 1
            counter.save(update_fields=["value"])
            return f"{year}-{counter.value:04d}"


class TicketQuerySet(models.QuerySet):
    def open(self):
        return self.exclude(status__in=(Ticket.Status.DONE, Ticket.Status.REJECTED))

    def overdue(self):
        """Открытые заявки, у которых срок уже вышел."""
        return self.open().filter(due_at__lt=timezone.now())


class Ticket(models.Model):
    """Карточка обращения."""

    class Status(models.TextChoices):
        NEW = "new", "Новая"
        IN_PROGRESS = "in_progress", "В работе"
        WAITING = "waiting", "Ждёт ответа"
        DONE = "done", "Выполнена"
        REJECTED = "rejected", "Отклонена"

    class AnswerMode(models.TextChoices):
        AI = "ai", "Отвечает ИИ"
        STAFF = "staff", "Отвечает сотрудник"

    number = models.CharField("номер", max_length=12, unique=True, editable=False)
    citizen = models.ForeignKey(
        Citizen, verbose_name="житель", on_delete=models.PROTECT, related_name="tickets",
    )
    title = models.CharField("заголовок", max_length=200, blank=True)
    description = models.TextField("суть обращения", blank=True)
    category = models.ForeignKey(
        "directory.Category", verbose_name="категория", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tickets",
    )
    district = models.ForeignKey(
        "directory.District", verbose_name="район", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tickets",
    )
    address = models.CharField("адрес", max_length=250, blank=True)
    # Координаты для карты; заполняются геокодером, могут быть пустыми.
    lat = models.FloatField("широта", null=True, blank=True)
    lon = models.FloatField("долгота", null=True, blank=True)

    status = models.CharField("статус", max_length=16, choices=Status.choices, default=Status.NEW)
    executor = models.ForeignKey(
        "directory.Executor", verbose_name="исполнитель", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tickets",
    )
    assignee = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="ответственный", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="tickets",
    )
    answer_mode = models.CharField(
        "кто отвечает", max_length=8, choices=AnswerMode.choices, default=AnswerMode.AI,
    )
    channel = models.CharField(
        "канал", max_length=16, choices=Channel.choices, default=Channel.TELEGRAM,
    )
    due_at = models.DateTimeField("срок исполнения", null=True, blank=True)
    unread = models.BooleanField("есть непрочитанное", default=True)

    created_at = models.DateTimeField("создана", auto_now_add=True)
    updated_at = models.DateTimeField("изменена", auto_now=True)
    last_message_at = models.DateTimeField("последнее сообщение", null=True, blank=True)

    objects = TicketQuerySet.as_manager()

    class Meta:
        verbose_name = "обращение"
        verbose_name_plural = "обращения"
        ordering = ["-last_message_at", "-created_at"]
        indexes = [
            models.Index(fields=["status"]),
            models.Index(fields=["-last_message_at"]),
        ]

    OPEN_STATUSES = (Status.NEW, Status.IN_PROGRESS, Status.WAITING)

    def __str__(self):
        return f"#{self.number} {self.title}".strip()

    def save(self, *args, **kwargs):
        if not self.number:
            self.number = TicketCounter.next_number()
        # Срок считаем один раз при создании — по SLA категории.
        if self.due_at is None and self.category_id and self.category.sla_hours:
            self.due_at = timezone.now() + timezone.timedelta(hours=self.category.sla_hours)
        super().save(*args, **kwargs)

    @property
    def is_open(self) -> bool:
        return self.status in self.OPEN_STATUSES

    @property
    def is_overdue(self) -> bool:
        return bool(self.is_open and self.due_at and self.due_at < timezone.now())


class Message(models.Model):
    """Одна реплика переписки."""

    class Author(models.TextChoices):
        CITIZEN = "citizen", "Житель"
        AI = "ai", "ИИ"
        STAFF = "staff", "Сотрудник"
        SYSTEM = "system", "Система"

    class Rating(models.TextChoices):
        UP = "up", "Помогло"
        DOWN = "down", "Не помогло"

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="messages")
    author = models.CharField("автор", max_length=8, choices=Author.choices)
    staff_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, verbose_name="сотрудник", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="sent_messages",
    )
    text = models.TextField("текст", blank=True)
    tg_message_id = models.BigIntegerField("id сообщения в Telegram", null=True, blank=True)
    # Оценка жителя под ответом ИИ: помогло / не помогло.
    rating = models.CharField(
        "оценка", max_length=4, choices=Rating.choices, null=True, blank=True,
    )
    created_at = models.DateTimeField("время", auto_now_add=True)

    class Meta:
        verbose_name = "сообщение"
        verbose_name_plural = "сообщения"
        ordering = ["created_at", "id"]

    def __str__(self):
        return f"{self.get_author_display()}: {self.text[:40]}"


def attachment_path(instance, filename: str) -> str:
    """Файлы раскладываются по заявкам: media/tickets/2026-0587/имя."""
    return f"tickets/{instance.message.ticket.number}/{filename}"


class Attachment(models.Model):
    """Фото или документ, присланные жителем (или отправленные сотрудником)."""

    message = models.ForeignKey(Message, on_delete=models.CASCADE, related_name="attachments")
    file = models.FileField("файл", upload_to=attachment_path)
    tg_file_id = models.CharField("file_id в Telegram", max_length=200, blank=True)
    mime = models.CharField("тип", max_length=100, blank=True)
    size = models.PositiveIntegerField("размер, байт", default=0)

    class Meta:
        verbose_name = "вложение"
        verbose_name_plural = "вложения"

    def __str__(self):
        return self.file.name

    @property
    def is_image(self) -> bool:
        return self.mime.startswith("image/")


class Note(models.Model):
    """Внутренняя заметка отдела. Жителю не отправляется."""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="notes")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.SET_NULL, related_name="notes",
    )
    text = models.TextField("текст")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "заметка"
        verbose_name_plural = "заметки"
        ordering = ["created_at"]


class Event(models.Model):
    """Запись ленты изменений: смена статуса, назначение, перехват разговора…"""

    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="events")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL,
    )
    kind = models.CharField("что произошло", max_length=40)
    payload = models.JSONField("детали", default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        verbose_name = "событие"
        verbose_name_plural = "события"
        ordering = ["created_at"]
