"""
directory/models.py — справочники.

Всё, что сотрудники ведут сами и на что ссылаются обращения:
категории, районы города, исполнители (муниципальные предприятия)
и контакты (телефоны отделов, горячие линии) — из контактов бот
собирает справочную часть своих ответов.
"""

from django.db import models


class Executor(models.Model):
    """Исполнитель — муниципальное предприятие или отдел мэрии."""

    name = models.CharField("полное название", max_length=200)
    short_name = models.CharField("короткое название", max_length=80, blank=True)
    is_active = models.BooleanField("действует", default=True)

    class Meta:
        verbose_name = "исполнитель"
        verbose_name_plural = "исполнители"
        ordering = ["name"]

    def __str__(self):
        return self.short_name or self.name


class Category(models.Model):
    """Категория обращения: по ней фильтруют список и считают срок."""

    name = models.CharField("название", max_length=100)
    slug = models.SlugField("код", unique=True)
    icon = models.CharField(
        "иконка", max_length=40, blank=True,
        help_text="Имя иконки из static/icons.svg, например road или water",
    )
    color = models.CharField(
        "цвет чипа", max_length=7, default="#5B7DB1",
        help_text="HEX, используется только фоном чипа в списке",
    )
    default_executor = models.ForeignKey(
        Executor, verbose_name="исполнитель по умолчанию",
        null=True, blank=True, on_delete=models.SET_NULL,
    )
    sla_hours = models.PositiveIntegerField(
        "срок исполнения, часов", default=72,
        help_text="Через сколько часов открытая заявка считается просроченной",
    )
    is_active = models.BooleanField("действует", default=True)
    order = models.PositiveIntegerField("порядок", default=100)

    class Meta:
        verbose_name = "категория"
        verbose_name_plural = "категории"
        ordering = ["order", "name"]

    def __str__(self):
        return self.name


class District(models.Model):
    """Район / микрорайон города."""

    name = models.CharField("название", max_length=100)
    slug = models.SlugField("код", unique=True)
    is_active = models.BooleanField("действует", default=True)

    class Meta:
        verbose_name = "район"
        verbose_name_plural = "районы"
        ordering = ["name"]

    def __str__(self):
        return self.name


class Contact(models.Model):
    """
    Контакт для справочных ответов бота: куда обратиться, кому позвонить.

    is_public=True означает «бот может называть это жителям».
    """

    class Kind(models.TextChoices):
        OFFICE = "office", "Отдел / учреждение"
        PERSON = "person", "Сотрудник"
        HOTLINE = "hotline", "Горячая линия"

    kind = models.CharField("тип", max_length=16, choices=Kind.choices, default=Kind.OFFICE)
    title = models.CharField("название или должность", max_length=200)
    phone = models.CharField("телефон", max_length=100, blank=True)
    address = models.CharField("адрес", max_length=200, blank=True)
    work_hours = models.CharField("часы работы", max_length=200, blank=True)
    comment = models.CharField("примечание", max_length=300, blank=True)
    category = models.ForeignKey(
        Category, verbose_name="категория", null=True, blank=True,
        on_delete=models.SET_NULL, related_name="contacts",
        help_text="Если задана, бот упоминает контакт в ответах на эту тему",
    )
    is_public = models.BooleanField("бот может называть жителям", default=True)

    class Meta:
        verbose_name = "контакт"
        verbose_name_plural = "контакты"
        ordering = ["kind", "title"]

    def __str__(self):
        return f"{self.title} ({self.phone})" if self.phone else self.title
