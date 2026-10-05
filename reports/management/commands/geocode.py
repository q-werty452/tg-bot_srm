"""
python manage.py geocode — превратить адреса заявок в точки на карте.

Использует бесплатный геокодер Nominatim (OpenStreetMap). Правила сервиса:
не чаще одного запроса в секунду — поэтому команда нетороплива и её
запускают руками или по расписанию, а не на каждую заявку.
Нераспознанные адреса помечаются, чтобы не спрашивать про них снова.
"""

import time

import httpx
from django.core.management.base import BaseCommand

from tickets.models import Ticket

# Подсказка геокодеру, где искать: без региона он найдёт улицу где угодно.
# Раньше здесь стоял город Джалал-Абад — годилось, пока бот обслуживал
# только его. Теперь аппарат ведёт всю область, и адреса приходят из
# райцентров и сёл по всей области («Кашка-Терек» и т.п.), а не только
# из областного центра — суффикс и рамку расширили до области целиком.
REGION_SUFFIX = ", Джалал-Абадская область, Кыргызстан"
MARK_FAILED = -1000.0  # lat=MARK_FAILED значит «пробовали, не нашлось»

# Границы Джалал-Абадской области (с запасом). Нужны дважды, и оба раза
# по делу: 1) передаём геокодеру как рамку поиска (viewbox + bounded);
# 2) проверяем результат сами — рамку сервис иногда игнорирует.
# Без этой проверки адрес мог найтись в другой области или в Узбекистане,
# и заявка вставала на карте не там. Пустая карта лучше, чем врущая.
LAT_MIN, LAT_MAX = 40.0, 42.6
LON_MIN, LON_MAX = 70.8, 74.6


def inside_region(lat: float, lon: float) -> bool:
    """Точка попала в границы области?"""
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


class Command(BaseCommand):
    help = "Определить координаты заявок по адресам (нужен интернет)"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25,
                            help="Сколько адресов обработать за запуск")

    def _ask(self, client, address: str) -> list:
        """Спросить геокодер, с одной повторной попыткой.

        Первый запрос иногда обрывается на ровном месте — не разогрет DNS,
        моргнула сеть. Из-за одной такой осечки команда раньше прекращала
        работу и писала «сеть недоступна», хотя со второго раза всё находилось.
        """
        for attempt in (1, 2):
            try:
                response = client.get(
                    "https://nominatim.openstreetmap.org/search",
                    params={
                        "q": address + REGION_SUFFIX,
                        "format": "json", "limit": 1,
                        "countrycodes": "kg",
                        # Рамка поиска: левый-верхний и правый-нижний углы.
                        "viewbox": f"{LON_MIN},{LAT_MAX},{LON_MAX},{LAT_MIN}",
                        "bounded": 1,
                    },
                )
                return response.json() if response.status_code == 200 else []
            except (httpx.HTTPError, ValueError):
                if attempt == 2:
                    raise
                time.sleep(2)
        return []

    def handle(self, *args, **options):
        tickets = (Ticket.objects.filter(lat__isnull=True)
                   .exclude(address="")[:options["limit"]])
        if not tickets:
            self.stdout.write("Нечего геокодировать.")
            return

        found = failed = 0
        with httpx.Client(
            headers={"User-Agent": "manas-crm/1.0 (city hall panel)"},
            timeout=15,
        ) as client:
            for ticket in tickets:
                try:
                    rows = self._ask(client, ticket.address)
                except (httpx.HTTPError, ValueError):
                    self.stderr.write("Сеть недоступна — попробуй позже.")
                    break
                lat = lon = None
                if rows:
                    lat, lon = float(rows[0]["lat"]), float(rows[0]["lon"])
                    if not inside_region(lat, lon):
                        # Нашлось, но не в нашей области — доверять нельзя.
                        self.stdout.write(
                            f"  {ticket.number}: «{ticket.address}» нашлось за "
                            f"пределами области ({lat:.3f}, {lon:.3f}) — пропускаю")
                        lat = lon = None

                if lat is not None:
                    ticket.lat, ticket.lon = lat, lon
                    found += 1
                else:
                    ticket.lat = MARK_FAILED  # больше не спрашиваем
                    ticket.lon = MARK_FAILED
                    failed += 1
                ticket.save(update_fields=["lat", "lon"])
                time.sleep(1.1)  # правило Nominatim: ≤1 запроса в секунду

        self.stdout.write(self.style.SUCCESS(
            f"Найдено координат: {found}, не распознано: {failed}."))
