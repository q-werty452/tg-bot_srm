"""
botcontrol/views.py — страницы управления ботом: настройки, ключи и токены.

Доступ только у роли «Администратор»: здесь лежат ключи и рубильник бота.
Сами значения ключей никогда не попадают ни в шаблоны, ни в журнал —
наружу выходят только последние 4 символа.
"""

from datetime import timedelta
from functools import wraps

import httpx
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect, render
from django.utils import timezone

from audit.models import log

from .api import build_facts
from . import keydetect
from .models import (BotAccount, BotSetting, Broadcast, Heartbeat,
                     Outbox, ProviderKey, QuickAnswer)

PROVIDER_LABELS = dict(ProviderKey.Provider.choices)

# Бот шлёт сердцебиение раз в минуту; молчит три — считаем, что он лежит.
ALIVE_WINDOW = timedelta(minutes=3)


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapper(request, *args, **kwargs):
        if not request.user.is_admin:
            messages.error(request, "Раздел доступен только администратору.")
            return redirect("dashboard")
        return view(request, *args, **kwargs)
    return wrapper


# ------------------------------------------------------------- настройки

@admin_required
def bot_settings(request):
    setting = BotSetting.get()

    if request.method == "POST":
        setting.enabled = bool(request.POST.get("enabled"))
        setting.invent_facts = bool(request.POST.get("invent_facts"))
        setting.maintenance_text = (request.POST.get("maintenance_text") or "").strip()[:300] \
            or setting.maintenance_text
        setting.prompt_detailed = (request.POST.get("prompt_detailed") or "").strip()
        setting.prompt_chat = (request.POST.get("prompt_chat") or "").strip()
        setting.save()
        log(request.user, "настройки бота изменены", summary=(
            f"включён={setting.enabled}, показ={setting.invent_facts}"))
        messages.success(request, "Сохранено. Бот подхватит изменения в течение минуты.")
        return redirect("bot_settings")

    health = Heartbeat.objects.order_by("-created_at").first()
    if health:
        health.alive = timezone.now() - health.created_at < ALIVE_WINDOW

    return render(request, "bot_settings.html", {
        "section": "bot_settings", "s": setting,
        "health": health, "facts": build_facts(),
    })


# ------------------------------------------------------------- проверка ключей

