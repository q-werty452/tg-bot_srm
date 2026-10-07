/*
 * minimap.js — мини-карта в карточке заявки.
 *
 * Тот же MapLibre и та же подложка, что у большой карты, но без анимаций
 * и без районов: только точка заявки, граница области и белая пелена за ней.
 * Пелена нужна и здесь — заявка у самой границы не должна выглядеть
 * «в чужой области». Файл границы берём облегчённый (только область),
 * а не полный с районами: карточку заявки открывают чаще всего.
 */
(function (global) {
  'use strict';

  const M = global.ManasMap;
  const box = document.getElementById('ticket-minimap');
  if (!box || !M) return;

  const lat = parseFloat(box.dataset.lat);
  const lon = parseFloat(box.dataset.lon);
  const approx = box.dataset.source === 'settlement';
  const pin = box.dataset.source === 'pin';
  const overdue = box.dataset.overdue === '1';
  const where = lat.toFixed(5) + ', ' + lon.toFixed(5);

  function fallback(message) {
    box.replaceChildren(M.h('div', { class: 'minimap-fallback' }, message + ' Координаты: ' + where + '.'));
  }

  if (!global.maplibregl || !M.webglSupported()) {
    fallback('Карта недоступна: в браузере не работает WebGL или не загрузилась библиотека.');
    return;
  }

  const P = M.palette();
  const color = M.safeColor(box.dataset.color, P.accent);

  Promise.all([M.loadBasemapStyle(), M.fetchJSON(box.dataset.regionUrl)]).then((loaded) => {
    const style = loaded[0];
    const region = loaded[1].features[0];
    const map = new global.maplibregl.Map({
      container: box, style, center: [lon, lat], zoom: approx ? 11.5 : 14.5, minZoom: 5, maxZoom: 18,
      dragRotate: false, renderWorldCopies: false, cooperativeGestures: true,
      locale: {
        'NavigationControl.ZoomIn': 'Приблизить', 'NavigationControl.ZoomOut': 'Отдалить',
        'AttributionControl.ToggleAttribution': 'Об источниках данных',
        'CooperativeGesturesHandler.WindowsHelpText': 'Ctrl + колёсико мыши — масштаб карты',
        'CooperativeGesturesHandler.MacHelpText': '⌘ + колёсико мыши — масштаб карты',
        'CooperativeGesturesHandler.MobileHelpText': 'Двигайте карту двумя пальцами',
      },
    });
    map.touchZoomRotate.disableRotation();
    map.addControl(new global.maplibregl.NavigationControl({ showCompass: false }), 'top-right');

    map.on('load', () => {
      map.addSource('mask', { type: 'geojson', data: M.maskFeature(region), tolerance: 0.2 });
      map.addLayer({ id: 'mask', type: 'fill', source: 'mask',
        paint: { 'fill-color': P.bg, 'fill-opacity': M.MASK_OPACITY, 'fill-antialias': false } });

      map.addSource('region', { type: 'geojson', data: M.regionLines(region), tolerance: 0.1 });
      map.addLayer({ id: 'region-glow', type: 'line', source: 'region',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': P.accent, 'line-opacity': 0.3, 'line-blur': 8,
          'line-width': ['interpolate', ['linear'], ['zoom'], 5, 7, 12, 16] } });
      map.addLayer({ id: 'region-line', type: 'line', source: 'region',
        layout: { 'line-cap': 'round', 'line-join': 'round' },
        paint: { 'line-color': P.navy, 'line-width': ['interpolate', ['linear'], ['zoom'], 5, 1.6, 12, 3.4] } });

      map.addSource('ticket', { type: 'geojson', data: {
        type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: [lon, lat] } } });
      if (approx) {  // ореол: место определено только по населённому пункту
        map.addLayer({ id: 'halo', type: 'circle', source: 'ticket', paint: {
          'circle-radius': ['interpolate', ['linear'], ['zoom'], 7, 12, 10, 18, 13, 34, 16, 60],
          'circle-color': color, 'circle-opacity': 0.16,
          'circle-stroke-color': color, 'circle-stroke-width': 1.5, 'circle-stroke-opacity': 0.45 } });
      }
      if (overdue) {
        map.addLayer({ id: 'overdue', type: 'circle', source: 'ticket', paint: {
          'circle-radius': 14, 'circle-color': 'rgba(0,0,0,0)', 'circle-stroke-color': P.danger,
          'circle-stroke-width': 2 } });
      }
      map.addLayer({ id: 'dot', type: 'circle', source: 'ticket', paint: {
        'circle-radius': 8.5, 'circle-color': color, 'circle-stroke-width': 2.5,
        'circle-stroke-color': pin ? P.navy : P.white } });
      if (pin) {
        map.addLayer({ id: 'pin', type: 'circle', source: 'ticket',
          paint: { 'circle-radius': 3, 'circle-color': P.white } });
      }
    });
  }).catch(() => fallback('Не удалось загрузить карту (нужен интернет).'));
})(window);
