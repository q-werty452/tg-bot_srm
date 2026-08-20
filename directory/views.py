"""
directory/views.py — справочники: категории, районы, исполнители, контакты.

Одна страница, четыре блока. Каждое действие — маленькая POST-форма
с полем action; ничего не удаляется, если на запись ссылаются заявки
(вместо удаления — выключение).
"""

from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.text import slugify

from audit.models import log
from botcontrol.models import BotSetting
from botcontrol.views import admin_required

from .models import Category, Contact, District, Executor

_SLUG_FALLBACK = "punkt"


def _make_slug(model, name: str) -> str:
    """Код из названия; кириллица не транслитерируется — берём порядковый."""
    base = slugify(name) or f"{_SLUG_FALLBACK}-{model.objects.count() + 1}"
    slug, i = base, 2
    while model.objects.filter(slug=slug).exists():
        slug, i = f"{base}-{i}", i + 1
    return slug


@admin_required
def directory_home(request):
    if request.method == "POST":
        action = request.POST.get("action", "")
        name = (request.POST.get("name") or "").strip()

        if action == "add_category" and name:
            Category.objects.create(
                name=name, slug=_make_slug(Category, name),
                sla_hours=int(request.POST.get("sla_hours") or 72),
                default_executor=Executor.objects.filter(
                    pk=request.POST.get("executor") or 0).first(),
            )
            log(request.user, "справочник: категория добавлена", name)
        elif action == "save_category":
            cat = Category.objects.filter(pk=request.POST.get("id") or 0).first()
            if cat:
                cat.name = name or cat.name
                cat.sla_hours = int(request.POST.get("sla_hours") or cat.sla_hours)
                cat.default_executor = Executor.objects.filter(
                    pk=request.POST.get("executor") or 0).first()
                cat.is_active = bool(request.POST.get("is_active"))
                cat.save()
                log(request.user, "справочник: категория изменена", cat.name)
        elif action == "add_district" and name:
            District.objects.create(name=name, slug=_make_slug(District, name))
            log(request.user, "справочник: район добавлен", name)
        elif action == "toggle_district":
            d = District.objects.filter(pk=request.POST.get("id") or 0).first()
            if d:
                d.is_active = not d.is_active
                d.save()
        elif action == "add_executor" and name:
            Executor.objects.create(
                name=name, short_name=(request.POST.get("short_name") or "").strip())
            log(request.user, "справочник: исполнитель добавлен", name)
        elif action == "toggle_executor":
            e = Executor.objects.filter(pk=request.POST.get("id") or 0).first()
            if e:
                e.is_active = not e.is_active
                e.save()
        elif action == "add_contact" and (request.POST.get("title") or "").strip():
            Contact.objects.create(
                kind=request.POST.get("kind") or Contact.Kind.OFFICE,
                title=request.POST["title"].strip(),
                phone=(request.POST.get("phone") or "").strip(),
                address=(request.POST.get("address") or "").strip(),
                work_hours=(request.POST.get("work_hours") or "").strip(),
                category=Category.objects.filter(
                    pk=request.POST.get("category") or 0).first(),
                is_public=bool(request.POST.get("is_public")),
            )
            log(request.user, "справочник: контакт добавлен", request.POST["title"])
            # Контакты входят в конфигурацию бота — поднимаем версию
            # (сигнал сделает это сам, строка ниже на случай его отключения).
            BotSetting.bump_version()
        elif action == "delete_contact":
            c = Contact.objects.filter(pk=request.POST.get("id") or 0).first()
            if c:
                log(request.user, "справочник: контакт удалён", c.title)
                c.delete()
        else:
            messages.error(request, "Заполни название — без него не сохранить.")
        return redirect("directory_home")

    return render(request, "directory.html", {
        "section": "directory",
        "categories": Category.objects.select_related("default_executor"),
        "districts": District.objects.all(),
        "executors": Executor.objects.all(),
        "contacts": Contact.objects.select_related("category"),
        "contact_kinds": Contact.Kind.choices,
    })