def _fetch_models(provider: str, key: str) -> list[str]:
    """
    Спросить у провайдера список моделей, доступных этому ключу.

    Возвращает список идентификаторов; исключение = ключ не работает.
    Списки фильтруются до чатовых моделей, чтобы в выпадашке не было
    embedding/tts/dall-e мусора.
    """
    if provider == "openai":
        response = httpx.get("https://api.openai.com/v1/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=15)
        response.raise_for_status()
        ids = [m["id"] for m in response.json().get("data", [])]
        good = [i for i in ids if i.startswith(("gpt-", "o1", "o3", "o4", "chatgpt"))
                and not any(x in i for x in ("audio", "realtime", "transcribe",
                                             "tts", "image", "search", "embed"))]
        return sorted(good)

    if provider == "gemini":
        response = httpx.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": key}, timeout=15)
        response.raise_for_status()
        return sorted(
            m["name"].removeprefix("models/")
            for m in response.json().get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
            and not any(x in m["name"] for x in ("tts", "image", "embed", "aqa"))
        )

    if provider == "openrouter":
        # OpenRouter — шлюз к чужим моделям, протокол как у OpenAI.
        # Список отдаёт без ключа, но с ним видно и то, что доступно лично вам.
        response = httpx.get("https://openrouter.ai/api/v1/models",
                             headers={"Authorization": f"Bearer {key}"}, timeout=20)
        response.raise_for_status()
        rows = response.json().get("data", [])
        # Имена составные: openai/gpt-4o-mini, google/gemini-2.0-flash.
        # Отсекаем то, что не для переписки.
        return sorted(
            r["id"] for r in rows
            if not any(x in r["id"] for x in ("whisper", "tts", "embed", "image",
                                              "vision-only", "moderation"))
        )

    if provider == "claude":
        response = httpx.get("https://api.anthropic.com/v1/models",
                             headers={"x-api-key": key,
                                      "anthropic-version": "2023-06-01"}, timeout=15)
        response.raise_for_status()
        return sorted(m["id"] for m in response.json().get("data", []))

    raise ValueError(f"Неизвестный провайдер: {provider}")


def smoke_test_model(provider: str, key: str, model: str) -> str:
    """
    Проверить, что модель годится боту: ведёт ли она ПЕРЕПИСКУ, а не только
    отвечает на одиночный вопрос.

    Зачем отдельная проверка: список моделей от провайдера включает и такие,
    которые принимают лишь одно сообщение. Бот шлёт им историю диалога и
    получает 400 на каждой второй реплике жителя. Список этого не показывает —
    видно только в бою, поэтому проверяем настоящим коротким диалогом.

    Возвращает пустую строку, если всё хорошо, иначе — текст проблемы.
    """
    two_turns = [
        {"role": "user", "content": "Привет"},
        {"role": "assistant", "content": "Здравствуйте! Чем помочь?"},
        {"role": "user", "content": "Ответь одним словом: работает?"},
    ]
    try:
        if provider == "gemini":
            # Через обычный HTTP, как и остальные проверки: SDK Google панели
            # не нужен, он есть только у бота.
            response = httpx.post(
                "https://generativelanguage.googleapis.com/v1beta/"
                f"models/{model}:generateContent",
                headers={"x-goog-api-key": key},
                json={
                    "contents": [
                        {"role": "model" if m["role"] == "assistant" else "user",
                         "parts": [{"text": m["content"]}]}
                        for m in two_turns
                    ],
                    "generationConfig": {"maxOutputTokens": 2000},
                },
                timeout=30)
            response.raise_for_status()

        elif provider in ("openai", "openrouter"):
            url = ("https://openrouter.ai/api/v1/chat/completions"
                   if provider == "openrouter"
                   else "https://api.openai.com/v1/chat/completions")
            response = httpx.post(
                url,
                headers={"Authorization": f"Bearer {key}"},
                json={"model": model,
                      "messages": [{"role": m["role"], "content": m["content"]}
                                   for m in two_turns],
                      "max_completion_tokens": 2000},
                timeout=30)
            response.raise_for_status()

        elif provider == "claude":
            response = httpx.post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": key, "anthropic-version": "2023-06-01"},
                json={"model": model, "max_tokens": 100, "messages": two_turns},
                timeout=30)
            response.raise_for_status()
    except Exception as e:
        text = " ".join(str(getattr(e, "message", None) or e).split())
        if "multiturn" in text.lower():
            return ("модель отвечает только на одиночные вопросы и не умеет "
                    "вести переписку — боту она не подходит")
        if "quota" in text.lower() or "429" in text:
            return ("у ключа кончилась квота — модель проверить не удалось, "
                    "попробуйте позже")
        return text[:200]
    return ""


