"""
tickets/api.py — ручки, которыми пользуется Telegram-бот.

Все они закрыты служебным токеном (см. botcontrol/authentication.py):
человек с логином сюда не попадёт, бот не попадёт на страницы сотрудников.

Главная ручка — incoming: любое сообщение жителя (текст или фото) попадает
сюда и превращается в карточку обращения либо дописывается в открытую.
"""

from django.conf import settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from botcontrol.authentication import BotTokenAuthentication, IsBot
from botcontrol.models import Outbox
from directory.models import Category, District, Executor
from reports.geocoding import apply_pin, schedule_geocode
from tickets.lifecycle import SplitError, current_ticket, split_ticket
from tickets.models import Attachment, Channel, Citizen, Event, Message, Ticket


class BotAPIView(APIView):
    """Общий предок ручек бота: токен вместо логина."""

    authentication_classes = [BotTokenAuthentication]
    permission_classes = [IsBot]


def _int_or_none(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class IncomingView(BotAPIView):
    """
    POST /api/v1/tickets/incoming/ — сообщение жителя.

    Поля: tg_user_id (обязательно), chat_id, first_name, last_name, username,
    phone, text, tg_message_id; файлы — в multipart-поле files (можно несколько);
    подсказки ИИ на случай, если классификация уже готова: title, category
    (slug), district (slug), address.

    Логика: найти жителя (или завести), найти открытую заявку (или завести),
    приложить сообщение и файлы. Ответ говорит боту, что делать дальше:
    answer_mode = ai — звать модель, staff — молчать, отвечает сотрудник.
    """

    def post(self, request):
        data = request.data
        channel = (data.get("channel") or Channel.TELEGRAM).strip().lower()
        if channel not in Channel.values:
            channel = Channel.TELEGRAM

        if channel == Channel.TELEGRAM:
            tg_user_id = _int_or_none(data.get("tg_user_id"))
            if tg_user_id is None:
                return Response({"detail": "tg_user_id обязателен и должен быть числом"}, status=400)
            chat_id = _int_or_none(data.get("chat_id")) or tg_user_id
            citizen, _ = Citizen.objects.get_or_create(
                tg_user_id=tg_user_id, defaults={"chat_id": chat_id, "channel": channel},
            )
        else:
            chat_id = _int_or_none(data.get("chat_id"))
            if chat_id is None:
                return Response({"detail": "chat_id (номер телефона) обязателен для WhatsApp"}, status=400)
            citizen, _ = Citizen.objects.get_or_create(
                channel=channel, chat_id=chat_id, defaults={"channel": channel, "chat_id": chat_id},
            )

        text = (data.get("text") or "").strip()
        files = request.FILES.getlist("files")
        if not text and not files:
            return Response({"detail": "Пустое обращение: нет ни текста, ни файлов"}, status=400)

        # Профиль обновляем каждым сообщением: люди меняют имена и номера.
        # Но если ФИО назвал сам житель (name_confirmed), ник из мессенджера
        # его не затирает — иначе «Иванов Айбек Маратович» снова станет «bek_77».
        updates = {"chat_id": chat_id, "last_inbound_at": timezone.now()}
        profile_fields = ["username", "phone"]
        if not citizen.name_confirmed:
            profile_fields = ["first_name", "last_name"] + profile_fields
        for field in profile_fields:
            value = (data.get(field) or "").strip()
            if value:
                updates[field] = value
        Citizen.objects.filter(pk=citizen.pk).update(**updates)

        # Продолжить открытую заявку или завести новую (см. tickets/lifecycle.py:
        # справочная заявка после долгой паузы закрывается сама).
        ticket, closed = current_ticket(citizen)
        created = ticket is None
        if created:
            ticket = Ticket(
                citizen=citizen,
                channel=channel,
                title=(data.get("title") or "").strip()[:200] or text[:60] or "Фото от жителя",
                description=text,
                address=(data.get("address") or "").strip()[:250],
            )
            slug = (data.get("category") or "").strip()
            if slug:
                ticket.category = Category.objects.filter(slug=slug, is_active=True).first()
            slug = (data.get("district") or "").strip()
            if slug:
                ticket.district = District.objects.filter(slug=slug, is_active=True).first()
            ticket.save()
            Event.objects.create(ticket=ticket, kind="created",
                                 payload={"source": channel})
            # Сотрудникам сообщаем не здесь, а когда бот поймёт, что это
            # настоящее обращение (ClassifyView, kind=appeal): иначе каждое
            # «здравствуйте» и справочный вопрос будили бы служебный чат.

        message = Message.objects.create(
            ticket=ticket,
            author=Message.Author.CITIZEN,
            text=text,
            tg_message_id=_int_or_none(data.get("tg_message_id")),
        )
        for f in files:
            Attachment.objects.create(
                message=message, file=f,
                mime=getattr(f, "content_type", "") or "",
                size=f.size or 0,
            )

        # Житель ответил в заявку, которая «ждала ответа», — она снова в работе.
        fields = ["last_message_at", "last_message_preview", "unread", "updated_at"]
        ticket.last_message_at = timezone.now()
        ticket.last_message_preview = (text.strip() or "[Вложение]")[:300]
        ticket.unread = True
        if ticket.status == Ticket.Status.WAITING:
            ticket.status = Ticket.Status.IN_PROGRESS
            fields.append("status")
            Event.objects.create(ticket=ticket, kind="status",
                                 payload={"to": "in_progress", "by": "citizen_message"})
        ticket.save(update_fields=fields)

        return Response({
            "ticket_id": ticket.pk,
            "number": ticket.number,
            "answer_mode": ticket.answer_mode,
            "created": created,
            # id сообщения жителя — по нему бот просит разделить заявку,
            # если с этого сообщения началась другая тема (SplitView).
            "message_id": message.pk,
            # Справочная заявка, закрытая сейчас из-за долгой паузы.
            "closed_ticket": closed.number if closed else None,
        })


def notify_staff(ticket: Ticket) -> None:
    """
    Уведомление в служебный чат о новом обращении — через общую очередь.

    Зовётся, когда бот впервые понял, что это настоящее обращение (а не
    справочный вопрос), поэтому в тексте уже есть суть и место, а не
    первое «здравствуйте».
    """
    staff_chat = _int_or_none(settings.STAFF_CHAT_ID)
    if not staff_chat:
        return
    place = " · ".join(x for x in (
        ticket.district.name if ticket.district else "", ticket.settlement, ticket.address) if x)
    summary = (ticket.description or "").strip()
    summary = summary[:200] + ("…" if len(summary) > 200 else "")
    lines = [f"Новое обращение #{ticket.number}: {ticket.title}",
             f"От: {ticket.citizen}"]
    if place:
        lines.append(f"Место: {place}")
    if summary:
        lines.append(summary)
    Outbox.objects.create(
        chat_id=staff_chat,
        kind=Outbox.Kind.NOTIFY,
        ticket=ticket,
        channel=Channel.TELEGRAM,  # уведомления сотрудникам — всегда в Telegram
        text="\n".join(lines),
    )


class AiMessageView(BotAPIView):
    """POST /api/v1/tickets/<pk>/messages/ — записать ответ ИИ в переписку."""

    def post(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)
        text = (request.data.get("text") or "").strip()
        if not text:
            return Response({"detail": "Пустой текст ответа"}, status=400)

        message = Message.objects.create(
            ticket=ticket, author=Message.Author.AI, text=text,
            tg_message_id=_int_or_none(request.data.get("tg_message_id")),
        )
        ticket.last_message_at = timezone.now()
        ticket.last_message_preview = text[:300]
        ticket.save(update_fields=["last_message_at", "last_message_preview", "updated_at"])
        return Response({"message_id": message.pk})


class RetitleView(BotAPIView):
    """
    POST /api/v1/tickets/<pk>/retitle/ — разовое уточнение темы после разговора.

    Заголовок при создании заявки ставится по первому сообщению («Привет» —
    и всё), а через несколько минут бот присылает сюда заголовок получше,
    определённый уже по всей переписке. Срабатывает не больше одного раза
    на заявку и не трогает заголовок, если сотрудник успел поправить его
    руками, — обе проверки идут по ленте событий заявки.
    """

    def post(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)
        title = (request.data.get("title") or "").strip()[:200]
        if not title:
            return Response({"detail": "Пустой заголовок"}, status=400)
        if ticket.events.filter(kind__in=("retitled", "title")).exists():
            return Response({"applied": False})
        ticket.title = title
        ticket.save(update_fields=["title", "updated_at"])
        Event.objects.create(ticket=ticket, kind="retitled", payload={"to": title})
        return Response({"applied": True})


class RatingView(BotAPIView):
    """POST /api/v1/messages/<pk>/rating/ — оценка «помогло / не помогло»."""

    def post(self, request, pk: int):
        rating = request.data.get("rating")
        if rating not in (Message.Rating.UP, Message.Rating.DOWN):
            return Response({"detail": "rating должен быть up или down"}, status=400)
        updated = Message.objects.filter(pk=pk, author=Message.Author.AI).update(rating=rating)
        if not updated:
            return Response({"detail": "Сообщение не найдено или это не ответ ИИ"}, status=404)
        return Response({"ok": True})


class ClassifyView(BotAPIView):
    """
    POST /api/v1/tickets/<pk>/classify/ — итог классификации от ИИ.

    Классификация в боте фоновая, поэтому приходит отдельным запросом позже.
    Правило: НЕ затирать то, что уже поправил сотрудник. Обновляются только
    пустые поля; category/district — по slug, незнакомый slug игнорируется.

    Дополнительно бот присылает данные, извлечённые из переписки:
      last_name / first_name / middle_name — ФИО, названное жителем;
      phone, settlement (населённый пункт), address;
      kind — тип обращения (appeal / question / other);
      executor — id организации из справочника бота (Executor.external_id);
      description — краткая суть обращения от ИИ.
    Ручную правку сотрудника узнаём по ленте событий заявки: событие
    с user — это действие человека (так же устроен RetitleView).
    """

    # Сколько символов краткой сути берём от ИИ.
    DESCRIPTION_MAX = 2000

    def post(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)

        applied = []
        data = request.data

        def text(key, limit):
            return (data.get(key) or "").strip()[:limit]

        def staff_touched(kind):
            """Сотрудник уже менял это поле руками?"""
            return ticket.events.filter(kind=kind, user__isnull=False).exists()

        title = text("title", 200)
        # Заголовок считается «автоматическим», если он равен началу описания —
        # такой можно заменить на осмысленный от ИИ.
        auto_title = not ticket.title or ticket.title == (ticket.description or "")[:60]
        if title and auto_title and title != ticket.title:
            ticket.title = title
            applied.append("title")

        if not ticket.category_id:
            category = Category.objects.filter(
                slug=(data.get("category") or "").strip(), is_active=True).first()
            if category:
                ticket.category = category
                applied.append("category")
                if ticket.due_at is None and category.sla_hours:
                    ticket.due_at = timezone.now() + timezone.timedelta(
                        hours=category.sla_hours)
                    applied.append("due_at")
                if ticket.executor_id is None and category.default_executor_id:
                    ticket.executor_id = category.default_executor_id
                    applied.append("executor")

        if not ticket.district_id:
            district = District.objects.filter(
                slug=(data.get("district") or "").strip(), is_active=True).first()
            if district:
                ticket.district = district
                applied.append("district")

        address = text("address", 250)
        if address and not ticket.address:
            ticket.address = address
            applied.append("address")

        settlement = text("settlement", 150)
        if settlement and not ticket.settlement:
            ticket.settlement = settlement
            applied.append("settlement")

        # Тип обращения бот уточняет по ходу разговора, пока его не менял сотрудник.
        kind = text("kind", 16)
        if (kind in Ticket.Kind.values and kind != ticket.kind
                and not staff_touched("kind")):
            ticket.kind = kind
            applied.append("kind")

        # Профильная организация вместо категорийного исполнителя по умолчанию:
        # заменяем, пока исполнитель пуст или поставлен автоматически.
        external_id = text("executor", 120)
        if external_id and not staff_touched("executor"):
            executor = Executor.objects.filter(
                external_id=external_id, is_active=True).first()
            if executor and executor.pk != ticket.executor_id:
                ticket.executor = executor
                if "executor" not in applied:
                    applied.append("executor")

        description = text("description", self.DESCRIPTION_MAX)
        if description and description != ticket.description and self._auto_description(ticket):
            ticket.description = description
            applied.append("description")

        if self._apply_citizen(ticket.citizen, data):
            applied.append("name")
        if self._apply_phone(ticket.citizen, text("phone", 40)):
            applied.append("phone")

        # Точка, которую житель отправил с карты (геометка в мессенджере).
        lat, lon = data.get("lat"), data.get("lon")
        if lat is not None and lon is not None:
            try:
                if apply_pin(ticket, float(lat), float(lon)):
                    applied.append("pin")
            except (TypeError, ValueError):
                pass

        if applied:
            ticket.save()
            Event.objects.create(ticket=ticket, kind="classified",
                                 payload={"applied": applied})
        # Адрес, село или район изменились — заявка встанет на карту сама
        # (фоном: геокодер внешний и не должен задерживать ответ боту).
        if {"address", "settlement", "district"} & set(applied):
            schedule_geocode(ticket.pk)

        # Бот впервые понял, что это настоящее обращение, — будим служебный
        # чат. Один раз на заявку: повторная классификация не дублирует.
        if (ticket.kind == Ticket.Kind.APPEAL
                and not ticket.events.filter(kind="staff_notified").exists()):
            Event.objects.create(ticket=ticket, kind="staff_notified", payload={})
            notify_staff(ticket)
        return Response({"applied": applied})

    @staticmethod
    def _auto_description(ticket: Ticket) -> bool:
        """Суть обращения ещё «автоматическая» и её можно заменить?

        Да, если она пуста, совпадает с первым сообщением жителя (так её
        ставит incoming) или была записана прошлой классификацией — и
        сотрудник её руками не правил.
        """
        if ticket.events.filter(kind="description", user__isnull=False).exists():
            return False
        current = (ticket.description or "").strip()
        if not current:
            return True
        first = ticket.messages.filter(author=Message.Author.CITIZEN).first()
        if first is not None and current == (first.text or "").strip():
            return True
        return any(
            "description" in e.payload.get("applied", [])
            for e in ticket.events.filter(kind="classified")
        )

    @staticmethod
    def _apply_citizen(citizen: Citizen, data) -> bool:
        """ФИО жителя. Вернуть True, если что-то записали.

        Первое ФИО от бота (name_confirmed=False) целиком заменяет данные
        профиля мессенджера: там ник вроде «Mama» или «bek_77», и если житель
        назвал только фамилию, ник не должен остаться в поле имени
        («Асанова Mama»). Когда ФИО уже подтверждено, дополняем только
        пустые поля (скажем, отчество, которое житель назвал позже) — так
        правку сотрудника бот не перетрёт.
        """
        incoming = {
            field: (data.get(field) or "").strip()[:120]
            for field in ("last_name", "first_name", "middle_name")
        }
        changed = []
        if not citizen.name_confirmed:
            if not (incoming["last_name"] or incoming["first_name"]):
                return False
            for field, value in incoming.items():
                if value != getattr(citizen, field):
                    setattr(citizen, field, value)
                    changed.append(field)
            citizen.name_confirmed = True
            changed.append("name_confirmed")
        else:
            for field, value in incoming.items():
                if value and not getattr(citizen, field):
                    setattr(citizen, field, value)
                    changed.append(field)
        if changed:
            citizen.save(update_fields=changed)
        return bool(changed)

    @staticmethod
    def _apply_phone(citizen: Citizen, phone: str) -> bool:
        """Телефон — только если у жителя его ещё нет."""
        if phone and not citizen.phone:
            citizen.phone = phone
            citizen.save(update_fields=["phone"])
            return True
        return False



class SplitView(BotAPIView):
    """
    POST /api/v1/tickets/<pk>/split/ — житель заговорил о другой проблеме.

    Поля: from_message_id (сообщение жителя, с которого началась новая
    тема), title (необязательно). Сообщения с этого места переезжают в
    новую заявку (см. tickets/lifecycle.py); старая остаётся в работе.
    Ответ: {ticket_id, number} новой заявки.
    """

    def post(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)
        message_id = _int_or_none(request.data.get("from_message_id"))
        message = ticket.messages.filter(pk=message_id).first() if message_id else None
        if message is None:
            return Response({"detail": "from_message_id не найдено в этой заявке"}, status=400)
        try:
            new = split_ticket(ticket, message, title=(request.data.get("title") or ""))
        except SplitError as e:
            return Response({"detail": str(e)}, status=409)
        return Response({"ticket_id": new.pk, "number": new.number})

class HistoryView(BotAPIView):
    """GET /api/v1/tickets/<pk>/history/?limit=20 — переписка для контекста ИИ."""

    def get(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)
        limit = min(_int_or_none(request.query_params.get("limit")) or 20, 100)
        messages = list(
            ticket.messages.exclude(author=Message.Author.SYSTEM)
            .order_by("-created_at", "-id")[:limit]
        )[::-1]
        return Response({
            "ticket_id": ticket.pk,
            "messages": [
                {"author": m.author, "text": m.text, "created_at": m.created_at.isoformat()}
                for m in messages
            ],
        })


class ChatContextView(BotAPIView):
    """
    GET /api/v1/chats/<chat_id>/context/ — восстановление после перезапуска бота.

    Бот потерял память — по chat_id получает открытую (или последнюю) заявку
    и хвост переписки, чтобы диалог продолжился, а не начался заново.
    """

    def get(self, request, chat_id: int):
        channel = (request.query_params.get("channel") or Channel.TELEGRAM).strip().lower()
        if channel not in Channel.values:
            channel = Channel.TELEGRAM
        citizen = Citizen.objects.filter(chat_id=chat_id, channel=channel).first()
        if citizen is None:
            return Response({"detail": "Житель не найден"}, status=404)
        ticket = (citizen.tickets.open().order_by("-created_at").first()
                  or citizen.tickets.order_by("-created_at").first())
        if ticket is None:
            return Response({"detail": "Обращений не было"}, status=404)
        limit = min(_int_or_none(request.query_params.get("limit")) or 20, 100)
        messages = list(
            ticket.messages.exclude(author=Message.Author.SYSTEM)
            .order_by("-created_at", "-id")[:limit]
        )[::-1]
        return Response({
            "ticket_id": ticket.pk,
            "number": ticket.number,
            "status": ticket.status,
            "answer_mode": ticket.answer_mode,
            "is_open": ticket.is_open,
            "messages": [
                {"author": m.author, "text": m.text} for m in messages
            ],
        })


class CloseByChatView(BotAPIView):
    """
    POST /api/v1/chats/<chat_id>/close/ — житель начал заново (/reset).

    Открытая заявка закрывается как выполненная по инициативе жителя;
    следующее сообщение заведёт новую карточку.
    """

    def post(self, request, chat_id: int):
        channel = (request.data.get("channel") or Channel.TELEGRAM).strip().lower()
        if channel not in Channel.values:
            channel = Channel.TELEGRAM
        citizen = Citizen.objects.filter(chat_id=chat_id, channel=channel).first()
        if citizen is None:
            return Response({"closed": False})
        ticket = citizen.tickets.open().order_by("-created_at").first()
        if ticket is None:
            return Response({"closed": False})
        ticket.status = Ticket.Status.DONE
        ticket.save(update_fields=["status", "updated_at"])
        Event.objects.create(ticket=ticket, kind="status",
                             payload={"to": "done", "by": "citizen_reset"})
        return Response({"closed": True, "number": ticket.number})


class SubscriptionView(BotAPIView):
    """POST /api/v1/citizens/subscription/ — подписка на рассылки (/stop, /start)."""

    def post(self, request):
        chat_id = _int_or_none(request.data.get("chat_id"))
        if chat_id is None:
            return Response({"detail": "chat_id обязателен"}, status=400)
        channel = (request.data.get("channel") or Channel.TELEGRAM).strip().lower()
        if channel not in Channel.values:
            channel = Channel.TELEGRAM
        # Значение может прийти и как JSON-булево, и как строка из формы:
        # bool("False") == True, поэтому строки разбираем сами.
        raw = request.data.get("subscribed", True)
        if isinstance(raw, str):
            subscribed = raw.strip().lower() not in ("false", "0", "no", "")
        else:
            subscribed = bool(raw)
        Citizen.objects.filter(chat_id=chat_id, channel=channel).update(subscribed=subscribed)
        return Response({"ok": True})
