from django.contrib import admin

from .models import Entry


@admin.register(Entry)
class EntryAdmin(admin.ModelAdmin):
    list_display = ["created_at", "user", "action", "obj", "summary"]
    list_filter = ["action"]
    search_fields = ["obj", "summary"]
    # Журнал только читают: правка задним числом обесценила бы его.
    def has_add_permission(self, request):
        return False
    def has_change_permission(self, request, obj=None):
        return False
    def has_delete_permission(self, request, obj=None):
        return False
