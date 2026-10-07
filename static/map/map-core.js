/*
 * map-core.js — большая карта обращений: всё, что не зависит от движка.
 *
 * Здесь живут фильтры, загрузка точек, счётчики, подсказка над районом,
 * ссылка /map/?ticket=…, сообщения об ошибках — и выбор движка отрисовки.
 * Движков два с одним и тем же набором методов:
 *   maplibre — основной (WebGL, векторная подложка, анимации);
 *   leaflet  — запасной, если в браузере нет WebGL или не загрузился MapLibre.
 * Логика данных одна на оба, поэтому запасной режим ведёт себя так же:
 * те же фильтры, те же попапы, те же счётчики.
 */
(function (global) {
  'use strict';

  const M = global.ManasMap;
  const byId = (id) => document.getElementById(id);

  const root = byId('map-app');
  if (!root) return;
  const config = JSON.parse(byId('map-config').textContent);

  const el = {
    canvas: byId('map-canvas'), panel: byId('map-panel'), legend: byId('map-legend'),
    tip: byId('map-tip'), toast: byId('map-toast'), empty: byId('map-empty'),
    note: byId('map-note'), error: byId('map-error'), emptyText: byId('map-empty-text'),
    onMap: byId('cnt-on-map'), without: byId('cnt-without'), counters: byId('map-counters'),
    density: byId('density-toggle'), reset: byId('filters-reset'),
  };

  const DEFAULTS = { status: 'open', period: 'all', category: '', district: '', kind: '' };
  const REFRESH_MS = 90 * 1000;

  const state = {
    filters: Object.assign({}, config.filters),
    focus: config.filters.ticket || '',   // заявка из ссылки: подсветить и показать
    data: null,
    engine: null,
    abort: null,
    seq: 0,
    toastTimer: 0,
    focusShown: false,
  };
  const params = new URLSearchParams(location.search);

  // ------------------------------------------------------------------ интерфейс

  function toast(message, ms) {
    el.toast.textContent = message;
    el.toast.hidden = false;
    clearTimeout(state.toastTimer);
    if (ms !== 0) state.toastTimer = setTimeout(() => { el.toast.hidden = true; }, ms || 7000);
  }

  function showError(message) {
    el.error.querySelector('p').textContent = message;
    el.error.hidden = false;
  }

  function setBusy(busy) {
    root.classList.toggle('is-loading', busy);
    root.setAttribute('aria-busy', busy ? 'true' : 'false');
  }

  // Отступ слева, который занимает плавающая панель: чтобы «вся область»
  // и «показать заявку» вписывались в свободную часть экрана.
  function insetLeft() {
    if (global.innerWidth <= 900) return 0;
    const box = el.panel.getBoundingClientRect();
    return Math.max(0, Math.round(box.right - root.getBoundingClientRect().left));
  }

  function updateCounters(meta) {
    el.onMap.textContent = meta.on_map;
    el.without.textContent = meta.without_coords;
    const parts = [];
    if (meta.no_address) parts.push('без адреса: ' + meta.no_address);
    if (meta.failed) parts.push('адрес не распознан: ' + meta.failed);
    const waiting = meta.without_coords - meta.no_address - meta.failed;
    if (waiting > 0) parts.push('координаты ещё определяются: ' + waiting);
    el.without.parentElement.title = parts.join('\n') || 'У всех заявок есть точка на карте';
    el.empty.hidden = meta.on_map > 0;
    el.emptyText.textContent = meta.without_coords > 0
      ? 'Пока ни у одной заявки нет координат — показана только область.'
      : 'По выбранным фильтрам заявок нет.';
  }

  // Подсказка над районом: название и число обращений по текущему фильтру.
  function onHover(info, point) {
    if (!info || !point) { el.tip.hidden = true; return; }
    const count = (state.data && state.data.meta.district_counts[info.slug]) || 0;
    el.tip.replaceChildren(M.h('b', {}, info.name), M.h('span', {}, M.countLabel(count)));
    el.tip.hidden = false;
    const box = root.getBoundingClientRect();
    const x = Math.min(point.x + 16, box.width - el.tip.offsetWidth - 8);
    const y = Math.min(point.y + 18, box.height - el.tip.offsetHeight - 8);
    el.tip.style.transform = 'translate(' + Math.max(8, x) + 'px,' + Math.max(8, y) + 'px)';
  }

  // ------------------------------------------------------------------ данные

  function queryOf(filters) {
    const query = new URLSearchParams();
    // status и period передаём всегда, даже «по умолчанию»: для ссылки на заявку
    // сервер по умолчанию берёт «все», а у пользователя может быть выбрано другое.
    Object.keys(DEFAULTS).forEach((key) => { if (filters[key]) query.set(key, filters[key]); });
    if (state.focus) query.set('ticket', state.focus);
    return query;
  }

  function syncUrl() {
    const query = queryOf(state.filters);
    // Адрес страницы — это сохранённый вид карты: его можно скопировать коллеге.
    history.replaceState(null, '', location.pathname + '?' + query.toString());
  }

  async function refresh() {
    if (state.abort) state.abort.abort();
    const controller = (state.abort = new AbortController());
    const seq = ++state.seq;
    setBusy(true);
    try {
      const data = await M.fetchJSON(config.dataUrl + '?' + queryOf(state.filters).toString(),
        { signal: controller.signal });
      if (seq !== state.seq) return;
      state.data = data;
      applyData(data);
    } catch (error) {
      if (error.name === 'AbortError') return;
      toast('Не удалось загрузить заявки. Проверьте соединение — карта обновится сама.');
    } finally {
      if (seq === state.seq) { setBusy(false); state.abort = null; }
    }
  }

  function applyData(data) {
    updateCounters(data.meta);
    if (!state.engine) return;
    state.engine.setPoints(data);
    state.engine.setCounts(data.meta.district_counts);
    if (data.meta.truncated) {
      toast('Показаны последние ' + data.meta.limit + ' заявок — сузьте фильтры, чтобы увидеть остальные.');
    }
    if (state.focus && !state.focusShown) showFocus(data);
  }

  // Ссылка из карточки: /map/?ticket=… — приблизить и открыть попап заявки.
  function showFocus(data) {
    state.focusShown = true;
    const feature = data.features.find((f) => f.properties.number === state.focus);
    if (feature) { state.engine.showTicket(feature, { fly: true }); return; }
    const info = data.meta.focus;
    if (info && !info.exists) toast('Заявка №' + state.focus + ' не найдена.');
    else toast('У заявки №' + state.focus + ' пока нет точки на карте: адрес ещё не найден. ' +
      'Укажите адрес или населённый пункт в карточке — точка появится сама.', 12000);
  }

  // ------------------------------------------------------------------ фильтры

  function syncControls() {
    root.querySelectorAll('.seg[data-filter]').forEach((group) => {
      const value = state.filters[group.dataset.filter];
      group.querySelectorAll('button').forEach((button) => {
        button.setAttribute('aria-checked', button.dataset.value === value ? 'true' : 'false');
      });
    });
    root.querySelectorAll('select[data-filter]').forEach((select) => {
      select.value = state.filters[select.dataset.filter] || '';
    });
  }

  function onFilterChange() {
    // Пользователь сам выбирает, что смотреть: заявка из ссылки больше не «приколота».
    state.focus = '';
    syncUrl();
    refresh();
  }

  function bindControls() {
    root.querySelectorAll('.seg[data-filter]').forEach((group) => {
      group.addEventListener('click', (event) => {
        const button = event.target.closest('button[data-value]');
        if (!button) return;
        state.filters[group.dataset.filter] = button.dataset.value;
        syncControls();
        onFilterChange();
      });
    });
    root.querySelectorAll('select[data-filter]').forEach((select) => {
      select.addEventListener('change', () => {
        state.filters[select.dataset.filter] = select.value;
        onFilterChange();
      });
    });
    el.reset.addEventListener('click', () => {
      Object.assign(state.filters, DEFAULTS);
      syncControls();
      onFilterChange();
    });

    // Плотность: заливка районов по числу заявок.
    const density = M.store.get('density') === '1';
    el.density.checked = density;
    el.density.addEventListener('change', () => {
      M.store.set('density', el.density.checked ? '1' : '0');
      root.classList.toggle('density-on', el.density.checked);
      if (state.engine) state.engine.setDensity(el.density.checked);
    });
    root.classList.toggle('density-on', density);

    // Свернуть панель и легенду — освободить карту.
    [['panel', byId('map-panel-toggle')], ['legend', byId('map-legend-toggle')]].forEach((pair) => {
      const name = pair[0], button = pair[1];
      const apply = (collapsed) => {
        root.classList.toggle(name + '-collapsed', collapsed);
        button.setAttribute('aria-expanded', collapsed ? 'false' : 'true');
        button.title = collapsed ? 'Развернуть' : 'Свернуть';
      };
      // Легенда на невысоком экране по умолчанию свёрнута: фильтры важнее,
      // а развернуть её — один щелчок (выбор потом запоминается).
      const stored = M.store.get(name + '-collapsed');
      apply(stored === null ? (name === 'legend' && global.innerHeight < 820) : stored === '1');
      button.addEventListener('click', () => {
        const collapsed = !root.classList.contains(name + '-collapsed');
        apply(collapsed);
        M.store.set(name + '-collapsed', collapsed ? '1' : '0');
        if (name === 'panel' && state.engine) state.engine.insetChanged();
      });
    });
  }

  // ------------------------------------------------------------------ запасной движок

  const LEAFLET = {
    css: ['https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
      'sha384-sHL9NAb7lN7rfvG5lfHpm643Xkcjzp4jFvuavGOndn6pjVqS6ny56CAt3nsEVT4H'],
    js: ['https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
      'sha384-cxOPjt7s7Iz04uaHJceBmS+qpjv2JkIHNVcuOrM+YHwZOmJGBXI00mdUXEq65HTH'],
    cluster: ['https://unpkg.com/supercluster@8.0.1/dist/supercluster.min.js',
      'sha384-HkQmq7PC2BUVUkCsRUnOyzpliMb5M4pVPnjEiyI92tdS8buRHN6ZnehP1n1Acj84'],
  };

  function loadScript(src, integrity) {
    return new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = src;
      if (integrity) { script.integrity = integrity; script.crossOrigin = 'anonymous'; }
      script.onload = resolve;
      script.onerror = () => reject(new Error('Не загрузился ' + src));
      document.head.appendChild(script);
    });
  }

  function loadStyle(href, integrity) {
    return new Promise((resolve, reject) => {
      const link = document.createElement('link');
      link.rel = 'stylesheet';
      link.href = href;
      if (integrity) { link.integrity = integrity; link.crossOrigin = 'anonymous'; }
      link.onload = resolve;
      link.onerror = () => reject(new Error('Не загрузился ' + href));
      document.head.appendChild(link);
    });
  }

  async function loadFallbackLibraries() {
    await Promise.all([
      loadStyle(LEAFLET.css[0], LEAFLET.css[1]),
      loadScript(LEAFLET.js[0], LEAFLET.js[1]).then(() => loadScript(LEAFLET.cluster[0], LEAFLET.cluster[1])),
      loadScript(root.dataset.leafletEngineUrl),
    ]);
  }

  // ------------------------------------------------------------------ запуск

  function engineContext(boundaries) {
    const names = {};
    config.districts.forEach((d) => { names[d.slug] = d.name; });
    return {
      container: el.canvas,
      boundaries,
      names,
      ticketUrl: config.ticketUrl,
      insetLeft,
      events: { hover: onHover, error: (message) => toast(message, 12000) },
    };
  }

  // Движок выбирается по возможностям браузера; ?engine=leaflet принудительно
  // включает запасной (для проверки и для случаев, когда WebGL «есть, но тормозит»).
  async function startEngine(boundaries) {
    const forced = params.get('engine');
    const context = engineContext(boundaries);
    let reason = '';
    if (forced !== 'leaflet') {
      if (!global.maplibregl) reason = 'library';
      else if (!M.webglSupported()) reason = 'webgl';
      else {
        const engine = M.engines.maplibre(context);
        try {
          await engine.init();
          return engine;
        } catch (error) {
          console.error('MapLibre не запустился:', error);
          engine.destroy();
          el.canvas.replaceChildren();
          el.canvas.className = 'map-canvas';
          reason = 'init';
        }
      }
    }
    await loadFallbackLibraries();
    const engine = M.engines.leaflet(context);
    await engine.init();
    if (reason === 'webgl' || reason === 'init') {
      el.note.textContent = M.messages.noWebgl;
      el.note.hidden = false;
    } else if (reason === 'library') {
      el.note.textContent = 'Основная карта не загрузилась (нет доступа к unpkg.com) — включён упрощённый режим.';
      el.note.hidden = false;
    }
    return engine;
  }

  async function start() {
    bindControls();
    syncControls();
    syncUrl();
    setBusy(true);
    let boundaries;
    try {
      boundaries = await M.fetchJSON(root.dataset.boundariesUrl);
    } catch (error) {
      setBusy(false);
      showError('Не удалось загрузить границы области. Обновите страницу.');
      return;
    }
    try {
      state.engine = await startEngine(boundaries);
    M.debug = { state };  // доступ из консоли браузера и автотестов
    } catch (error) {
      console.error(error);
      setBusy(false);
      showError(M.messages.noLibrary);
      return;
    }
    state.engine.setDensity(el.density.checked);
    await refresh();

    // Новые заявки появляются, пока карта открыта: раз в полторы минуты
    // обновляем точки (только на видимой вкладке — скрытую не тревожим).
    setInterval(() => { if (!document.hidden && !state.abort) refresh(); }, REFRESH_MS);
  }

  el.error.querySelector('button').addEventListener('click', () => location.reload());
  start();
})(window);
