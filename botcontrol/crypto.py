"""
botcontrol/crypto.py — шифрование секретов, хранящихся в базе.

Ключи моделей и токены ботов лежат в базе ЗАШИФРОВАННЫМИ (Fernet).
Ключ шифрования — SECRETS_KEY в .env панели: это единственный секрет,
который остаётся в файле. База без него бесполезна.

Генерация ключа:
  python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


class SecretsError(RuntimeError):
    """Понятная ошибка настройки шифрования."""


def _fernet() -> Fernet:
    key = settings.SECRETS_KEY
    if not key:
        raise SecretsError(
            "Не задан SECRETS_KEY в .env панели — без него нельзя хранить ключи. "
            "Сгенерируй: python -c \"from cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())\""
        )
    try:
        return Fernet(key.encode())
    except ValueError as e:
        raise SecretsError(f"SECRETS_KEY повреждён: {e}") from e


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken:
        raise SecretsError(
            "Не удалось расшифровать секрет: SECRETS_KEY не тот, которым шифровали."
        )
