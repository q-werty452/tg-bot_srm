"""
tickets/lifecycle.py — когда сообщение жителя продолжает старую заявку,
а когда открывает новую.

Почему не «сутки тишины — новая заявка» (так было в b3cc299, откатили):
жалоба, которую отдел ещё не решил, не должна закрываться сама только
потому, что житель какое-то время не писал. Закрыть «как выполненную» без
сотрудника можно только то, что и правда исчерпано без него:

1. После паузы дольше CONSULTATION_IDLE_HOURS новое сообщение — всегда
   новая заявка со своей темой (прошлые видны в карточке жителя).
2. Старая при этом закрывается, только если это не жалоба и сотрудники к
   ней не прикасались (справочный вопрос, приветствие). Жалоба остаётся
   открытой в работе — её никто не решил.
3. Если в живом разговоре житель пишет о ДРУГОЙ проблеме, бот по смыслу
   переписки просит разделить заявку (split_ticket).
"""

from datetime import timedelta

from django.db import transaction
from django.utils import timezone

from tickets.models import Event, Message, Ticket

# Сколько часов тишины закрывают справочную заявку. 12 — как срок жизни
# сессии в боте: вернувшийся на следующий день человек начинает новую тему.
CONSULTATION_IDLE_HOURS = 12


def _is_idle(ticket: Ticket) -> bool:
    """Житель молчал дольше CONSULTATION_IDLE_HOURS — следующее сообщение уже новый разговор."""
    last = ticket.last_message_at or ticket.created_at
    return timezone.now() - last > timedelta(hours=CONSULTATION_IDLE_HOURS)


def _staff_involved(ticket: Ticket) -> bool:
    """Заявкой занимались люди: перехват, ответственный, ответ или любое действие."""
    return (ticket.answer_mode != Ticket.AnswerMode.AI or bool(ticket.assignee_id)
            or ticket.events.filter(user__isnull=False).exists()
            or ticket.messages.filter(author=Message.Author.STAFF).exists())


def current_ticket(citizen) -> tuple[Ticket | None, Ticket | None]:
    """
    Открытая заявка, в которую пойдёт новое сообщение жителя.

    Возвращает (заявка или None — тогда нужна новая, закрытая сейчас
    заявка или None). После паузы новое сообщение — всегда новая заявка
    со своей темой: старая не держит чужие разговоры. При этом старая
    закрывается («Выполнена», с пометкой системы в ленте), только если это
    не жалоба и сотрудники к ней не прикасались; жалоба и всё, чем заняты
    люди, остаётся открытым в работе.
    """
    ticket = citizen.tickets.open().order_by("-created_at").first()
    if ticket is None or not _is_idle(ticket):
        return ticket, None
    if ticket.kind == Ticket.Kind.APPEAL or _staff_involved(ticket):
        return None, None
    ticket.status = Ticket.Status.DONE
    ticket.save(update_fields=["status", "updated_at"])
    Event.objects.create(ticket=ticket, kind="status",
                         payload={"to": "done", "by": "consultation_idle"})
    return None, ticket


def refresh_last_message(ticket: Ticket) -> None:
    """Пересчитать «последнее сообщение» и превью после переноса сообщений."""
    last = ticket.messages.order_by("-created_at", "-pk").first()
    ticket.last_message_at = last.created_at if last else None
    ticket.last_message_preview = ((last.text or "").strip() or "[Вложение]")[:300] if last else ""
    ticket.save(update_fields=["last_message_at", "last_message_preview", "updated_at"])


class SplitError(ValueError):
    """Разделить заявку нельзя — текст объясняет почему (уходит боту в ответе)."""


@transaction.atomic
def split_ticket(ticket: Ticket, from_message: Message, title: str = "") -> Ticket:
    """
    Вынести сообщения начиная с from_message в новую заявку того же жителя.

    from_message — сообщение жителя, с которого началась другая тема. Оно и
    всё, что пришло после (ответы бота, следующие сообщения), переезжает в
    новую карточку вместе с вложениями. Исходная заявка остаётся как была —
    со своим исполнителем и сроком.
    """
    if from_message.ticket_id != ticket.pk:
        raise SplitError("Сообщение относится к другой заявке")
    if from_message.author != Message.Author.CITIZEN:
        raise SplitError("Делить можно только по сообщению жителя")
    if not ticket.messages.filter(pk__lt=from_message.pk).exists():
        raise SplitError("Это первое сообщение заявки — делить нечего")
    if not ticket.is_open:
        raise SplitError("Заявка уже закрыта")

    text = (from_message.text or "").strip()
    new = Ticket(
        citizen=ticket.citizen,
        channel=ticket.channel,
        title=(title.strip()[:200] or text[:60] or "Новое обращение"),
        description=text,
    )
    new.save()
    moved = ticket.messages.filter(pk__gte=from_message.pk).update(ticket=new)

    refresh_last_message(ticket)
    refresh_last_message(new)
    new.unread = True
    new.save(update_fields=["unread"])

    Event.objects.create(ticket=new, kind="created",
                         payload={"source": ticket.channel, "split_from": ticket.number})
    Event.objects.create(ticket=ticket, kind="split",
                         payload={"to": new.number, "moved": moved})
    return new
