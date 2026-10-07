"""Тесты management-команды import_directory."""

import json
import tempfile
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from directory.management.commands.import_directory import readable_territory
from directory.models import District, Executor
from tickets.models import Citizen, Ticket


def org(oid, name_ru=None, name_ky=None, aliases=(), territory=(), visibility="PUBLIC"):
    return {"organization_id": oid, "name_ru": name_ru, "name_ky": name_ky,
            "aliases_ru": list(aliases), "territory": list(territory),
            "visibility": visibility}


class ImportDirectoryTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.write([
            org("org-1", "Водоканал", aliases=["Водоканал Сузак", "ВК"],
                territory=["district:Сузак"]),
            org("org-2", None, "Кыргызча аталышы", territory=["city:гак", "city:Манас"]),
            org("org-3", "Закрытая", visibility="INTERNAL"),
            org("org-4", "Длинная " + "я" * 300, aliases=["а" * 120, "Короткий"],
                territory=["region:Жалал-Абад"]),
            org("org-5", "Без территории"),
        ])

    def write(self, rows, name="organizations.jsonl"):
        (self.dir / name).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
            encoding="utf-8")

    def run_import(self):
        out = StringIO()
        call_command("import_directory", str(self.dir), stdout=out)
        return out.getvalue()

    def test_districts_created_with_names_and_kinds(self):
        self.run_import()
        self.assertEqual(District.objects.filter(kind="district").count(), 8)
        self.assertEqual(District.objects.filter(kind="city").count(), 4)
        manas = District.objects.get(slug="manas")
        self.assertEqual((manas.name, manas.kind, manas.is_active),
                         ("г. Манас (Жалал-Абад)", "city", True))
        self.assertEqual(District.objects.get(slug="toguz-toro").name, "Тогуз-Тороуский район")
        self.assertEqual(District.objects.get(slug="tash-komur").name, "г. Таш-Кумыр")

    def test_old_microdistricts_disabled_not_deleted(self):
        old = [District.objects.create(name=f"Район {i}", slug=f"d0{i}") for i in range(1, 6)]
        citizen = Citizen.objects.create(tg_user_id=1, chat_id=1)
        ticket = Ticket.objects.create(citizen=citizen, title="x", district=old[0])
        self.run_import()
        self.assertEqual(District.objects.filter(slug__startswith="d0", is_active=True).count(), 0)
        self.assertEqual(District.objects.filter(slug__startswith="d0").count(), 5)
        ticket.refresh_from_db()
        self.assertEqual(ticket.district, old[0])
        self.assertEqual(District.objects.filter(is_active=True).count(), 12)

    def test_only_public_executors_imported(self):
        self.run_import()
        self.assertEqual(Executor.objects.filter(external_id__isnull=False).count(), 4)
        self.assertFalse(Executor.objects.filter(external_id="org-3").exists())

    def test_executor_fields(self):
        self.run_import()
        one = Executor.objects.get(external_id="org-1")
        self.assertEqual((one.name, one.short_name, one.territory),
                         ("Водоканал", "Водоканал Сузак", "Сузак (район)"))
        two = Executor.objects.get(external_id="org-2")
        self.assertEqual(two.name, "Кыргызча аталышы")  # name_ru пуст — берём name_ky
        self.assertEqual(two.territory, "г. Манас")      # «city:гак» пропущен
        four = Executor.objects.get(external_id="org-4")
        self.assertEqual(len(four.name), 200)
        self.assertEqual(four.short_name, "Короткий")    # первый алиас не влез в 80
        self.assertEqual(four.territory, "область")
        self.assertEqual(Executor.objects.get(external_id="org-5").territory, "")

    def test_idempotent(self):
        self.run_import()
        snapshot = (District.objects.count(), Executor.objects.count())
        out = self.run_import()
        self.assertEqual((District.objects.count(), Executor.objects.count()), snapshot)
        self.assertIn("Районы: создано 0, обновлено 0", out)
        self.assertIn("Исполнители: создано 0, обновлено 0", out)

    def test_first_run_reports_counts(self):
        out = self.run_import()
        self.assertIn("Районы: создано 12", out)
        self.assertIn("Исполнители: создано 4", out)

    def test_rerun_updates_changed_but_keeps_staff_switches(self):
        self.run_import()
        Executor.objects.filter(external_id="org-1").update(is_active=False)
        District.objects.filter(slug="aksy").update(is_active=False, name="Старое имя")
        self.write([org("org-1", "Водоканал новый", territory=["district:Сузак"])])
        out = self.run_import()
        one = Executor.objects.get(external_id="org-1")
        self.assertEqual(one.name, "Водоканал новый")
        self.assertFalse(one.is_active)
        aksy = District.objects.get(slug="aksy")
        self.assertEqual(aksy.name, "Аксыйский район")
        self.assertFalse(aksy.is_active)
        self.assertIn("Исполнители: создано 0, обновлено 1", out)
        # остальные исполнители из прошлого файла не удалены
        self.assertTrue(Executor.objects.filter(external_id="org-5").exists())

    def test_manual_executors_untouched(self):
        manual = Executor.objects.create(name="Аппарат мэрии", short_name="Мэрия")
        self.run_import()
        manual.refresh_from_db()
        self.assertEqual((manual.name, manual.external_id, manual.territory),
                         ("Аппарат мэрии", None, ""))
        self.assertTrue(manual.is_active)

    def test_missing_file_and_bad_json(self):
        with self.assertRaises(CommandError):
            call_command("import_directory", str(self.dir / "nope"))
        (self.dir / "organizations.jsonl").write_text("{не json\n", encoding="utf-8")
        with self.assertRaises(CommandError):
            call_command("import_directory", str(self.dir), stdout=StringIO())
        self.assertEqual(District.objects.count(), 0)  # транзакция не началась

    def test_readable_territory(self):
        cases = {
            ("district:Сузак",): "Сузак (район)",
            ("city:Манас",): "г. Манас",
            ("ayyl_area:Кашка-Терек",): "а/о Кашка-Терек",
            ("village:Казарман",): "с. Казарман",
            ("region:Жалал-Абад",): "область",
            ("city:гак", "village:Казарман"): "с. Казарман",
            ("city:ГАК",): "",
            ("странное", "x:y"): "",
            (): "",
        }
        for territory, expected in cases.items():
            self.assertEqual(readable_territory(list(territory)), expected, territory)
