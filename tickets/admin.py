from django.contrib import admin

from .models import Attachment, Citizen, Event, Message, Note, Ticket


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ["author", "text", "created_at", "rating"]


@admin.register(Ticket)
class TicketAdmin(admin.ModelAdmin):
    list_display = ["number", "title", "citizen", "channel", "category", "district",
                    "status", "answer_mode", "created_at"]
    list_filter = ["status", "channel", "category", "district", "answer_mode"]
    search_fields = ["number", "title", "description", "address"]
    readonly_fields = ["number", "created_at", "updated_at", "last_message_at"]
    inlines = [MessageInline]


@admin.register(Citizen)
class CitizenAdmin(admin.ModelAdmin):
    list_display = ["__str__", "channel", "tg_user_id", "chat_id", "phone",
                    "district", "is_blocked", "subscribed"]
    list_filter = ["channel", "is_blocked", "subscribed"]
    search_fields = ["first_name", "last_name", "username", "phone"]


admin.site.register([Message, Attachment, Note, Event])
