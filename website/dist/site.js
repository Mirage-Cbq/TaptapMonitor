(() => {
  'use strict';
  const site = JSON.parse(document.querySelector('#site-data').textContent);
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; return element;};
  const base = `/studio/${site.developer_id}`;
  const highlights = site.highlights || [];
  const count = value => value >= 10000 ? `${+(value / 10000).toFixed(1)}万` : value >= 1000 ? `${+(value / 1000).toFixed(1)}k` : value.toLocaleString('zh-CN');
  const ratingLabel = rating => rating.score == null ? '暂无评分' : `${rating.score}${rating.max == null ? '' : ` / ${rating.max}`}`;
  const externalLink = (label, url, className = 'text-link') => {
    const link = make('a', className, label);
    link.href = url; link.target = '_blank'; link.rel = 'noopener noreferrer';
    return link;
  };
  document.documentElement.classList.add('js');
  document.querySelectorAll('[data-studio-name]').forEach(e => e.textContent = site.name);
  document.querySelectorAll('[data-taptap]').forEach(e => e.href = site.taptap_url);
  $('#tagline').textContent = site.tagline;
  $('#studio-intro').textContent = site.intro;
  $('#about-copy').textContent = site.about;
  $('#year').textContent = new Date().getFullYear();
  $('#game-count').textContent = String(site.games.length).padStart(2, '0');
  document.querySelectorAll('[data-studio-logo]').forEach(image => image.src = site.logo);
  const addMetric = (label, value) => {
    if (value == null) return;
    const metric = make('div'); metric.append(make('dt', '', label), make('dd', '', value)); $('#studio-summary').append(metric);
  };
  addMetric('作品数', count(site.games.length));
  const sumStat = key => {const values = site.games.map(game => game.stats[key]).filter(value => value != null); return values.length ? values.reduce((sum, value) => sum + value, 0) : null;};
  const fans = sumStat('fans_count'), reviews = sumStat('review_count');
  const ratings = site.games.map(game => game.stats.rating.score).filter(value => value != null);
  addMetric('总关注', fans == null ? null : count(fans));
  addMetric('平均评分', ratings.length ? (ratings.reduce((sum, value) => sum + value, 0) / ratings.length).toFixed(1) : null);
  addMetric('玩家评价数', reviews == null ? null : count(reviews));
  const renderStats = (container, stats, detail = false) => {
    container.replaceChildren();
    if (!detail && stats.rating.score != null) {
      const badge = make('span', 'rating-badge', `★ ${ratingLabel(stats.rating)}`);
      badge.setAttribute('aria-label', `玩家评分 ${ratingLabel(stats.rating)}`); container.append(badge);
    }
    const metrics = [['fans_count', '关注'], ['review_count', '评价'], ...(detail ? [['topic_count', '话题'], ['hits_total', '浏览']] : [])];
    metrics.forEach(([key, label]) => {if (stats[key] != null) {const metric = make('span', '', `${count(stats[key])} ${label}`); metric.title = `${stats[key].toLocaleString('zh-CN')} ${label}`; container.append(metric);}});
    container.hidden = !container.childElementCount;
  };
  const renderReviews = (container, reviews, showGameTitle = true) => {
    container.replaceChildren();
    reviews.forEach(review => {
      const article = make('article', 'player-review');
      if (showGameTitle) {const game = site.games.find(game => game.id === review.game_id); const link = make('a', 'review-game', review.game_title); link.href = `${base}/games/${review.game_id}`; link.addEventListener('click', event => openGameLink(event, game)); article.append(link);}
      const stars = make('p', 'review-stars', '★'.repeat(review.score) + '☆'.repeat(5 - review.score)); stars.setAttribute('aria-label', `${review.score} 星，满分 5 星`);
      const quote = make('blockquote', '', review.excerpt);
      const footer = make('footer', 'review-footer'); const author = make('span', '', review.author);
      const date = make('time', '', new Date(review.published_at * 1000).toLocaleDateString('zh-CN')); date.dateTime = new Date(review.published_at * 1000).toISOString();
      footer.append(author, date);
      if (/^https?:\/\//i.test(review.url || '')) footer.append(externalLink('查看原评 ↗', review.url));
      article.append(stars, quote, footer); container.append(article);
    });
  };
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {if (entry.isIntersecting) {entry.target.classList.add('seen'); observer.unobserve(entry.target);}}), {threshold: .08});
  document.querySelectorAll('.reveal').forEach(e => observer.observe(e));
  let selected = null;
  let returnFocus = null;
  const dialog = $('#game-dialog');
  const updateMetadata = game => {
    document.title = (game ? game.title + ' · ' : '') + site.name;
    document.querySelector('meta[property="og:title"]').content = document.title;
    const description = (game ? game.description : site.intro).slice(0, 200);
    document.querySelector('meta[name="description"]').content = description;
    document.querySelector('meta[property="og:description"]').content = description;
    document.querySelector('meta[property="og:image"]').content = (game && game.cover) || site.logo;
  };
  const renderRating = stats => {
    const panel = $('#dialog-rating'); panel.replaceChildren();
    const summary = make('div', 'rating-summary'); summary.append(make('p', 'eyebrow', '玩家评分'), make('p', 'rating-value', ratingLabel(stats.rating)));
    panel.append(summary);
    const total = Object.values(stats.vote_info).reduce((sum, value) => sum + value, 0);
    if (!total) {panel.append(make('p', 'review-note', '暂无星级分布')); return;}
    const distribution = make('div', 'rating-distribution');
    for (let star = 5; star >= 1; star--) {
      const votes = stats.vote_info[String(star)];
      const row = make('div', 'rating-row'); row.setAttribute('aria-label', `${star} 星：${votes} 条，占 ${Math.round(votes / total * 100)}%`);
      const track = make('span', 'rating-track'); const bar = make('span', 'rating-fill'); bar.style.width = `${votes / total * 100}%`; track.append(bar); track.setAttribute('aria-hidden', 'true');
      row.append(make('span', '', `${star} 星`), track, make('span', 'rating-count', count(votes))); distribution.append(row);
    }
    panel.append(distribution);
  };
  const showGame = (game, navigate = true) => {
    selected = game;
    if (!dialog.open) returnFocus = document.activeElement;
    $('#dialog-image').src = game.cover || game.icon || site.logo;
    $('#dialog-image').alt = game.title + '宣传图';
    $('#dialog-title').textContent = game.title;
    $('#dialog-description').textContent = game.description || '前往 TapTap，了解这款游戏。';
    $('#dialog-tags').textContent = game.tags.filter(tag => tag !== 'TapTap制造').join(' / ');
    $('#dialog-play').href = game.url;
    $('#dialog-forum').href = game.forum_url;
    $('#share-status').textContent = '';
    renderStats($('#dialog-stats'), game.stats, true);
    renderRating(game.stats);
    const reviews = highlights.filter(review => review.game_id === game.id);
    $('#dialog-reviews').hidden = !reviews.length;
    renderReviews($('#dialog-review-list'), reviews, false);
    updateMetadata(game);
    if (navigate) history.pushState({studioGame: true}, '', `${base}/games/${game.id}`);
    if (!dialog.open) dialog.showModal();
    dialog.scrollTop = 0;
    document.body.style.overflow = 'hidden';
  };
  const syncLocation = () => {
    const match = location.pathname.match(/\/games\/(\d+)$/);
    const game = match && site.games.find(game => game.id === Number(match[1]));
    if (game) showGame(game, false);
    else {if (dialog.open) dialog.close(); updateMetadata(null);}
  };
  const closeGame = () => {
    if (history.state && history.state.studioGame) history.back();
    else {history.replaceState({}, '', base); syncLocation();}
  };
  const openGameLink = (event, game) => {
    if (event.button === 0 && !event.ctrlKey && !event.metaKey && !event.shiftKey && !event.altKey) {event.preventDefault(); showGame(game);}
  };
  dialog.querySelector('.dialog-close').addEventListener('click', closeGame);
  dialog.addEventListener('cancel', event => {event.preventDefault(); closeGame();});
  dialog.addEventListener('click', event => {if (event.target === dialog) {const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeGame();}});
  dialog.addEventListener('close', () => {if (!dialog.open) {document.body.style.overflow = ''; selected = null; if (returnFocus && returnFocus.isConnected) returnFocus.focus({preventScroll: true});}});
  window.addEventListener('popstate', syncLocation);
  $('#share-game').addEventListener('click', async () => {
    if (!selected) return;
    const url = new URL(`${base}/games/${selected.id}`, location.origin).href;
    try {
      if (navigator.share) await navigator.share({title: selected.title, url});
      else if (navigator.clipboard && window.isSecureContext) {await navigator.clipboard.writeText(url); $('#share-status').textContent = '作品链接已复制。';}
      else {$('#share-status').textContent = url;}
    } catch (error) {if (error.name !== 'AbortError') $('#share-status').textContent = url;}
  });
  const render = (filter) => {
    const container = $('#game-list'); container.replaceChildren();
    const games = site.games.filter(g => filter === '全部作品' || g.tags.includes(filter));
    $('#empty').hidden = games.length > 0;
    $('#empty').textContent = site.games.length ? '这个分类还没有作品，试试其他分类吧。' : '作品正在准备中。过一会儿再来逛逛吧。';
    games.forEach((game, index) => {
      const article = make('article', 'game reveal');
      const artwork = make('a', 'game-art'); artwork.href = `${base}/games/${game.id}`; artwork.setAttribute('aria-label', '了解' + game.title);
      const image = make('img'); image.loading = 'lazy'; image.width = 1920; image.height = 1080; image.referrerPolicy = 'no-referrer'; image.src = game.cover || game.icon || site.logo; image.alt = game.title + '宣传图';
      artwork.append(image, make('span', 'open-mark', '↗'));
      if (game.featured) artwork.append(make('span', 'featured-label', '精选作品'));
      artwork.addEventListener('click', event => openGameLink(event, game));
      const tags = make('p', 'game-meta', `${String(index + 1).padStart(2, '0')} / ${game.tags.filter(x => x !== 'TapTap制造').slice(0, 2).join(' · ') || '独立游戏'}`);
      const heading = make('h3'); const title = make('a', '', game.title); title.href = artwork.href; title.addEventListener('click', event => openGameLink(event, game)); heading.append(title);
      const stats = make('div', 'game-stats'); renderStats(stats, game.stats);
      const bottom = make('div', 'game-bottom'); const details = make('a', 'text-link', '了解这款游戏 ↗'); details.href = artwork.href; details.addEventListener('click', event => openGameLink(event, game));
      const external = externalLink('前往 TapTap ↗', game.url, 'game-external');
      bottom.append(details, external); article.append(artwork, tags, heading, stats, make('p', 'game-description', game.description), bottom); container.append(article); observer.observe(article);
    });
  };
  const tags = ['全部作品', ...new Set(site.games.flatMap(g => g.tags).filter(t => t !== 'TapTap制造'))].slice(0, 6);
  tags.forEach((tag, index) => {const button = make('button', '', tag); button.type = 'button'; button.setAttribute('aria-pressed', String(index === 0)); button.addEventListener('click', () => {$('#filters').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button))); render(tag);}); $('#filters').append(button);});
  render('全部作品');
  $('#reviews').hidden = !highlights.length;
  renderReviews($('#review-list'), highlights);
  syncLocation();
})();
