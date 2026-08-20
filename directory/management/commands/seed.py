"""
python manage.py seed — начальные данные для запуска и показа.

Команду можно гонять сколько угодно: существующие записи не трогаются
(ищем по slug/названию, создаём только недостающее). Реальные списки
категорий, районов и предприятий мэрия потом правит в панели.
"""

from django.core.management.base import BaseCommand

from botcontrol.models import BotSetting
from directory.models import Category, District, Executor

EXECUTORS = [
    ("МП «МанасЖолБашкарма»", "МанасЖолБашкарма"),   # дороги
    ("МП «Тазалык»", "Тазалык"),                     # мусор, чистота
    ("МП «Жашыл»", "Жашыл"),                          # озеленение
    ("МП «Курулуш»", "Курулуш"),                      # строительство
    ("МП «Коомдук транспорт»", "Коомдук"),            # транспорт
    ("Аппарат мэрии", "Мэрия"),
]

# (название, slug, иконка, цвет чипа, исполнитель, срок в часах)
CATEGORIES = [
    ("ЖКХ", "zhkh", "home", "#5B7DB1", "Мэрия", 72),
    ("Вода и свет", "utilities", "drop", "#3E8FB0", "Мэрия", 48),
    ("Дороги и транспорт", "roads", "road", "#8A6FB1", "МанасЖолБашкарма", 120),
    ("Благоустройство и мусор", "cleanup", "leaf", "#4F9E7E", "Тазалык", 72),
    ("Справки и документы", "docs", "doc", "#B08A4F", "Мэрия", 24),
    ("Земля и строительство", "build", "build", "#B06A5B", "Курулуш", 168),
    ("Социальные вопросы", "social", "people", "#7E7E9E", "Мэрия", 72),
    ("Прочее", "other", "dots", "#8B96A8", "Мэрия", 72),
]

DISTRICTS = ["Центр", "Таш-Булак", "Спутник", "Курманбек", "Тоолос"]


class Command(BaseCommand):
    help = "Создать начальные справочники (категории, районы, исполнители)"

    def handle(self, *args, **options):
        created = 0
        executors = {}
        for name, short in EXECUTORS:
            obj, was_created = Executor.objects.get_or_create(
                name=name, defaults={"short_name": short})
            executors[short] = obj
            created += was_created

        for name, slug, icon, color, executor, sla in CATEGORIES:
            _, was_created = Category.objects.get_or_create(
                slug=slug,
                defaults={
                    "name": name, "icon": icon, "color": color,
                    "default_executor": executors.get(executor),
                    "sla_hours": sla,
                },
            )
            created += was_created

        for i, name in enumerate(DISTRICTS, start=1):
            _, was_created = District.objects.get_or_create(
                name=name, defaults={"slug": f"d{i:02d}"})
            created += was_created

        BotSetting.get()  # завести запись настроек, если её ещё нет
        self.stdout.write(self.style.SUCCESS(
            f"Готово. Новых записей: {created}. Повторный запуск ничего не ломает."
        ))
