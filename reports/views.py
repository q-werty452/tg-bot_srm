"""
reports/views.py — статистика, карта и выгрузка в Excel.

Графики статистики — SVG и CSS, без внешних библиотек: панель обязана
работать без интернета. Единственное исключение — карта: ей нужны векторные
тайлы OpenFreeMap и библиотека MapLibre из сети, о чём страница честно
предупреждает. Сама страница карты отдаёт каркас и настройки, а точки
приходят отдельным JSON-запросом (map_data) — так фильтры на карте работают
без перезагрузки страницы.
"""

import math
import re
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.cache import never_cache

from directory.models import Category, District
from tickets.models import Channel, Message, Ticket

# Категориальная палитра для графиков (кольцевая диаграмма по темам).
# Порядок подобран так, что у СОСЕДНИХ цветов достаточная разница и для
# дальтоников, и для обычного зрения (проверено валидатором contrast/CVD) —
# менять порядок нельзя, можно только целиком заменить палитру и проверить
# заново. Цвет закрепляется за категорией по её порядку в списке (id),
# а не по месту в рейтинге обращений — иначе цвета «прыгали» бы при
# каждом пересчёте статистики.
CHART_PALETTE = [
    "#2a78d6",  # 1 синий
    "#eb6834",  # 2 оранжевый
    "#1baf7a",  # 3 бирюзовый
    "#eda100",  # 4 жёлтый
    "#e87ba4",  # 5 розовый
    "#008300",  # 6 зелёный
    "#4a3aa7",  # 7 фиолетовый
    "#e34948",  # 8 красный
]


def _donut_segments(rows, r=50, gap=3):
    """
    Подготовить дуги кольцевой диаграммы (SVG stroke-dasharray/dashoffset).

    rows — [(color, label, count), ...]; сегменты идут в этом же порядке
    по кругу, с постоянным зазором между соседними, доля каждого — от
    суммы count по всем строкам.
    """
    total = sum(count for _, _, count in rows)
    if not total:
        return []
    circumference = 2 * math.pi * r
    segments = []
    offset = 0.0
    for color, label, count in rows:
        if not count:
            continue
        length = count / total * circumference
        dash = max(length - gap, 0)
        segments.append({
            "color": color, "label": label, "count": count,
            "percent": round(count * 100 / total),
            "dasharray": f"{dash:.2f} {circumference - dash:.2f}",
            "dashoffset": f"{-offset:.2f}",
        })
        offset += length
    return segments


def _smooth_path(coords):
    """
    Сгладить ломаную в кривую Безье (метод Catmull-Rom -> кубический
    Безье). Крайние точки дублируются, чтобы концы кривой не «улетали».
    """
    pts = [coords[0]] + coords + [coords[-1]]
    path = [f"M{coords[0][0]:.1f},{coords[0][1]:.1f}"]
    for i in range(1, len(pts) - 2):
        p0, p1, p2, p3 = pts[i - 1], pts[i], pts[i + 1], pts[i + 2]
        c1x, c1y = p1[0] + (p2[0] - p0[0]) / 6, p1[1] + (p2[1] - p0[1]) / 6
        c2x, c2y = p2[0] - (p3[0] - p1[0]) / 6, p2[1] - (p3[1] - p1[1]) / 6
        path.append(f"C{c1x:.1f},{c1y:.1f} {c2x:.1f},{c2y:.1f} {p2[0]:.1f},{p2[1]:.1f}")
    return " ".join(path)


