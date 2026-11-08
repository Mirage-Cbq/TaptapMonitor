(() => {
  'use strict';
  const site = JSON.parse(document.querySelector('#site-data').textContent);
  const $ = (selector) => document.querySelector(selector);
  const make = (tag, className, text) => {const element = document.createElement(tag); if (className) element.className = className; if (text !== undefined) element.textContent = text; return element;};
  const base = `/studio/${site.developer_id}`;
  document.documentElement.classList.add('js');
  document.querySelectorAll('[data-studio-name]').forEach(e => e.textContent = site.name);
  document.querySelectorAll('[data-taptap]').forEach(e => e.href = site.taptap_url);
  $('#tagline').textContent = site.tagline;
  $('#studio-intro').textContent = site.intro;
  $('#about-copy').textContent = site.about;
  $('#year').textContent = new Date().getFullYear();
  $('#game-count').textContent = String(site.games.length).padStart(2, '0');
  document.querySelectorAll('[data-studio-logo]').forEach(image => image.src = site.logo);
  const observer = new IntersectionObserver(entries => entries.forEach(entry => {if (entry.isIntersecting) {entry.target.classList.add('seen'); observer.unobserve(entry.target);}}), {threshold: .08});
  document.querySelectorAll('.reveal').forEach(e => observer.observe(e));
  let selected = null;
  const dialog = $('#game-dialog');
  const showGame = (game, navigate = true) => {
    selected = game;
    $('#dialog-image').src = game.cover;
    $('#dialog-image').alt = game.title + '宣传图';
    $('#dialog-title').textContent = game.title;
    $('#dialog-description').textContent = game.description || '前往 TapTap，了解这款游戏。';
    $('#dialog-tags').textContent = game.tags.join(' / ');
    $('#dialog-play').href = game.url;
    $('#dialog-forum').href = game.forum_url;
    $('#share-status').textContent = '';
    if (!dialog.open) dialog.showModal();
    document.body.style.overflow = 'hidden';
    if (navigate) history.pushState({game: game.id}, '', `${base}/games/${game.id}`);
  };
  const closeGame = () => {if (dialog.open) dialog.close();};
  dialog.querySelector('.dialog-close').addEventListener('click', closeGame);
  dialog.addEventListener('click', event => {if (event.target === dialog) {const r = dialog.getBoundingClientRect(); if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) closeGame();}});
  dialog.addEventListener('close', () => {document.body.style.overflow = ''; if (/\/games\/\d+$/.test(location.pathname)) history.replaceState({}, '', base);});
  window.addEventListener('popstate', () => {const match = location.pathname.match(/\/games\/(\d+)$/); const game = match && site.games.find(g => g.id === Number(match[1])); if (game) showGame(game, false); else closeGame();});
  $('#share-game').addEventListener('click', async () => {
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
    games.forEach((game, index) => {
      const article = make('article', 'game reveal');
      const artwork = make('a', 'game-art'); artwork.href = `${base}/games/${game.id}`; artwork.setAttribute('aria-label', '了解' + game.title);
      const image = make('img'); image.src = game.cover; image.alt = game.title + '宣传图'; image.loading = 'lazy'; image.width = 1920; image.height = 1080;
      artwork.append(image, make('span', 'open-mark', '↗'));
      if (game.featured) artwork.append(make('span', 'featured-label', '精选作品'));
      artwork.addEventListener('click', event => {if (!event.ctrlKey && !event.metaKey && !event.shiftKey) {event.preventDefault(); showGame(game);}});
      const tags = make('p', 'game-meta', `${String(index + 1).padStart(2, '0')} / ${game.tags.filter(x => x !== 'TapTap制造').slice(0, 2).join(' · ') || '独立游戏'}`);
      const heading = make('h3'); const title = make('a', '', game.title); title.href = artwork.href; title.addEventListener('click', e => {e.preventDefault(); showGame(game);}); heading.append(title);
      const bottom = make('div', 'game-bottom'); const details = make('a', 'text-link', '了解这款游戏 ↗'); details.href = artwork.href; details.addEventListener('click', e => {e.preventDefault(); showGame(game);});
      const external = make('a', '', '前往 TapTap ↗'); external.href = game.url; external.target = '_blank'; external.rel = 'noopener noreferrer';
      bottom.append(details, external); article.append(artwork, tags, heading, make('p', 'game-description', game.description), bottom); container.append(article); observer.observe(article);
    });
  };
  const tags = ['全部作品', ...new Set(site.games.flatMap(g => g.tags).filter(t => t !== 'TapTap制造'))].slice(0, 6);
  tags.forEach((tag, index) => {const button = make('button', '', tag); button.type = 'button'; button.setAttribute('aria-pressed', String(index === 0)); button.addEventListener('click', () => {$('#filters').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b === button))); render(tag);}); $('#filters').append(button);});
  render('全部作品');
  const initial = site.games.find(g => g.id === site.selected_game); if (initial) showGame(initial, false);
})();
