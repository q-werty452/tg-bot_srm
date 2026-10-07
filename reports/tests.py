"""Тесты статистики, выгрузки и карты."""

from django.contrib.auth import get_user_model
from django.test import TestCase

from directory.models import Category, District
from tickets.models import Channel, Citizen, Message, Ticket

User = get_user_model()


class ReportsTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user = User.objects.create_user(email="op@meriya.kg",
                                            password="pass12345")
        cat = Category.objects.create(name="Дороги", slug="roads")
        dist = District.objects.create(name="Центр", slug="center")
        citizen = Citizen.objects.create(tg_user_id=1, chat_id=1, first_name="А")
        cls.ticket = Ticket.objects.create(
            citizen=citizen, title="Яма на Ленина", category=cat,
            district=dist, address="Ленина 5")
        message = Message.objects.create(ticket=cls.ticket, author="ai",
                                         text="ответ", rating="up")

    def setUp(self):
        self.client.login(email="op@meriya.kg", password="pass12345")


class StatsTests(ReportsTestCase):
    def test_page_renders_with_data(self):
        response = self.client.get("/stats/")
        self.assertEqual(response.status_code, 200)
        html = response.content.decode()
        self.assertIn("Дороги", html)
        self.assertIn("Центр", html)
        self.assertIn("100%", html)  # доля «помогло»

    def test_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/stats/").status_code, 302)


class ExportTests(ReportsTestCase):
    def test_xlsx_download(self):
        response = self.client.get("/export/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("spreadsheetml", response["Content-Type"])
        self.assertIn(".xlsx", response["Content-Disposition"])

        from io import BytesIO
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(response.content)).active
        rows = list(ws.values)
        self.assertEqual(rows[0][0], "Номер")
        self.assertEqual(rows[0][1], "Канал")
        self.assertEqual(rows[1][1], "Telegram")
        self.assertEqual(rows[1][3], "Яма на Ленина")

    def test_export_respects_filters(self):
        response = self.client.get("/export/?q=несуществующее")
        from io import BytesIO
        from openpyxl import load_workbook
        ws = load_workbook(BytesIO(response.content)).active
        self.assertEqual(len(list(ws.values)), 1, "только заголовок")

    def test_export_filters_by_channel(self):
        wa_citizen = Citizen.objects.create(channel=Channel.WHATSAPP, chat_id=996700123456)
        Ticket.objects.create(citizen=wa_citizen, title="WhatsApp-обращение",
                              channel=Channel.WHATSAPP)
        from io import BytesIO
        from openpyxl import load_workbook

        response = self.client.get("/export/?channel=whatsapp")
        ws = load_workbook(BytesIO(response.content)).active
        rows = list(ws.values)
        self.assertEqual(len(rows), 2)  # заголовок + 1 WhatsApp-заявка
        self.assertEqual(rows[1][3], "WhatsApp-обращение")