def _line_chart(points, width=560, height=108, pad_x=10, pad_y=13):
    """
    Подготовить координаты линейного графика с заливкой под линией.

    points — [(label, n), ...] по порядку оси X. Подписываем не каждую
    точку (иначе при двух неделях подряд это нечитаемо), а только первую,
    последнюю и максимум — остальные видны по наведению (title в SVG).
    Линия сглажена (Catmull-Rom), а не ломаная — визуально спокойнее
    при дневных колебаниях.
    """
    if not points:
        return None
    values = [n for _, n in points]
    top = max(values) or 1
    max_n = max(values)
    step = (width - 2 * pad_x) / (len(points) - 1) if len(points) > 1 else 0
    coords = []
    for i, (label, n) in enumerate(points):
        x = pad_x + i * step
        y = height - pad_y - (n / top) * (height - 2 * pad_y)
        coords.append((x, y, label, n))
    baseline = height - pad_y
    xy_only = [(x, y) for x, y, _, _ in coords]
    line_path = _smooth_path(xy_only) if len(xy_only) > 1 else \
        f"M{xy_only[0][0]:.1f},{xy_only[0][1]:.1f}"
    area = (line_path
            + f" L{coords[-1][0]:.1f},{baseline:.1f}"
            + f" L{coords[0][0]:.1f},{baseline:.1f} Z")
    grid = [round(baseline - frac * (height - 2 * pad_y), 1) for frac in (0.5, 1.0)]
    dots = [
        {"x": round(x, 1), "y": round(y, 1), "label_y": round(y - 9, 1),
         "label": label, "n": n,
         "show_label": n == max_n or i in (0, len(coords) - 1)}
        for i, (x, y, label, n) in enumerate(coords)
    ]
    return {"width": width, "height": height, "line_path": line_path, "area": area,
            "dots": dots, "baseline": baseline, "grid": grid}


def _bars(rows, label_key, count_key="n"):
    """Подготовить строки для CSS-графика: подписи + ширина в процентах."""
    top = max((r[count_key] for r in rows), default=0) or 1
    return [
        {"label": r[label_key] or "—", "count": r[count_key],
         "percent": round(r[count_key] * 100 / top)}
        for r in rows
    ]


@login_required
def stats(request):
    now = timezone.now()
    month_ago = now - timedelta(days=30)
    tickets = Ticket.objects.filter(created_at__gte=month_ago)

    # Плитки: где мы сейчас.
    tiles = {
        "total": tickets.count(),
        "open": Ticket.objects.open().count(),
        "overdue": Ticket.objects.overdue().count(),
        "done": tickets.filter(status=Ticket.Status.DONE).count(),
    }

    # Оценки жителей под ответами ИИ.
    ratings = Message.objects.filter(
        author=Message.Author.AI, created_at__gte=month_ago, rating__isnull=False)
    up = ratings.filter(rating="up").count()
    total_rated = ratings.count()
    tiles["rating"] = f"{round(up * 100 / total_rated)}%" if total_rated else "—"
    tiles["rated_count"] = total_rated
    # Шкала «помогло» — 0..100 с порогами, для наглядной полоски-градусника.
    rating_percent = round(up * 100 / total_rated) if total_rated else None

    # Обращения по дням за две недели — линейный график с заливкой.
    by_day = []
    for shift in range(13, -1, -1):
        day = (now - timedelta(days=shift)).date()
        n = Ticket.objects.filter(created_at__date=day).count()
        by_day.append({"label": day.strftime("%d.%m"), "n": n})
    day_chart = _line_chart([(d["label"], d["n"]) for d in by_day])
    day_total = sum(d["n"] for d in by_day)
    prev_start = (now - timedelta(days=27)).date()
    prev_end = (now - timedelta(days=14)).date()
    prev_total = Ticket.objects.filter(
        created_at__date__gte=prev_start, created_at__date__lt=prev_end).count()
    day_delta = round((day_total - prev_total) * 100 / prev_total) if prev_total else None

    # По категориям и районам (за месяц).
    by_category = _bars(
        list(tickets.values("category__name").annotate(n=Count("id"))
             .order_by("-n")[:10]), "category__name")
    by_district = _bars(
        list(tickets.values("district__name").annotate(n=Count("id"))
             .order_by("-n")[:10]), "district__name")

    # Структура обращений по темам — кольцевая диаграмма. Цвет закреплён
    # за категорией по порядку id (см. CHART_PALETTE), доля — от общего
    # числа КАТЕГОРИЗИРОВАННЫХ обращений за месяц.
    cat_counts = dict(
        tickets.exclude(category=None).values_list("category_id")
        .annotate(n=Count("id")).values_list("category_id", "n"))
    categories = list(Category.objects.filter(is_active=True).order_by("id"))
    category_donut = _donut_segments([
        (CHART_PALETTE[i % len(CHART_PALETTE)], c.name, cat_counts.get(c.id, 0))
        for i, c in enumerate(categories)
    ])

    # По часам суток: когда жителям удобно писать. Считаем в Python —
    # это переживёт переезд с SQLite на PostgreSQL без правок.
    hour_counts: dict[int, int] = {}
    for created in tickets.values_list("created_at", flat=True):
        hour = timezone.localtime(created).hour
        hour_counts[hour] = hour_counts.get(hour, 0) + 1
    top_h = max(hour_counts.values(), default=0) or 1
    by_hour = [{"label": f"{h:02d}", "n": hour_counts.get(h, 0),
                "percent": round(hour_counts.get(h, 0) * 100 / top_h)}
               for h in range(24)]

    return render(request, "stats.html", {
        "section": "stats", "tiles": tiles, "by_day": by_day,
        "day_chart": day_chart, "day_total": day_total, "day_delta": day_delta,
        "rating_percent": rating_percent,
        "by_category": by_category, "by_district": by_district,
        "category_donut": category_donut,
        "category_total": sum(cat_counts.values()), "by_hour": by_hour,
    })


