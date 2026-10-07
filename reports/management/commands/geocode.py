"""
python manage.py geocode — превратить адреса заявок в точки на карте.

Новые заявки получают точку сами: адрес из переписки уходит в фоновую очередь
(reports.geocoding.schedule_geocode). Эта команда — «догонялка» для всего,
что осталось без точки: заявки, пришедшие, пока сеть была недоступна, или до
того, как автоматика появилась. Берёт заявки без координат, у которых есть
адрес или населённый пункт, и разбирает их тем же геокодером
(reports.geocoding.geocode_ticket): тот же каскад, тот же кэш, та же пауза
между запросами (правило Nominatim — не чаще раза в секунду), поэтому команда
нетороплива. Нераспознанные адреса помечаются и повторно не спрашиваются,
пока адрес не изменится (или не указан --retry-failed).
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from reports import geocoding
# Рамка области и проверка по ней жили здесь раньше; оставлены под старым
# именем — на них могут ссылаться внешние скрипты.
from reports.geocoding import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN, inside_region  # noqa: F401
from tickets.models import Ticket


class Command(BaseCommand):
    help = "Определить координаты заявок по адресам (нужен интернет)"

    def add_arguments(self, parser):
        parser.add_argument("--limit", type=int, default=25,
                            help="Сколько заявок обработать за запуск")
        parser.add_argument("--retry-failed", action="store_true",
                            help="Спросить заново и те, у кого адрес прежде не распознался")

    def _candidates(self, limit: int, retry_failed: bool) -> list:
        """Заявки, про которые сервис действительно стоит спрашивать.

        Отбор в Python, а не в SQL: «адрес изменился после неудачи» — это
        сравнение строки запроса, и считать его должен тот же код, что решает
        в самом геокодере. Иначе нераспознанные заявки забивали бы лимит,
        а команда ничего бы не делала.
        """
        qs = (Ticket.objects.filter(lat__isnull=True)
              .exclude(geo_source__in=geocoding.HUMAN_SOURCES)
              .filter(Q(address__gt="") | Q(settlement__gt=""))
              .select_related("district").order_by("-created_at"))
        picked = []
        for ticket in qs.iterator():
            if geocoding.needs_geocoding(ticket, force=retry_failed):
                picked.append(ticket)
                if len(picked) >= limit:
                    break
        return picked

    def handle(self, *args, **options):
        tickets = self._candidates(options["limit"], options["retry_failed"])
        if not tickets:
            self.stdout.write("Нечего геокодировать.")
            return

        by_address = by_settlement = failed = 0
        for ticket in tickets:
            outcome = geocoding.run_geocoding(ticket, force=options["retry_failed"])
            if outcome == geocoding.ERROR:
                self.stderr.write("Сеть недоступна — попробуй позже.")
                break
            if outcome == geocoding.FOUND:
                if ticket.geo_source == Ticket.GeoSource.ADDRESS:
                    by_address += 1
                else:
                    by_settlement += 1
                self.stdout.write(
                    f"  {ticket.number}: {ticket.get_geo_source_display()} "
                    f"({ticket.lat:.4f}, {ticket.lon:.4f})")
            elif outcome == geocoding.NOT_FOUND:
                failed += 1
                self.stdout.write(f"  {ticket.number}: адрес не распознан "
                                  f"«{geocoding.build_query(ticket)}»")

        self.stdout.write(self.style.SUCCESS(
            f"Найдено координат: {by_address + by_settlement} "
            f"(по адресу: {by_address}, по населённому пункту: {by_settlement}), "
            f"не распознано: {failed}."))
