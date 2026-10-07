/*
 * engine-leaflet.js — запасной движок большой карты: Leaflet + подложка Esri.
 *
 * Включается, когда в браузере нет WebGL (старые машины, удалённые сессии,
 * отключённое ускорение) или не загрузился MapLibre. Умеет то же, что
 * основной, в упрощённом виде: пелена за границей области, сплошная
 * граница со свечением, пунктир районов, подсветка и плотность районов,
 * кластеры точек, попапы. Анимации — на CSS (static/map/map.css) и
 * ступенчатые, чтобы слабая машина не задыхалась: это запасной путь,
 * а не витрина.
 *
 * Данные, фильтры, счётчики и тексты — общие с основным движком (map-core.js).
 */
(function (global) {
  'use strict';

  const M = global.ManasMap;
  M.engines = M.engines || {};

  const ESRI = 'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/';
  const EMPTY = { type: 'FeatureCollection', features: [] };

  M.engines.leaflet = function createLeafletEngine(ctx) {
    const L = global.L;
    const P = M.palette();
    const region = ctx.boundaries.features.find((f) => f.properties.kind === 'region');
    const territories = ctx.boundaries.features.filter((f) => f.properties.kind !== 'region');
    const regionBBox = M.bboxOf(region.geometry);
    const regionBounds = L.latLngBounds([regionBBox[1], regionBBox[0]], [regionBBox[3], regionBBox[2]]);
    const latLngs = (ring) => ring.map((p) => [p[1], p[0]]);

    let map = null;
    let pointLayer = null;
    let index = null;
    let points = EMPTY;
    let selected = '';
    let selectedRing = null;
    let density = false;
    let counts = {};
    let popup = null;
    const layers = {};   // slug → слой района
    const info = {};     // slug → {slug, kind, name}
    const reduced = () => M.reducedMotion();

    territories.forEach((f) => {
      const slug = f.properties.slug;
      info[slug] = { slug, kind: f.properties.kind, name: ctx.names[slug] || f.properties.name };
    });

    // Мягкая шкала плотности — та же, что у основного движка.
    function densityColor(n, max) {
      const stops = ['#EAF1FF', '#BCD2F8', '#86AEF0', '#4F83E6', '#1F4FB0'];
      const k = max ? n / max : 0;
      return stops[Math.min(stops.length - 1, Math.round(k * (stops.length - 1)))];
    }

    function styleOf(slug, hover) {
      const n = counts[slug] || 0;
      const max = Math.max.apply(null, [1].concat(Object.keys(counts).map((k) => counts[k])));
      const filled = density && n > 0;
      return {
        color: P.accent, weight: hover ? 2.4 : 1.3, opacity: hover ? 0.95 : 0.9, dashArray: '6 5',
        fillColor: filled ? densityColor(n, max) : P.accent,
        fillOpacity: hover ? (filled ? 0.7 : 0.14) : (filled ? 0.55 : 0),
      };
    }

    function restyle() {
      Object.keys(layers).forEach((slug) => layers[slug].setStyle(styleOf(slug, false)));
    }

    function padding() {
      return { paddingTopLeft: [ctx.insetLeft() + 30, 40], paddingBottomRight: [60, 40] };
    }

    function applyConstraints() {
      const zoom = map.getBoundsZoom(regionBounds, false, L.point(ctx.insetLeft() + 90, 80));
      map.setMinZoom(Math.max(5, zoom - 0.5));
      map.setMaxBounds(regionBounds.pad(0.6));
    }

    function fitOblast(animate) {
      map.fitBounds(regionBounds, Object.assign({ animate: !!animate && !reduced(), maxZoom: 10 }, padding()));
    }

    function fitTerritory(slug) {
      const f = territories.find((t) => t.properties.slug === slug);
      if (!f) return;
      const box = M.bboxOf(f.geometry);
      map.fitBounds([[box[1], box[0]], [box[3], box[2]]], Object.assign(
        { animate: !reduced(), maxZoom: 11 }, padding()));
    }

    // ------------------------------------------------------------ точки

    function colorOf(p) { return M.safeColor(p.color, P.muted); }

    function singleMarker(feature) {
      const p = feature.properties;
      const at = [feature.geometry.coordinates[1], feature.geometry.coordinates[0]];
      const group = L.layerGroup();
      if (p.approx) {  // ореол: место определено только по населённому пункту
        L.circleMarker(at, { radius: 22, stroke: true, weight: 1.5, color: colorOf(p), opacity: 0.45,
          fillColor: colorOf(p), fillOpacity: 0.16, interactive: false }).addTo(group);
      }
      if (p.overdue) {
        L.circleMarker(at, { radius: 13, weight: 2, color: P.danger, opacity: 0.95, fill: false,
          interactive: false, className: 'leaflet-overdue-ring' }).addTo(group);
      }
      const dot = L.circleMarker(at, { radius: 8, weight: 2, color: p.pin ? P.navy : P.white, opacity: 1,
        fillColor: colorOf(p), fillOpacity: 1 }).addTo(group);
      if (p.pin) {  // белый центр — геометка жителя
        L.circleMarker(at, { radius: 3, stroke: false, fillColor: P.white, fillOpacity: 1, interactive: false })
          .addTo(group);
      }
      dot.on('click', () => {
        // Несколько заявок в одной точке — показываем списком.
        const same = points.features.filter((f) =>
          f.geometry.coordinates[0] === feature.geometry.coordinates[0] &&
          f.geometry.coordinates[1] === feature.geometry.coordinates[1]);
        openPopup(same.length ? same : [feature], at);
      });
      return group;
    }

    function clusterMarker(cluster) {
      const count = cluster.properties.point_count;
      const size = count < 10 ? 32 : count < 50 ? 40 : count < 200 ? 48 : 58;
      const color = count < 10 ? P.accent : count < 50 ? '#2557B8' : count < 200 ? '#183F91' : P.navy;
      const overdue = cluster.properties.overdue > 0;
      const icon = L.divIcon({
        className: 'leaflet-cluster' + (overdue ? ' has-overdue' : ''),
        html: '<span style="background:' + color + '">' + (cluster.properties.point_count_abbreviated || count) + '</span>',
        iconSize: [size, size],
      });
      const marker = L.marker([cluster.geometry.coordinates[1], cluster.geometry.coordinates[0]], { icon });
      marker.on('click', () => {
        const target = index.getClusterExpansionZoom(cluster.properties.cluster_id);
        if (target > map.getZoom() + 0.05) {
          map.setView(marker.getLatLng(), Math.min(target + 0.15, map.getMaxZoom()), { animate: !reduced() });
        } else {
          openPopup(index.getLeaves(cluster.properties.cluster_id, 50, 0), marker.getLatLng());
        }
      });
      return marker;
    }

    function redraw() {
      if (!map || !index) return;
      pointLayer.clearLayers();
      const b = map.getBounds().pad(0.2);
      const zoom = Math.max(0, Math.min(16, Math.round(map.getZoom())));
      index.getClusters([b.getWest(), b.getSouth(), b.getEast(), b.getNorth()], zoom).forEach((item) => {
        (item.properties.cluster ? clusterMarker(item) : singleMarker(item)).addTo(pointLayer);
      });
    }

    function openPopup(features, at) {
      closePopup();
      const seen = new Set();
      const unique = features.filter((f) => !seen.has(f.properties.number) && seen.add(f.properties.number));
      selectTicket(unique[0].properties.number);
      popup = L.popup({ maxWidth: 340, className: 'map-popup', offset: [0, -6] })
        .setLatLng(at).setContent(M.popupContent(unique.map((f) => f.properties), ctx.ticketUrl)).openOn(map);
      const mine = popup;
      map.once('popupclose', () => { if (popup === mine) { popup = null; selectTicket(''); } });
    }

    function closePopup() {
      if (popup) { const p = popup; popup = null; map.closePopup(p); }
    }

    function selectTicket(number) {
      selected = number || '';
      if (selectedRing) { map.removeLayer(selectedRing); selectedRing = null; }
      const feature = selected && points.features.find((f) => f.properties.number === selected);
      if (feature) {
        selectedRing = L.circleMarker([feature.geometry.coordinates[1], feature.geometry.coordinates[0]], {
          radius: 18, weight: 2.5, color: P.accent, fillColor: P.accent, fillOpacity: 0.12, interactive: false,
        }).addTo(map);
      }
    }

    // ------------------------------------------------------------ интерфейс движка

    const engine = {
      name: 'leaflet',

      async init() {
        map = L.map(ctx.container, {
          zoomControl: false, zoomSnap: 0.25, minZoom: 5, maxZoom: 16, worldCopyJump: false,
          maxBoundsViscosity: 1,
        });
        map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener">Leaflet</a>');
        L.control.zoom({ position: 'topright', zoomInTitle: 'Приблизить', zoomOutTitle: 'Отдалить' }).addTo(map);
        const Home = L.Control.extend({
          onAdd() {
            const box = L.DomUtil.create('div', 'leaflet-bar');
            const button = L.DomUtil.create('a', 'map-home-btn', box);
            button.href = '#';
            button.title = 'Показать всю область';
            button.setAttribute('role', 'button');
            button.setAttribute('aria-label', 'Показать всю область');
            button.innerHTML = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" ' +
              'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
              '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
            L.DomEvent.on(button, 'click', (e) => { L.DomEvent.stop(e); fitOblast(true); });
            return box;
          },
        });
        map.addControl(new Home({ position: 'topright' }));
        L.control.scale({ imperial: false, position: 'bottomright', maxWidth: 110 }).addTo(map);

        // Слои друг над другом: подложка (200) → пелена → районы → граница → заявки (600).
        [['mask', 250], ['territories', 260], ['region', 270]].forEach((pane) => {
          map.createPane(pane[0]).style.zIndex = pane[1];
        });

        const attribution = 'Подложка © Esri · границы © участники ' +
          '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a>';
        L.tileLayer(ESRI + 'World_Light_Gray_Base/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16, attribution }).addTo(map);
        L.tileLayer(ESRI + 'World_Light_Gray_Reference/MapServer/tile/{z}/{y}/{x}', { maxZoom: 16 }).addTo(map);

        // Пелена: кольцо на весь мир с «дыркой» по границе области (even-odd).
        const world = [[-85, -180], [-85, 180], [85, 180], [85, -180]];
        const holes = M.polygonsOf(region.geometry).map((polygon) => latLngs(polygon[0]));
        L.polygon([world].concat(holes), {
          pane: 'mask', stroke: false, fillColor: P.bg, fillOpacity: M.MASK_OPACITY, fillRule: 'evenodd',
          interactive: false, className: 'leaflet-mask',
        }).addTo(map);

        // Районы и города: пунктир, подсветка при наведении.
        territories.forEach((f) => {
          const slug = f.properties.slug;
          const layer = L.geoJSON(f, {
            pane: 'territories', style: () => Object.assign(styleOf(slug, false), { className: 'leaflet-district' }),
          }).addTo(map);
          layers[slug] = layer;
          layer.on('mouseover', () => { layer.setStyle(styleOf(slug, true)); });
          layer.on('mousemove', (e) => { ctx.events.hover(info[slug], e.containerPoint); });
          layer.on('mouseout', () => { layer.setStyle(styleOf(slug, false)); ctx.events.hover(null); });
          layer.on('click', () => { fitTerritory(slug); });
        });

        // Граница области: свечение под линией, обе «прорисовываются» через pathLength.
        const lines = M.regionLines(region).features.map((f) => latLngs(f.geometry.coordinates));
        const glow = L.polyline(lines, { pane: 'region', color: P.accent, weight: 12, opacity: 0.3,
          lineCap: 'round', lineJoin: 'round', interactive: false, className: 'leaflet-oblast-glow' }).addTo(map);
        const line = L.polyline(lines, { pane: 'region', color: P.navy, weight: 3, opacity: 1,
          lineCap: 'round', lineJoin: 'round', interactive: false, className: 'leaflet-oblast-line' }).addTo(map);
        [glow, line].forEach((layer) => {
          const path = layer.getElement && layer.getElement();
          if (path) path.setAttribute('pathLength', '1');
        });

        pointLayer = L.layerGroup().addTo(map);
        map.on('moveend', redraw);
        map.on('resize', () => { applyConstraints(); });

        fitOblast(false);
        applyConstraints();
        fitOblast(false);
      },

      setPoints(collection) {
        points = collection;
        index = new global.Supercluster({
          radius: 52, maxZoom: 16,
          map: (props) => ({ overdue: props.overdue ? 1 : 0 }),
          reduce: (acc, props) => { acc.overdue += props.overdue; },
        });
        index.load(collection.features);
        redraw();
        if (selected && !collection.features.some((f) => f.properties.number === selected)) {
          closePopup();
          selectTicket('');
        }
      },

      setCounts(newCounts) { counts = newCounts || {}; restyle(); },
      setDensity(on) { density = !!on; restyle(); },

      showTicket(feature, options) {
        const at = [feature.geometry.coordinates[1], feature.geometry.coordinates[0]];
        const zoom = feature.properties.approx ? 12 : 15;
        selectTicket(feature.properties.number);
        const animate = !!(options && options.fly) && !reduced();
        map.setView(at, zoom, { animate });
        // Попап открываем после перелёта: во время анимации он «уезжает».
        if (animate) map.once('moveend', () => openPopup([feature], at)); else openPopup([feature], at);
      },

      selectTicket, fitOblast, fitTerritory,

      insetChanged() { if (map) { applyConstraints(); } },
      resize() { if (map) map.invalidateSize(); },

      destroy() {
        if (map) { map.remove(); map = null; }
      },
    };

    return engine;
  };
})(window);
