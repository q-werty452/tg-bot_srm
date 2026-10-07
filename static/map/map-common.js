/*
 * map-common.js — общие помощники карты обращений.
 *
 * Их используют и большая карта (map-core.js + движки), и мини-карта в
 * карточке заявки (minimap.js). Здесь нет ничего про конкретный движок
 * отрисовки: только данные, геометрия, подпись попапа и проверки браузера.
 * Один набор помощников на всех — чтобы «внутри области» на мини-карте,
 * на большой карте и в запасном режиме значило одно и то же.
 */
(function (global) {
  'use strict';

  const M = (global.ManasMap = global.ManasMap || {});

  // ------------------------------------------------------------------ настройки

  // Векторная подложка OpenFreeMap: бесплатно, без ключа, чёткая на любом
  // масштабе и на retina-экранах (растровые тайлы на них мылятся).
  M.STYLE_URL = 'https://tiles.openfreemap.org/styles/positron';

  // Подписи подложки по-русски: стиль по умолчанию рисует латиницу и местное
  // название в две строки, сотрудникам нужно одно, привычное, название.
  M.RU_NAME = ['coalesce', ['get', 'name:ru'], ['get', 'name'], ['get', 'name_en']];

  // Насколько белая пелена закрывает всё за границей области.
  M.MASK_OPACITY = 0.84;

  // Цвета берём из переменных панели (static/app.css), чтобы карта не
  // выбивалась из фирменной гаммы; запасные значения — те же, что в app.css.
  M.palette = function () {
    const css = getComputedStyle(document.documentElement);
    const pick = (name, fallback) => (css.getPropertyValue(name) || '').trim() || fallback;
    return {
      navy: pick('--sidebar', '#0E1C36'),    // граница области
      accent: pick('--accent', '#2F6BDE'),   // районы, кластеры, свечение
      danger: pick('--danger', '#D4373E'),   // просрочено
      bg: pick('--bg', '#F4F6FA'),           // пелена за границей области
      muted: '#8B96A8',                      // заявка без категории
      white: '#FFFFFF',
    };
  };

  const HEX = /^#[0-9a-fA-F]{6}$/;
  M.safeColor = (value, fallback) => (HEX.test(value || '') ? value : fallback || '#8B96A8');

  // ------------------------------------------------------------------ браузер

  M.webglSupported = function () {
    try {
      const canvas = document.createElement('canvas');
      const gl = canvas.getContext('webgl2') || canvas.getContext('webgl') ||
        canvas.getContext('experimental-webgl');
      if (!gl) return false;
      const lose = gl.getExtension('WEBGL_lose_context');
      if (lose) lose.loseContext();  // не занимаем контекст: их в браузере ограниченное число
      return true;
    } catch (e) {
      return false;
    }
  };

  M.reducedMotion = function () {
    return !!(global.matchMedia && global.matchMedia('(prefers-reduced-motion: reduce)').matches);
  };

  // Постоянные подсказки пользователя (свёрнутая панель и т. п.). Хранилище
  // бывает недоступно (приватный режим) — тогда просто не запоминаем.
  M.store = {
    get(key) { try { return global.localStorage.getItem('map.' + key); } catch (e) { return null; } },
    set(key, value) { try { global.localStorage.setItem('map.' + key, value); } catch (e) { /* не страшно */ } },
  };

  // ------------------------------------------------------------------ сеть

  M.fetchJSON = async function (url, options) {
    const response = await fetch(url, Object.assign(
      { credentials: 'same-origin', headers: { Accept: 'application/json' } }, options));
    if (!response.ok) throw new Error('HTTP ' + response.status);
    return response.json();
  };

  // Стиль подложки тянем сами и правим до создания карты: так подписи сразу
  // русские, без «мигания» латиницей при смене текста после загрузки.
  M.loadBasemapStyle = async function () {
    const response = await fetch(M.STYLE_URL, { credentials: 'omit' });
    if (!response.ok) throw new Error('HTTP ' + response.status);
    const style = await response.json();
    for (const layer of style.layers) {
      if (layer.type !== 'symbol' || !layer.layout) continue;
      const field = layer.layout['text-field'];
      if (field && JSON.stringify(field).indexOf('"name') !== -1) {
        layer.layout['text-field'] = M.RU_NAME;
      }
      // Названия областей и стран на карте области не нужны: название области
      // и районов рисуем сами, а подпись страны, как назло, садится на самый
      // край области и читается поверх границы.
      if (layer.id === 'label_state' || layer.id.indexOf('label_country') === 0) {
        layer.layout.visibility = 'none';
      }
    }
    return style;
  };

  // ------------------------------------------------------------------ геометрия

  M.polygonsOf = function (geometry) {
    if (!geometry) return [];
    if (geometry.type === 'Polygon') return [geometry.coordinates];
    if (geometry.type === 'MultiPolygon') return geometry.coordinates;
    return [];
  };

  // Знаковая площадь кольца: > 0 — против часовой стрелки (ось Y вверх).
  M.ringArea = function (ring) {
    let sum = 0;
    for (let i = 0, n = ring.length - 1; i < n; i++) {
      sum += ring[i][0] * ring[i + 1][1] - ring[i + 1][0] * ring[i][1];
    }
    return sum / 2;
  };

  M.orient = function (ring, clockwise) {
    const isClockwise = M.ringArea(ring) < 0;
    return isClockwise === clockwise ? ring : ring.slice().reverse();
  };

  // [запад, юг, восток, север] по всем точкам геометрии.
  M.bboxOf = function (geometry) {
    let w = Infinity, s = Infinity, e = -Infinity, n = -Infinity;
    for (const polygon of M.polygonsOf(geometry)) {
      for (const [lon, lat] of polygon[0]) {
        if (lon < w) w = lon;
        if (lon > e) e = lon;
        if (lat < s) s = lat;
        if (lat > n) n = lat;
      }
    }
    return [w, s, e, n];
  };

  M.unionBBox = (a, b) => [Math.min(a[0], b[0]), Math.min(a[1], b[1]),
    Math.max(a[2], b[2]), Math.max(a[3], b[3])];

  // Расширить рамку на долю её размера с каждой стороны.
  M.expandBBox = function (box, fraction) {
    const dx = (box[2] - box[0]) * fraction, dy = (box[3] - box[1]) * fraction;
    return [box[0] - dx, box[1] - dy, box[2] + dx, box[3] + dy];
  };

  // Пелена: «весь мир» с дыркой в форме области. Внешнее кольцо идёт против
  // часовой стрелки, дырки — по часовой (так требует GeoJSON); внутренние
  // кольца самой области (если бы они были) закрашиваем отдельными полигонами.
  M.maskFeature = function (regionFeature) {
    const world = [[-180, -85], [180, -85], [180, 85], [-180, 85], [-180, -85]];
    const holes = [], islands = [];
    for (const polygon of M.polygonsOf(regionFeature.geometry)) {
      holes.push(M.orient(polygon[0], true));
      for (const inner of polygon.slice(1)) islands.push([M.orient(inner, false)]);
    }
    const polygons = [[world].concat(holes)].concat(islands);
    return {
      type: 'Feature', properties: {},
      geometry: polygons.length === 1
        ? { type: 'Polygon', coordinates: polygons[0] }
        : { type: 'MultiPolygon', coordinates: polygons },
    };
  };

  // Граница области как линия: по часовой стрелке, от самой северной точки.
  // Анимация «прорисовки» идёт вдоль линии, и начинать с севера красивее,
  // чем с произвольной вершины.
  M.regionLines = function (regionFeature) {
    const features = [];
    for (const polygon of M.polygonsOf(regionFeature.geometry)) {
      let points = M.orient(polygon[0], true).slice(0, -1);
      let top = 0;
      points.forEach((p, i) => { if (p[1] > points[top][1]) top = i; });
      points = points.slice(top).concat(points.slice(0, top));
      points.push(points[0]);
      features.push({ type: 'Feature', properties: {},
        geometry: { type: 'LineString', coordinates: points } });
    }
    return { type: 'FeatureCollection', features };
  };

  // Сгладить замкнутое кольцо [[lon, lat], ...] для «свечения» границы.
  // У размытой полупрозрачной линии на частых изломах соседние отрезки
  // накладываются друг на друга, и свечение выходит колючим, с лучами по
  // сторонам. Если сперва равномерно «пересэмплировать» кольцо и усреднить
  // соседние точки, острые углы скругляются, и свечение ложится ровной
  // мягкой полосой. Саму границу рисует точная линия поверх — сглаживание
  // нужно только свечению. stepKm — шаг выборки, windowSamples — ширина
  // окна усреднения в отсчётах, passes — число проходов.
  M.smoothRing = function (ring, stepKm, windowSamples, passes) {
    const lat0 = ring.reduce((sum, p) => sum + p[1], 0) / ring.length;
    const kx = 111.32 * Math.cos((lat0 * Math.PI) / 180), ky = 110.57;
    const pts = ring.slice(0, -1).map((p) => [p[0] * kx, p[1] * ky]);
    const n = pts.length;
    const cumulative = [0];
    for (let i = 1; i <= n; i++) {
      const a = pts[i - 1], b = pts[i % n];
      cumulative.push(cumulative[i - 1] + Math.hypot(b[0] - a[0], b[1] - a[1]));
    }
    const total = cumulative[n];
    const count = Math.max(24, Math.round(total / stepKm));
    let sample = [], seg = 0;
    for (let k = 0; k < count; k++) {
      const at = (total * k) / count;
      while (cumulative[seg + 1] < at) seg++;
      const t = (at - cumulative[seg]) / ((cumulative[seg + 1] - cumulative[seg]) || 1);
      const a = pts[seg], b = pts[(seg + 1) % n];
      sample.push([a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]);
    }
    const half = Math.floor(windowSamples / 2);
    for (let pass = 0; pass < passes; pass++) {
      sample = sample.map((_, i) => {
        let x = 0, y = 0;
        for (let d = -half; d <= half; d++) {
          const q = sample[(i + d + count) % count];
          x += q[0]; y += q[1];
        }
        const w = 2 * half + 1;
        return [x / w, y / w];
      });
    }
    const out = sample.map((q) => [q[0] / kx, q[1] / ky]);
    out.push(out[0]);
    return out;
  };

  // ------------------------------------------------------------------ анимация

  M.easeOutCubic = (t) => 1 - Math.pow(1 - t, 3);
  M.easeInOutCubic = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  M.clamp01 = (t) => Math.max(0, Math.min(1, t));

  // Кадры «бегущего пунктира». Сдвигать узор можно только сменой самого
  // line-dasharray, поэтому готовим заранее замкнутый набор кадров: штрих
  // dash, пробел gap, сдвиг узора на 1/n периода за кадр. Нечётный массив у
  // MapLibre означает «первый и последний штрих склеены» — так узор
  // сдвигается без швов. Конечный набор кадров важен: каждый новый узор —
  // новая строка в атласе линий, и бесконечно разных узоров он не потянет.
  M.dashFrames = function (dash, gap, n) {
    const period = dash + gap;
    const r = (x) => Math.round(x * 1000) / 1000;
    const frames = [];
    for (let i = 0; i < n; i++) {
      const offset = (period * i) / n;
      if (i === 0) frames.push([dash, gap]);
      else if (offset < dash) frames.push([r(dash - offset), gap, r(offset)]);
      else frames.push([0, r(gap - (offset - dash)), dash, r(offset - dash)]);
    }
    return frames;
  };

  // ------------------------------------------------------------------ тексты

  M.plural = function (n, forms) {
    const abs = Math.abs(n) % 100, last = abs % 10;
    if (abs > 10 && abs < 20) return forms[2];
    if (last > 1 && last < 5) return forms[1];
    if (last === 1) return forms[0];
    return forms[2];
  };

  M.countLabel = (n) => n + ' ' + M.plural(n, ['обращение', 'обращения', 'обращений']);

  // ------------------------------------------------------------------ DOM и попап

  // Маленький конструктор DOM. Всё, что пришло от жителей (заголовки, адреса,
  // имена), попадает на страницу только через textContent — никакого innerHTML.
  function h(tag, attrs, children) {
    const el = document.createElement(tag);
    for (const key in (attrs || {})) {
      const value = attrs[key];
      if (value === null || value === undefined || value === false) continue;
      if (key === 'class') el.className = value;
      else el.setAttribute(key, value === true ? '' : value);
    }
    for (const child of [].concat(children === undefined ? [] : children)) {
      if (child === null || child === undefined || child === false) continue;
      el.appendChild(typeof child === 'string' ? document.createTextNode(child) : child);
    }
    return el;
  }
  M.h = h;

  function row(label, value) {
    return h('div', { class: 'tp-row' }, [h('span', { class: 'tp-k' }, label), h('span', { class: 'tp-v' }, value)]);
  }

  // Одна заявка в попапе: номер, тема, категория, статус, заявитель, место,
  // дата, точность и ссылка на карточку.
  M.ticketCard = function (p, ticketUrl) {
    const url = ticketUrl.replace('__N__', encodeURIComponent(p.number));
    const place = [p.district, p.settlement, p.address].filter(Boolean).join(' · ');
    const badge = p.overdue
      ? h('span', { class: 'badge overdue' }, 'Просрочена')
      : h('span', { class: 'badge st-' + p.status }, p.status_label);
    return h('div', { class: 'tp-card' }, [
      h('div', { class: 'tp-head' }, [h('a', { class: 'tp-num', href: url }, '#' + p.number), badge]),
      h('div', { class: 'tp-title' }, p.title),
      p.category && h('span', { class: 'tp-chip' }, [
        h('i', { style: 'background:' + M.safeColor(p.color) }), p.category]),
      row('Заявитель', p.citizen || '—'),
      row('Место', place || '—'),
      row('Дата', p.created),
      p.accuracy && row('Точность', p.accuracy),
      h('a', { class: 'tp-open', href: url }, 'Открыть карточку →'),
    ]);
  };

  // Содержимое попапа: одна заявка — карточка целиком; несколько в одной точке
  // (адрес один, а заявок несколько) — список карточек с заголовком.
  M.popupContent = function (propsList, ticketUrl) {
    if (propsList.length === 1) return M.ticketCard(propsList[0], ticketUrl);
    return h('div', { class: 'tp-multi' }, [
      h('div', { class: 'tp-multi-head' }, 'В этой точке: ' + M.countLabel(propsList.length)),
      h('div', { class: 'tp-multi-list' }, propsList.map((p) => M.ticketCard(p, ticketUrl))),
    ]);
  };

  // Тексты для случаев, когда карту показать не удалось.
  M.messages = {
    noWebgl: 'В этом браузере не работает WebGL — включён упрощённый режим карты. ' +
      'Чтобы увидеть полную, включите аппаратное ускорение в настройках браузера.',
    noLibrary: 'Не удалось загрузить библиотеку карты (нужен доступ к unpkg.com). ' +
      'Проверьте интернет — остальные разделы панели работают и без него.',
    noBasemap: 'Подложка карты не загрузилась (нужен доступ к tiles.openfreemap.org). ' +
      'Границы и заявки показаны без неё.',
  };
})(window);
