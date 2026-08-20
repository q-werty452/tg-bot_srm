"""users/views.py — сотрудники панели: список, добавление, блокировка."""

import secrets

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.shortcuts import redirect, render

from audit.models import log
from botcontrol.views import admin_required
from directory.models import Executor

User = get_user_model()


@admin_required
def staff_list(request):
    if request.method == "POST":
        action = request.POST.get("action", "")

        if action == "add":
            email = (request.POST.get("email") or "").strip().lower()
            if not email or User.objects.filter(email=email).exists():
                messages.error(request, "Email пустой или уже занят.")
                return redirect("staff_list")
            # Пароль генерируется и показывается ОДИН раз — дальше только смена.
            password = secrets.token_urlsafe(9)
            user = User.objects.create_user(
                email=email, password=password,
                first_name=(request.POST.get("first_name") or "").strip(),
                last_name=(request.POST.get("last_name") or "").strip(),
                role=request.POST.get("role") if request.POST.get("role")
                     in User.Role.values else User.Role.OPERATOR,
                department=Executor.objects.filter(
                    pk=request.POST.get("department") or 0).first(),
            )
            log(request.user, "сотрудник добавлен", email)
            messages.success(
                request,
                f"Сотрудник {email} создан. Пароль (показывается один раз): {password}",
            )
        elif action == "toggle":
            user = User.objects.filter(pk=request.POST.get("id") or 0).first()
            if user and user.pk != request.user.pk:  # себя не выключить
                user.is_active = not user.is_active
                user.save(update_fields=["is_active"])
                log(request.user,
                    "сотрудник включён" if user.is_active else "сотрудник отключён",
                    user.email)
        elif action == "reset_password":
            user = User.objects.filter(pk=request.POST.get("id") or 0).first()
            if user:
                password = secrets.token_urlsafe(9)
                user.set_password(password)
                user.save()
                log(request.user, "пароль сотрудника сброшен", user.email)
                messages.success(
                    request, f"Новый пароль для {user.email}: {password}")
        return redirect("staff_list")

    return render(request, "staff.html", {
        "section": "staff",
        "staff": User.objects.select_related("department").order_by("email"),
        "roles": User.Role.choices,
        "departments": Executor.objects.filter(is_active=True),
    })
