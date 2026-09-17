"""
botcontrol/api.py — служебные ручки для бота: очередь исходящих,
конфигурация и сердцебиение.

Про конфигурацию. Бот раз в ~30 секунд спрашивает /config/?version=N.
Совпала версия — короткий ответ «не изменилось». Не совпала — полный набор:
тексты, ключи (расшифрованные — канал закрыт служебным токеном), готовые
ответы и справочник контактов, собранный в текст для промпта.
"""

from django.db.models import F
from django.utils import timezone
from rest_framework.response import Response

from directory.models import Category, Contact, District
from tickets.api import BotAPIView, _int_or_none
from tickets.models import Channel, Citizen

from .crypto import SecretsError
from .models import BotAccount, BotSetting, Broadcast, Heartbeat, Outbox, ProviderKey, QuickAnswer


class OutboxListView(BotAPIView):
    """GET /api/v1/outbox/?channel=telegram&limit=50 — что боту забрать на отправку.

    channel обязателен: у каждого канала свой процесс-отправитель, и им
    нельзя случайно забрать и отправить чужую строку очереди.
    """

    def get(self, request):
        channel = (request.query_params.get("channel") or "").strip().lower()
        if channel not in Channel.values:
            return Response({"detail": "channel обязателен: telegram или whatsapp"}, status=400)
        limit = min(_int_or_none(request.query_params.get("limit")) or 50, 200)
        rows = Outbox.objects.filter(
            status=Outbox.Status.PENDING, channel=channel).order_by("created_at")[:limit]
        return Response({
            "items": [
                {"id": r.pk, "chat_id": r.chat_id, "text": r.text, "kind": r.kind}
                for r in rows
            ],
        })


class OutboxSentView(BotAPIView):
    """POST /api/v1/outbox/<pk>/sent/ — доставлено."""

    def post(self, request, pk: int):
        row = Outbox.objects.filter(pk=pk).first()
        if row is None:
            return Response({"detail": "Строка очереди не найдена"}, status=404)
        if row.status != Outbox.Status.SENT:
            row.status = Outbox.Status.SENT
            row.sent_at = timezone.now()
            row.attempts += 1
            row.save(update_fields=["status", "sent_at", "attempts"])
            if row.broadcast_id:
                Broadcast.objects.filter(pk=row.broadcast_id).update(sent=F("sent") + 1)
                self._finish_if_done(row.broadcast_id)
        return Response({"ok": True})

    @staticmethod
    def _finish_if_done(broadcast_id: int) -> None:
        """Все строки рассылки разобраны — рассылка завершена."""
        pending = Outbox.objects.filter(
            broadcast_id=broadcast_id, status=Outbox.Status.PENDING).exists()
        if not pending:
            Broadcast.objects.filter(pk=broadcast_id).update(
                status=Broadcast.Status.DONE)


class OutboxFailedView(BotAPIView):
    """
    POST /api/v1/outbox/<pk>/failed/ — не доставлено.

    Тело: error (текст), blocked (true, если житель заблокировал бота —
    такого помечаем и пропускаем в будущих рассылках).
    """

    def post(self, request, pk: int):
        row = Outbox.objects.filter(pk=pk).first()
        if row is None:
            return Response({"detail": "Строка очереди не найдена"}, status=404)
        row.status = Outbox.Status.FAILED
        row.error = str(request.data.get("error") or "")[:300]
        row.attempts += 1
        row.save(update_fields=["status", "error", "attempts"])
        if row.broadcast_id:
            Broadcast.objects.filter(pk=row.broadcast_id).update(failed=F("failed") + 1)
            OutboxSentView._finish_if_done(row.broadcast_id)
        if request.data.get("blocked"):
            # Заблокировал бота — и рассылки ему больше не ставим в очередь.
            Citizen.objects.filter(chat_id=row.chat_id, channel=row.channel).update(
                is_blocked=True, subscribed=False)
        return Response({"ok": True})


