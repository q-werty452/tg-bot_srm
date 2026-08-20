from django.contrib import admin

from .models import Category, Contact, District, Executor


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "default_executor", "sla_hours", "is_active", "order"]
    list_editable = ["sla_hours", "is_active", "order"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(District)
class DistrictAdmin(admin.ModelAdmin):
    list_display = ["name", "slug", "is_active"]
    prepopulated_fields = {"slug": ["name"]}


@admin.register(Executor)
class ExecutorAdmin(admin.ModelAdmin):
    list_display = ["name", "short_name", "is_active"]


@admin.register(Contact)
class ContactAdmin(admin.ModelAdmin):
    list_display = ["title", "kind", "phone", "category", "is_public"]
    list_filter = ["kind", "is_public", "category"]
    search_fields = ["title", "phone"]