class MapTests(ReportsTestCase):
    def test_empty_state(self):
        response = self.client.get("/map/")
        self.assertContains(response, "нет координат")

    def test_page_structure(self):
        response = self.client.get("/map/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "maplibre-gl@4.7.1")
        self.assertContains(response, 'id="map-canvas"')
        self.assertContains(response, "jalalabad_boundaries.geojson")
        self.assertContains(response, "map-config")

    def test_points_in_data_endpoint(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(
            lat=40.93, lon=73.0, geo_source="address")
        data = self.client.get("/map/data/").json()
        self.assertEqual(len(data["features"]), 1)
        props = data["features"][0]["properties"]
        self.assertEqual(props["number"], self.ticket.number)
        self.assertEqual(props["accuracy"], "точный адрес")
        self.assertEqual(data["features"][0]["geometry"]["coordinates"], [73.0, 40.93])
        self.assertEqual(data["meta"]["on_map"], 1)
        self.assertEqual(data["meta"]["district_counts"], {"center": 1})

    def test_ticket_without_coords_counted(self):
        data = self.client.get("/map/data/").json()
        self.assertEqual(data["features"], [])
        self.assertEqual(data["meta"]["without_coords"], 1)

    def test_filters(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(lat=40.93, lon=73.0)
        def n(q):
            return len(self.client.get("/map/data/?" + q).json()["features"])
        self.assertEqual(n("status=all"), 1)
        self.assertEqual(n("status=overdue"), 0)
        self.assertEqual(n("category=roads"), 1)
        self.assertEqual(n("category=nope"), 0)
        self.assertEqual(n("district=center"), 1)
        self.assertEqual(n("kind=question"), 0)
        self.assertEqual(n("period=7"), 1)

    def test_data_queries_do_not_grow(self):
        from django.db import connection
        from django.test.utils import CaptureQueriesContext
        Ticket.objects.filter(pk=self.ticket.pk).update(lat=40.93, lon=73.0)
        with CaptureQueriesContext(connection) as one:
            self.client.get("/map/data/")
        citizen = Citizen.objects.get(tg_user_id=1)
        for i in range(15):
            Ticket.objects.create(citizen=citizen, title=f"t{i}", lat=40.9, lon=73.1)
        with CaptureQueriesContext(connection) as many:
            self.client.get("/map/data/")
        self.assertEqual(len(one), len(many))

    def test_focus_ticket_included_despite_filters(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(
            lat=40.93, lon=73.0, status=Ticket.Status.DONE)
        self.assertEqual(len(self.client.get("/map/data/?status=open").json()["features"]), 0)
        data = self.client.get(f"/map/data/?status=open&ticket={self.ticket.number}").json()
        self.assertEqual(len(data["features"]), 1)
        page = self.client.get(f"/map/?ticket={self.ticket.number}")
        self.assertEqual(page.status_code, 200)

    def test_failed_geocode_not_on_map(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(geo_source="failed")
        data = self.client.get("/map/data/").json()
        self.assertEqual(data["features"], [])
        self.assertEqual(data["meta"]["failed"], 1)

    def test_data_requires_login(self):
        self.client.logout()
        self.assertEqual(self.client.get("/map/data/").status_code, 302)


class TicketMinimapTests(ReportsTestCase):
    def test_minimap_with_coords(self):
        Ticket.objects.filter(pk=self.ticket.pk).update(lat=40.93, lon=73.0, geo_source="settlement")
        html = self.client.get(f"/tickets/{self.ticket.number}/").content.decode()
        self.assertIn("ticket-minimap", html)
        self.assertIn(f"/map/?ticket={self.ticket.number}", html)
        self.assertIn("примерно", html)

    def test_no_coords_with_address(self):
        html = self.client.get(f"/tickets/{self.ticket.number}/").content.decode()
        self.assertNotIn("ticket-minimap", html)
        self.assertIn("Адрес ещё не найден на карте", html)


class GeocodeBoundsTests(TestCase):
    """
    Проверка рамки области. Раньше рамка была размером с город Джалал-Абад,
    и адрес из другого райцентра той же области (например, Таш-Кумыра)
    отбрасывался как «слишком далеко». Теперь аппарат ведёт всю область —
    рамка должна принимать любую точку внутри неё и отбрасывать только то,
    что объективно за её пределами.
    """

    def test_region_points_accepted(self):
        from reports.management.commands.geocode import inside_region
        self.assertTrue(inside_region(40.9333, 72.9833))    # Джалал-Абад (центр)
        self.assertTrue(inside_region(41.34717, 72.22169))  # Таш-Кумыр, та же область

    def test_far_points_rejected(self):
        from reports.management.commands.geocode import inside_region
        self.assertFalse(inside_region(42.87, 74.59))        # Бишкек
        self.assertFalse(inside_region(41.2995, 69.2401))    # Ташкент, другая страна
        self.assertFalse(inside_region(0, 0))                # пустой ответ сервиса

    def test_bounds_are_sane(self):
        from reports.management.commands.geocode import LAT_MAX, LAT_MIN, LON_MAX, LON_MIN
        self.assertLess(LAT_MAX - LAT_MIN, 3.5, "рамка не должна быть на пол-страны")
        self.assertLess(LON_MAX - LON_MIN, 5.0, "рамка не должна быть на пол-страны")


class TemplateHygieneTests(TestCase):
    """
    Страницы не должны показывать служебный текст.

    Появилось после реального случая: многострочный комментарий {# ... #}
    Django комментарием НЕ считает (только однострочный) и вывел пояснение
    для разработчика прямо в таблицу обращений на глазах у заказчика.
    Для многострочных есть тег comment.
    """

    @classmethod
    def setUpTestData(cls):
        cls.admin = get_user_model().objects.create_user(
            email="adm@meriya.kg", password="pass12345", role="admin")

    def setUp(self):
        self.client.login(email="adm@meriya.kg", password="pass12345")

    def test_no_leaked_comment_markers_on_pages(self):
        for url in ("/", "/tickets/new/", "/bot/settings/", "/bot/keys/",
                    "/bot/answers/", "/broadcasts/", "/stats/", "/map/",
                    "/directory/", "/staff/", "/audit/"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                for marker in ("{#", "#}", "{%", "%}"):
                    self.assertNotIn(marker, html,
                                     f"на странице {url} видна разметка шаблона")

    def test_templates_have_no_multiline_hash_comments(self):
        """Однострочный {# … #} допустим, многострочный — нет."""
        from django.conf import settings
        offenders = []
        for template_dir in [settings.BASE_DIR / "templates"]:
            for path in template_dir.rglob("*.html"):
                for number, line in enumerate(path.read_text().splitlines(), 1):
                    if "{#" in line and "#}" not in line:
                        offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [],
                         "многострочные {# #} выводятся на страницу как текст")


class ExportEnrichedTests(ReportsTestCase):
    def test_new_fields_exported(self):
        from io import BytesIO
        from openpyxl import load_workbook
        Citizen.objects.filter(tg_user_id=1).update(
            last_name="Иванов", first_name="Айбек", middle_name="Маратович",
            phone="+996700111222")
        Ticket.objects.filter(pk=self.ticket.pk).update(
            settlement="Кашка-Терек", kind=Ticket.Kind.APPEAL)
        ws = load_workbook(BytesIO(self.client.get("/export/").content)).active
        rows = list(ws.values)
        row = dict(zip(rows[0], rows[1]))
        self.assertEqual(row["Тип обращения"], "Обращение/жалоба")
        self.assertEqual(row["Населённый пункт"], "Кашка-Терек")
        self.assertEqual(row["Житель"], "Иванов Айбек Маратович")
        self.assertEqual((row["Фамилия"], row["Имя"], row["Отчество"]),
                         ("Иванов", "Айбек", "Маратович"))
        self.assertEqual(row["Телефон"], "+996700111222")

    def test_export_filters_by_kind_search_and_slugs(self):
        from io import BytesIO
        from openpyxl import load_workbook
        Ticket.objects.filter(pk=self.ticket.pk).update(
            settlement="Кашка-Терек", kind=Ticket.Kind.APPEAL)

        def count(query):
            ws = load_workbook(BytesIO(self.client.get("/export/?" + query).content)).active
            return len(list(ws.values)) - 1

        self.assertEqual(count("kind=appeal"), 1)
        self.assertEqual(count("kind=question"), 0)
        self.assertEqual(count("q=Кашка"), 1)
        self.assertEqual(count("district=center&category=roads"), 1)
        self.assertEqual(count("district=nowhere"), 0)


class MapWithRegionDistrictsTests(ReportsTestCase):
    def test_map_does_not_depend_on_district_list(self):
        District.objects.update(is_active=False)
        District.objects.create(name="Сузакский район", slug="suzak", kind="district")
        response = self.client.get("/map/")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "jalalabad_boundaries.geojson")
