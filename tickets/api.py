"""
tickets/api.py — ручки, которыми пользуется Telegram-бот.

Все они закрыты служебным токеном (см. botcontrol/authentication.py):
человек с логином сюда не попадёт, бот не попадёт на страницы сотрудников.

Главная ручка — incoming: любое сообщение жителя (текст или фото) попадает
сюда и превращается в карточку обращения либо дописывается в открытую.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from botcontrol.authentication import BotTokenAuthentication, IsBot
from botcontrol.models import Outbox
from directory.models import Category, District
from tickets.models import (
    Attachment, Channel, Citizen, Event, Message, Ticket, TICKET_STALE_HOURS,
)


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
        updates = {"chat_id": chat_id, "last_inbound_at": timezone.now()}
        for field in ("first_name", "last_name", "username", "phone"):
            value = (data.get(field) or "").strip()
            if value:
                updates[field] = value
        Citizen.objects.filter(pk=citizen.pk).update(**updates)

        ticket = citizen.tickets.open().order_by("-created_at").first()
        if ticket and ticket.last_message_at and (
            timezone.now() - ticket.last_message_at
            > timedelta(hours=TICKET_STALE_HOURS)
        ):
            # Житель молчал больше суток и написал заново — вероятно, уже
            # по другому вопросу. Закрываем старую заявку, заводим новую.
            stale_ticket = ticket
            stale_ticket.status = Ticket.Status.DONE
            stale_ticket.save(update_fields=["status", "updated_at"])
            Event.objects.create(ticket=stale_ticket, kind="status",
                                 payload={"to": "done", "by": "inactivity_timeout"})
            ticket = None

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
            self._notify_staff(ticket, text)

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
        fields = ["last_message_at", "unread", "updated_at"]
        ticket.last_message_at = timezone.now()
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
        })

    @staticmethod
    def _notify_staff(ticket: Ticket, text: str) -> None:
        """Уведомление в служебный чат о новой заявке — через общую очередь."""
        staff_chat = _int_or_none(settings.STAFF_CHAT_ID)
        if not staff_chat:
            return
        preview = text[:120] + ("…" if len(text) > 120 else "")
        Outbox.objects.create(
            chat_id=staff_chat,
            kind=Outbox.Kind.NOTIFY,
            ticket=ticket,
            channel=Channel.TELEGRAM,  # уведомления сотрудникам — всегда в Telegram
            text=f"Новое обращение #{ticket.number}\nОт: {ticket.citizen}\n{preview}",
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
        ticket.save(update_fields=["last_message_at", "updated_at"])
        return Response({"message_id": message.pk})


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
    """

    def post(self, request, pk: int):
        ticket = Ticket.objects.filter(pk=pk).first()
        if ticket is None:
            return Response({"detail": "Заявка не найдена"}, status=404)

        applied = []
        data = request.data

        title = (data.get("title") or "").strip()[:200]
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

        address = (data.get("address") or "").strip()[:250]
        if address and not ticket.address:
            ticket.address = address
            applied.append("address")

        if applied:
            ticket.save()
            Event.objects.create(ticket=ticket, kind="classified",
                                 payload={"applied": applied})
        return Response({"applied": applied})


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
