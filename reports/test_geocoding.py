"""Тесты геокодера: запрос, каскад, кэш, рамка области, приоритет точки человека."""

import importlib
from unittest import mock

import httpx
from django.test import TestCase, override_settings

from directory.models import District
from reports import geocoding
from reports.models import GeocodeCache
from tickets.models import Citizen, Ticket


class FakeResponse:
    def __init__(self, rows, status=200):
        self._rows, self.status_code = rows, status

    def json(self):
        return self._rows


def row(lat, lon, rank=26):
    return {"lat": str(lat), "lon": str(lon), "place_rank": rank}


class GeoBase(TestCase):
    def setUp(self):
        self.citizen = Citizen.objects.create(tg_user_id=5, chat_id=5)
        self.district = District.objects.create(name="Сузакский район", slug="suzak", kind="district")
        self.ticket = Ticket.objects.create(
            citizen=self.citizen, address="ул. Ленина 5", settlement="Сузак", district=self.district)
        # сеть запрещена: любой неожиданный запрос роняет тест
        patcher = mock.patch("reports.geocoding.httpx.get", side_effect=AssertionError("сеть запрещена"))
        self.get = patcher.start()
        self.addCleanup(patcher.stop)
        for name in ("sleep",):
            p = mock.patch(f"reports.geocoding.time.{name}")
            p.start()
            self.addCleanup(p.stop)

    def answer(self, *responses):
        self.get.side_effect = list(responses)


class BuildQueryTests(GeoBase):
    def test_full(self):
        self.assertEqual(
            geocoding.build_query(self.ticket),
            "ул. Ленина 5, Сузак, Сузакский район, Джалал-Абадская область, Кыргызстан")

    def test_empty_without_address_and_settlement(self):
        self.ticket.address = self.ticket.settlement = ""
        self.assertEqual(geocoding.build_query(self.ticket), "")

    def test_settlement_only_and_whitespace(self):
        self.ticket.address = "  "
        self.ticket.settlement = " Кашка\n Терек "
        self.assertTrue(geocoding.build_query(self.ticket).startswith("Кашка Терек, Сузакский"))

    def test_legacy_district_ignored(self):
        self.ticket.district = District.objects.create(name="Центр", slug="d01")
        self.assertNotIn("Центр", geocoding.build_query(self.ticket))


class CascadeTests(GeoBase):
    def test_address_found(self):
        self.answer(FakeResponse([row(40.93, 72.98)]))
        self.assertTrue(geocoding.geocode_ticket(self.ticket))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "address")
        self.assertEqual(self.ticket.geo_query, geocoding.build_query(self.ticket))

    def test_falls_back_to_settlement(self):
        self.answer(FakeResponse([]), FakeResponse([row(40.9, 72.9, rank=19)]))
        self.assertTrue(geocoding.geocode_ticket(self.ticket))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "settlement")

    def test_vague_address_result_is_approximate(self):
        self.answer(FakeResponse([row(40.9, 72.9, rank=19)]))
        geocoding.geocode_ticket(self.ticket)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "settlement")

    def test_outside_region_rejected(self):
        # Бишкек, затем Ош (в рамке, но не в области): оба отброшены
        self.answer(FakeResponse([row(42.87, 74.59)]), FakeResponse([row(40.53, 72.8)]))
        self.assertFalse(geocoding.geocode_ticket(self.ticket))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "failed")
        self.assertIsNone(self.ticket.lat)

    def test_cache_prevents_second_request(self):
        self.answer(FakeResponse([row(40.93, 72.98)]))
        geocoding.geocode_ticket(self.ticket)
        other = Ticket.objects.create(citizen=self.citizen, address="ул. Ленина 5",
                                      settlement="Сузак", district=self.district)
        self.assertTrue(geocoding.geocode_ticket(other))
        self.assertEqual(self.get.call_count, 1)
        self.assertEqual(GeocodeCache.objects.count(), 1)

    def test_network_error_not_cached_and_not_failed(self):
        self.get.side_effect = httpx.ConnectError("нет сети")
        self.assertEqual(geocoding.run_geocoding(self.ticket), geocoding.ERROR)
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "")
        self.assertEqual(GeocodeCache.objects.count(), 0)

    def test_http_error_status_is_not_a_negative_answer(self):
        self.answer(FakeResponse([], 403), FakeResponse([], 403))
        self.assertEqual(geocoding.run_geocoding(self.ticket), geocoding.ERROR)
        self.assertEqual(GeocodeCache.objects.count(), 0)

    def test_retry_once(self):
        self.answer(httpx.ReadTimeout("t"), FakeResponse([row(40.93, 72.98)]))
        self.assertTrue(geocoding.geocode_ticket(self.ticket))
        self.assertEqual(self.get.call_count, 2)

    def test_regeocode_only_when_query_changes(self):
        self.answer(FakeResponse([row(40.93, 72.98)]), FakeResponse([row(40.94, 72.99)]))
        geocoding.geocode_ticket(self.ticket)
        self.assertFalse(geocoding.geocode_ticket(self.ticket))
        self.assertEqual(self.get.call_count, 1)
        self.ticket.address = "ул. Мира 1"
        self.assertTrue(geocoding.geocode_ticket(self.ticket))
        self.assertEqual(self.get.call_count, 2)

    def test_cleared_address_drops_auto_point(self):
        self.answer(FakeResponse([row(40.93, 72.98)]))
        geocoding.geocode_ticket(self.ticket)
        self.ticket.address = self.ticket.settlement = ""
        geocoding.geocode_ticket(self.ticket)
        self.ticket.refresh_from_db()
        self.assertIsNone(self.ticket.lat)
        self.assertEqual(self.ticket.geo_source, "")

    def test_rate_limit_waits(self):
        with mock.patch("reports.geocoding.time.monotonic", side_effect=[0.5, 0.5, 5.0]), \
                mock.patch("reports.geocoding.time.sleep") as sleep:
            geocoding._last_request_at = 0.0
            geocoding._throttle()
            self.assertTrue(sleep.called)