def _filtered_tickets(request):
    """Те же фильтры, что на дашборде, — выгрузка отдаёт то, что видно."""
    qs = Ticket.objects.select_related(
        "citizen", "category", "district", "executor", "assignee")
    status = request.GET.get("status", "")
    if status == "overdue":
        qs = qs.overdue()
    elif status in Ticket.Status.values:
        qs = qs.filter(status=status)
    # Дашборд передаёт slug, старые ссылки на выгрузку — числовой id: понимаем оба.
    for param, field in (("category", "category"), ("district", "district")):
        value = request.GET.get(param, "").strip()
        if value.isdigit():
            qs = qs.filter(**{f"{field}_id": value})
        elif value:
            qs = qs.filter(**{f"{field}__slug": value})
    channel = request.GET.get("channel", "")
    if channel in Channel.values:
        qs = qs.filter(channel=channel)
    kind = request.GET.get("kind", "")
    if kind in Ticket.Kind.values:
        qs = qs.filter(kind=kind)
    return qs.search(request.GET.get("q", "")).order_by("-created_at")


@login_required
def export_xlsx(request):
    """Выгрузка обращений в Excel — для отчётности мэрии."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Обращения"
    headers = ["Номер", "Канал", "Создана", "Заголовок", "Тип обращения", "Категория",
               "Район", "Населённый пункт", "Адрес",
               "Статус", "Просрочена", "Исполнитель", "Ответственный",
               "Житель", "Фамилия", "Имя", "Отчество", "Телефон", "Кто отвечает"]
    ws.append(headers)

    for t in _filtered_tickets(request):
        ws.append([
            t.number,
            t.get_channel_display(),
            timezone.localtime(t.created_at).strftime("%d.%m.%Y %H:%M"),
            t.title,
            t.get_kind_display(),
            t.category.name if t.category else "",
            t.district.name if t.district else "",
            t.settlement,
            t.address,
            t.get_status_display(),
            "да" if t.is_overdue else "",
            str(t.executor) if t.executor else "",
            str(t.assignee) if t.assignee else "",
            str(t.citizen),
            t.citizen.last_name,
            t.citizen.first_name,
            t.citizen.middle_name,
            t.citizen.phone,
            t.get_answer_mode_display(),
        ])

    # Ширина колонок по содержимому, без фанатизма.
    for col, header in enumerate(headers, start=1):
        width = max(len(header), *(len(str(c.value or "")) for c in
                                   ws[get_column_letter(col)])) + 2
        ws.column_dimensions[get_column_letter(col)].width = min(width, 45)

    response = HttpResponse(
        content_type="application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet")
    filename = f"obrashcheniya_{timezone.localdate().isoformat()}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    wb.save(response)
    return response


# ------------------------------------------------------------------ карта

# Подписи точности точки для попапа: от них зависит, насколько точке верить.
GEO_ACCURACY = {
    Ticket.GeoSource.ADDRESS: "точный адрес",
    Ticket.GeoSource.SETTLEMENT: "примерно — по населённому пункту",
    Ticket.GeoSource.PIN: "геометка жителя",
    Ticket.GeoSource.MANUAL: "поставил сотрудник",
}

MAP_STATUSES = (("open", "Открытые"), ("all", "Все"), ("overdue", "Просроченные"))
MAP_PERIODS = (("7", "7 дней"), ("30", "30 дней"), ("90", "90 дней"), ("all", "Всё время"))
MAP_MAX_POINTS = 5000  # потолок точек в одном ответе: больше карта всё равно не покажет
DEFAULT_POINT_COLOR = "#8B96A8"  # заявка без категории
HEX_COLOR = re.compile(r"^#[0-9a-fA-F]{6}$")


def _map_filters(request) -> dict:
    """Фильтры карты из строки запроса; всё непонятное — значение по умолчанию."""
    get = request.GET
    focus = get.get("ticket", "").strip()[:12]
    # Ссылка «На карте» из карточки ведёт на конкретную заявку. Она может быть
    # закрытой или старой, и фильтры по умолчанию спрятали бы её точку.
    status_default = "all" if focus else "open"
    status, period, kind = get.get("status", ""), get.get("period", ""), get.get("kind", "")
    return {
        "status": status if status in dict(MAP_STATUSES) else status_default,
        "period": period if period in dict(MAP_PERIODS) else "all",
        "category": get.get("category", "").strip()[:60],
        "district": get.get("district", "").strip()[:60],
        "kind": kind if kind in Ticket.Kind.values else "",
        "ticket": focus,
    }


def _map_tickets(f: dict, *, ignore_district: bool = False):
    """Заявки под фильтры карты. ignore_district — для счётчиков по районам:
    чтобы при выбранном районе подписи на остальных не обнулялись."""
    qs = Ticket.objects.all()
    if f["status"] == "open":
        qs = qs.open()
    elif f["status"] == "overdue":
        qs = qs.overdue()
    # Карта передаёт slug; число понимаем как id — как и выгрузка в Excel.
    for key in ("category",) if ignore_district else ("category", "district"):
        value = f[key]
        if value.isdigit():
            qs = qs.filter(**{f"{key}_id": value})
        elif value:
            qs = qs.filter(**{f"{key}__slug": value})
    if f["kind"]:
        qs = qs.filter(kind=f["kind"])
    days = {"7": 7, "30": 30, "90": 90}.get(f["period"])
    if days:
        qs = qs.filter(created_at__gte=timezone.now() - timedelta(days=days))
    return qs


def _map_totals(qs) -> dict:
    """Счётчики «на карте / без координат» одним запросом."""
    totals = qs.aggregate(
        on_map=Count("id", filter=Q(lat__isnull=False)),
        without=Count("id", filter=Q(lat__isnull=True)),
        no_address=Count("id", filter=Q(lat__isnull=True, address="", settlement="")),
        failed=Count("id", filter=Q(lat__isnull=True,
                                    geo_source=Ticket.GeoSource.FAILED)),
    )
    return {"on_map": totals["on_map"], "without_coords": totals["without"],
            "no_address": totals["no_address"], "failed": totals["failed"]}


def _map_feature(t: Ticket) -> dict:
    """Точка заявки для карты: GeoJSON-фича со всем, что нужно попапу."""
    category = t.category
    color = category.color if category and HEX_COLOR.match(category.color or "") \
        else DEFAULT_POINT_COLOR
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [round(t.lon, 6), round(t.lat, 6)]},
        "properties": {
            "number": t.number,
            "title": t.title or "Без темы",
            "status": t.status,
            "status_label": t.get_status_display(),
            "overdue": int(t.is_overdue),
            "category": category.name if category else "",
            "color": color,
            "citizen": str(t.citizen),
            "district": t.district.name if t.district else "",
            "settlement": t.settlement,
            "address": t.address,
            "created": timezone.localtime(t.created_at).strftime("%d.%m.%Y %H:%M"),
            "geo": t.geo_source,
            "accuracy": GEO_ACCURACY.get(t.geo_source, ""),
            "approx": int(t.geo_source == Ticket.GeoSource.SETTLEMENT),
            "pin": int(t.geo_source == Ticket.GeoSource.PIN),
        },
    }


def _focus_info(number: str):
    """Что известно про заявку из ссылки /map/?ticket=… (None — ссылки нет)."""
    if not number:
        return None
    t = Ticket.objects.filter(number=number).first()
    if t is None:
        return {"number": number, "exists": False, "on_map": False}
    return {"number": t.number, "exists": True, "on_map": t.lat is not None,
            "place": t.address or t.settlement}


@login_required
def tickets_map(request):
    """Страница карты: каркас, фильтры и настройки. Точки грузит JS из map_data."""
    f = _map_filters(request)
    focus = _focus_info(f["ticket"])
    categories = list(Category.objects.filter(is_active=True))
    # Только территории области (районы и города), а не прежние микрорайоны.
    districts = list(District.objects.filter(is_active=True).exclude(kind=""))
    config = {
        "dataUrl": reverse("map_data"),
        # Шаблон ссылки на карточку: JS подставляет номер вместо __N__.
        "ticketUrl": reverse("ticket_detail", args=["__N__"]),
        "filters": f,
        "focus": focus,
        "districts": [{"slug": d.slug, "name": d.name, "kind": d.kind} for d in districts],
        "categories": [{"slug": c.slug, "name": c.name,
                        "color": c.color if HEX_COLOR.match(c.color or "") else DEFAULT_POINT_COLOR}
                       for c in categories],
    }
    return render(request, "map.html", {
        "section": "map", "f": f, "focus": focus, "config": config,
        "categories": categories, "districts": districts,
        "kinds": Ticket.Kind.choices, "statuses": MAP_STATUSES, "periods": MAP_PERIODS,
        "totals": _map_totals(_map_tickets(f)),
    })


@never_cache
@login_required
def map_data(request):
    """Точки карты под фильтры — GeoJSON плюс счётчики в поле meta.

    Запросов к базе всегда одно и то же число, сколько бы ни было заявок:
    точки (с категорией, районом и заявителем одним JOIN), общий счётчик
    и счётчики по районам; ещё один — только если запрошена заявка из ссылки.
    """
    f = _map_filters(request)
    base = _map_tickets(f)

    rows = list(
        base.filter(lat__isnull=False, lon__isnull=False)
        .select_related("category", "district", "citizen")
        .defer("description", "last_message_preview")
        .order_by("-created_at")[:MAP_MAX_POINTS + 1]
    )
    truncated = len(rows) > MAP_MAX_POINTS
    rows = rows[:MAP_MAX_POINTS]

    focus = None
    if f["ticket"]:
        # Заявка из ссылки видна на карте, даже если фильтры её исключают.
        extra = (Ticket.objects.filter(number=f["ticket"])
                 .select_related("category", "district", "citizen")
                 .defer("description", "last_message_preview").first())
        focus = {"number": f["ticket"], "exists": extra is not None,
                 "on_map": bool(extra and extra.lat is not None)}
        if focus["on_map"] and all(r.pk != extra.pk for r in rows):
            rows.append(extra)

    district_counts = dict(
        _map_tickets(f, ignore_district=True).exclude(district=None)
        .values_list("district__slug").annotate(n=Count("id"))
    )
    return JsonResponse({
        "type": "FeatureCollection",
        "features": [_map_feature(t) for t in rows],
        "meta": {
            **_map_totals(base),
            "shown": len(rows), "truncated": truncated, "limit": MAP_MAX_POINTS,
            "district_counts": district_counts,
            "filters": f, "focus": focus,
        },
    }, json_dumps_params={"ensure_ascii": False})