@admin_required
def bot_keys(request):
    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "detect_key":
            # Сотрудник вставил ключ, не указав провайдера. Определяем сами:
            # сперва догадка по виду ключа, потом настоящая проверка —
            # у кого ключ примут, тот и хозяин (см. keydetect.py).
            key_value = (request.POST.get("key") or "").strip()
            provider, models_list, problem = keydetect.detect(key_value, _fetch_models)

            if provider is None:
                messages.error(request, problem)
            else:
                row, _ = ProviderKey.objects.get_or_create(provider=provider)
                row.set_secret(key_value)
                row.is_active = True
                row.last_check_ok = True
                row.last_check_at = timezone.now()
                if not row.model and models_list:
                    row.model = models_list[0]
                row.save()
                request.session["model_choices"] = {
                    "provider": provider,
                    "label": PROVIDER_LABELS[provider],
                    "models": models_list[:60],
                    "current": row.model,
                }
                log(request.user, "ключ определён и сохранён", provider)
                messages.success(
                    request,
                    f"Это ключ «{PROVIDER_LABELS[provider]}», он работает. "
                    f"Сохранён (…{row.tail4}). Моделей доступно: "
                    f"{len(models_list)} — выберите нужную ниже.")

        elif action == "save_key":
            provider = request.POST.get("provider", "")
            key_value = (request.POST.get("key") or "").strip()
            if provider in PROVIDER_LABELS and key_value:
                row, _ = ProviderKey.objects.get_or_create(provider=provider)
                row.set_secret(key_value)
                row.last_check_ok = None
                row.is_active = True
                row.save()
                log(request.user, "ключ заменён", provider)
                messages.success(
                    request,
                    f"Ключ {PROVIDER_LABELS[provider]} сохранён (…{row.tail4}). "
                    "Нажмите «Проверить», чтобы убедиться, что он работает.")
            else:
                messages.error(request, "Ключ пустой или провайдер неизвестен.")

        elif action == "check":
            provider = request.POST.get("provider", "")
            row = ProviderKey.objects.filter(provider=provider).first()
            if row is None:
                messages.error(request, "Сначала сохраните ключ.")
            else:
                try:
                    models_list = _fetch_models(provider, row.get_secret())
                    row.last_check_ok = True
                    # Список моделей — на один показ, в сессии (не в базе).
                    request.session["model_choices"] = {
                        "provider": provider,
                        "label": PROVIDER_LABELS[provider],
                        "models": models_list[:60],
                        "current": row.model,
                    }
                    messages.success(
                        request,
                        f"Ключ работает. Моделей доступно: {len(models_list)} — "
                        "выберите нужную ниже.")
                except httpx.HTTPStatusError as e:
                    row.last_check_ok = False
                    code = e.response.status_code
                    reason = ("ключ отклонён" if code in (401, 403)
                              else f"ошибка {code}")
                    messages.error(request, f"Проверка не прошла: {reason}.")
                except httpx.HTTPError:
                    row.last_check_ok = False
                    messages.error(request, "Провайдер недоступен: нет связи. "
                                            "Проверьте интернет и попробуйте ещё раз.")
                row.last_check_at = timezone.now()
                row.save()
                log(request.user, "проверка ключа", provider,
                    "успех" if row.last_check_ok else "провал")

        elif action == "set_model":
            provider = request.POST.get("provider", "")
            model = (request.POST.get("model") or "").strip()[:100]
            row = ProviderKey.objects.filter(provider=provider).first()
            if row and model:
                # Проверяем модель настоящим диалогом до сохранения: иначе
                # в бот попадёт модель, которая не умеет вести переписку,
                # и жители начнут получать ошибку вместо ответа.
                problem = smoke_test_model(provider, row.get_secret(), model)
                if problem:
                    log(request.user, "модель отклонена", provider,
                        f"{model}: {problem}")
                    messages.error(
                        request,
                        f"Модель {model} не подошла: {problem}. "
                        f"Оставил прежнюю ({row.model or 'не выбрана'}).")
                else:
                    row.model = model
                    row.save()
                    request.session.pop("model_choices", None)
                    log(request.user, "выбрана модель", provider, model)
                    messages.success(
                        request, f"Модель {model} проверена диалогом и сохранена.")

        elif action == "default_provider":
            value = request.POST.get("default_provider", "")
            if value in PROVIDER_LABELS:
                setting = BotSetting.get()
                setting.default_provider = value
                setting.save()
                log(request.user, "провайдер по умолчанию", value)
                messages.success(request,
                                 f"По умолчанию — {PROVIDER_LABELS[value]}.")

        elif action == "add_bot":
            name = (request.POST.get("name") or "").strip()[:100]
            token = (request.POST.get("token") or "").strip()
            if name and token and ":" in token:
                account = BotAccount(name=name, is_active=not BotAccount.objects.exists())
                account.set_secret(token)
                account.save()
                log(request.user, "добавлен telegram-бот", name)
                messages.success(request, f"Бот «{name}» добавлен (…{account.tail4}).")
            else:
                messages.error(request, "Название пустое или токен не похож на токен "
                                        "BotFather (в нём есть двоеточие).")

        elif action == "activate_bot":
            account = BotAccount.objects.filter(pk=request.POST.get("bot_id")).first()
            if account:
                account.is_active = True
                account.save()
                log(request.user, "переключён активный бот", account.name)
                messages.success(request, f"Активный бот — «{account.name}». "
                                          "Действующий бот переподключится сам.")

        return redirect("bot_keys")

    existing = {row.provider: row for row in ProviderKey.objects.all()}
    providers = [
        {"name": value, "label": label,
         "obj": existing.get(value) or ProviderKey(provider=value)}
        for value, label in ProviderKey.Provider.choices
    ]
    return render(request, "bot_keys.html", {
        "section": "bot_keys",
        "providers": providers,
        "provider_choices": ProviderKey.Provider.choices,
        "bots": BotAccount.objects.all(),
        "s": BotSetting.get(),
        "model_choices": request.session.pop("model_choices", None),
    })


# ------------------------------------------------------------- готовые ответы

