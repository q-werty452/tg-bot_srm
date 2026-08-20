from django.contrib import admin

from .models import (
    BotAccount, BotSetting, Broadcast, Heartbeat, Outbox, ProviderKey, QuickAnswer,
)


@admin.register(BotAccount)
class BotAccountAdmin(admin.ModelAdmin):
    list_display = ["name", "username", "masked", "is_active", "check_ok", "checked_at"]
    readonly_fields = ["secret_encrypted", "tail4"]


@admin.register(ProviderKey)
class ProviderKeyAdmin(admin.ModelAdmin):
    list_display = ["provider", "masked", "model", "is_active", "last_check_ok"]
    readonly_fields = ["secret_encrypted", "tail4"]


@admin.register(BotSetting)
class BotSettingAdmin(admin.ModelAdmin):
    list_display = ["__str__", "enabled", "invent_facts", "default_provider", "updated_at"]


@admin.register(QuickAnswer)
class QuickAnswerAdmin(admin.ModelAdmin):
    list_display = ["__str__", "is_active", "hits"]


@admin.register(Broadcast)
class BroadcastAdmin(admin.ModelAdmin):
    list_display = ["__str__", "audience", "status", "total", "sent", "failed", "created_at"]


@admin.register(Outbox)
class OutboxAdmin(admin.ModelAdmin):
    list_display = ["kind", "chat_id", "status", "attempts", "created_at", "sent_at"]
    list_filter = ["kind", "status"]


@admin.register(Heartbeat)
class HeartbeatAdmin(admin.ModelAdmin):
    list_display = ["created_at", "providers", "counters"]
