(() => {
  'use strict';
  const site = JSON.parse(document.querySelector('#site-data').textContent);
  const $ = selector => document.querySelector(selector);
  const make = (tag, className, text) => {const node = document.createElement(tag); if (className) node.className = className; if (text !== undefined && text !== null) node.textContent = text; return node;};
  const base = `/studio/${site.developer_id}`;
  const games = site.games;
  const hiddenTags = new Set(['TapTap制造']);
  const tagsOf = game => game.tags.filter(tag => !hiddenTags.has(tag));
  const count = value => value >= 10000 ? `${+(value / 10000).toFixed(1)}万` : value >= 1000 ? `${+(value / 1000).toFixed(1)}k` : Number(value).toLocaleString('zh-CN');
  const rating = game => game.stats?.rating?.score ?? null;
  const ratingMax = game => game.stats?.rating?.max || 10;
  const img = (src, alt, eager = false) => {
    const node = make('img'); node.src = src || site.logo; node.alt = alt; node.width = 1920; node.height = 1080;
    node.referrerPolicy = 'no-referrer'; node.decoding = 'async'; if (!eager) node.loading = 'lazy';
    node.addEventListener('error', () => {if (node.src !== new URL(site.logo, location.href).href) node.src = site.logo;}, {once: true});
    return node;
  };
  const external = (label, url, className) => {const a = make('a', className, label); a.href = url; a.target = '_blank'; a.rel = 'noopener noreferrer'; return a;};

  document.documentElement.classList.add('js');
  document.querySelectorAll('[data-studio-name]').forEach(node => node.textContent = site.name);
  document.querySelectorAll('[data-taptap]').forEach(node => node.href = site.taptap_url);
  document.querySelectorAll('[data-studio-logo]').forEach(node => node.src = site.logo);
  $('#tagline').textContent = site.tagline;
  $('#studio-intro').textContent = site.intro;
  $('#about-copy').textContent = site.about;
  $('#year').textContent = new Date().getFullYear();
  if (site.synced_at) $('#sync-note').textContent = `作品数据由管理系统每日同步 · 最近更新 ${new Date(site.synced_at * 1000).toLocaleString('zh-CN', {hour12: false, year: 'numeric', month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit'})}`;

  // 顶栏滚动后加底色
  const header = $('#header');
  const onScroll = () => header.classList.toggle('scrolled', scrollY > 8);
  addEventListener('scroll', onScroll, {passive: true}); onScroll();

  // 首屏数据与封面扇形
  const sum = key => {const values = games.map(game => game.stats?.[key]).filter(value => value != null); return values.length ? values.reduce((a, b) => a + b, 0) : null;};
  const ratings = games.map(rating).filter(value => value != null);
  const stats = [
    ['作品', games.length ? String(games.length) : null],
    ['TapTap 关注', sum('fans_count') == null ? null : count(sum('fans_count'))],
    ['平均评分', ratings.length ? [(ratings.reduce((a, b) => a + b, 0) / ratings.length).toFixed(1), '/10'] : null],
    ['玩家评价', sum('review_count') == null ? null : count(sum('review_count'))],
  ];
  stats.forEach(([label, value]) => {
    if (value == null) return;
    const item = make('div'), dd = make('dd');
    if (Array.isArray(value)) dd.append(value[0], make('small', '', value[1])); else dd.textContent = value;
    item.append(make('dt', '', label), dd); $('#studio-stats').append(item);
  });
  games.filter(game => game.cover).slice(0, 3).forEach((game, index) => {const card = make('div', 'fan-card'); card.append(img(game.cover, '', index === 0)); $('#hero-fan').append(card);});

  const reveal = new IntersectionObserver(entries => entries.forEach(entry => {if (entry.isIntersecting) {entry.target.classList.add('seen'); reveal.unobserve(entry.target);}}), {threshold: .08});
  const observe = node => reveal.observe(node);
  document.querySelectorAll('.reveal').forEach(observe);

  // 详情弹窗与地址同步
  const dialog = $('#game-dialog');
  let selected = null, returnFocus = null;
  const updateMetadata = game => {
    document.title = (game ? game.title + ' · ' : '') + site.name;
    const description = (game ? game.description : site.intro).slice(0, 200);
    document.querySelector('meta[property="og:title"]').content = document.title;
    document.querySelector('meta[name="description"]').content = description;
    document.querySelector('meta[property="og:description"]').content = description;
    document.querySelector('meta[property="og:image"]').content = (game && game.cover) || site.logo;
  };
  const statPills = (container, stats, keys) => {
    container.replaceChildren();
    keys.forEach(([key, label]) => {if (stats?.[key]) {const pill = make('span'); pill.title = `${stats[key].toLocaleString('zh-CN')} ${label}`; pill.append(make('b', '', count(stats[key])), label); container.append(pill);}});
    container.hidden = !container.childElementCount;
  };
  const scoreBlock = game => {
    const wrap = make('div', 'score-line'), score = rating(game);
    if (score == null) {wrap.append(make('span', 'score-big', '—'), make('span', 'muted', '暂无评分')); return wrap;}
    const big = make('span', 'score-big', score.toFixed(1)); big.append(make('small', '', '/' + ratingMax(game)));
    const meter = make('span', 'score-meter'), fill = make('i'); fill.style.width = `${Math.min(100, score / ratingMax(game) * 100)}%`; meter.append(fill);
    wrap.setAttribute('aria-label', `TapTap 评分 ${score} 分，满分 ${ratingMax(game)} 分`); wrap.append(big, meter); return wrap;
  };
  const renderRating = game => {
    const panel = $('#dialog-rating'); panel.replaceChildren();
    const summary = make('div'); summary.append(make('p', 'label', 'TapTap 评分'), scoreBlock(game)); panel.append(summary);
    const votes = game.stats?.vote_info || {}, total = Object.values(votes).reduce((a, b) => a + b, 0);
    if (!total) return;
    const rows = make('div', 'rating-rows');
    for (let star = 5; star >= 1; star--) {
      const n = votes[String(star)] || 0, row = make('div', 'rating-row'), track = make('span', 'rating-track'), fill = make('i');
      fill.style.width = `${n / total * 100}%`; track.append(fill); track.setAttribute('aria-hidden', 'true');
      row.setAttribute('aria-label', `${star * 2} 分：${n} 人，占 ${Math.round(n / total * 100)}%`);
      row.append(make('span', '', `${star * 2} 分`), track, make('span', '', count(n))); rows.append(row);
    }
    panel.append(rows);
  };
  const showGame = (game, navigate = true) => {
    selected = game;
    if (!dialog.open) returnFocus = document.activeElement;
    $('#dialog-image').src = game.cover || game.icon || site.logo;
    $('#dialog-image').alt = game.title + '宣传图';
    $('#dialog-title').textContent = game.title;
    $('#dialog-tags').replaceChildren(...tagsOf(game).map(tag => make('span', 'tag', tag)));
    $('#dialog-description').textContent = game.description || '前往 TapTap，了解这款游戏。';
    $('#dialog-play').href = game.url; $('#dialog-forum').href = game.forum_url; $('#share-status').textContent = '';
    statPills($('#dialog-stats'), game.stats, [['fans_count', '关注'], ['review_count', '评价'], ['topic_count', '话题'], ['hits_total', '浏览']]);
    renderRating(game); updateMetadata(game);
    if (navigate) history.pushState({studioGame: true}, '', `${base}/games/${game.id}`);
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0; document.body.style.overflow = 'hidden';
  };
  const syncLocation = () => {
    const match = location.pathname.match(/\/games\/(\d+)$/);
    const game = match && games.find(item => item.id === Number(match[1]));
    if (game) showGame(game, false); else {if (dialog.open) dialog.close(); updateMetadata(null);}
  };
  const closeGame = () => {if (history.state?.studioGame) history.back(); else {history.replaceState({}, '', base); syncLocation();}};
  const opener = game => event => {if (event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {event.preventDefault(); showGame(game);}};
  dialog.querySelector('.dialog-close').addEventListener('click', closeGame);
  dialog.addEventListener('cancel', event => {event.preventDefault(); closeGame();});
  dialog.addEventListener('click', event => {if (event.target !== dialog) return; const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeGame();});
  dialog.addEventListener('close', () => {document.body.style.overflow = ''; selected = null; if (returnFocus?.isConnected) returnFocus.focus({preventScroll: true});});
  addEventListener('popstate', syncLocation);
  $('#share-game').addEventListener('click', async () => {
    if (!selected) return;
    const url = new URL(`${base}/games/${selected.id}`, location.origin).href;
    try {
      if (navigator.share) await navigator.share({title: selected.title, url});
      else if (navigator.clipboard && isSecureContext) {await navigator.clipboard.writeText(url); $('#share-status').textContent = '作品链接已复制。';}
      else $('#share-status').textContent = url;
    } catch (error) {if (error.name !== 'AbortError') $('#share-status').textContent = url;}
  });

  // 精选作品
  const spotlight = games.find(game => game.featured) || (games.length > 3 ? games[0] : null);
  if (spotlight) {
    const card = $('#spotlight-card'), art = make('a', 'spotlight-art'), body = make('div', 'spotlight-body');
    art.href = `${base}/games/${spotlight.id}`; art.setAttribute('aria-label', '查看' + spotlight.title + '详情'); art.addEventListener('click', opener(spotlight));
    art.append(img(spotlight.cover || spotlight.icon, spotlight.title + '宣传图'));
    const tags = make('div', 'tag-row'); tagsOf(spotlight).slice(0, 3).forEach(tag => tags.append(make('span', 'tag', tag)));
    const pills = make('div', 'stat-pills'); statPills(pills, spotlight.stats, [['fans_count', '关注'], ['review_count', '评价']]);
    const actions = make('div', 'spotlight-actions'), detail = make('a', 'button', '查看详情 →');
    detail.href = art.href; detail.addEventListener('click', opener(spotlight));
    actions.append(detail, external('前往 TapTap ↗', spotlight.url, 'text-link'));
    body.append(tags, make('h3', '', spotlight.title), scoreBlock(spotlight), pills, make('p', 'desc', spotlight.description), actions);
    card.append(art, body); $('#spotlight').hidden = false;
  }

  // 作品网格与筛选
  const renderGrid = filter => {
    const list = $('#game-list'); list.replaceChildren();
    const shown = games.filter(game => filter ? game.tags.includes(filter) : game !== spotlight);
    $('#empty').hidden = shown.length > 0;
    $('#empty').textContent = games.length ? '这个分类还没有作品，试试其他分类吧。' : '作品正在准备中，过一会儿再来逛逛吧。';
    shown.forEach(game => {
      const card = make('article', 'game-card reveal'), art = make('div', 'art');
      art.append(img(game.cover || game.icon, game.title + '宣传图'));
      if (rating(game) != null) {const chip = make('span', 'rating-chip', rating(game).toFixed(1)); chip.append(make('small', '', '/' + ratingMax(game))); chip.setAttribute('aria-label', `TapTap 评分 ${rating(game)} 分`); art.append(chip);}
      if (game.featured) art.append(make('span', 'featured-chip', '精选'));
      const body = make('div', 'body'), title = make('h3'), link = make('a', '', game.title);
      link.href = `${base}/games/${game.id}`; link.addEventListener('click', opener(game)); title.append(link);
      const tags = make('div', 'tag-row'); tagsOf(game).slice(0, 3).forEach(tag => tags.append(make('span', 'tag', tag)));
      const meta = make('div', 'meta'), numbers = [];
      if (game.stats?.fans_count != null) numbers.push(`${count(game.stats.fans_count)} 关注`);
      if (game.stats?.review_count != null) numbers.push(`${count(game.stats.review_count)} 评价`);
      meta.append(make('span', '', numbers.join(' · ')), external('TapTap ↗', game.url, 'ext'));
      body.append(title, tags, make('p', 'desc', game.description), meta);
      card.append(art, body); list.append(card); observe(card);
    });
  };
  const tagList = [...new Set(games.flatMap(tagsOf))].slice(0, 6);
  if (tagList.length > 1) [['全部', ''], ...tagList.map(tag => [tag, tag])].forEach(([label, value], index) => {
    const button = make('button', '', label); button.type = 'button'; button.setAttribute('aria-pressed', String(index === 0));
    button.addEventListener('click', () => {$('#filters').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button))); renderGrid(value);});
    $('#filters').append(button);
  });
  renderGrid('');
  syncLocation();
})();
