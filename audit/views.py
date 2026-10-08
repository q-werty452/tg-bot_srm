"""audit/views.py — журнал действий сотрудников (только чтение, только админ)."""

import re

from django.core.paginator import Paginator
from django.shortcuts import render

from botcontrol.views import admin_required

from .models import Entry

TICKET_NUMBER = re.compile(r"\d{4}-\d{4,}")


@admin_required
def audit_log(request):
    qs = Entry.objects.select_related("user")
    q = request.GET.get("q", "").strip()
    if q:
        from django.db.models import Q
        qs = qs.filter(Q(action__icontains=q) | Q(obj__icontains=q)
                       | Q(summary__icontains=q) | Q(user__email__icontains=q))
    page = Paginator(qs, 50).get_page(request.GET.get("page"))
    # Записи о заявках («#2026-0032») — ссылкой прямо в карточку.
    for entry in page.object_list:
        number = entry.obj[1:] if entry.obj.startswith("#") else ""
        entry.ticket_number = number if TICKET_NUMBER.fullmatch(number) else ""
    return render(request, "audit.html", {"section": "audit", "page": page, "q": q})