class AnswerHitView(BotAPIView):
    """POST /api/v1/answers/<pk>/hit/ — готовый ответ сработал (для счётчика)."""

    def post(self, request, pk: int):
        QuickAnswer.objects.filter(pk=pk).update(hits=F("hits") + 1)
        return Response({"ok": True})


def build_facts() -> str:
    """
    Собрать справочный блок для промпта из публичных контактов.

    Пример строки: «Отдел ЖКХ: тел. 5-02-41, ул. Ленина 1, пн-пт 8:30-17:30».
    Пустой справочник — пустая строка, бот живёт своими значениями.
    """
    lines = []
    for c in Contact.objects.filter(is_public=True).select_related("category"):
        parts = [c.title]
        details = []
        if c.phone:
            details.append(f"тел. {c.phone}")
        if c.address:
            details.append(c.address)
        if c.work_hours:
            details.append(c.work_hours)
        if c.comment:
            details.append(c.comment)
        if details:
            parts.append(": " + ", ".join(details))
        if c.category:
            parts.append(f" (тема: {c.category.name})")
        lines.append("- " + "".join(parts))
    return "\n".join(lines)


class ConfigView(BotAPIView):
    """GET /api/v1/config/?version=N — конфигурация бота."""

    def get(self, request):
        setting = BotSetting.get()
        known = _int_or_none(request.query_params.get("version"))
        if known is not None and known == setting.version:
            return Response({"changed": False, "version": setting.version})

        providers = []
        for key in ProviderKey.objects.filter(is_active=True):
            try:
                secret = key.get_secret()
            except SecretsError:
                continue  # ключ шифровался другим SECRETS_KEY — пропускаем, не падаем
            providers.append({
                "provider": key.provider, "key": secret, "model": key.model,
            })

        bot_token = None
        active_bot = BotAccount.objects.filter(is_active=True).first()
        if active_bot:
            try:
                bot_token = active_bot.get_secret()
            except SecretsError:
                bot_token = None

        return Response({
            "changed": True,
            "version": setting.version,
            "enabled": setting.enabled,
            "maintenance_text": setting.maintenance_text,
            "invent_facts": setting.invent_facts,
            "prompt_detailed": setting.prompt_detailed,
            "prompt_chat": setting.prompt_chat,
            "default_provider": setting.default_provider,
            "bot_token": bot_token,
            "providers": providers,
            # Списки для классификатора в боте: он обязан выбирать
            # категорию и район только из существующих в панели.
            "categories": [
                {"slug": c.slug, "name": c.name}
                for c in Category.objects.filter(is_active=True)
            ],
            "districts": [
                {"slug": d.slug, "name": d.name}
                for d in District.objects.filter(is_active=True)
            ],
            "quick_answers": [
                {"id": qa.pk, "triggers": qa.trigger_list(), "answer": qa.answer}
                for qa in QuickAnswer.objects.filter(is_active=True)
            ],
            "facts": build_facts(),
            # Категории и районы нужны боту для классификации обращений:
            # ИИ выбирает slug строго из этих списков.
            "categories": [
                {"slug": c.slug, "name": c.name}
                for c in Category.objects.filter(is_active=True)
            ],
            "districts": [
                {"slug": d.slug, "name": d.name}
                for d in District.objects.filter(is_active=True)
            ],
        })


class HealthView(BotAPIView):
    """POST /api/v1/health/ — сигнал «я жив» от бота, раз в минуту."""

    KEEP = 100

    def post(self, request):
        Heartbeat.objects.create(
            providers=request.data.get("providers") or {},
            counters=request.data.get("counters") or {},
        )
        # Храним последнюю сотню, старое подчищаем.
        stale_ids = Heartbeat.objects.order_by("-created_at").values_list(
            "pk", flat=True)[self.KEEP:]
        if stale_ids:
            Heartbeat.objects.filter(pk__in=list(stale_ids)).delete()
        return Response({"ok": True})
