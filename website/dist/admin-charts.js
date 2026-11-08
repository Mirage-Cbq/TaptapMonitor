(() => {
  'use strict';
  const NS = 'http://www.w3.org/2000/svg';
  const series = [
    ['review', '评价', 'var(--accent)', ''], ['topic', '论坛帖子', 'var(--blue)', '6 3'],
    ['review_comment', '评价回复', 'var(--orange)', '2 3'], ['topic_comment', '论坛回复', 'var(--purple)', '9 3 2 3']
  ];
  function svgNode(tag, attrs = {}, text) {
    const node = document.createElementNS(NS, tag);
    Object.entries(attrs).forEach(([key, value]) => node.setAttribute(key, value));
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function base(host, width, height, description) {
    host.replaceChildren();
    const wrap = document.createElement('div'); wrap.className = 'chart-wrap';
    const svg = svgNode('svg', {viewBox: `0 0 ${width} ${height}`, class: 'chart', role: 'group', 'aria-label': description});
    const tip = document.createElement('div'); tip.className = 'chart-tip'; tip.hidden = true;
    wrap.append(svg, tip); host.append(wrap);
    return {svg, wrap, tip, width, height};
  }
  function tooltip(chart, node, text, x, y) {
    const show = () => {
      chart.tip.textContent = text; chart.tip.hidden = false;
      chart.tip.style.left = `${Math.min(82, Math.max(18, x / chart.width * 100))}%`;
      chart.tip.style.top = `${Math.max(0, y / chart.height * chart.wrap.clientHeight - 38)}px`;
    };
    const hide = () => {chart.tip.hidden = true;};
    node.addEventListener('pointerenter', show); node.addEventListener('pointerleave', hide);
    node.addEventListener('focus', show); node.addEventListener('blur', hide);
    node.setAttribute('tabindex', '0'); node.setAttribute('role', 'img'); node.setAttribute('aria-label', text);
    node.append(svgNode('title', {}, text));
  }
  function empty(host, text = '暂无反馈数据，同步后将在这里显示。') {
    const p = document.createElement('p'); p.className = 'empty'; p.textContent = text; host.replaceChildren(p);
  }
  function trend(host, daily) {
    if (!daily.some(day => series.some(([key]) => day[key]))) {empty(host); return;}
    const W = Math.max(300, host.clientWidth || 960), H = W < 500 ? 230 : 265, L = 38, R = 16, T = 20, B = 34;
    const chart = base(host, W, H, '每日新增反馈折线图。可用 Tab 键查看每日明细。');
    const rawMax = Math.max(...daily.flatMap(day => series.map(([key]) => day[key])));
    const ticks = Math.min(4, rawMax), max = Math.ceil(rawMax / ticks) * ticks;
    const x = i => L + i / Math.max(1, daily.length - 1) * (W - L - R);
    const y = value => H - B - value / max * (H - T - B);
    for (let n = 0; n <= ticks; n++) {
      const value = max * n / ticks, yy = y(value);
      chart.svg.append(svgNode('line', {x1:L, x2:W-R, y1:yy, y2:yy, class:'grid'}), svgNode('text', {x:L-10, y:yy+4, 'text-anchor':'end'}, value));
    }
    chart.svg.append(svgNode('text', {x:L, y:10}, '条'));
    const labelCount = W < 500 ? 3 : 6;
    for (let n = 0; n <= labelCount; n++) {
      const i = Math.round(n * (daily.length - 1) / labelCount);
      chart.svg.append(svgNode('text', {x:x(i), y:H-10, 'text-anchor':i === 0 ? 'start' : i === daily.length-1 ? 'end' : 'middle'}, daily[i].date.slice(5)));
    }
    series.forEach(([key, , color, dash]) => {
      const points = daily.map((day, i) => `${x(i)},${y(day[key])}`);
      if (key === 'review') chart.svg.append(svgNode('polygon', {points:`${L},${H-B} ${points.join(' ')} ${W-R},${H-B}`, fill:color, opacity:'.07'}));
      chart.svg.append(svgNode('polyline', {points:points.join(' '), fill:'none', stroke:color, 'stroke-width':2.5, 'stroke-dasharray':dash, 'stroke-linejoin':'round'}));
    });
    daily.forEach((day, i) => {
      const hit = svgNode('rect', {x:Math.max(L, x(i)-(W-L-R)/daily.length/2), y:T, width:(W-L-R)/daily.length+2, height:H-T-B, fill:'transparent'});
      tooltip(chart, hit, `${day.date}\n${series.map(([key, label]) => `${label}：${day[key]} 条`).join('\n')}`, x(i), T + 30);
      chart.svg.append(hit);
    });
    const legend = document.createElement('div'); legend.className = 'legend';
    series.forEach(([, label, color]) => {const span = document.createElement('span'), mark = document.createElement('i'); mark.style.background = color; span.append(mark, document.createTextNode(label)); legend.append(span);});
    host.append(legend);
  }
  function scores(host, distribution) {
    const counts = [1,2,3,4,5].map(n => distribution[String(n)] || 0), total = counts.reduce((a,b) => a+b,0);
    if (!total) {empty(host, '暂无带评分的评价。'); return;}
    const W=Math.max(300,host.clientWidth || 460), H=250, L=34, T=20, B=36, R=15, ticks=Math.min(4,Math.max(...counts)), max=Math.ceil(Math.max(...counts)/ticks)*ticks;
    const chart = base(host,W,H,'评价评分分布柱状图。可用 Tab 键查看各评分数量。');
    for (let i=0;i<=ticks;i++) {
      const yy=H-B-(H-T-B)*i/ticks;
      chart.svg.append(svgNode('line',{x1:L,x2:W-R,y1:yy,y2:yy,class:'grid'}),svgNode('text',{x:L-8,y:yy+4,'text-anchor':'end'},max*i/ticks));
    }
    chart.svg.append(svgNode('text',{x:L,y:10},'条'));
    counts.forEach((count,i) => {
      const step=(W-L-R)/5, x=L+step*i+step*.2, height=count/max*(H-T-B), y=H-B-height;
      const bar=svgNode('rect',{x,y,width:step*.6,height:Math.max(2,height),rx:3,fill:'var(--accent)',opacity:.5+i*.12});
      tooltip(chart,bar,`${i+1} 星：${count} 条（${(count/total*100).toFixed(1)}%）`,x+step*.3,y);
      chart.svg.append(bar,svgNode('text',{x:x+step*.3,y:H-12,'text-anchor':'middle'},`${i+1} 星`),svgNode('text',{x:x+step*.3,y:y-7,'text-anchor':'middle'},count));
    });
  }
  window.AdminCharts = {trend, scores, empty};
})();
