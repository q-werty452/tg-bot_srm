"""
config/urls.py — все адреса панели.

  /               страницы сотрудников (вход по логину)
  /admin/         служебная админка Django
  /api/v1/...     ручки для Telegram-бота (служебный токен)
  /api/schema/    OpenAPI-схема, /api/docs/ — её просмотр
"""

from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

from botcontrol.api import (
    AnswerHitView, ConfigView, HealthView,
    OutboxFailedView, OutboxListView, OutboxSentView,
)
from botcontrol.views import bot_keys, bot_settings, broadcasts, quick_answers
from audit.views import audit_log
from directory.views import directory_home
from reports.views import export_xlsx, stats, tickets_map
from users.views import staff_list
from tickets.api import (
    AiMessageView, ChatContextView, ClassifyView, CloseByChatView,
    HistoryView, IncomingView, RatingView, RetitleView, SubscriptionView,
)
from tickets.views import dashboard, ticket_action, ticket_detail, ticket_new

api_v1 = [
    path("tickets/incoming/", IncomingView.as_view(), name="api-incoming"),
    path("tickets/<int:pk>/messages/", AiMessageView.as_view(), name="api-ai-message"),
    path("tickets/<int:pk>/history/", HistoryView.as_view(), name="api-history"),
    path("tickets/<int:pk>/classify/", ClassifyView.as_view(), name="api-classify"),
    path("tickets/<int:pk>/retitle/", RetitleView.as_view(), name="api-retitle"),
    path("messages/<int:pk>/rating/", RatingView.as_view(), name="api-rating"),
    path("chats/<int:chat_id>/context/", ChatContextView.as_view(), name="api-chat-context"),
    path("chats/<int:chat_id>/close/", CloseByChatView.as_view(), name="api-chat-close"),
    path("citizens/subscription/", SubscriptionView.as_view(), name="api-subscription"),
    path("outbox/", OutboxListView.as_view(), name="api-outbox"),
    path("outbox/<int:pk>/sent/", OutboxSentView.as_view(), name="api-outbox-sent"),
    path("outbox/<int:pk>/failed/", OutboxFailedView.as_view(), name="api-outbox-failed"),
    path("answers/<int:pk>/hit/", AnswerHitView.as_view(), name="api-answer-hit"),
    path("config/", ConfigView.as_view(), name="api-config"),
    path("health/", HealthView.as_view(), name="api-health"),
]

urlpatterns = [
    # --- вход и выход
    path("login/", auth_views.LoginView.as_view(template_name="login.html"),
         name="login"),
    path("logout/", auth_views.LogoutView.as_view(), name="logout_page"),

    # --- обращения
    path("", dashboard, name="dashboard"),
    path("tickets/new/", ticket_new, name="ticket_new"),
    path("tickets/<str:number>/", ticket_detail, name="ticket_detail"),
    path("tickets/<str:number>/action/", ticket_action, name="ticket_action"),

    # --- управление ботом
    path("bot/settings/", bot_settings, name="bot_settings"),
    path("bot/keys/", bot_keys, name="bot_keys"),
    path("bot/answers/", quick_answers, name="quick_answers"),
    path("broadcasts/", broadcasts, name="broadcasts"),

    # --- аналитика и администрирование
    path("stats/", stats, name="stats"),
    path("export/", export_xlsx, name="export"),
    path("map/", tickets_map, name="tickets_map"),
    path("audit/", audit_log, name="audit_log"),
    path("directory/", directory_home, name="directory_home"),
    path("staff/", staff_list, name="staff_list"),

    # --- служебное
    path("admin/", admin.site.urls),
    path("api/v1/", include(api_v1)),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
]

# Файлы жителей в разработке отдаёт сам Django; в бою — веб-сервер.
if settings.DEBUG:
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
