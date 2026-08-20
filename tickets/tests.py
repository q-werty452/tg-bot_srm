"""Тесты моделей обращений: номера, сроки, просрочка."""

from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from directory.models import Category
from tickets.models import Citizen, Ticket, TicketCounter


def make_citizen(uid=1001):
    return Citizen.objects.create(tg_user_id=uid, chat_id=uid, first_name="Тест")


class NumberTests(TestCase):
    def test_sequential_numbers_within_year(self):
        c = make_citizen()
        year = timezone.localdate().year
        t1 = Ticket.objects.create(citizen=c, title="Первая")
        t2 = Ticket.objects.create(citizen=c, title="Вторая")
        self.assertEqual(t1.number, f"{year}-0001")
        self.assertEqual(t2.number, f"{year}-0002")

    def test_number_unique_and_immutable(self):
        c = make_citizen()
        t = Ticket.objects.create(citizen=c, title="Заявка")
        original = t.number
        t.title = "Переименована"
        t.save()
        t.refresh_from_db()
        self.assertEqual(t.number, original)

    def test_counter_isolated_by_year(self):
        TicketCounter.objects.create(year=2020, value=500)
        c = make_citizen()
        t = Ticket.objects.create(citizen=c)
        self.assertNotIn("2020", t.number)
        self.assertTrue(t.number.endswith("-0001"))


class DueAndOverdueTests(TestCase):
    def setUp(self):
        self.citizen = make_citizen()
        self.category = Category.objects.create(name="Дороги", slug="roads", sla_hours=48)

    def test_due_at_from_category_sla(self):
        t = Ticket.objects.create(citizen=self.citizen, category=self.category)
        self.assertIsNotNone(t.due_at)
        expected = timezone.now() + timedelta(hours=48)
        self.assertLess(abs((t.due_at - expected).total_seconds()), 60)

    def test_no_category_no_due(self):
        t = Ticket.objects.create(citizen=self.citizen)
        self.assertIsNone(t.due_at)
        self.assertFalse(t.is_overdue)

    def test_overdue_only_when_open_and_past_due(self):
        t = Ticket.objects.create(citizen=self.citizen, category=self.category)
        self.assertFalse(t.is_overdue)

        t.due_at = timezone.now() - timedelta(hours=1)
        t.save()
        self.assertTrue(t.is_overdue)

        t.status = Ticket.Status.DONE
        t.save()
        self.assertFalse(t.is_overdue, "закрытая заявка не бывает просроченной")

    def test_overdue_queryset(self):
        past = timezone.now() - timedelta(hours=1)
        t1 = Ticket.objects.create(citizen=self.citizen, category=self.category)
        Ticket.objects.filter(pk=t1.pk).update(due_at=past)
        t2 = Ticket.objects.create(citizen=self.citizen, category=self.category)
        t3 = Ticket.objects.create(citizen=self.citizen, category=self.category,
                                   status=Ticket.Status.DONE)
        Ticket.objects.filter(pk=t3.pk).update(due_at=past)

        overdue = set(Ticket.objects.overdue().values_list("pk", flat=True))
        self.assertEqual(overdue, {t1.pk})
        self.assertIn(t2.pk, set(Ticket.objects.open().values_list("pk", flat=True)))