@admin_required
def quick_answers(request):
    if request.method == "POST":
        action = request.POST.get("action", "save")
        qa_id = request.POST.get("id")
        qa = QuickAnswer.objects.filter(pk=qa_id).first() if qa_id else None

        if action == "delete" and qa:
            log(request.user, "готовый ответ удалён", str(qa)[:60])
            qa.delete()
            messages.success(request, "Готовый ответ удалён.")
        elif action == "save":
            triggers = (request.POST.get("triggers") or "").strip()
            answer = (request.POST.get("answer") or "").strip()
            if triggers and answer:
                if qa is None:
                    qa = QuickAnswer()
                qa.triggers, qa.answer = triggers, answer
                qa.is_active = bool(request.POST.get("is_active", qa_id is None))
                qa.save()
                log(request.user, "готовый ответ сохранён", str(qa)[:60])
                messages.success(request, "Сохранено. Бот подхватит в течение минуты.")
            else:
                messages.error(request, "Нужны и фразы, и текст ответа.")
        return redirect("quick_answers")

    return render(request, "quick_answers.html", {
        "section": "quick_answers",
        "answers": QuickAnswer.objects.order_by("-is_active", "-hits"),
    })


# ------------------------------------------------------------- рассылки

def _broadcast_recipients(broadcast):
    """Кому уходит рассылка: подписанные, не заблокировавшие бота жители.

    Рукозаведённые жители (Telegram, tg_user_id<0 — карточки без чата)
    исключены всегда. Жители WhatsApp вне 24-часового окна ответа Meta
    исключены тоже — свободное сообщение им уже не уйдёт. Telegram этим
    ограничением не затронут вообще, см. Citizen.whatsapp_window_open().
    """
    from tickets.models import Channel, Citizen

    qs = Citizen.objects.filter(subscribed=True, is_blocked=False)
    qs = qs.exclude(channel=Channel.TELEGRAM, tg_user_id__lt=0)
    qs = qs.whatsapp_window_open()
    if broadcast.audience == Broadcast.Audience.DISTRICT and broadcast.district_id:
        qs = qs.filter(district_id=broadcast.district_id)
    elif broadcast.audience == Broadcast.Audience.CATEGORY and broadcast.category_id:
        qs = qs.filter(tickets__category_id=broadcast.category_id).distinct()
    return qs


@admin_required
def broadcasts(request):
    from directory.models import Category, District

    if request.method == "POST":
        text = (request.POST.get("text") or "").strip()
        audience = request.POST.get("audience", "all")
        if not text:
            messages.error(request, "Текст рассылки пустой.")
            return redirect("broadcasts")
        if audience not in Broadcast.Audience.values:
            audience = Broadcast.Audience.ALL

        broadcast = Broadcast(
            text=text, audience=audience, created_by=request.user,
            status=Broadcast.Status.QUEUED,
        )
        if audience == Broadcast.Audience.DISTRICT:
            broadcast.district = District.objects.filter(
                pk=request.POST.get("district")).first()
        elif audience == Broadcast.Audience.CATEGORY:
            broadcast.category = Category.objects.filter(
                pk=request.POST.get("category")).first()
        broadcast.save()

        recipients = list(_broadcast_recipients(broadcast))
        Outbox.objects.bulk_create([
            Outbox(chat_id=citizen.chat_id, text=text, channel=citizen.channel,
                   kind=Outbox.Kind.BROADCAST, broadcast=broadcast)
            for citizen in recipients
        ])
        broadcast.total = len(recipients)
        broadcast.status = (Broadcast.Status.SENDING if recipients
                            else Broadcast.Status.DONE)
        broadcast.save(update_fields=["total", "status"])

        log(request.user, "рассылка", broadcast.get_audience_display(),
            f"{broadcast.total} получателей: {text[:80]}")
        if recipients:
            messages.success(request, f"Рассылка поставлена в очередь: "
                                      f"{broadcast.total} получателей.")
        else:
            messages.info(request, "Под условия не попал ни один житель — "
                                   "отправлять некому.")
        return redirect("broadcasts")

    from tickets.models import Channel, Citizen

    whatsapp_excluded = Citizen.objects.filter(
        subscribed=True, is_blocked=False, channel=Channel.WHATSAPP,
    ).whatsapp_window_closed().count()

    return render(request, "broadcasts.html", {
        "section": "broadcasts",
        "items": Broadcast.objects.select_related("district", "category")[:50],
        "districts": District.objects.filter(is_active=True),
        "categories": Category.objects.filter(is_active=True),
        "whatsapp_excluded": whatsapp_excluded,
    })
