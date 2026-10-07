"""
reports/geocoding.py — адрес заявки -> точка на карте.

Зачем. Раньше координаты ставила только ручная команда `manage.py geocode`,
и заявка попадала на карту лишь после чьего-то запуска. Теперь адрес, который
житель написал в чате, становится точкой сам: панель ставит заявку в очередь,
фоновый поток спрашивает бесплатный геокодер Nominatim (OpenStreetMap)
и записывает результат в заявку.

Откуда может взяться точка (Ticket.geo_source):
  address     — нашли по адресу (улица/дом);
  settlement  — адрес не нашёлся или слишком расплывчат, взяли центр
                населённого пункта: на карте такая точка «примерная»;
  pin         — житель прислал геометку из мессенджера;
  manual      — поставил сотрудник;
  failed      — ни адрес, ни населённый пункт не распознаны.
Точку, поставленную человеком (pin, manual), автоматика не трогает никогда:
человек лучше знает, где это.

Правила Nominatim, которые тут соблюдены: осмысленный User-Agent, не чаще
одного запроса в секунду, кэш ответов (GeocodeCache), результат проверяется
самим сервисом — рамка области — и нами — по контуру области.
"""

import json
import logging
import math
import queue
import threading
import time
from datetime import timedelta
from functools import lru_cache
from typing import NamedTuple

import httpx
from django.conf import settings
from django.db import connections, transaction
from django.utils import timezone

from reports.models import GeocodeCache
from tickets.models import Ticket

logger = logging.getLogger(__name__)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
# Политика Nominatim: User-Agent обязателен и должен называть приложение.
USER_AGENT = "manas-crm/1.0 (Jalal-Abad oblast appeals panel)"
REQUEST_TIMEOUT = 8.0   # секунд на один запрос
MIN_INTERVAL = 1.1      # не чаще ~1 запроса в секунду на процесс
RETRY_PAUSE = 2.0       # пауза перед повторной попыткой
# «Ничего не нашлось» помним месяц: карта OSM пополняется, а спрашивать
# одно и то же каждый день незачем. Найденное помним бессрочно.
NEGATIVE_TTL = timedelta(days=30)

# Подсказка геокодеру, где искать: без региона он найдёт улицу где угодно.
REGION_SUFFIX = "Джалал-Абадская область, Кыргызстан"

# Рамка Джалал-Абадской области (с небольшим запасом). Нужна дважды, и оба
# раза по делу: 1) передаём геокодеру как область поиска (viewbox + bounded);
# 2) быстро отсекаем заведомо чужие ответы до точной проверки по контуру.
# Границы взяты по контуру области из OSM: он тянется от 70.17° до 74.74° по
# долготе (западный край Чаткальского и восточный Тогуз-Тороуского районов)
# и от 40.80° до 42.22° по широте. Прежняя рамка (70.8–74.6) обрезала эти края,
# и заявки оттуда не могли встать на карту.
LAT_MIN, LAT_MAX = 40.7, 42.3
LON_MIN, LON_MAX = 70.1, 74.85

# Допуск при проверке по контуру: у границы OSM и Nominatim могут расходиться
# на сотни метров, а посёлок на самой границе не должен пропадать с карты.
BORDER_TOLERANCE_KM = 2.0

# place_rank Nominatim: 30 — дом, 26 — улица, 24 — жилой массив/участок,
# 20 и ниже — посёлок, село, город, район. Всё, что грубее участка,
# честнее считать «примерной» точкой, даже если искали по адресу.
PRECISE_RANK = 24

HUMAN_SOURCES = (Ticket.GeoSource.PIN, Ticket.GeoSource.MANUAL)

# Что вернула попытка геокодирования.
FOUND = "found"          # точка записана
NOT_FOUND = "not_found"  # сервис ответил, но места не знает
SKIPPED = "skipped"      # делать нечего: точка человека, пустой адрес, запрос не менялся
ERROR = "error"          # сети нет или сервис отказал — попробуем позже


class GeocodeError(Exception):
    """Сервис недоступен или ответил не так, как должен. Не «не нашлось»!"""


class Place(NamedTuple):
    lat: float
    lon: float
    rank: int | None


# ------------------------------------------------------------------ рамка и контур области

def inside_region(lat: float, lon: float) -> bool:
    """Точка в прямоугольной рамке области? Быстрая грубая проверка."""
    return LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX


@lru_cache(maxsize=1)
def _region_polygons():
    """Контур области из static/geo: [(внешнее кольцо, [дыры]), ...] или None.

    Читаем тот же файл, что рисует карта, — так «внутри области» для карты
    и для геокодера означает одно и то же.
    """
    path = settings.BASE_DIR / "static" / "geo" / "jalalabad_region.geojson"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        geometry = data["features"][0]["geometry"]
        polygons = ([geometry["coordinates"]] if geometry["type"] == "Polygon"
                    else geometry["coordinates"])
        return [(poly[0], poly[1:]) for poly in polygons]
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        logger.warning("Контур области не прочитан (%s): проверяю только по рамке", path)
        return None


