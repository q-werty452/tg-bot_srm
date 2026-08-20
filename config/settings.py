"""
Настройки панели управления ботом мэрии города Манас.

Секреты (ключ Django, ключ шифрования, служебный токен бота) живут в файле
.env рядом с manage.py — его нет в git. Всё остальное задано здесь.
"""

from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------- .env

def _read_env(path: Path) -> dict[str, str]:
    """Простое чтение .env без сторонних библиотек: СТРОКА=значение."""
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


ENV = _read_env(BASE_DIR / ".env")

SECRET_KEY = ENV.get("DJANGO_SECRET_KEY", "dev-insecure-key-change-me")
DEBUG = ENV.get("DEBUG", "1") == "1"
ALLOWED_HOSTS = [h for h in ENV.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",") if h]

# Служебный токен, по которому Telegram-бот ходит в API панели.
BOT_API_TOKEN = ENV.get("BOT_API_TOKEN", "")

# Ключ шифрования секретов в базе (Fernet). Генерация:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
SECRETS_KEY = ENV.get("SECRETS_KEY", "")

# Служебный чат Telegram для уведомлений о новых обращениях (id группы).
# Пусто — уведомления не создаются.
STAFF_CHAT_ID = ENV.get("STAFF_CHAT_ID", "")


# --------------------------------------------------------------- приложения

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # сторонние
    "rest_framework",
    "django_filters",
    "drf_spectacular",
    # наши
    "users",
    "directory",
    "tickets",
    "botcontrol",
    "reports",
    "audit",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# --------------------------------------------------------------- база

# SQLite сейчас; переезд на PostgreSQL — замена этого блока, код не меняется.
DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
        "OPTIONS": {
            # Меньше блокировок при одновременной работе бота и сотрудников.
            "init_command": "PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000;",
            "transaction_mode": "IMMEDIATE",
        },
    }
}


# --------------------------------------------------------------- пользователи

AUTH_USER_MODEL = "users.User"
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# --------------------------------------------------------------- DRF

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_FILTER_BACKENDS": ["django_filters.rest_framework.DjangoFilterBackend"],
    "DEFAULT_SCHEMA_CLASS": "drf_spectacular.openapi.AutoSchema",
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 50,
}

SPECTACULAR_SETTINGS = {
    "TITLE": "Панель бота мэрии г. Манас",
    "DESCRIPTION": "API для Telegram-бота и интерфейса панели",
    "VERSION": "1.0.0",
    "SERVE_INCLUDE_SCHEMA": False,
}


# --------------------------------------------------------------- язык и время

LANGUAGE_CODE = "ru"
TIME_ZONE = "Asia/Bishkek"
USE_I18N = True
USE_TZ = True


# --------------------------------------------------------------- статика и файлы

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Фотографии из Telegram бывают большими; лимит одного запроса — 25 МБ.
DATA_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 25 * 1024 * 1024
