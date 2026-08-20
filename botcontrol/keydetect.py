"""
botcontrol/keydetect.py — «а от какого это ИИ ключ?»

Задача: сотрудник вставляет ключ, не выбирая провайдера, а панель сама
определяет, чей он.

Делается в два шага:

1. **Догадка по виду.** У ключей есть узнаваемые начала. Но полагаться
   только на них нельзя: у Google сейчас в ходу два формата — старый
   `AIza…` и новый `AQ.…`, а OpenAI-совместимых сервисов много, и все
   они начинаются на `sk-`. Поэтому вид ключа задаёт лишь ПОРЯДОК проверки.

2. **Настоящая проверка.** По очереди спрашиваем у провайдеров список
   моделей. Кто принял ключ — тот и хозяин. Это единственный надёжный
   ответ; заодно сразу получаем список доступных моделей.
"""

import re

# Порядок проверки, если по виду ничего не понятно.
ALL_PROVIDERS = ("openai", "gemini", "claude")

# Начала ключей: подсказка -> кого проверять первым.
# Список открытый: не совпало ничего — просто проверим всех по очереди.
SHAPES: list[tuple[str, str, str]] = [
    # (регулярка, провайдер, человеческое описание для подсказки)
    (r"^sk-ant-", "claude", "похоже на ключ Anthropic"),
    (r"^AIza[0-9A-Za-z_\-]{30,}$", "gemini", "похоже на ключ Google (старый формат)"),
    (r"^AQ\.[0-9A-Za-z_\-]{20,}$", "gemini", "похоже на ключ Google (новый формат)"),
    (r"^sk-proj-", "openai", "похоже на ключ OpenAI"),
    (r"^sk-svcacct-", "openai", "похоже на служебный ключ OpenAI"),
    (r"^sk-[0-9A-Za-z_\-]{20,}$", "openai",
     "начинается на sk-: чаще всего OpenAI, но так же выглядят ключи "
     "сторонних OpenAI-совместимых сервисов"),
]

# Токен Telegram-бота легко перепутать с ключом ИИ: цифры, двоеточие, буквы.
TELEGRAM_TOKEN = re.compile(r"^\d{6,}:[0-9A-Za-z_\-]{20,}$")


def looks_like_telegram_token(key: str) -> bool:
    """Это вообще не ключ ИИ, а токен бота от @BotFather?"""
    return bool(TELEGRAM_TOKEN.match(key.strip()))


def guess(key: str) -> tuple[str | None, str]:
    """
    Догадка по виду ключа: (провайдер или None, пояснение для человека).

    Это только подсказка — окончательный ответ даёт проверка у провайдера.
    """
    key = key.strip()
    for pattern, provider, hint in SHAPES:
        if re.match(pattern, key):
            return provider, hint
    return None, "формат незнакомый — проверю у всех провайдеров по очереди"


def probe_order(key: str) -> list[str]:
    """В каком порядке опрашивать провайдеров: сперва наиболее вероятный."""
    provider, _ = guess(key)
    if provider is None:
        return list(ALL_PROVIDERS)
    return [provider] + [p for p in ALL_PROVIDERS if p != provider]


def detect(key: str, fetch_models) -> tuple[str | None, list[str], str]:
    """
    Определить провайдера, спросив у каждого список моделей.

    fetch_models(provider, key) -> список моделей; исключение = ключ не принят.
    Передаётся снаружи, чтобы эту функцию можно было тестировать без сети.

    Возвращает (провайдер или None, модели, сообщение для человека).
    """
    key = key.strip()
    if not key:
        return None, [], "Ключ пустой."
    if looks_like_telegram_token(key):
        return None, [], (
            "Это похоже на токен Telegram-бота, а не на ключ ИИ. "
            "Ему место в разделе «Telegram-боты» ниже.")

    errors: list[str] = []
    for provider in probe_order(key):
        try:
            models = fetch_models(provider, key)
        except Exception as e:  # сеть, 401, что угодно — просто идём дальше
            errors.append(f"{provider}: {_short(e)}")
            continue
        return provider, models, ""

    _, hint = guess(key)
    return None, [], (
        "Ни один провайдер не принял этот ключ. "
        f"По виду {hint}. Проверьте, что ключ скопирован целиком и не отозван. "
        "Подробности: " + "; ".join(errors))


def _short(error: Exception, limit: int = 90) -> str:
    text = " ".join(str(error).split())
    return text if len(text) <= limit else text[:limit] + "…"
