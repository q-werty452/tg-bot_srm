"""
reports/views.py — статистика, карта и выгрузка в Excel.

Графики статистики — SVG и CSS, без внешних библиотек: панель обязана
работать без интернета. Единственное исключение — карта: ей нужны тайлы
OpenStreetMap из сети, о чём страница честно предупреждает.
"""

import json
import math
from datetime import timedelta

from django.contrib.auth.decorators import login_required
from django.db.models import Count, Q
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone

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
    if request.GET.get("category", "").isdigit():
        qs = qs.filter(category_id=request.GET["category"])
    if request.GET.get("district", "").isdigit():
        qs = qs.filter(district_id=request.GET["district"])
    channel = request.GET.get("channel", "")
    if channel in Channel.values:
        qs = qs.filter(channel=channel)
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(number__icontains=q) | Q(title__icontains=q)
                       | Q(description__icontains=q) | Q(address__icontains=q)
                       | Q(citizen__first_name__icontains=q)
                       | Q(citizen__last_name__icontains=q)
                       | Q(citizen__phone__icontains=q))
    return qs.order_by("-created_at")


@login_required
def export_xlsx(request):
    """Выгрузка обращений в Excel — для отчётности мэрии."""
    from openpyxl import Workbook
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Обращения"
    headers = ["Номер", "Канал", "Создана", "Заголовок", "Категория", "Район", "Адрес",
               "Статус", "Просрочена", "Исполнитель", "Ответственный",
               "Житель", "Телефон", "Кто отвечает"]
    ws.append(headers)

    for t in _filtered_tickets(request):
        ws.append([
            t.number,
            t.get_channel_display(),
            timezone.localtime(t.created_at).strftime("%d.%m.%Y %H:%M"),
            t.title,
            t.category.name if t.category else "",
            t.district.name if t.district else "",
            t.address,
            t.get_status_display(),
            "да" if t.is_overdue else "",
            str(t.executor) if t.executor else "",
            str(t.assignee) if t.assignee else "",
            str(t.citizen),
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


@login_required
def tickets_map(request):
    """Карта обращений. Точки — заявки, у которых определены координаты."""
    points = [
        {"lat": t.lat, "lon": t.lon, "number": t.number, "title": t.title,
         "status": t.get_status_display(), "overdue": t.is_overdue}
        for t in Ticket.objects.filter(lat__isnull=False, lon__isnull=False)
                                .exclude(lat__lt=-90)  # -1000 = «адрес не распознан»
                                .select_related("category")
    ]
    without = Ticket.objects.filter(lat__isnull=True).exclude(address="").count()
    return render(request, "map.html", {
        "section": "map", "points": points,
        "points_json": json.dumps(points, ensure_ascii=False),
        "without_coords": without,
    })