class HumanPointTests(GeoBase):
    def test_pin_and_manual_not_overwritten(self):
        for source in ("pin", "manual"):
            Ticket.objects.filter(pk=self.ticket.pk).update(lat=40.9, lon=72.9, geo_source=source)
            self.ticket.refresh_from_db()
            self.assertFalse(geocoding.geocode_ticket(self.ticket))
        self.assertEqual(self.get.call_count, 0)

    def test_apply_pin_overrides_address_not_manual(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(lat=1.0, lon=1.0, geo_source="address")
        self.ticket.refresh_from_db()
        self.assertTrue(geocoding.apply_pin(self.ticket, 40.93, 72.98))
        self.ticket.refresh_from_db()
        self.assertEqual(self.ticket.geo_source, "pin")
        Ticket.objects.filter(pk=self.ticket.pk).update(geo_source="manual")
        self.ticket.refresh_from_db()
        self.assertFalse(geocoding.apply_pin(self.ticket, 40.94, 72.99))

    def test_apply_pin_outside_region(self):
        self.assertFalse(geocoding.apply_pin(self.ticket, 42.87, 74.59))   # Бишкек
        self.assertFalse(geocoding.apply_pin(self.ticket, 40.53, 72.80))   # Ош
        self.assertFalse(geocoding.apply_pin(self.ticket, "x", None))


class InsideOblastTests(TestCase):
    def test_polygon(self):
        self.assertTrue(geocoding.inside_oblast(40.93, 72.98))
        self.assertTrue(geocoding.inside_oblast(41.34717, 72.22169))
        self.assertFalse(geocoding.inside_oblast(40.53, 72.80))


class ScheduleTests(GeoBase):
    def test_disabled_in_tests(self):
        from django.conf import settings
        self.assertFalse(settings.GEOCODE_ENABLED)
        with mock.patch("reports.geocoding._enqueue") as enqueue:
            geocoding.schedule_geocode(self.ticket.pk)
        enqueue.assert_not_called()

    @override_settings(GEOCODE_ENABLED=True)
    def test_enabled_enqueues_after_commit(self):
        with mock.patch("reports.geocoding._enqueue") as enqueue, \
                self.captureOnCommitCallbacks(execute=True):
            geocoding.schedule_geocode(self.ticket.pk)
        enqueue.assert_called_once_with(self.ticket.pk)


class MigrationLogicTests(GeoBase):
    def test_failed_mark_converted(self):
        mod = importlib.import_module("tickets.migrations.0006_ticket_geo_query_ticket_geo_source")
        Ticket.objects.filter(pk=self.ticket.pk).update(lat=-1000, lon=-1000)
        ok = Ticket.objects.create(citizen=self.citizen, lat=40.9, lon=72.9)
        from django.apps import apps
        mod.failed_marks_to_geo_source(apps, None)
        self.ticket.refresh_from_db(); ok.refresh_from_db()
        self.assertEqual((self.ticket.geo_source, self.ticket.lat), ("failed", None))
        self.assertEqual(ok.geo_source, "address")


class GeoFilesTests(TestCase):
    def test_boundaries_file(self):
        import json
        from django.conf import settings
        d = json.loads((settings.BASE_DIR / "static/geo/jalalabad_boundaries.geojson").read_text())
        slugs = {f["properties"]["slug"] for f in d["features"]}
        self.assertEqual(len(d["features"]), 13)
        self.assertIn("aksy", slugs)
        self.assertIn("manas", slugs)
