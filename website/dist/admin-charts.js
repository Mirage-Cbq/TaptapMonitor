(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const SERIES = [['review', '评价'], ['topic', '论坛帖子'], ['review_comment', '评价回复'], ['topic_comment', '论坛回复']];
  const color = key => `var(--c-${key})`;
  const hiddenSeries = new Set();
  const svg = (tag, attrs = {}, text) => {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  };
  const html = (tag, className, text) => {const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined) node.textContent = text; return node;};
  function empty(host, text = '暂无数据，同步后会显示在这里。') {host.replaceChildren(html('p', 'empty', text));}
  // 刻度取 1/2/5 × 10^n，保证网格数字整齐
  function niceMax(value, ticks = 4) {
    if (value <= ticks) return {max: Math.max(1, value), step: 1};
    const raw = value / ticks, power = 10 ** Math.floor(Math.log10(raw));
    const step = [1, 2, 5, 10].map(m => m * power).find(s => s >= raw);
    return {max: Math.ceil(value / step) * step, step};
  }

  function trend(host, daily) {
    const visible = SERIES.filter(([key]) => !hiddenSeries.has(key));
    if (!daily.some(day => SERIES.some(([key]) => day[key]))) {empty(host, '所选范围内没有新增反馈。'); return;}
    host.replaceChildren();
    const W = Math.max(320, host.clientWidth || 800), H = W < 520 ? 220 : 260, L = 34, R = 8, T = 12, B = 28;
    const totals = daily.map(day => visible.reduce((sum, [key]) => sum + day[key], 0));
    const {max, step} = niceMax(Math.max(1, ...totals));
    const wrap = html('div', 'chart-wrap'), tip = html('div', 'chart-tip'); tip.hidden = true;
    const chart = svg('svg', {viewBox: `0 0 ${W} ${H}`, class: 'chart', role: 'group', 'aria-label': '每日新增反馈堆叠柱状图，可用 Tab 键逐日查看'});
    const y = value => H - B - value / max * (H - T - B);
    for (let v = 0; v <= max; v += step) chart.append(svg('line', {x1: L, x2: W - R, y1: y(v), y2: y(v), class: 'grid'}), svg('text', {x: L - 8, y: y(v) + 4, 'text-anchor': 'end'}, v));
    const slot = (W - L - R) / daily.length, barW = Math.max(2, Math.min(28, slot * .64));
    const labelEvery = Math.ceil(daily.length / (W < 520 ? 4 : 8));
    // 每隔 labelEvery 天一个日期；末日总是标注，若离上一个刻度太近则替换它
    const last = daily.length - 1, labelled = new Set();
    for (let i = 0; i <= last; i += labelEvery) labelled.add(i);
    const lastTick = Math.floor(last / labelEvery) * labelEvery;
    if (last - lastTick < labelEvery / 2) labelled.delete(lastTick);
    labelled.add(last);
    daily.forEach((day, i) => {
      const x = L + slot * i + (slot - barW) / 2, group = svg('g', {class: 'col', tabindex: '0', role: 'img'});
      group.append(svg('rect', {class: 'hover-bg', x: L + slot * i, y: T, width: slot, height: H - T - B, fill: 'transparent', rx: 4}));
      let acc = 0;
      visible.forEach(([key], index) => {
        const value = day[key]; if (!value) return;
        const top = y(acc + value), height = y(acc) - top; acc += value;
        const isTop = visible.slice(index + 1).every(([k]) => !day[k]);
        group.append(svg('rect', {class: 'bar', x, y: top, width: barW, height: Math.max(1, height - (isTop ? 0 : 1)), rx: isTop ? Math.min(4, barW / 2) : 0, fill: color(key)}));
      });
      const label = `${day.date}\n` + SERIES.map(([key, name]) => `${name}　${day[key]} 条`).join('\n') + `\n合计　${SERIES.reduce((s, [k]) => s + day[k], 0)} 条`;
      group.setAttribute('aria-label', label.replace(/\n/g, '，'));
      const show = () => {tip.textContent = label; tip.hidden = false; tip.style.left = `${Math.min(88, Math.max(12, (x + barW / 2) / W * 100))}%`; tip.style.top = `${Math.max(36, y(totals[i]) / H * wrap.clientHeight - 6)}px`;};
      const hide = () => {tip.hidden = true;};
      group.addEventListener('pointerenter', show); group.addEventListener('pointerleave', hide); group.addEventListener('focus', show); group.addEventListener('blur', hide);
      chart.append(group);
      if (labelled.has(i)) chart.append(svg('text', {x: L + slot * i + slot / 2, y: H - 8, 'text-anchor': 'middle'}, day.date.slice(5).replace('-', '/')));
    });
    wrap.append(chart, tip); host.append(wrap);
    const legend = html('div', 'legend');
    SERIES.forEach(([key, name]) => {
      const button = html('button'), mark = html('i'); button.type = 'button'; mark.style.background = color(key);
      const total = daily.reduce((sum, day) => sum + day[key], 0);
      button.append(mark, `${name} ${total}`); button.setAttribute('aria-pressed', String(!hiddenSeries.has(key)));
      button.addEventListener('click', () => {if (hiddenSeries.has(key)) hiddenSeries.delete(key); else if (visible.length > 1) hiddenSeries.add(key); trend(host, daily);});
      legend.append(button);
    });
    host.append(legend);
  }

  function sparkline(values, className = 'spark') {
    const W = 96, H = 34, max = Math.max(1, ...values);
    const node = svg('svg', {viewBox: `0 0 ${W} ${H}`, class: className, 'aria-hidden': 'true', preserveAspectRatio: 'none'});
    const points = values.map((v, i) => `${(i / Math.max(1, values.length - 1) * W).toFixed(1)},${(H - 2 - v / max * (H - 4)).toFixed(1)}`);
    node.append(svg('polygon', {points: `0,${H} ${points.join(' ')} ${W},${H}`, fill: 'var(--accent)', opacity: '.12'}));
    node.append(svg('polyline', {points: points.join(' '), fill: 'none', stroke: 'var(--accent)', 'stroke-width': 1.8, 'stroke-linejoin': 'round', 'vector-effect': 'non-scaling-stroke'}));
    return node;
  }

  // 评价为 1–5 星，统一按 ×2 显示为十分制
  function scores(host, distribution, average) {
    const counts = [5, 4, 3, 2, 1].map(star => [star, distribution[String(star)] || 0]);
    const total = counts.reduce((sum, [, n]) => sum + n, 0);
    if (!total) {empty(host, '暂无带评分的评价。'); return;}
    host.replaceChildren();
    const summary = html('div', 'score-summary'), strong = html('strong', '', average == null ? '—' : (average * 2).toFixed(1));
    summary.append(strong, html('span', 'muted', `/10 · ${total} 条评价`)); host.append(summary);
    const rows = html('div', 'score-rows');
    const tone = star => star >= 4 ? 'var(--ok)' : star === 3 ? 'var(--c-review_comment)' : 'var(--danger)';
    counts.forEach(([star, n]) => {
      const row = html('div', 'score-row'), track = html('span', 'track'), fill = html('i');
      fill.style.width = `${n / total * 100}%`; fill.style.background = tone(star); track.append(fill);
      row.setAttribute('aria-label', `${star * 2} 分：${n} 条，占 ${(n / total * 100).toFixed(0)}%`);
      row.append(html('span', '', `${star * 2} 分`), track, html('span', '', `${n} · ${(n / total * 100).toFixed(0)}%`)); rows.append(row);
    });
    host.append(rows);
  }

  window.AdminCharts = {trend, scores, sparkline, empty, SERIES};
})();