def _in_ring(lat: float, lon: float, ring) -> bool:
    """Лучевой тест: точка внутри кольца [[lon, lat], ...]?"""
    inside = False
    j = len(ring) - 1
    for i, (xi, yi) in enumerate(ring):
        xj, yj = ring[j]
        if (yi > lat) != (yj > lat) and lon < (xj - xi) * (lat - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _km_to_ring(lat: float, lon: float, ring) -> float:
    """Расстояние от точки до ломаной кольца, км (плоское приближение)."""
    kx = 111.32 * math.cos(math.radians(lat))
    ky = 110.57
    best = float("inf")
    ax, ay = ring[-1]
    for bx, by in ring:
        # сдвигаем начало координат в точку и считаем в километрах
        x1, y1 = (ax - lon) * kx, (ay - lat) * ky
        x2, y2 = (bx - lon) * kx, (by - lat) * ky
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if not length2 else max(0.0, min(1.0, -(x1 * dx + y1 * dy) / length2))
        d = math.hypot(x1 + t * dx, y1 + t * dy)
        if d < best:
            best = d
        ax, ay = bx, by
    return best


def inside_oblast(lat: float, lon: float, tolerance_km: float = BORDER_TOLERANCE_KM) -> bool:
    """Точка лежит в Джалал-Абадской области (по контуру, с допуском у границы)?

    Прямоугольная рамка захватывает кусок Ошской области и Узбекистан:
    Ош, Кара-Суу и Узген лежат в ней, но областью не являются. Поэтому после
    рамки сверяем с настоящим контуром. Если файл контура не читается —
    остаётся рамка: лучше принять сомнительное, чем не принимать ничего.
    """
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not inside_region(lat, lon):  # заодно отсекает NaN
        return False
    polygons = _region_polygons()
    if polygons is None:
        return True
    for outer, holes in polygons:
        if _in_ring(lat, lon, outer) and not any(_in_ring(lat, lon, h) for h in holes):
            return True
    return any(_km_to_ring(lat, lon, outer) <= tolerance_km for outer, _ in polygons)


# ------------------------------------------------------------------ строка запроса

def _clean(text) -> str:
    """Схлопнуть пробелы и переводы строк: «ул.  Ленина\\n5» -> «ул. Ленина 5»."""
    return " ".join(str(text or "").split())


def _district_name(ticket) -> str:
    """Название района для запроса. Старые микрорайоны города (без вида) — нет:
    «Центр» или «Спутник» только сбивают геокодер с толку."""
    district = ticket.district if ticket.district_id else None
    if district and district.kind:
        return _clean(district.name)
    return ""


def _join(parts) -> str:
    """Склеить части запроса через запятую, без пустых и повторов."""
    seen, out = set(), []
    for part in parts:
        key = part.casefold()
        if part and key not in seen:
            seen.add(key)
            out.append(part)
    return ", ".join(out)


def _query_parts(ticket):
    """Части запроса, уже подогнанные под лимит поля geo_query (300 знаков)."""
    address = _clean(ticket.address)
    settlement = _clean(ticket.settlement)
    district = _district_name(ticket)
    limit = Ticket._meta.get_field("geo_query").max_length
    overflow = len(_join([address, settlement, district, REGION_SUFFIX])) - limit
    if overflow > 0 and address:
        # Адрес в запросе самый длинный и самый «шумный» — режем его, а не район.
        address = address[:max(len(address) - overflow, 0)].rstrip(" ,")
    return address, settlement, district


def build_query(ticket) -> str:
    """Строка для геокодера: адрес, населённый пункт, район и область.

    Пусто, если нет ни адреса, ни населённого пункта: район без них — это
    «где-то в районе», а не место, ставить такую точку на карту нельзя.
    Эта же строка сохраняется в Ticket.geo_query; если после правки адреса
    строка стала другой — заявку геокодируют заново.
    """
    address, settlement, district = _query_parts(ticket)
    if not address and not settlement:
        return ""
    return _join([address, settlement, district, REGION_SUFFIX])


def _attempts(ticket):
    """Каскад запросов: [(строка, чем считать результат), ...] от точного к грубому."""
    address, settlement, district = _query_parts(ticket)
    attempts = []
    if address:
        attempts.append((_join([address, settlement, district, REGION_SUFFIX]),
                         Ticket.GeoSource.ADDRESS))
    if settlement:
        attempts.append((_join([settlement, district, REGION_SUFFIX]),
                         Ticket.GeoSource.SETTLEMENT))
    return attempts


# ------------------------------------------------------------------ сеть

_throttle_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    """Выдержать паузу между запросами: глобально для процесса, под замком —
    несколько потоков (воркер и команда) в сумме не превысят лимит сервиса."""
    global _last_request_at
    with _throttle_lock:
        wait = MIN_INTERVAL - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _search(query: str) -> list:
    """Один вопрос Nominatim: таймаут 8 с, одна повторная попытка.

    Первый запрос иногда обрывается на ровном месте — не разогрет DNS,
    моргнула сеть, и со второго раза всё находится. Но ответ «не 200»
    (блокировка, перегрузка) пустым результатом НЕ считается: иначе
    сбой сервиса превратился бы в «адрес не распознан» и закрепился в кэше.
    """
    params = {
        "q": query, "format": "json", "limit": 1, "countrycodes": "kg",
        # Рамка поиска: левый-верхний и правый-нижний углы.
        "viewbox": f"{LON_MIN},{LAT_MAX},{LON_MAX},{LAT_MIN}", "bounded": 1,
    }
    problem = None
    for attempt in (1, 2):
        _throttle()
        try:
            response = httpx.get(NOMINATIM_URL, params=params, timeout=REQUEST_TIMEOUT,
                                 headers={"User-Agent": USER_AGENT})
            if response.status_code != 200:
                raise GeocodeError(f"Nominatim ответил кодом {response.status_code}")
            rows = response.json()
            if not isinstance(rows, list):
                raise GeocodeError("Nominatim вернул не список")
            return rows
        except (httpx.HTTPError, ValueError, GeocodeError) as exc:
            problem = exc
            if attempt == 1:
                time.sleep(RETRY_PAUSE)
    raise GeocodeError(str(problem) or type(problem).__name__) from problem


def _lookup(query: str) -> Place | None:
    """Найти место по строке: сначала в кэше, потом в сети. None — «не знаем»."""
    cached = GeocodeCache.objects.filter(query=query).first()
    if cached and (cached.found or cached.created_at > timezone.now() - NEGATIVE_TTL):
        return Place(cached.lat, cached.lon, cached.rank) if cached.found else None

    rows = _search(query)
    place = None
    if rows:
        try:
            rank = rows[0].get("place_rank")
            place = Place(float(rows[0]["lat"]), float(rows[0]["lon"]),
                          int(rank) if rank is not None else None)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise GeocodeError(f"Непонятный ответ Nominatim: {rows[0]!r}") from exc
    GeocodeCache.objects.update_or_create(query=query, defaults={
        "found": place is not None,
        "lat": place.lat if place else None,
        "lon": place.lon if place else None,
        "rank": place.rank if place else None,
        "created_at": timezone.now(),
    })
    return place


def _acceptable(place: Place | None, query: str) -> Place | None:
    """Оставить результат, только если он внутри области: рамку Nominatim
    иногда игнорирует, и «Кара-Суу» находится в Ошской области."""
    if place is None:
        return None
    if not inside_oblast(place.lat, place.lon):
        logger.info("«%s» нашлось за пределами области (%.4f, %.4f) — пропускаю",
                    query, place.lat, place.lon)
        return None
    return place


# ------------------------------------------------------------------ запись в заявку

def _write(ticket, *, lat, lon, source, query) -> bool:
    """Записать результат в заявку, не затирая точку, которую человек поставил
    за время, пока мы ждали ответ сети. update(), а не save(): геокодер не
    должен менять «изменена» и чужие поля, которые сотрудник правит параллельно."""
    changed = (Ticket.objects.filter(pk=ticket.pk)
               .exclude(geo_source__in=HUMAN_SOURCES)
               .update(lat=lat, lon=lon, geo_source=source, geo_query=query))
    if changed:
        ticket.lat, ticket.lon, ticket.geo_source, ticket.geo_query = lat, lon, source, query
    return bool(changed)


def needs_geocoding(ticket, *, force: bool = False) -> bool:
    """Стоит ли вообще спрашивать сервис про эту заявку?"""
    if ticket.geo_source in HUMAN_SOURCES:
        return False
    query = build_query(ticket)
    if not query:
        return False
    return force or ticket.geo_query != query


def run_geocoding(ticket, *, force: bool = False) -> str:
    """Определить точку заявки; вернуть FOUND / NOT_FOUND / SKIPPED / ERROR.

    force=True — спросить заново, даже если строка запроса не менялась
    (повторная попытка для «не распознан»).
    """
    if ticket.geo_source in HUMAN_SOURCES:
        return SKIPPED  # точка человека главнее любого геокодера

    query = build_query(ticket)
    if not query:
        # Адрес и населённый пункт стёрли. Точка, поставленная по ним, больше
        # ничем не подкреплена: карта должна показывать то, что написано в карточке.
        if ticket.lat is not None or ticket.geo_source or ticket.geo_query:
            _write(ticket, lat=None, lon=None, source=Ticket.GeoSource.NONE, query="")
        return SKIPPED
    if not force and ticket.geo_query == query:
        return SKIPPED  # по этой строке уже искали

    found, source = None, None
    try:
        for attempt_query, attempt_source in _attempts(ticket):
            found = _acceptable(_lookup(attempt_query), attempt_query)
            if found:
                source = attempt_source
                break
    except GeocodeError as exc:
        # Ничего не пишем: geo_query остаётся прежним, заявку возьмут в следующий раз.
        logger.warning("Геокодирование заявки %s не удалось: %s", ticket.pk, exc)
        return ERROR

    if not found:
        _write(ticket, lat=None, lon=None, source=Ticket.GeoSource.FAILED, query=query)
        return NOT_FOUND
    if source == Ticket.GeoSource.ADDRESS and found.rank is not None and found.rank < PRECISE_RANK:
        # Искали по адресу, а сервис нашёл только село или район: честнее
        # показать это как «примерно», с ореолом на карте.
        source = Ticket.GeoSource.SETTLEMENT
    _write(ticket, lat=found.lat, lon=found.lon, source=source, query=query)
    return FOUND


def geocode_ticket(ticket, *, force: bool = False) -> bool:
    """Синхронно определить точку заявки. True — точка записана.

    Каскад: адрес + населённый пункт + район, затем населённый пункт + район.
    Точку жителя и сотрудника не трогает; повторно не спрашивает, пока адрес
    не изменился (строка Ticket.geo_query). Ответы кэшируются в GeocodeCache.
    """
    return run_geocoding(ticket, force=force) == FOUND


def apply_pin(ticket, lat, lon) -> bool:
    """Точка от жителя (геометка из мессенджера). True — точка записана.

    Геометка главнее адреса: житель показал место пальцем. Перекрывает
    address/settlement, но не manual — решение сотрудника главнее всех.
    Геометка за пределами области не принимается: человек мог отправить
    чужое место, а пустая карта лучше врущей.
    """
    if ticket.geo_source == Ticket.GeoSource.MANUAL:
        return False
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return False
    if not inside_oblast(lat, lon):
        return False
    changed = (Ticket.objects.filter(pk=ticket.pk)
               .exclude(geo_source=Ticket.GeoSource.MANUAL)
               .update(lat=lat, lon=lon, geo_source=Ticket.GeoSource.PIN))
    if changed:
        ticket.lat, ticket.lon, ticket.geo_source = lat, lon, Ticket.GeoSource.PIN
    return bool(changed)


# ------------------------------------------------------------------ фоновая очередь

_queue: "queue.Queue[int]" = queue.Queue()
_pending: set[int] = set()
_state_lock = threading.Lock()
_worker: threading.Thread | None = None


def _process(ticket_id: int) -> None:
    """Обработать одну заявку из очереди — по свежим данным из базы."""
    ticket = Ticket.objects.select_related("district").filter(pk=ticket_id).first()
    if ticket is not None:
        run_geocoding(ticket)


def _worker_loop() -> None:
    while True:
        ticket_id = _queue.get()
        with _state_lock:
            _pending.discard(ticket_id)  # с этого момента новая правка снова встанет в очередь
        try:
            _process(ticket_id)
        except Exception:  # поток не должен умирать из-за одной заявки
            logger.exception("Геокодирование заявки %s упало", ticket_id)
        finally:
            connections.close_all()  # у потока своё соединение с базой — не копим
            _queue.task_done()


def _enqueue(ticket_id: int) -> None:
    global _worker
    with _state_lock:
        if ticket_id in _pending:
            return  # уже ждёт своей очереди; воркер возьмёт свежие данные
        _pending.add(ticket_id)
        if _worker is None or not _worker.is_alive():
            _worker = threading.Thread(target=_worker_loop, name="geocoder", daemon=True)
            _worker.start()
    _queue.put(ticket_id)


def schedule_geocode(ticket_id: int) -> None:
    """Поставить заявку в очередь на определение точки — без ожидания.

    Вызывается после правки адреса/населённого пункта. Один фоновый поток
    на процесс разбирает очередь с паузой между запросами, поэтому ответ
    боту или сотруднику не ждёт сеть. Выключено флагом GEOCODE_ENABLED
    (в тестах — всегда). Если вызвали внутри транзакции, очередь получит
    заявку после коммита — иначе воркер увидел бы ещё старый адрес.
    """
    if not getattr(settings, "GEOCODE_ENABLED", False):
        return
    transaction.on_commit(lambda: _enqueue(ticket_id))
