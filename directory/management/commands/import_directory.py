"""
python manage.py import_directory <папка runtime_export> — справочники области.

Что делает:
  1. Заводит районы и города областного значения Джалал-Абадской области
     (по slug), а прежние микрорайоны города d01–d05 выключает: на них
     ссылаются старые заявки, поэтому не удаляем.
  2. Загружает исполнителей из organizations.jsonl — только публичные
     (visibility == PUBLIC). Ключ — id организации из справочника бота
     (external_id). Исполнителей, заведённых вручную, не трогает.

Команду можно гонять сколько угодно раз: повторный запуск ничего не
дублирует. Файлы справочника — недоверенные данные, их только читаем как
JSON, ничего оттуда не исполняем.
"""

import json
from pathlib import Path

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from directory.models import District, Executor

# (slug, название для показа, вид)
DISTRICTS = [
    ("aksy", "Аксыйский район", District.Kind.DISTRICT),
    ("ala-buka", "Ала-Букинский район", District.Kind.DISTRICT),
    ("bazar-korgon", "Базар-Коргонский район", District.Kind.DISTRICT),
    ("nooken", "Ноокенский район", District.Kind.DISTRICT),
    ("suzak", "Сузакский район", District.Kind.DISTRICT),
    ("toguz-toro", "Тогуз-Тороуский район", District.Kind.DISTRICT),
    ("toktogul", "Токтогульский район", District.Kind.DISTRICT),
    ("chatkal", "Чаткальский район", District.Kind.DISTRICT),
    ("manas", "г. Манас (Жалал-Абад)", District.Kind.CITY),
    ("kara-kol", "г. Кара-Куль", District.Kind.CITY),
    ("mailuu-suu", "г. Майлуу-Суу", District.Kind.CITY),
    ("tash-komur", "г. Таш-Кумыр", District.Kind.CITY),
]

# Прежние микрорайоны города (см. seed) — выключаем, не удаляем.
OLD_DISTRICT_SLUGS = ["d01", "d02", "d03", "d04", "d05"]

# Значения территории, которые парсер справочника выдал по ошибке.
JUNK_TERRITORY_VALUES = {"гак"}


def readable_territory(territory: list) -> str:
    """Первая осмысленная территория организации в читаемом виде.

    «district:Сузак» → «Сузак (район)», «city:Манас» → «г. Манас»,
    «ayyl_area:X» → «а/о X», «village:X» → «с. X», «region:…» → «область».
    """
    for raw in territory or []:
        if not isinstance(raw, str) or ":" not in raw:
            continue
        kind, _, value = raw.partition(":")
        kind, value = kind.strip(), value.strip()
        if not value or value.lower() in JUNK_TERRITORY_VALUES:
            continue
        if kind == "district":
            return f"{value} (район)"
        if kind == "city":
            return f"г. {value}"
        if kind == "ayyl_area":
            return f"а/о {value}"
        if kind == "village":
            return f"с. {value}"
        if kind == "region":
            return "область"
    return ""


class Command(BaseCommand):
    help = "Загрузить районы области и исполнителей из runtime_export (идемпотентно)"

    def add_arguments(self, parser):
        parser.add_argument("path", help="Папка runtime_export с organizations.jsonl")

    def handle(self, *args, path, **options):
        source = Path(path) / "organizations.jsonl"
        if not source.is_file():
            raise CommandError(f"Не найден файл {source}")
        rows = self._read_rows(source)

        with transaction.atomic():
            d_created, d_updated, d_disabled = self._import_districts()
            e_created, e_updated, e_skipped = self._import_executors(rows)

        self.stdout.write(self.style.SUCCESS(
            f"Районы: создано {d_created}, обновлено {d_updated}, "
            f"выключено старых микрорайонов {d_disabled}.\n"
            f"Исполнители: создано {e_created}, обновлено {e_updated}, "
            f"пропущено {e_skipped} (не публичные или без id/названия)."
        ))

    @staticmethod
    def _read_rows(source: Path) -> list:
        rows = []
        with source.open(encoding="utf-8") as fh:
            for number, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except ValueError as exc:
                    raise CommandError(f"{source.name}, строка {number}: {exc}")
                if isinstance(row, dict):
                    rows.append(row)
        return rows

    @staticmethod
    def _import_districts():
        created = updated = 0
        for slug, name, kind in DISTRICTS:
            district, was_created = District.objects.get_or_create(
                slug=slug, defaults={"name": name, "kind": kind, "is_active": True})
            if was_created:
                created += 1
            elif (district.name, district.kind) != (name, kind):
                # is_active не трогаем: если сотрудник выключил район, так и оставим.
                district.name, district.kind = name, kind
                district.save(update_fields=["name", "kind"])
                updated += 1
        disabled = District.objects.filter(
            slug__in=OLD_DISTRICT_SLUGS, is_active=True).update(is_active=False)
        return created, updated, disabled

    @staticmethod
    def _import_executors(rows: list):
        created = updated = skipped = 0
        name_max = Executor._meta.get_field("name").max_length
        short_max = Executor._meta.get_field("short_name").max_length
        id_max = Executor._meta.get_field("external_id").max_length
        territory_max = Executor._meta.get_field("territory").max_length

        for row in rows:
            external_id = str(row.get("organization_id") or "").strip()
            name = str(row.get("name_ru") or row.get("name_ky") or "").strip()
            if row.get("visibility") != "PUBLIC" or not external_id or not name \
                    or len(external_id) > id_max:
                skipped += 1
                continue
            # Короткое название — первый алиас, но только целиком: обрезанный
            # на середине слова алиас в выпадающем списке хуже, чем полное имя.
            aliases = [str(a).strip() for a in (row.get("aliases_ru") or []) if a]
            short_name = next((a for a in aliases if len(a) <= short_max), "")
            values = {
                "name": name[:name_max],
                "short_name": short_name,
                "territory": readable_territory(row.get("territory"))[:territory_max],
            }
            executor, was_created = Executor.objects.get_or_create(
                external_id=external_id, defaults=values)
            if was_created:
                created += 1
                continue
            changed = [f for f, v in values.items() if getattr(executor, f) != v]
            if changed:
                for f in changed:
                    setattr(executor, f, values[f])
                # is_active не трогаем — его ведут сотрудники.
                executor.save(update_fields=changed)
                updated += 1
        return created, updated, skipped
