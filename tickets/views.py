"""
tickets/views.py — страницы сотрудников: список обращений и карточка.

Каждое действие в карточке — маленькая POST-форма с полем action:
так вся карточка работает без JavaScript, а каждое изменение оставляет
след в ленте событий и в журнале.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from audit.models import log
from botcontrol.models import Outbox
from directory.models import Category, District, Executor
from tickets.models import Channel, Citizen, Event, Message, Note, Ticket

# Человеческие описания событий для ленты в карточке.
EVENT_TEXTS = {
    "created": "заявка создана",
    "classified": "ИИ определил тему",
    "status": "статус изменён",
    "mode": "переключили, кто отвечает",
    "assignee": "назначен ответственный",
    "executor": "назначен исполнитель",
    "category": "изменена категория",
    "district": "изменён район",
    "title": "изменён заголовок",
    "address": "изменён адрес",
    "reply": "сотрудник ответил жителю",
    "manual": "заведена вручную",
}


def _event_human(event: Event) -> str:
    base = EVENT_TEXTS.get(event.kind, event.kind)
    to = event.payload.get("to")
    if to and event.kind == "status":
        label = dict(Ticket.Status.choices).get(to, to)
        return f"{base}: {label}"
    if to and event.kind == "mode":
        label = dict(Ticket.AnswerMode.choices).get(to, to)
        return f"{base}: {label}"
    if to:
        return f"{base}: {to}"
    return base


@login_required
def dashboard(request):
    qs = (Ticket.objects.select_related("category", "district", "executor", "citizen")
          .order_by("-last_message_at", "-created_at"))

    f = {
        "q": request.GET.get("q", "").strip(),
        "status": request.GET.get("status", "").strip(),
        "category": request.GET.get("category", "").strip(),
        "district": request.GET.get("district", "").strip(),
        "channel": request.GET.get("channel", "").strip(),
    }
    if f["q"]:
        qs = qs.filter(
            Q(number__icontains=f["q"]) | Q(title__icontains=f["q"])
            | Q(description__icontains=f["q"]) | Q(address__icontains=f["q"])
            | Q(citizen__first_name__icontains=f["q"])
            | Q(citizen__last_name__icontains=f["q"])
            | Q(citizen__phone__icontains=f["q"])
        )
    if f["status"] == "overdue":
        qs = qs.overdue()
    elif f["status"]:
        qs = qs.filter(status=f["status"])
    if f["category"]:
        qs = qs.filter(category__slug=f["category"])
    if f["district"]:
        qs = qs.filter(district__slug=f["district"])
    if f["channel"] in Channel.values:
        qs = qs.filter(channel=f["channel"])

    today = timezone.localdate()
    all_tickets = Ticket.objects.all()
    stats = {
        "new": all_tickets.filter(status=Ticket.Status.NEW).count(),
        "new_today": all_tickets.filter(status=Ticket.Status.NEW,
                                        created_at__date=today).count(),
        "in_progress": all_tickets.filter(status=Ticket.Status.IN_PROGRESS).count(),
        "overdue": all_tickets.overdue().count(),
        "done": all_tickets.filter(status=Ticket.Status.DONE).count(),
        "done_today": all_tickets.filter(status=Ticket.Status.DONE,
                                         updated_at__date=today).count(),
    }

    page = Paginator(qs, 25).get_page(request.GET.get("page"))
    # Превью — первая фотография заявки (для колонки со снимком).
    for ticket in page.object_list:
        ticket.preview = next(
            (a for m in ticket.messages.all() for a in m.attachments.all() if a.is_image),
            None,
        )

    query = request.GET.copy()
    query.pop("page", None)

    return render(request, "dashboard.html", {
        "section": "dashboard",
        "page": page, "stats": stats, "f": f,
        "qs": query.urlencode(),
        "statuses": Ticket.Status.choices,
        "categories": Category.objects.filter(is_active=True),
        "districts": District.objects.filter(is_active=True),
        "channels": Channel.choices,
    })


@login_required
def ticket_detail(request, number: str):
    ticket = get_object_or_404(
        Ticket.objects.select_related("citizen", "category", "district", "executor"),
        number=number,
    )
    if ticket.unread:
        ticket.unread = False
        ticket.save(update_fields=["unread"])

    events = list(ticket.events.select_related("user"))
    for event in events:
        event.human = _event_human(event)

    whatsapp_window_open = (
        ticket.channel != Channel.WHATSAPP
        or Citizen.objects.filter(pk=ticket.citizen_id).whatsapp_window_open().exists()
    )

    # Другие заявки этого же жителя — заголовок текущей заявки хранит только
    # первую тему разговора, старые обращения так проще найти без поиска.
    other_tickets = (
        ticket.citizen.tickets.exclude(pk=ticket.pk)
        .order_by("-created_at")[:10]
    )

    return render(request, "ticket_detail.html", {
        "section": "dashboard",
        "t": ticket,
        "events": events,
        "other_tickets": other_tickets,
        "messages_list": ticket.messages.prefetch_related("attachments")
                                        .select_related("staff_user"),
        "statuses": Ticket.Status.choices,
        "categories": Category.objects.filter(is_active=True),
        "districts": District.objects.filter(is_active=True),
        "executors": Executor.objects.filter(is_active=True),
        "staff": get_user_model().objects.filter(is_active=True),
        "whatsapp_window_open": whatsapp_window_open,
    })


@login_required
@require_POST
def ticket_action(request, number: str):
    """Все действия в карточке. Поле action решает, что делать."""
    ticket = get_object_or_404(Ticket, number=number)
    action = request.POST.get("action", "")
    user = request.user

    def event(kind, **payload):
        Event.objects.create(ticket=ticket, user=user, kind=kind, payload=payload)

    if action == "reply":
        text = (request.POST.get("text") or "").strip()
        if not text:
            messages.error(request, "Пустой ответ не отправлен.")
            return redirect("ticket_detail", number=number)
        if (ticket.channel == Channel.WHATSAPP
                and not Citizen.objects.filter(pk=ticket.citizen_id).whatsapp_window_open().exists()):
            messages.error(request, "Нельзя отправить: житель не писал в WhatsApp больше "
                                    "23 часов — окно ответа закрыто. Дождитесь нового "
                                    "сообщения от жителя.")
            return redirect("ticket_detail", number=number)
        Message.objects.create(ticket=ticket, author=Message.Author.STAFF,
                               staff_user=user, text=text)
        # Жителю ответ уходит через очередь исходящих — её разбирает бот.
        Outbox.objects.create(chat_id=ticket.citizen.chat_id, text=text,
                              kind=Outbox.Kind.REPLY, ticket=ticket, channel=ticket.channel)
        fields = ["last_message_at", "updated_at"]
        ticket.last_message_at = timezone.now()
        if ticket.status == Ticket.Status.NEW:
            ticket.status = Ticket.Status.IN_PROGRESS
            fields.append("status")
        ticket.save(update_fields=fields)
        event("reply")
        log(user, "ответ жителю", f"#{ticket.number}", text[:120])
        destination = "WhatsApp" if ticket.channel == Channel.WHATSAPP else "Telegram"
        messages.success(request, f"Ответ поставлен в очередь — житель получит его в {destination}.")

    elif action == "mode":
        mode = request.POST.get("mode")
        if mode in Ticket.AnswerMode.values and mode != ticket.answer_mode:
            ticket.answer_mode = mode
            ticket.save(update_fields=["answer_mode", "updated_at"])
            event("mode", to=mode)
            log(user, "переключение ответчика", f"#{ticket.number}",
                "сотрудник" if mode == "staff" else "ИИ")
            messages.success(
                request,
                "Разговор перехвачен: ИИ в этом чате молчит." if mode == "staff"
                else "ИИ снова отвечает в этом чате.")

    elif action == "status":
        status = request.POST.get("status")
        if status in Ticket.Status.values and status != ticket.status:
            ticket.status = status
            ticket.save(update_fields=["status", "updated_at"])
            event("status", to=status)
            log(user, "смена статуса", f"#{ticket.number}",
                dict(Ticket.Status.choices)[status])
            messages.success(request, "Статус обновлён.")

    elif action == "category":
        slug = request.POST.get("category", "")
        category = Category.objects.filter(slug=slug, is_active=True).first() if slug else None
        ticket.category = category
        ticket.save(update_fields=["category", "updated_at"])
        event("category", to=category.name if category else "—")
        messages.success(request, "Категория обновлена.")

    elif action == "district":
        slug = request.POST.get("district", "")
        district = District.objects.filter(slug=slug, is_active=True).first() if slug else None
        ticket.district = district
        ticket.save(update_fields=["district", "updated_at"])
        event("district", to=district.name if district else "—")
        messages.success(request, "Район обновлён.")

    elif action == "executor":
        raw = request.POST.get("executor", "")
        executor = Executor.objects.filter(pk=raw).first() if raw.isdigit() else None
        ticket.executor = executor
        ticket.save(update_fields=["executor", "updated_at"])
        event("executor", to=str(executor) if executor else "—")
        messages.success(request, "Исполнитель обновлён.")

    elif action == "assignee":
        raw = request.POST.get("assignee", "")
        assignee = get_user_model().objects.filter(pk=raw).first() if raw.isdigit() else None
        ticket.assignee = assignee
        ticket.save(update_fields=["assignee", "updated_at"])
        event("assignee", to=str(assignee) if assignee else "—")
        messages.success(request, "Ответственный обновлён.")

    elif action == "title":
        title = (request.POST.get("title") or "").strip()[:200]
        if title and title != ticket.title:
            ticket.title = title
            ticket.save(update_fields=["title", "updated_at"])
            event("title", to=title)
            messages.success(request, "Заголовок сохранён.")

    elif action == "address":
        address = (request.POST.get("address") or "").strip()[:250]
        if address != ticket.address:
            ticket.address = address
            ticket.save(update_fields=["address", "updated_at"])
            event("address", to=address or "—")
            messages.success(request, "Адрес сохранён.")

    elif action == "note":
        text = (request.POST.get("text") or "").strip()
        if text:
            Note.objects.create(ticket=ticket, user=user, text=text)
            messages.success(request, "Заметка добавлена.")

    else:
        messages.error(request, "Неизвестное действие.")

    return redirect("ticket_detail", number=number)


@login_required
def ticket_new(request):
    """Ручное заведение: житель пришёл лично или позвонил."""
    form = {k: "" for k in ("name", "phone", "description", "title", "address")}
    if request.method == "POST":
        form.update({k: (request.POST.get(k) or "").strip() for k in form})
        if form["name"] and form["description"]:
            # Обращениям «не из Telegram» даём искусственные отрицательные id,
            # чтобы они не пересеклись с настоящими аккаунтами Telegram.
            manual_id = -(Citizen.objects.filter(tg_user_id__lt=0).count() + 1)
            citizen = Citizen.objects.create(
                tg_user_id=manual_id, chat_id=manual_id,
                first_name=form["name"][:120], phone=form["phone"][:40],
            )
            ticket = Ticket(
                citizen=citizen,
                title=form["title"][:200] or form["description"][:60],
                description=form["description"],
                address=form["address"][:250],
                category=Category.objects.filter(
                    slug=request.POST.get("category", "")).first(),
                district=District.objects.filter(
                    slug=request.POST.get("district", "")).first(),
                answer_mode=Ticket.AnswerMode.STAFF,  # в Telegram ответить некому
            )
            ticket.save()
            Message.objects.create(ticket=ticket, author=Message.Author.CITIZEN,
                                   text=form["description"])
            ticket.last_message_at = timezone.now()
            ticket.save(update_fields=["last_message_at"])
            Event.objects.create(ticket=ticket, user=request.user, kind="manual")
            log(request.user, "заявка вручную", f"#{ticket.number}")
            messages.success(request, f"Заявка #{ticket.number} создана.")
            return redirect("ticket_detail", number=ticket.number)
        messages.error(request, "Заполните имя жителя и суть обращения.")

    return render(request, "ticket_new.html", {
        "section": "ticket_new", "form": form,
        "categories": Category.objects.filter(is_active=True),
        "districts": District.objects.filter(is_active=True),
    })
