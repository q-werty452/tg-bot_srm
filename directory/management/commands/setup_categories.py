"""
python manage.py setup_categories — темы обращений для всей области.

Прежний список (ЖКХ, «Вода и свет», МП мэрии по умолчанию) был городским.
Новый — по разделам из документов администрации области (Ноокен, Сузак,
Токтогул): питьевая и поливная вода, электричество и освещение, мусор,
дороги, земля, строительство, соцпомощь, медицина, образование,
правопорядок, сельское хозяйство, документы, жалобы на должностных лиц.

Идемпотентно: создаёт недостающие, обновляет названия и порядок у
существующих (по slug), id не меняются — старые заявки остаются при своих
темах. «Вода и свет» (utilities) становится «Водой», свет — отдельной темой.

--clear-default-executors — убрать исполнителей «по умолчанию» у тем:
для области они неверны (дороги села в Ноокене — не МП мэрии Манаса);
исполнителя по территории подбирает бот из справочника организаций.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from directory.models import Category

# slug, название, цвет чипа, срок в часах
CATEGORIES = [
    ("water", "Вода: питьевая и поливная", "#3E8FB0", 48),
    ("power", "Электроснабжение и уличное освещение", "#C9A227", 24),
    ("zhkh", "ЖКХ: канализация, отопление, газ", "#5B7DB1", 72),
    ("cleanup", "Мусор, санитария, благоустройство", "#4F9E7E", 72),
    ("roads", "Дороги, мосты и транспорт", "#8A6FB1", 120),
    ("land", "Земля и кадастр", "#9C7B4F", 168),
    ("build", "Строительство и архитектура", "#B06A5B", 168),
    ("social", "Социальная помощь, пособия, занятость", "#7E7E9E", 72),
    ("health", "Здравоохранение", "#D0506A", 48),
    ("education", "Образование: школы и детсады", "#4C8BD6", 72),
    ("safety", "Правопорядок и безопасность", "#2F4A7A", 24),
    ("agro", "Сельское хозяйство и ветеринария", "#6E9E3E", 120),
    ("docs", "Документы и госуслуги", "#B08A4F", 24),
    ("officials", "Жалобы на должностных лиц", "#A33A3A", 72),
    ("other", "Прочее", "#8B96A8", 72),
]


class Command(BaseCommand):
    help = "Темы обращений для всей Джалал-Абадской области"

    def add_arguments(self, parser):
        parser.add_argument("--clear-default-executors", action="store_true")
        parser.add_argument("--deactivate", nargs="*", default=[],
                            help="slug тем, которые выключить (не удаляются)")

    @transaction.atomic
    def handle(self, *args, **opts):
        if (Category.objects.filter(slug="utilities").exists()
                and not Category.objects.filter(slug="water").exists()):
            Category.objects.filter(slug="utilities").update(slug="water")
        created = updated = 0
        for order, (slug, name, color, sla) in enumerate(CATEGORIES, start=1):
            cat, was_created = Category.objects.get_or_create(
                slug=slug, defaults={"name": name, "color": color,
                                     "sla_hours": sla, "order": order * 10})
            if was_created:
                created += 1
                continue
            cat.name, cat.order, cat.is_active = name, order * 10, True
            if not cat.color:
                cat.color = color
            cat.save()
            updated += 1
        if opts["clear_default_executors"]:
            Category.objects.update(default_executor=None)
        off = Category.objects.filter(slug__in=opts["deactivate"]).update(is_active=False)
        self.stdout.write(self.style.SUCCESS(
            f"Тем создано: {created}, обновлено: {updated}, выключено: {off}"))
