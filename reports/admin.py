from django.contrib import admin

from .models import GeocodeCache


@admin.register(GeocodeCache)
class GeocodeCacheAdmin(admin.ModelAdmin):
    """Кэш геокодера: посмотреть, что спрашивали у Nominatim, или стереть
    устаревший ответ, чтобы адрес спросили заново."""

    list_display = ["query", "found", "lat", "lon", "rank", "created_at"]
    list_filter = ["found"]
    search_fields = ["query"]
    readonly_fields = ["created_at"]
