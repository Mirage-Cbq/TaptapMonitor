(() => {
  'use strict';
  const $ = s => document.querySelector(s);
  const el = (tag, className, text) => {const x = document.createElement(tag); if (className) x.className = className; if (text !== undefined) x.textContent = text; return x;};
  const labels = {review: '评价', topic: '论坛帖子', review_comment: '评价回复', topic_comment: '论坛回复'};
  let token = sessionStorage.getItem('monitor-token') || '', developerId, developers = [], site, offset = 0, wasCollecting = false;
  const notify = text => {$('#notice').textContent = text; $('#notice').hidden = false;};
  const date = timestamp => timestamp ? new Date(timestamp * 1000).toLocaleString() : '—';
  async function api(path, method = 'GET', body) {
    const binary = body instanceof Blob;
    const response = await fetch('/api/' + path, {method, headers: {Authorization: 'Bearer ' + token, ...(body === undefined ? {} : {'Content-Type': binary ? body.type : 'application/json'})}, body: body === undefined ? undefined : binary ? body : JSON.stringify(body), cache: 'no-store'});
    if (!response.ok) {let detail = '请求失败'; try {const data = await response.json(); detail = typeof data.detail === 'string' ? data.detail : '请检查输入内容';} catch {} throw new Error(`${response.status} · ${detail}`);}
    return response.json();
  }
  const guarded = fn => async event => {if (event) event.preventDefault(); try {await fn(event);} catch (error) {if ($('#workspace').hidden) $('#login-error').textContent = error.message; else notify(error.message);}};
  async function loadStatus() {
    const status = await api('status');
    $('#status').replaceChildren();
    for (const [key, label] of [['developers','监测厂商'],['games','收录作品'],['items','反馈记录']]) {const metric = el('div','metric'); metric.append(el('strong','',status.counts[key]),el('span','',label)); $('#status').append(metric);}
    const metric = el('div','metric'); metric.append(el('strong','',status.streams.length),el('span','','待处理数据源')); $('#status').append(metric);
    $('#status').append(el('p','status-note',status.collecting ? '● 正在同步' : '● 等待下一次同步'));
    $('#scan').disabled = status.collecting;
    if (wasCollecting && !status.collecting) {await loadSite(); await loadFeedback(); notify('本轮同步已结束，可在运行记录查看结果。');}
    wasCollecting = status.collecting;
    $('#errors').replaceChildren();
    status.streams.slice(0,15).forEach(stream => $('#errors').append(el('p','error-block',`${stream.key} · ${stream.status === 'pending' ? '已保存分页进度，下轮继续采集' : stream.status === 'restricted' ? 'TapTap 要求登录才能展开部分楼中楼，已保存公开预览' : stream.error}`)));
    const runs = await api('runs?limit=8');
    const table = el('table'), thead = el('thead'), tr = el('tr'); ['运行','开始时间','结果','新收录','内容修改'].forEach(x => tr.append(el('th','',x))); thead.append(tr); table.append(thead); const tbody = el('tbody');
    runs.forEach(run => {const row = el('tr'); [run.id,date(run.started_at),({ok:'完成',partial:'部分完成',running:'进行中',interrupted:'已中断'})[run.status] || run.status,run.new_items,run.updated_items].forEach(x => row.append(el('td','',x))); tbody.append(row);}); table.append(tbody); $('#runs').replaceChildren(table);
  }
  async function loadDevelopers() {
    developers = await api('developers');
    $('#developer').replaceChildren();
    developers.forEach(d => {const option = el('option','',d.name); option.value = d.id; $('#developer').append(option);});
    if (!developers.some(d => d.id === developerId)) developerId = developers[0]?.id;
    $('#developer').value = developerId;
    if (developerId) await loadSite();
  }
  function field(form, name) {return form.elements.namedItem(name);}
  function labeled(text, element) {const label = el('label','',text); label.append(element); return label;}
  async function loadSite() {
    site = await api(`sites/${developerId}`);
    $('#studio-link').href = `/studio/${developerId}`;
    const form = $('#site-form');
    field(form,'published').checked = site.custom.published !== false;
    ['display_name','tagline','intro','about'].forEach(name => field(form,name).value = site.custom[name] || '');
    $('#logo-preview').src = site.logo;
    $('#set-primary').disabled = site.is_primary; $('#set-primary').textContent = site.is_primary ? '当前默认官网' : '设为默认官网';
    const developer = developers.find(d => d.id === developerId); field($('#subscription-form'),'enabled').checked = !!developer.enabled; field($('#subscription-form'),'target_umo').value = developer.target_umo;
    $('#games').replaceChildren();
    site.games.forEach(game => {
      const details = el('details','game-editor'), summary = el('summary'), icon = el('img'); icon.src = game.icon; icon.alt = ''; const name = el('div'); name.append(el('strong','',game.title),el('span','muted',`ID ${game.id} · ${game.visible ? '展示中' : '已隐藏'}${game.featured ? ' · 精选' : ''}`)); summary.append(icon,name); details.append(summary);
      const editor = el('form','game-form'), title = el('input'); title.maxLength = 200; title.value = game.overrides.title || ''; title.placeholder = game.source.title;
      const description = el('textarea'); description.rows = 4; description.maxLength = 10000; description.value = game.overrides.description || ''; description.placeholder = '留空，持续采用 TapTap 介绍';
      const visible = el('input'); visible.type = 'checkbox'; visible.checked = game.visible; const featured = el('input'); featured.type = 'checkbox'; featured.checked = game.featured; const sort = el('input'); sort.type = 'number'; sort.min = -100000; sort.max = 100000; sort.value = game.sort_order;
      const toggles = el('div','toggles'), visibleLabel = labeled('在官网展示',visible), featuredLabel = labeled('精选置顶',featured); visibleLabel.className = featuredLabel.className = 'check'; toggles.append(visibleLabel,featuredLabel,labeled('排序值',sort));
      const actions = el('div','actions'), save = el('button','primary','保存展示设置'), reset = el('button','','恢复自动文案'); reset.type = 'button'; actions.append(save,reset);
      editor.append(labeled('自定义标题',title),labeled('自定义介绍',description),el('p','muted','当前 TapTap 介绍'),el('p','source-description',game.source.description),toggles,actions); details.append(editor); $('#games').append(details);
      const persist = async resetText => {await api(`sites/${developerId}/games/${game.id}`,'PATCH',{title:resetText ? '' : title.value,description:resetText ? '' : description.value,visible:visible.checked,featured:featured.checked,sort_order:Number(sort.value)}); await loadSite(); notify('作品设置已保存，官网已更新。');};
      editor.addEventListener('submit',guarded(() => persist(false))); reset.addEventListener('click',guarded(() => persist(true)));
    });
    if (!site.games.length) $('#games').append(el('p','empty','还没有同步到作品，点击“立即同步”开始获取。'));
  }
  async function loadFeedback() {
    const kind = $('#kind').value; const items = await api(`items?developer_id=${developerId}&limit=20&offset=${offset}${kind ? '&kind='+kind : ''}`);
    $('#feedback').replaceChildren();
    items.forEach(item => {const article = el('article','feedback-item'), meta = el('div','meta'), game = site.games.find(g => g.id === item.app_id); [game?.title || item.app_id,labels[item.kind],item.author,item.score === null ? '' : `${item.score} 星`,date(item.published_at)].filter(Boolean).forEach(x => meta.append(el('span','',x))); article.append(meta); if (item.title) article.append(el('h3','',item.title)); article.append(el('p','body',item.body)); const link = el('a','','查看原文 ↗'); link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; article.append(link); $('#feedback').append(article);});
    if (!items.length) $('#feedback').append(el('p','empty','当前筛选下暂无反馈记录。'));
    $('#previous').disabled = offset === 0; $('#next').disabled = items.length < 20; $('#page-label').textContent = `第 ${offset/20+1} 页`;
  }
  async function connect() {await loadDevelopers(); sessionStorage.setItem('monitor-token',token); $('#login').hidden = true; $('#workspace').hidden = false; $('#logout').hidden = false; $('#login-error').textContent = ''; await loadStatus(); if (developerId) await loadFeedback();}
  $('#login').addEventListener('submit',guarded(async () => {token = $('#token').value.trim(); await connect();}));
  $('#logout').addEventListener('click',() => {sessionStorage.removeItem('monitor-token'); location.reload();});
  $('#developer').addEventListener('change',guarded(async () => {developerId = Number($('#developer').value); offset = 0; await loadSite(); await loadFeedback();}));
  $('#add-developer').addEventListener('submit',guarded(async () => {const input = $('#developer-url').value.trim(); let id; if (/^\d+$/.test(input)) id = Number(input); else {const url = new URL(input); if (url.hostname !== 'www.taptap.cn' && url.hostname !== 'taptap.cn') throw new Error('请输入 TapTap 厂商主页链接'); id = Number(url.pathname.match(/^\/developer\/(\d+)\/?$/)?.[1]);} if (!Number.isSafeInteger(id) || id < 1) throw new Error('无法识别厂商 ID'); await api('developers','POST',{id}); developerId = id; $('#developer-url').value = ''; await loadDevelopers(); await loadStatus(); notify('厂商已添加并安排同步，作品和官网内容将自动生成。');}));
  $('#scan').addEventListener('click',guarded(async () => {const result = await api(`scan?developer_id=${developerId}`,'POST'); notify(`同步任务 ${result.run_id} 已开始。`); await loadStatus();}));
  $('#site-form').addEventListener('submit',guarded(async () => {const form = $('#site-form'), body = {}; ['display_name','tagline','intro','about'].forEach(name => body[name] = field(form,name).value); body.published = field(form,'published').checked; await api(`sites/${developerId}`,'PATCH',body); notify('官网设置已保存。');}));
  $('#upload-logo').addEventListener('click',guarded(async () => {const image = $('#studio-logo').files[0]; if (!image) throw new Error('请先选择图标文件'); if (image.size > 8*1024*1024) throw new Error('图标不能超过 8 MB'); await api(`sites/${developerId}/logo`,'PUT',image); await loadSite(); $('#studio-logo').value = ''; notify('厂商图标已更新。');}));
  $('#reset-logo').addEventListener('click',guarded(async () => {await api(`sites/${developerId}/logo`,'DELETE'); await loadSite(); notify('已恢复采用 TapTap 厂商图标。');}));
  $('#set-primary').addEventListener('click',guarded(async () => {await api(`site-default/${developerId}`,'PUT'); await loadSite(); notify('默认官网已更新。');}));
  $('#subscription-form').addEventListener('submit',guarded(async () => {const form = $('#subscription-form'); await api(`developers/${developerId}`,'PATCH',{enabled:field(form,'enabled').checked,target_umo:field(form,'target_umo').value.trim()}); await loadDevelopers(); notify('订阅设置已保存。');}));
  $('#kind').addEventListener('change',guarded(async () => {offset = 0; await loadFeedback();}));
  $('#previous').addEventListener('click',guarded(async () => {offset = Math.max(0,offset-20); await loadFeedback();}));
  $('#next').addEventListener('click',guarded(async () => {offset += 20; await loadFeedback();}));
  $('#refresh').addEventListener('click',guarded(loadStatus));
  $('#export').addEventListener('click',guarded(async () => {let cursor = 0, events = [], hasMore; do {const batch = await api(`digest-input?developer_id=${developerId}&after=${cursor}&limit=500&include_baseline=true`); events.push(...batch.events); cursor = batch.next_cursor; hasMore = batch.has_more;} while (hasMore); const url = URL.createObjectURL(new Blob([JSON.stringify({developer_id:developerId,events},null,2)],{type:'application/json'})); const a = el('a'); a.href = url; a.download = `taptap-feedback-${developerId}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);}));
  setInterval(() => {if (!$('#workspace').hidden) loadStatus().catch(error => notify(error.message));},8000);
  if (token) connect().catch(error => {sessionStorage.removeItem('monitor-token'); $('#login-error').textContent = error.message;});
})();
