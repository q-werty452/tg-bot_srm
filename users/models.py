"""
users/models.py — сотрудники панели.

Вход по email. Роль решает, что человеку доступно:
  admin    — всё, включая ключи, рассылки и учётки;
  operator — обращения, справочники на чтение, ответы жителям.
"""

from django.contrib.auth.models import AbstractUser
from django.db import models

from .managers import UserManager


class User(AbstractUser):
    class Role(models.TextChoices):
        ADMIN = "admin", "Администратор"
        OPERATOR = "operator", "Оператор"

    username = None
    email = models.EmailField("email", unique=True)
    role = models.CharField("роль", max_length=16, choices=Role.choices, default=Role.OPERATOR)
    department = models.ForeignKey(
        "directory.Executor",
        verbose_name="отдел / предприятие",
        null=True, blank=True,
        on_delete=models.SET_NULL,
        related_name="staff",
    )

    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = []

    objects = UserManager()

    class Meta:
        verbose_name = "сотрудник"
        verbose_name_plural = "сотрудники"

    def __str__(self):
        return self.get_full_name() or self.email

    @property
    def is_admin(self) -> bool:
        return self.role == self.Role.ADMIN or self.is_superuser
