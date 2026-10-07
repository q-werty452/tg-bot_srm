/*
 * engine-maplibre.js — основной движок большой карты: MapLibre GL JS
 * поверх векторной подложки OpenFreeMap.
 *
 * Что рисуем, снизу вверх:
 *   подложка → белая пелена за границей области (с «дыркой» в форме области)
 *   → заливка районов (подсветка при наведении, плотность обращений)
 *   → пунктир районов («бегущие муравьи»)
 *   → граница области: сплошная линия с мягким свечением
 *   → подписи районов → точки и кластеры заявок.
 *
 * Анимация устроена экономно: после короткого вступления (пелена проявляется,
 * затем граница «прорисовывается») крутится один цикл requestAnimationFrame
 * на ~12 кадров в секунду; он стоит, пока вкладка скрыта, пока карту двигают
 * руками, при prefers-reduced-motion — и совсем отключается, если машина не
 * успевает (программный WebGL в удалённых сессиях).
 */
(function (global) {
  'use strict';

  const M = global.ManasMap;
  M.engines = M.engines || {};

  // Бегущий пунктир: штрих 3 толщины, пробел 2.4, полный оборот за 1.6 с.
  const DASH_FRAMES = M.dashFrames(3, 2.4, 16);
  const DASH_CYCLE_MS = 1600;
  const TICK_MS = 1000 / 12;
  const GLOW_PERIOD_MS = 4600;   // период «дыхания» свечения
  const PULSE_PERIOD_MS = 2000;  // период пульса просроченных заявок

  // Вступление, мс: пелена → граница → пунктир и подписи → точки.
  const INTRO = { mask: 600, lineFrom: 450, lineTo: 2300, dashFrom: 1500, dashTo: 2300, end: 2500 };

  function hexToRgb(hex) {
    const n = parseInt(hex.replace('#', ''), 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  // Градиент вдоль линии: до доли p — цвет, дальше прозрачно. Так линия
  // «прорисовывается» (line-progress у MapLibre — доля пути от начала линии).
  function lineGradient(hex, p) {
    const [r, g, b] = hexToRgb(hex);
    const solid = 'rgba(' + r + ',' + g + ',' + b + ',1)';
    const clear = 'rgba(' + r + ',' + g + ',' + b + ',0)';
    if (p <= 0.0005) return ['interpolate', ['linear'], ['line-progress'], 0, clear, 1, clear];
    if (p >= 0.9995) return ['interpolate', ['linear'], ['line-progress'], 0, solid, 1, solid];
    return ['interpolate', ['linear'], ['line-progress'], 0, solid, p, solid, Math.min(p + 0.002, 0.9999), clear, 1, clear];
  }

  // Размер, зависящий от масштаба: [[zoom, значение], ...] → interpolate.
  // zoom в MapLibre можно использовать только в самом внешнем interpolate,
  // поэтому вариации (прибавка к радиусу и т. п.) считаем заранее.
  function byZoom(stops, base) {
    const expr = ['interpolate', base ? ['exponential', base] : ['linear'], ['zoom']];
    stops.forEach((s) => expr.push(s[0], s[1]));
    return expr;
  }

  // Старые версии MapLibre принимали callback, новые отдают Promise.
  function asPromise(call) {
    return new Promise((resolve, reject) => {
      const result = call((error, value) => (error ? reject(error) : resolve(value)));
      if (result && typeof result.then === 'function') result.then(resolve, reject);
    });
  }

  M.engines.maplibre = function createMapLibreEngine(ctx) {
    const ml = global.maplibregl;
    const P = M.palette();
    const region = ctx.boundaries.features.find((f) => f.properties.kind === 'region');
    const territories = {
      type: 'FeatureCollection',
      features: ctx.boundaries.features.filter((f) => f.properties.kind !== 'region'),
    };
    const regionBBox = M.bboxOf(region.geometry);
    const bboxBySlug = {};
    const infoBySlug = {};
    territories.features.forEach((f) => {
      const slug = f.properties.slug;
      bboxBySlug[slug] = M.bboxOf(f.geometry);
      infoBySlug[slug] = { slug, kind: f.properties.kind, name: ctx.names[slug] || f.properties.name };
    });

    // Подпись района: «Аксыйский район» → «Аксыйский», «г. Манас (Жалал-Абад)» → «Манас (Жалал-Абад)».
    const shortName = (name) => name.replace(/^(г\.|город)\s+/i, '').replace(/\s+район$/i, '');
    const labels = {
      type: 'FeatureCollection',
      features: territories.features
        .filter((f) => f.properties.kind === 'district' && f.properties.center)
        .map((f) => ({
          type: 'Feature',
          properties: { slug: f.properties.slug, label: shortName(infoBySlug[f.properties.slug].name), n: 0 },
          geometry: { type: 'Point', coordinates: f.properties.center },
        })),
    };

    let map = null;
    let popup = null;
    let hoverSlug = null;
    let selected = '';
    let density = false;
    let points = { type: 'FeatureCollection', features: [] };
    let hasOverdue = false;
    let ambient = false;
    let introPlaying = false;
    let destroyed = false;
    let basemapErrors = 0;
    let rafId = 0;

    const reduced = () => M.reducedMotion();
    const animated = !reduced();  // вступление решаем один раз при загрузке
    // ?anim=on — не выключать анимацию, даже если машина не успевает (для проверок
    // на программном WebGL, где защита от тормозов срабатывает сразу).
    const forceAnimation = new URLSearchParams(global.location.search).get('anim') === 'on';

    // ------------------------------------------------------------ слои

    // Итоговая прозрачность слоёв заявок: во вступлении они стартуют с нуля.
    const FADE = [
      ['pt-halo', 'circle-opacity', 0.16], ['pt-halo', 'circle-stroke-opacity', 0.45],
      ['pt-shadow', 'circle-opacity', 0.22],
      ['pt-overdue', 'circle-stroke-opacity', 0.95],
      ['pt-circle', 'circle-opacity', 1], ['pt-circle', 'circle-stroke-opacity', 1],
      ['pt-pin', 'circle-opacity', 1],
      ['cl-shadow', 'circle-opacity', 0.2],
      ['clusters', 'circle-opacity', 0.95], ['clusters', 'circle-stroke-opacity', 1],
      ['cluster-count', 'text-opacity', 1],
    ];
    const finalOf = (layer, prop) => FADE.find((f) => f[0] === layer && f[1] === prop)[2];
    const startOf = (layer, prop) => (animated ? 0 : finalOf(layer, prop));

    // Радиус точки заявки по масштабу (+ прибавка для колец и ореола).
    const R = (extra, scale) => byZoom([[6, 5], [10, 7], [14, 9], [17, 11]].map(
      (s) => [s[0], s[1] * (scale || 1) + (extra || 0)]));

    const UNCLUSTERED = ['!', ['has', 'point_count']];
    const IS_OVERDUE = ['==', ['get', 'overdue'], 1];

    function addLayers() {
      const fontBold = ['Noto Sans Bold'];

      // 1. Пелена за границей области. Над ВСЕЙ подложкой, включая её подписи:
      //    так подписи городов Узбекистана и соседних областей не пробиваются
      //    сквозь неё. fill-antialias выключен: на стыках тайлов полупрозрачная
      //    заливка с обводкой давала видимую сетку.
      map.addSource('mask', { type: 'geojson', data: M.maskFeature(region), tolerance: 0.2 });
      map.addLayer({
        id: 'mask', type: 'fill', source: 'mask',
        paint: { 'fill-color': P.bg, 'fill-opacity': animated ? 0 : M.MASK_OPACITY, 'fill-antialias': false },
      });

      // 2. Районы и города: подсветка, плотность, пунктир.
      map.addSource('territories', { type: 'geojson', data: territories, promoteId: 'slug', tolerance: 0.2 });
      map.addLayer({
        id: 't-density', type: 'fill', source: 'territories', layout: { visibility: 'none' },
        paint: {
          'fill-color': densityColor(1),
          'fill-opacity': ['case', ['>', ['coalesce', ['feature-state', 'n'], 0], 0], 0.55, 0.0],
        },
      });
      map.addLayer({
        id: 't-hover', type: 'fill', source: 'territories',
        paint: {
          'fill-color': P.accent,
          'fill-opacity': ['case', ['boolean', ['feature-state', 'hover'], false], 0.14, 0],
        },
      });
      map.addLayer({
        id: 't-line', type: 'line', source: 'territories',
        paint: {
          'line-color': P.accent,
          'line-width': byZoom([[5, 0.9], [8, 1.3], [12, 1.9], [16, 2.6]]),
          'line-opacity': animated ? 0 : 0.9,
          'line-dasharray': DASH_FRAMES[0],
          // Без перехода: иначе каждая смена кадра пунктира «доплывала» бы 300 мс.
          'line-dasharray-transition': { duration: 0, delay: 0 },
        },
      });
      map.addLayer({
        id: 't-hover-line', type: 'line', source: 'territories', filter: ['==', ['get', 'slug'], ''],
        paint: { 'line-color': P.accent, 'line-width': byZoom([[5, 1.6], [12, 2.6], [16, 3.4]]), 'line-opacity': 0.9 },
      });

      // 3. Граница области: свечение + линия. Источнику нужен lineMetrics —
      //    без него у линии нет «доли пути» и градиентная прорисовка невозможна.
      map.addSource('region', { type: 'geojson', data: M.regionLines(region), lineMetrics: true, tolerance: 0.1 });
      // Свечению — сглаженная копия границы (см. M.smoothRing): иначе размытая
      // линия на изломах даёт колючую бахрому.
      const soft = M.regionLines(region);
      soft.features.forEach((f) => { f.geometry.coordinates = M.smoothRing(f.geometry.coordinates, 0.8, 7, 2); });
      map.addSource('region-soft', { type: 'geojson', data: soft, lineMetrics: true });
      map.addLayer({
        id: 'region-glow', type: 'line', source: 'region-soft',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-gradient': lineGradient(P.accent, animated ? 0 : 1),
          'line-width': byZoom([[5, 10], [8, 14], [11, 14], [13, 0]]),
          'line-blur': 10,
          'line-opacity': 0.34,
          'line-opacity-transition': { duration: 0, delay: 0 },
        },
      });
      map.addLayer({
        id: 'region-line', type: 'line', source: 'region',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: {
          'line-gradient': lineGradient(P.navy, animated ? 0 : 1),
          'line-width': byZoom([[5, 1.6], [8, 2.6], [12, 3.6], [16, 5]], 1.3),
        },
      });

      // 4. Подписи районов (названия заглавными, с разрядкой — как на бумажных картах).
      map.addSource('t-labels', { type: 'geojson', data: labels });
      map.addLayer({
        id: 't-labels', type: 'symbol', source: 't-labels', minzoom: 6,
        layout: {
          'text-field': labelText(false), 'text-font': fontBold,
          'text-size': byZoom([[6, 10], [9, 13]]),
          'text-transform': 'uppercase', 'text-letter-spacing': 0.14, 'text-max-width': 9,
        },
        paint: {
          'text-color': '#12306B', 'text-halo-color': 'rgba(255,255,255,0.9)', 'text-halo-width': 1.6,
          'text-opacity': animated ? 0 : labelOpacity(),
          'text-opacity-transition': { duration: 800, delay: 0 },
        },
      });

      // 5. Заявки. Кластеризацию делает сам MapLibre: cluster:true у источника.
      //    В кластере считаем и просроченные — их кластер обводим красным.
      map.addSource('tickets', {
        type: 'geojson', data: points, cluster: true, clusterRadius: 52, clusterMaxZoom: 16,
        clusterProperties: { overdue: ['+', ['get', 'overdue']] },
      });
      map.addLayer({  // ореол «примерное место»: заявка найдена только по населённому пункту
        id: 'pt-halo', type: 'circle', source: 'tickets', filter: ['all', UNCLUSTERED, ['==', ['get', 'approx'], 1]],
        paint: {
          'circle-radius': byZoom([[7, 12], [10, 18], [13, 30], [16, 46]]),
          'circle-color': ['get', 'color'], 'circle-opacity': startOf('pt-halo', 'circle-opacity'),
          'circle-stroke-color': ['get', 'color'], 'circle-stroke-width': 1.5,
          'circle-stroke-opacity': startOf('pt-halo', 'circle-stroke-opacity'),
        },
      });
      map.addLayer({
        id: 'pt-shadow', type: 'circle', source: 'tickets', filter: UNCLUSTERED,
        paint: {
          'circle-radius': R(2.5), 'circle-color': P.navy, 'circle-blur': 0.9,
          'circle-translate': [0, 1.5], 'circle-opacity': startOf('pt-shadow', 'circle-opacity'),
        },
      });
      map.addLayer({  // постоянное красное кольцо просрочки (видно и без анимации)
        id: 'pt-overdue', type: 'circle', source: 'tickets', filter: ['all', UNCLUSTERED, IS_OVERDUE],
        paint: {
          'circle-radius': R(5), 'circle-color': 'rgba(0,0,0,0)', 'circle-opacity': 0,
          'circle-stroke-color': P.danger, 'circle-stroke-width': 2,
          'circle-stroke-opacity': startOf('pt-overdue', 'circle-stroke-opacity'),
        },
      });
      map.addLayer({  // расходящееся кольцо — «пульс» просроченной заявки
        id: 'pt-pulse', type: 'circle', source: 'tickets', filter: ['all', UNCLUSTERED, IS_OVERDUE],
        paint: {
          'circle-radius': 9, 'circle-color': 'rgba(0,0,0,0)', 'circle-opacity': 0,
          'circle-stroke-color': P.danger, 'circle-stroke-width': 2, 'circle-stroke-opacity': 0,
          'circle-radius-transition': { duration: 0, delay: 0 },
          'circle-stroke-opacity-transition': { duration: 0, delay: 0 },
        },
      });
      map.addLayer({
        id: 'pt-circle', type: 'circle', source: 'tickets', filter: UNCLUSTERED,
        paint: {
          'circle-radius': R(0), 'circle-color': ['get', 'color'],
          'circle-opacity': startOf('pt-circle', 'circle-opacity'),
          // Точка, поставленная самим жителем (геометка), — с тёмной обводкой
          // и белым центром, чтобы отличалась от найденных по адресу.
          'circle-stroke-color': ['case', ['==', ['get', 'pin'], 1], P.navy, P.white],
          'circle-stroke-width': 2, 'circle-stroke-opacity': startOf('pt-circle', 'circle-stroke-opacity'),
        },
      });
      map.addLayer({
        id: 'pt-pin', type: 'circle', source: 'tickets', filter: ['all', UNCLUSTERED, ['==', ['get', 'pin'], 1]],
        paint: { 'circle-radius': R(0, 0.34), 'circle-color': P.white, 'circle-opacity': startOf('pt-pin', 'circle-opacity') },
      });
      map.addLayer({  // выбранная заявка (открыт попап или пришли по ссылке)
        id: 'pt-selected', type: 'circle', source: 'tickets', filter: ['==', ['get', 'number'], ''],
        paint: {
          'circle-radius': R(10), 'circle-color': 'rgba(47,107,222,0.12)',
          'circle-stroke-color': P.accent, 'circle-stroke-width': 2.5,
        },
      });
      map.addLayer({
        id: 'cl-shadow', type: 'circle', source: 'tickets', filter: ['has', 'point_count'],
        paint: {
          'circle-radius': ['step', ['get', 'point_count'], 18, 10, 22, 50, 27, 200, 33],
          'circle-color': P.navy, 'circle-blur': 0.8, 'circle-translate': [0, 2],
          'circle-opacity': startOf('cl-shadow', 'circle-opacity'),
        },
      });
      map.addLayer({
        id: 'clusters', type: 'circle', source: 'tickets', filter: ['has', 'point_count'],
        paint: {
          'circle-color': ['step', ['get', 'point_count'], P.accent, 10, '#2557B8', 50, '#183F91', 200, P.navy],
          'circle-radius': ['step', ['get', 'point_count'], 15, 10, 19, 50, 24, 200, 30],
          'circle-stroke-width': 3,
          'circle-stroke-color': ['case', ['>', ['get', 'overdue'], 0], P.danger, P.white],
          'circle-opacity': startOf('clusters', 'circle-opacity'),
          'circle-stroke-opacity': startOf('clusters', 'circle-stroke-opacity'),
        },
      });
      map.addLayer({
        id: 'cluster-count', type: 'symbol', source: 'tickets', filter: ['has', 'point_count'],
        layout: {
          'text-field': ['get', 'point_count_abbreviated'], 'text-font': fontBold, 'text-size': 13,
          'text-allow-overlap': true, 'text-ignore-placement': true,
        },
        paint: { 'text-color': '#FFFFFF', 'text-opacity': startOf('cluster-count', 'text-opacity') },
      });
    }

    // Подписи районов гаснут по мере приближения: на крупном плане о районе
    // говорит уже сама карта.
    function labelOpacity() { return byZoom([[6, 0.85], [10, 0.75], [11.5, 0]]); }

    function labelText(withCounts) {
      return withCounts
        ? ['concat', ['get', 'label'], ['case', ['>', ['get', 'n'], 0], ['concat', '\n', ['to-string', ['get', 'n']]], '']]
        : ['get', 'label'];
    }

    // Мягкая шкала плотности: от почти прозрачного к насыщенному синему.
    function densityColor(max) {
      const m = Math.max(max, 1);
      return ['interpolate', ['linear'], ['coalesce', ['feature-state', 'n'], 0],
        0, '#EAF1FF', m * 0.25, '#BCD2F8', m * 0.5, '#86AEF0', m * 0.75, '#4F83E6', m, '#1F4FB0'];
    }

    // ------------------------------------------------------------ камера

    function padding(extra) {
      const left = ctx.insetLeft() + (extra || 0);
      return { top: 40, bottom: 40, right: 60, left: left + 30 };
    }

    // Рамка, за которую карту не пускаем: всё, что видно при «вся область»,
    // плюс запас. Только запас от рамки области не годится: окно обычно
    // шире области по пропорциям, и карту пришлось бы приблизить, чтобы
    // уместиться в ограничение.
    function applyConstraints() {
      const before = { center: map.getCenter(), zoom: map.getZoom() };
      map.setMaxBounds(null);
      map.setMinZoom(0);
      map.fitBounds(regionBBox, { padding: padding(), animate: false });
      const zoom = map.getZoom();
      const view = map.getBounds();
      const visible = [view.getWest(), view.getSouth(), view.getEast(), view.getNorth()];
      const box = M.expandBBox(M.unionBBox(visible, regionBBox), 0.12);
      const regionWide = M.expandBBox(regionBBox, 0.28);
      const limit = M.unionBBox(box, regionWide);
      map.setMaxBounds([[limit[0], Math.max(limit[1], -84)], [limit[2], Math.min(limit[3], 84)]]);
      map.setMinZoom(zoom - 0.3);
      return before;
    }

    function fitOblast(animate) {
      map.fitBounds(regionBBox, {
        padding: padding(), animate: !!animate && !reduced(), duration: 900, maxZoom: 10,
      });
    }

    function fitTerritory(slug) {
      const box = bboxBySlug[slug];
      if (!box) return;
      map.fitBounds(box, {
        padding: { top: 60, bottom: 60, right: 60, left: ctx.insetLeft() + 40 },
        maxZoom: 11.5, animate: !reduced(), duration: 900,
      });
    }

    // ------------------------------------------------------------ попап и выбор

    function closePopup() {
      if (popup) { const p = popup; popup = null; p.remove(); }
    }

    function selectTicket(number) {
      selected = number || '';
      if (map && map.getLayer('pt-selected')) map.setFilter('pt-selected', ['==', ['get', 'number'], selected]);
    }

    function openPopup(features, lngLat) {
      closePopup();
      const seen = new Set();
      const unique = features.filter((f) => !seen.has(f.properties.number) && seen.add(f.properties.number));
      selectTicket(unique[0].properties.number);
      popup = new ml.Popup({ closeButton: true, closeOnClick: true, maxWidth: '340px', offset: 16, className: 'map-popup' })
        .setLngLat(lngLat).setDOMContent(M.popupContent(unique.map((f) => f.properties), ctx.ticketUrl)).addTo(map);
      const mine = popup;
      mine.on('close', () => { if (popup === mine) { popup = null; selectTicket(''); } });
    }

    function showTicket(feature, options) {
      const coords = feature.geometry.coordinates;
      const zoom = feature.properties.approx ? 12 : 15.5;
      selectTicket(feature.properties.number);
      const target = { center: coords, zoom };
      const opened = () => { if (!destroyed) openPopup([feature], coords); };
      if (options && options.fly && !reduced()) {
        map.once('moveend', opened);
        map.flyTo(Object.assign({ duration: 1600, essential: false, padding: { left: ctx.insetLeft() } }, target));
      } else {
        map.jumpTo(Object.assign({ padding: { left: ctx.insetLeft() } }, target));
        opened();
      }
    }

    // ------------------------------------------------------------ наведение и клики

    function pickTerritory(hits) {
      // Город лежит «дыркой» в районе, но на всякий случай предпочитаем меньшее.
      const city = hits.find((f) => f.properties.kind === 'city');
      return city || hits[0] || null;
    }

    function setHover(slug) {
      if (slug === hoverSlug) return;
      if (hoverSlug) map.setFeatureState({ source: 'territories', id: hoverSlug }, { hover: false });
      hoverSlug = slug;
      if (slug) map.setFeatureState({ source: 'territories', id: slug }, { hover: true });
      map.setFilter('t-hover-line', ['==', ['get', 'slug'], slug || '']);
    }

    function bindInteractions() {
      let pending = null;
      let frame = 0;

      const onMove = () => {
        frame = 0;
        const e = pending;
        if (!e || destroyed) return;
        const overPoint = map.queryRenderedFeatures(e.point, { layers: ['pt-circle', 'clusters'] }).length > 0;
        const territory = overPoint ? null
          : pickTerritory(map.queryRenderedFeatures(e.point, { layers: ['t-hover'] }));
        map.getCanvas().style.cursor = (overPoint || territory) ? 'pointer' : '';
        setHover(territory ? territory.properties.slug : null);
        ctx.events.hover(territory ? infoBySlug[territory.properties.slug] : null, { x: e.point.x, y: e.point.y });
      };
      map.on('mousemove', (e) => {
        pending = e;
        if (!frame) frame = requestAnimationFrame(onMove);  // не чаще раза за кадр
      });
      map.getCanvas().addEventListener('mouseleave', () => {
        pending = null;
        setHover(null);
        ctx.events.hover(null);
      });

      map.on('click', (e) => {
        const clusterHit = map.queryRenderedFeatures(e.point, { layers: ['clusters'] })[0];
        if (clusterHit) { expandCluster(clusterHit); return; }
        const pointHits = map.queryRenderedFeatures(e.point, { layers: ['pt-circle'] });
        if (pointHits.length) {
          openPopup(pointHits, pointHits[0].geometry.coordinates.slice());
          return;
        }
        const territory = pickTerritory(map.queryRenderedFeatures(e.point, { layers: ['t-hover'] }));
        if (territory) fitTerritory(territory.properties.slug);
      });
    }

    function expandCluster(feature) {
      const id = feature.properties.cluster_id;
      const source = map.getSource('tickets');
      const coords = feature.geometry.coordinates.slice();
      asPromise((cb) => source.getClusterExpansionZoom(id, cb)).then((zoom) => {
        if (zoom > map.getZoom() + 0.05) {
          map.easeTo({ center: coords, zoom: Math.min(zoom + 0.15, 18.5), duration: reduced() ? 0 : 600 });
          return null;
        }
        // Раскрывать некуда: все заявки кластера в одной точке — покажем списком.
        return asPromise((cb) => source.getClusterLeaves(id, 50, 0, cb)).then((leaves) => openPopup(leaves, coords));
      }).catch(() => {});
    }

    // ------------------------------------------------------------ анимация

    function setRegionProgress(p) {
      map.setPaintProperty('region-line', 'line-gradient', lineGradient(P.navy, p));
      map.setPaintProperty('region-glow', 'line-gradient', lineGradient(P.accent, p));
    }

    function applyFinalState() {
      map.setPaintProperty('mask', 'fill-opacity', M.MASK_OPACITY);
      setRegionProgress(1);
      map.setPaintProperty('t-line', 'line-opacity', 0.9);
      map.setPaintProperty('t-labels', 'text-opacity', labelOpacity());
      FADE.forEach((f) => map.setPaintProperty(f[0], f[1], f[2]));
    }

    function playIntro() {
      introPlaying = true;
      const t0 = performance.now();
      let pointsShown = false;
      const step = (now) => {
        if (destroyed) return;
        const t = now - t0;
        map.setPaintProperty('mask', 'fill-opacity', M.MASK_OPACITY * M.easeOutCubic(M.clamp01(t / INTRO.mask)));
        setRegionProgress(M.easeInOutCubic(M.clamp01((t - INTRO.lineFrom) / (INTRO.lineTo - INTRO.lineFrom))));
        map.setPaintProperty('t-line', 'line-opacity', 0.9 * M.clamp01((t - INTRO.dashFrom) / (INTRO.dashTo - INTRO.dashFrom)));
        if (!pointsShown && t >= INTRO.dashFrom) {
          pointsShown = true;
          // дальше сработают штатные переходы MapLibre (по 300 мс)
          map.setPaintProperty('t-labels', 'text-opacity', labelOpacity());
          FADE.forEach((f) => map.setPaintProperty(f[0], f[1], f[2]));
        }
        if (t < INTRO.end) { rafId = requestAnimationFrame(step); return; }
        introPlaying = false;
        applyFinalState();
        startAmbient();
      };
      rafId = requestAnimationFrame(step);
    }

    // Фоновая «жизнь»: пунктир бежит, свечение дышит, просроченные пульсируют.
    function startAmbient() {
      if (ambient || destroyed) return;
      ambient = true;
      let last = performance.now();
      let behind = 0;
      let dashIndex = -1;
      // Вернулись на вкладку — не считать часы простоя «тормозами».
      document.addEventListener('visibilitychange', () => { last = performance.now(); behind = 0; });
      const loop = (now) => {
        if (!ambient || destroyed) return;
        rafId = requestAnimationFrame(loop);
        if (now - last < TICK_MS) return;
        const dt = now - last;
        last = now;
        if (document.hidden || reduced() || map.isMoving() || introPlaying) { behind = 0; return; }
        // Не успеваем (программный WebGL, слабая машина) — анимацию отключаем
        // насовсем: красота не стоит зависшего браузера.
        behind = dt > 260 ? behind + 1 : Math.max(0, behind - 1);
        if (behind >= 6 && !forceAnimation) { ambient = false; return; }

        const index = Math.floor(((now % DASH_CYCLE_MS) / DASH_CYCLE_MS) * DASH_FRAMES.length);
        if (index !== dashIndex) {
          dashIndex = index;
          map.setPaintProperty('t-line', 'line-dasharray', DASH_FRAMES[index]);
        }
        const breath = 0.5 + 0.5 * Math.sin((now / GLOW_PERIOD_MS) * 2 * Math.PI);
        map.setPaintProperty('region-glow', 'line-opacity', 0.2 + 0.22 * breath);
        if (hasOverdue) {
          const phase = (now % PULSE_PERIOD_MS) / PULSE_PERIOD_MS;
          map.setPaintProperty('pt-pulse', 'circle-radius', 9 + 15 * M.easeOutCubic(phase));
          map.setPaintProperty('pt-pulse', 'circle-stroke-opacity', 0.85 * (1 - phase));
        }
      };
      rafId = requestAnimationFrame(loop);
    }

    // ------------------------------------------------------------ интерфейс движка

    const engine = {
      name: 'maplibre',

      async init() {
        const style = await M.loadBasemapStyle();
        if (destroyed) return;
        map = new ml.Map({
          container: ctx.container, style, center: [72.9, 41.5], zoom: 7, maxZoom: 18.5,
          attributionControl: false, dragRotate: false, pitchWithRotate: false,
          renderWorldCopies: false, fadeDuration: 150,
          locale: {
            'NavigationControl.ZoomIn': 'Приблизить', 'NavigationControl.ZoomOut': 'Отдалить',
            'AttributionControl.ToggleAttribution': 'Об источниках данных',
            'AttributionControl.MapFeedback': 'Сообщить об ошибке на карте',
          },
        });
        map.touchZoomRotate.disableRotation();
        map.addControl(new ml.NavigationControl({ showCompass: false }), 'top-right');
        map.addControl(homeControl(), 'top-right');
        // В нижних углах контролы встают снизу вверх в порядке добавления:
        // источники данных — самым нижним рядом, линейка масштаба над ними.
        map.addControl(new ml.AttributionControl({
          customAttribution: '<a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">границы © участники OpenStreetMap</a>',
        }), 'bottom-right');
        map.addControl(new ml.ScaleControl({ maxWidth: 110, unit: 'metric' }), 'bottom-right');
        map.on('error', (e) => {
          // Не показываем каждую мелочь: сообщаем, только если подложка явно не грузится.
          if (e && e.sourceId === 'openmaptiles' && ++basemapErrors === 4) ctx.events.error(M.messages.noBasemap);
        });

        await new Promise((resolve) => map.once('style.load', resolve));
        if (destroyed) return;
        addLayers();
        applyConstraints();
        bindInteractions();
        // Окно изменили — пропорции экрана другие, пересчитываем границы панорамирования.
        let resizeTimer = 0;
        map.on('resize', () => {
          clearTimeout(resizeTimer);
          resizeTimer = setTimeout(() => { if (!destroyed) engine.insetChanged(); }, 250);
        });

        // Вступление начинаем, когда подложка впервые отрисовалась (но не ждём вечно).
        let started = false;
        const begin = () => {
          if (started || destroyed) return;
          started = true;
          if (animated) playIntro(); else { applyFinalState(); startAmbient(); }
        };
        map.once('idle', begin);
        setTimeout(begin, 2500);
      },

      setPoints(collection) {
        points = collection;
        hasOverdue = collection.features.some((f) => f.properties.overdue);
        const source = map.getSource('tickets');
        if (source) source.setData(collection);
        if (!hasOverdue && map.getLayer('pt-pulse')) map.setPaintProperty('pt-pulse', 'circle-stroke-opacity', 0);
        // Точка, которую показывали, могла пропасть из выборки — сбрасываем выделение.
        if (selected && !collection.features.some((f) => f.properties.number === selected)) {
          closePopup();
          selectTicket('');
        }
      },

      setCounts(counts) {
        let max = 0;
        territories.features.forEach((f) => {
          const n = counts[f.properties.slug] || 0;
          if (n > max) max = n;
          map.setFeatureState({ source: 'territories', id: f.properties.slug }, { n });
        });
        labels.features.forEach((f) => { f.properties.n = counts[f.properties.slug] || 0; });
        map.getSource('t-labels').setData(labels);
        map.setPaintProperty('t-density', 'fill-color', densityColor(max));
      },

      setDensity(on) {
        density = !!on;
        map.setLayoutProperty('t-density', 'visibility', density ? 'visible' : 'none');
        map.setLayoutProperty('t-labels', 'text-field', labelText(density));
      },

      showTicket, selectTicket, fitOblast, fitTerritory,

      // Для отладки и автотестов в браузере: сам объект карты.
      raw() { return map; },

      insetChanged() {
        // Панель свернули или развернули: отступ для «вся область» изменился.
        if (!map) return;
        const before = applyConstraints();
        map.jumpTo(before);
      },

      resize() { if (map) map.resize(); },

      destroy() {
        destroyed = true;
        ambient = false;
        cancelAnimationFrame(rafId);
        closePopup();
        if (map) map.remove();
        map = null;
      },
    };

    // Кнопка «вся область» рядом с зумом.
    function homeControl() {
      return {
        onAdd() {
          const box = M.h('div', { class: 'maplibregl-ctrl maplibregl-ctrl-group' });
          const button = M.h('button', {
            type: 'button', class: 'map-home-btn', title: 'Показать всю область', 'aria-label': 'Показать всю область',
          });
          button.innerHTML = '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" ' +
            'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
            '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></svg>';
          button.addEventListener('click', () => fitOblast(true));
          box.appendChild(button);
          return box;
        },
        onRemove() {},
      };
    }

    return engine;
  };
})(window);
