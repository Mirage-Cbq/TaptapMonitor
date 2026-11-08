(() => {
  'use strict';
  const $ = selector => document.querySelector(selector);
  const el = (tag, className, text) => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  };
  const labels = {review:'评价', topic:'论坛帖子', review_comment:'评价回复', topic_comment:'论坛回复'};
  const statuses = {ok:'完成', partial:'部分完成', running:'进行中', interrupted:'已中断', pending:'待重试', error:'失败', sent:'已发送', sending:'发送中', failed:'失败', restricted:'访问受限'};
  const views = {overview:'概览', data:'反馈数据', developers:'厂商管理', runs:'运行与日志', settings:'系统设置'};
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '—';
  const number = value => Number(value || 0).toLocaleString('zh-CN');
  const average = value => value === null || value === undefined ? '—' : value.toFixed(2);
  const field = (form, key) => form.elements.namedItem(key);
  let token = sessionStorage.getItem('monitor-token') || '';
  let developers = [], games = [], developerId = null, site = null, view = 'overview';
  let offset = 0, total = 0, filterParams = new URLSearchParams(), status = null;
  let statsTicket = 0, searchTicket = 0, siteTicket = 0, polling = false, runtime = null, overviewData = null;
  const resets = new Set();
  const notify = message => {$('#notice').textContent = message; $('#notice').hidden = false;};
  const translations = {
    'Valid Bearer token required':'管理令牌无效，请重新登录', 'Developer already subscribed':'该厂商已在监测列表中',
    'A collection run is already active':'已有同步任务正在运行，请稍后再试', 'Developer not found':'未找到厂商',
    'Game not found for developer':'未找到该厂商的作品', 'Invalid logo image':'图标不是有效图片',
    'Logo must be smaller than 8 MB':'图标不能超过 8 MB', 'Use PNG, JPEG or WebP, up to 4096 pixels per side':'请使用 PNG、JPEG 或 WebP，每边不超过 4096 像素'
  };
  async function api(path, method='GET', body) {
    const binary = body instanceof Blob;
    let response;
    try {
      response = await fetch('/api/' + path, {method, headers:{Authorization:'Bearer '+token,
        ...(body === undefined ? {} : {'Content-Type':binary ? body.type : 'application/json'})},
        body:body === undefined ? undefined : binary ? body : JSON.stringify(body), cache:'no-store'});
    } catch {throw new Error('无法连接服务器，请检查 SSH 隧道与服务状态。');}
    if (!response.ok) {
      let data = {}; try {data = await response.json();} catch {}
      const detail = data.detail;
      const error = new Error(Array.isArray(detail) ? '输入内容有误，请检查标注字段。' : translations[detail] || (typeof detail === 'string' && /[\u4e00-\u9fff]/.test(detail) ? detail : '请求失败，请检查服务状态。'));
      error.details = Array.isArray(detail) ? detail : []; error.status = response.status;
      if (response.status === 401) {sessionStorage.removeItem('monitor-token'); $('#workspace').hidden = true; $('#login').hidden = false; $('#login-error').textContent = error.message;}
      throw error;
    }
    return response.json();
  }
  const guarded = fn => async event => {
    if (event) event.preventDefault();
    try {await fn(event);} catch (error) {
      if ($('#workspace').hidden) $('#login-error').textContent = error.message;
      else notify(error.message);
    }
  };
  function button(text, action, className='') {
    const node = el('button', className, text); node.type = 'button';
    node.addEventListener('click', guarded(async event => {
      node.disabled = true; try {await action(event);} finally {if (node.isConnected) node.disabled = false;}
    })); return node;
  }
  function safeUrl(value, image=false) {
    try {const url = new URL(value, location.origin);
      if (url.origin === location.origin && url.protocol === location.protocol) return url.href;
      if (url.protocol === 'https:' && (image ? /(^|\.)tapimg\.com$/.test(url.hostname) : /(^|\.)taptap\.cn$/.test(url.hostname))) return url.href;
    } catch {} return '';
  }
  function link(text, url) {
    const a=el('a','',text); a.href=safeUrl(url) || '#'; a.target='_blank'; a.rel='noopener noreferrer'; return a;
  }
  function image(url, alt, size=48) {
    const node=el('img'); node.src=safeUrl(url,true) || '/favicon.svg'; node.alt=alt;
    node.width=node.height=size; node.referrerPolicy='no-referrer'; node.loading='lazy';
    node.addEventListener('error', () => {if (!node.src.endsWith('/favicon.svg')) node.src='/favicon.svg';}, {once:true}); return node;
  }
  function labeled(text, node) {const label=el('label','',text); label.append(node); return label;}
  function chip(value) {return el('span','chip '+value,statuses[value] || '未知状态');}
  function table(headers) {
    const node=el('table'), head=el('thead'), row=el('tr'), body=el('tbody');
    headers.forEach(text => {const th=el('th','',text); th.scope='col'; row.append(th);}); head.append(row); node.append(head,body); return {node,body};
  }
  function cell(row, value, className='') {const td=el('td',className); if (value instanceof Node) td.append(value); else td.textContent=value ?? '—'; row.append(td); return td;}
  function options(select, rows, placeholder, key='name') {
    const previous=select.value; select.replaceChildren(); const first=el('option','',placeholder); first.value=''; select.append(first);
    rows.forEach(row => {const option=el('option','',row[key]); option.value=row.id; select.append(option);});
    if ([...select.options].some(option => option.value === previous)) select.value=previous;
  }
  async function loadCatalog() {
    [developers,games] = await Promise.all([api('developers'),api('games')]);
    options($('#overview-developer'),developers,'全部厂商'); options($('#filter-developer'),developers,'全部厂商');
  }
  async function gameOptions(developerSelect, gameSelect) {
    const rows=developerSelect.value ? await api('games?developer_id='+developerSelect.value) : games;
    options(gameSelect,rows,'全部作品','title');
  }
  async function loadStatus() {
    const previous=status?.collecting;
    status=await api('status');
    const indicator=$('#collecting'); indicator.replaceChildren(el('i','status-dot'),document.createTextNode(status.collecting ? '正在同步采集…' : '等待下一次同步'));
    indicator.classList.toggle('busy',status.collecting); $('#scan').disabled=status.collecting;
    if (previous && !status.collecting) {
      await loadCatalog(); notify('本轮同步已结束，数据已更新。');
      if (view === 'overview') await loadOverview();
      if (view === 'data') {await gameOptions($('#filter-developer'),$('#filter-game')); await loadFeedback();}
      if (view === 'developers') {renderDevelopers(); if (developerId) await loadSite();}
    }
    return status;
  }
  async function scan(id=null) {
    $('#scan').disabled=true;
    try {const result=await api('scan'+(id ? '?developer_id='+id : ''),'POST'); notify(`同步任务 ${result.run_id} 已开始。`); await loadStatus();}
    finally {$('#scan').disabled=!!status?.collecting;}
  }
  async function loadOverview() {
    const ticket=++statsTicket, params=new URLSearchParams({days:$('#range').value});
    if ($('#overview-developer').value) params.set('developer_id',$('#overview-developer').value);
    if ($('#overview-game').value) params.set('app_id',$('#overview-game').value);
    const data=await api('stats?'+params); if (ticket !== statsTicket) return;
    overviewData=data;
    const metrics=[['监测厂商',number(data.totals.developers)],['收录作品',number(data.totals.games)],['反馈总数',number(data.totals.items)],['近 7 天新增',number(data.totals.recent_7_days)],['平均评分',average(data.totals.average_score)]];
    $('#kpis').replaceChildren();
    metrics.forEach(([label,value]) => {const tile=el('div','kpi'); tile.append(el('span','',label),el('strong','',value)); $('#kpis').append(tile);});
    const last=data.runs.items[0], tile=el('div','kpi'); tile.append(el('span','','最近一次同步'),el('strong','sync-value',last ? statuses[last.status] || '未知状态' : '尚未同步'),el('small','',last ? date(last.started_at) : '等待首次运行')); $('#kpis').append(tile);
    AdminCharts.trend($('#trend'),data.daily); AdminCharts.scores($('#scores'),data.score_distribution);
    if (!data.games.length) AdminCharts.empty($('#game-comparison'),'暂无作品，添加厂商并同步后可查看。');
    else {
      const t=table(['作品 / 反馈量','评价','均分']);
      const max=Math.max(1,...data.games.map(g => Object.values(g.counts).reduce((a,b)=>a+b,0)));
      data.games.forEach(game => {
        const row=el('tr'), count=Object.values(game.counts).reduce((a,b)=>a+b,0);
        const name=el('div'); name.append(el('span','',game.title),el('span','muted small',' · '+number(count)+' 条'));
        const bar=el('div','comparison-bar'); bar.style.width=(count/max*100)+'%';
        name.append(bar); const td=cell(row,name); td.title=Object.entries(game.counts).map(([key,n])=>`${labels[key]} ${n}`).join('，');
        cell(row,number(game.review_count)); cell(row,average(game.average_score),'nowrap'); t.body.append(row);
      }); $('#game-comparison').replaceChildren(t.node);
    }
    const rate=data.runs.success_rate;
    $('#success-rate').textContent=`最近 ${data.runs.items.length} 次运行 · 已结束运行成功率 ${rate === null ? '—' : (rate*100).toFixed(0)+'%'} · 厂商筛选包含全量同步运行，作品筛选不改变运行范围。`;
    $('#run-strip').replaceChildren();
    if (!data.runs.items.length) AdminCharts.empty($('#run-strip'),'尚无运行记录。');
    data.runs.items.slice().reverse().forEach(run => {
      const text=`运行 ${run.id} · ${date(run.started_at)} · ${statuses[run.status] || '未知'} · 新增 ${run.new_items} · 修改 ${run.updated_items} · 异常 ${run.error_count}`;
      const node=button('',async()=>{location.hash='#/runs';},run.status); node.title=text; node.setAttribute('aria-label',text); $('#run-strip').append(node);
    });
  }
  function readFilters() {
    const params=new URLSearchParams(); const form=$('#filters');
    for (const key of ['developer_id','app_id','kind','score','q','sort']) {const value=field(form,key).value.trim(); if (value) params.set(key,value);}
    for (const key of ['since','until']) {
      const value=field(form,key).value;
      if (value) {const time=new Date(value+'T00:00:00'); if (key === 'until') time.setDate(time.getDate()+1); params.set(key,Math.floor(time.getTime()/1000)-(key === 'until' ? 1 : 0));}
    }
    if (params.has('since') && params.has('until') && +params.get('since') > +params.get('until')) throw new Error('开始日期不能晚于结束日期。');
    if (field(form,'official').checked) params.set('official','true');
    return params;
  }
  async function applyFilters() {filterParams=readFilters(); offset=0; await loadFeedback();}
  async function loadFeedback() {
    const ticket=++searchTicket, params=new URLSearchParams(filterParams); params.set('limit','20'); params.set('offset',offset);
    const data=await api('items/search?'+params); if (ticket !== searchTicket) return;
    total=data.total; $('#feedback-total').textContent=`共 ${number(total)} 条反馈`;
    if (!data.items.length) AdminCharts.empty($('#feedback'),'当前筛选下暂无反馈记录。');
    else {
      const t=table(['作品 / 类型','反馈内容','作者','评分','发布时间']);
      data.items.forEach(item => {
        const row=el('tr'), info=el('div'); info.append(el('div','',item.game_title || `作品 ${item.app_id}`),el('span','chip',labels[item.kind]));
        if (item.official) info.append(el('span','chip ok','官方'));
        cell(row,info); const preview=el('div'), excerpt=el('p','excerpt',item.body || '（无文字内容）');
        if (item.title) preview.append(button(item.title,async()=>showItem(item),'table-button'),excerpt);
        else preview.append(excerpt,button('详情 ›',async()=>showItem(item),'table-button'));
        cell(row,preview);
        cell(row,item.author || '匿名'); cell(row,item.score == null ? '—' : item.score+' 星','nowrap'); cell(row,date(item.published_at),'nowrap');
        row.addEventListener('click',event=>{if (!event.target.closest('button,a')) showItem(item);}); t.body.append(row);
      }); $('#feedback').replaceChildren(t.node);
    }
    $('#previous').disabled=offset===0; $('#next').disabled=offset+20>=total;
    $('#page-label').textContent=total ? `第 ${offset/20+1} / ${Math.ceil(total/20)} 页` : '第 0 / 0 页';
  }
  function showItem(item) {
    const body=$('#item-detail'); body.replaceChildren(); $('#item-title').textContent=item.title || labels[item.kind]+'详情';
    body.append(el('p','muted small',`${item.game_title || item.app_id} · ${labels[item.kind]} · ${item.author || '匿名'}${item.official ? ' · 官方' : ''}${item.score == null ? '' : ' · '+item.score+' 星'} · ${date(item.published_at)}`),el('p','body',item.body || '（无文字内容）'));
    const media=el('div','media-grid');
    (item.media || []).forEach(url=>{if (!safeUrl(url,true)) return; const a=link('',''); a.href=safeUrl(url,true); a.append(image(url,'反馈附件，点击查看原图',130)); media.append(a);});
    body.append(media); if (safeUrl(item.url)) body.append(link('在 TapTap 查看原文 ↗',item.url));
    $('#item-dialog').showModal();
  }
  function download(content, name, type) {
    const url=URL.createObjectURL(new Blob([content],{type})), a=el('a'); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function exportCsv() {
    const params=new URLSearchParams(filterParams); params.set('limit','500'); let start=0, rows=[], count;
    do {params.set('offset',start); const batch=await api('items/search?'+params); rows.push(...batch.items); count=batch.total; start+=batch.items.length; if (!batch.items.length) break;} while (start<count);
    const quote=value=>{let text=String(value ?? ''); if (/^[\s]*[=+\-@\t\r]/.test(text)) text="'"+text; return '"'+text.replaceAll('"','""')+'"';};
    const records=[['作品','类型','标题','正文','作者','评分','官方','发布时间','原文链接'],...rows.map(item=>[item.game_title,labels[item.kind],item.title,item.body,item.author,item.score,item.official?'是':'否',date(item.published_at),item.url])];
    download('\ufeff'+records.map(row=>row.map(quote).join(',')).join('\r\n'),'反馈筛选结果.csv','text/csv;charset=utf-8'); notify(`已导出 ${rows.length} 条反馈。`);
  }
  async function exportAi() {
    const id=filterParams.get('developer_id') || developerId;
    if (!id) throw new Error('请先在筛选栏选择厂商并查询，再导出该厂商的 AI 汇总数据。');
    let cursor=0, events=[], more;
    do {const batch=await api(`digest-input?developer_id=${id}&after=${cursor}&limit=500&include_baseline=true`); events.push(...batch.events); cursor=batch.next_cursor; more=batch.has_more;} while (more);
    download(JSON.stringify({developer_id:Number(id),events},null,2),`厂商-${id}-AI汇总.json`,'application/json;charset=utf-8'); notify(`已导出该厂商全部历史反馈事件 ${events.length} 条（含修改与基线记录）。`);
  }
  function renderDevelopers() {
    $('#developers').replaceChildren();
    if (!developers.length) {AdminCharts.empty($('#developers'),'尚未添加厂商。'); $('#developer-detail').hidden=true; return;}
    developers.forEach(developer=>{
      const card=el('article','developer-card'+(developer.id===developerId ? ' selected':'')), identity=el('div','identity'), title=el('div');
      title.append(el('h3','',developer.name),el('small','muted','厂商 ID · '+developer.id)); identity.append(image(developer.logo,developer.name+'图标'),title);
      const counts=el('div','counts muted small'); [['game_count','作品'],['feedback_count','反馈']].forEach(([key,label])=>{const span=el('span'); span.append(el('strong','',number(developer[key])),document.createTextNode(label)); counts.append(span);});
      const enabled=el('input'); enabled.type='checkbox'; enabled.checked=!!developer.enabled;
      const toggle=labeled('启用监测',enabled); toggle.className='check';
      enabled.addEventListener('change',guarded(async()=>{
        enabled.disabled=true;
        try {await api('developers/'+developer.id,'PATCH',{enabled:enabled.checked}); await loadCatalog(); renderDevelopers(); if (site?.developer_id===developer.id) field($('#subscription-form'),'enabled').checked=enabled.checked;}
        catch(error) {enabled.checked=!enabled.checked; throw error;} finally {enabled.disabled=false;}
      }));
      const actions=el('div','actions'); actions.append(button('管理与重命名',async()=>{developerId=developer.id; renderDevelopers(); await loadSite(); $('#developer-detail').scrollIntoView({behavior:matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth',block:'start'});}),button('立即同步该厂商',()=>scan(developer.id)),link('官网 ↗','/studio/'+developer.id));
      card.append(identity,counts,toggle,actions); $('#developers').append(card);
    });
  }
  async function loadSite() {
    const ticket=++siteTicket, id=developerId, data=await api('sites/'+id);
    if (ticket!==siteTicket || id!==developerId) return; site=data;
    $('#developer-detail').hidden=false; $('#developer-title').textContent=developers.find(d=>d.id===id)?.name || site.name;
    $('#studio-link').href='/studio/'+id;
    const form=$('#site-form'); ['display_name','tagline','intro','about'].forEach(key=>field(form,key).value=site.custom[key] || '');
    field(form,'published').checked=site.custom.published!==false; $('#logo-preview').src=safeUrl(site.logo,true) || '/favicon.svg';
    $('#set-primary').disabled=site.is_primary; $('#set-primary').textContent=site.is_primary?'当前默认官网':'设为默认官网';
    const developer=developers.find(d=>d.id===id);
    field($('#rename-form'),'name').value=developer.name; field($('#subscription-form'),'enabled').checked=!!developer.enabled; field($('#subscription-form'),'target_umo').value=developer.target_umo;
    $('#games').replaceChildren();
    site.games.forEach(game=>{
      const details=el('details'), summary=el('summary','',game.title+(game.visible?'':' · 已隐藏')+(game.featured?' · 精选':'')), editor=el('form','game-editor');
      const title=el('input'); title.maxLength=200; title.value=game.overrides.title || ''; title.placeholder=game.source.title;
      const description=el('textarea'); description.rows=4; description.maxLength=10000; description.value=game.overrides.description || '';
      const visible=el('input'); visible.type='checkbox'; visible.checked=game.visible;
      const featured=el('input'); featured.type='checkbox'; featured.checked=game.featured;
      const sort=el('input'); sort.type='number'; sort.min=-100000; sort.max=100000; sort.value=game.sort_order;
      const toggles=el('div','toggles'), vl=labeled('在官网展示',visible), fl=labeled('精选置顶',featured); vl.className=fl.className='check'; toggles.append(vl,fl,labeled('排序值（越小越靠前）',sort));
      const actions=el('div','actions'), save=el('button','primary','保存展示设置');
      const persist=async reset=>{await api(`sites/${id}/games/${game.id}`,'PATCH',{title:reset?'':title.value,description:reset?'':description.value,visible:visible.checked,featured:featured.checked,sort_order:Number(sort.value)}); if (reset) title.value=description.value=''; summary.textContent=(title.value || game.source.title)+(visible.checked?'':' · 已隐藏')+(featured.checked?' · 精选':''); notify('作品展示设置已保存。');};
      actions.append(save,button('恢复自动文案',()=>persist(true)));
      editor.append(labeled('自定义标题',title),labeled('自定义介绍',description),el('p','muted small','当前 TapTap 介绍'),el('p','source-description',game.source.description),toggles,actions);
      editor.addEventListener('submit',guarded(async()=>{save.disabled=true; try {await persist(false);} finally {save.disabled=false;}}));
      details.append(summary,editor); $('#games').append(details);
    });
    if (!site.games.length) AdminCharts.empty($('#games'),'还没有同步到作品，点击“立即同步该厂商”开始获取。');
  }
  function runErrors(run) {try {const errors=JSON.parse(run.errors); return Array.isArray(errors)?errors:[];} catch {return ['异常记录格式无法读取'];}}
  async function loadRuns(refreshStatus=true) {
    const [runs,deliveries] = await Promise.all([api('runs?limit=100'),api('deliveries?limit=20'),...(refreshStatus?[loadStatus()]:[])]);
    if (!runs.length) AdminCharts.empty($('#runs'),'尚无运行记录。');
    else {
      const t=table(['运行 / 范围','开始时间','耗时','状态','新增 / 修改','异常详情']);
      runs.forEach(run=>{
        const row=el('tr'); cell(row,`#${run.id} · ${run.developer_id ? developers.find(d=>d.id===run.developer_id)?.name || '厂商 '+run.developer_id : '全部厂商'}`);
        cell(row,date(run.started_at),'nowrap'); cell(row,`${Math.max(0,Math.round((run.finished_at || Date.now()/1000)-run.started_at))} 秒`,'nowrap'); cell(row,chip(run.status)); cell(row,`${run.new_items} / ${run.updated_items}`,'nowrap');
        const errors=runErrors(run);
        if (errors.length) {const details=el('details'), list=el('ul','run-errors'); errors.forEach(error=>list.append(el('li','',typeof error==='string'?error:JSON.stringify(error)))); details.append(el('summary','',errors.length+' 项异常'),list); cell(row,details);}
        else cell(row,'—'); t.body.append(row);
      }); $('#runs').replaceChildren(t.node);
    }
    $('#streams').replaceChildren();
    if (!status?.streams.length) AdminCharts.empty($('#streams'),'暂无待处理或异常数据源。');
    (status?.streams || []).forEach(stream=>{
      const node=el('article','stream'); node.append(chip(stream.status),el('code','', ' '+stream.key));
      const wording=stream.status==='pending'?'已保存分页进度，下轮继续采集':stream.status==='restricted'?'TapTap 要求登录才能展开部分楼中楼，已保存公开预览':stream.error || '数据源异常';
      node.append(el('p','muted small',wording),el('p','muted small','最近尝试：'+date(stream.last_attempt))); $('#streams').append(node);
    });
    $('#deliveries').replaceChildren();
    if (!deliveries.length) AdminCharts.empty($('#deliveries'),'暂无推送记录。');
    deliveries.forEach(delivery=>{const node=el('article','stream'); node.append(chip(delivery.status),el('span','',` 厂商 ${delivery.developer_id} · 尝试 ${delivery.attempts} 次 · ${date(delivery.created_at)}`)); if (delivery.error) node.append(el('p','error',delivery.error)); $('#deliveries').append(node);});
  }
  const settingDefinitions=[
    ['poll_seconds','采集周期','秒；至少 60 秒。',60,null,1],
    ['request_interval','请求间隔','秒；0–60 秒，控制 TapTap 请求频率。',0,60,'any'],
    ['request_timeout','请求超时','秒；大于 0，最多 120 秒。',0,120,'any'],
    ['max_pages','单次分页上限','页；1–1000 页，达到上限后保存进度。',1,1000,1],
    ['collect_comments','采集评论回复','同时采集评价回复与论坛回复。'],
    ['cache_images','缓存作品图片','将作品图片缓存到本机。'],
    ['digest_enabled','启用简报推送','需配置 AstrBot 接入信息与厂商接收会话。'],
    ['digest_interval','简报间隔','秒；至少 60 秒。',60,null,1],
    ['digest_batch_size','简报批量大小','条；每批 1–500 条反馈事件。',1,500,1]
  ];
  function settingMeta(key) {
    const node=$('#meta-'+key), show=value=>typeof value==='boolean'?(value?'开启':'关闭'):value;
    node.textContent=`配置默认：${show(runtime.defaults[key])} · ${resets.has(key)?'待恢复默认':Object.hasOwn(runtime.overrides,key)?'已覆盖':'使用默认'}`;
  }
  async function loadSettings() {
    runtime=await api('settings'); resets.clear(); $('#runtime-fields').replaceChildren(); $('#settings-message').textContent='';
    settingDefinitions.forEach(([key,label,help,min,max,step])=>{
      const row=el('div','setting-row'), input=el('input'); input.id='setting-'+key; input.name=key;
      if (min === undefined) {input.type='checkbox'; input.checked=runtime.values[key];}
      else {input.type='number'; input.min=min; if (max!==null) input.max=max; input.step=step; input.required=true; input.value=runtime.values[key];}
      const name=el('label','',label); name.htmlFor=input.id; name.append(el('small','',help));
      const meta=el('span','setting-meta'); meta.id='meta-'+key;
      const error=el('p','field-error'); error.id='error-'+key; error.hidden=true; input.setAttribute('aria-describedby',meta.id+' '+error.id);
      input.addEventListener('input',()=>{resets.delete(key); error.hidden=true; input.removeAttribute('aria-invalid'); settingMeta(key);});
      const reset=button('恢复默认',async()=>{resets.add(key); if (min===undefined) input.checked=runtime.defaults[key]; else input.value=runtime.defaults[key]; input.removeAttribute('aria-invalid'); error.hidden=true; settingMeta(key); $('#settings-message').textContent='默认值已填入，保存后生效。';});
      row.append(name,input,meta,reset,error); $('#runtime-fields').append(row); settingMeta(key);
    });
    $('#system-info').replaceChildren();
    [['应用版本',runtime.info.version],['数据库路径',runtime.info.database],['AstrBot 接入',runtime.info.astrbot_configured?'已配置':'未配置']].forEach(([key,value])=>$('#system-info').append(el('dt','',key),el('dd','',value)));
  }
  async function saveSettings() {
    const body={}; let invalid=null;
    settingDefinitions.forEach(([key,,,min])=>{
      const input=$('#setting-'+key), value=input.type==='checkbox'?input.checked:Number(input.value), error=$('#error-'+key);
      error.hidden=true; input.removeAttribute('aria-invalid');
      if (!input.validity.valid || (key==='request_timeout' && value<=0)) {
        error.textContent='请输入符合字段说明的'+(min===undefined?'内容。':'数值。'); error.hidden=false; input.setAttribute('aria-invalid','true'); invalid ||= input;
      }
      if (resets.has(key)) body[key]=null; else if (value!==runtime.values[key]) body[key]=value;
    });
    if (invalid) {invalid.focus(); throw new Error('请检查标注字段后保存。');}
    try {await api('settings','PATCH',body); await loadSettings(); $('#notice').hidden=true; $('#settings-message').textContent='已保存，设置已生效。';}
    catch(error) {error.details?.forEach(detail=>{const key=detail.loc[detail.loc.length-1], node=$('#error-'+key); if (node) {node.textContent='数值不符合要求，请按字段说明填写。'; node.hidden=false; $('#setting-'+key).setAttribute('aria-invalid','true');}}); throw error;}
  }
  async function route(focus=false) {
    view=location.hash.replace(/^#\//,'').split('/')[0]; if (!views[view]) {location.hash='#/overview'; return;}
    $('#notice').hidden=true;
    Object.keys(views).forEach(key=>{$('#view-'+key).hidden=key!==view; const nav=document.querySelector(`[data-view="${key}"]`); if (key===view) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');});
    $('#view-title').textContent=views[view]; document.title=views[view]+' · 作品监测管理'; if (focus) $('#view-title').focus();
    if ($('#workspace').hidden) return;
    if (view==='overview') await loadOverview();
    if (view==='data') await loadFeedback();
    if (view==='developers') {await loadCatalog(); renderDevelopers(); if (developerId) await loadSite();}
    if (view==='runs') await loadRuns();
    if (view==='settings') await loadSettings();
  }
  async function connect() {
    await loadCatalog(); sessionStorage.setItem('monitor-token',token);
    await Promise.all([gameOptions($('#overview-developer'),$('#overview-game')),gameOptions($('#filter-developer'),$('#filter-game'))]);
    $('#login').hidden=true; $('#workspace').hidden=false; $('#login-error').textContent=''; await loadStatus();
    if (!views[location.hash.replace(/^#\//,'')]) location.hash='#/'+view; else await route();
  }
  $('#login').addEventListener('submit',guarded(async()=>{token=$('#token').value.trim(); await connect();}));
  $('#logout').addEventListener('click',()=>{sessionStorage.removeItem('monitor-token'); token=''; location.reload();});
  $('#scan').addEventListener('click',guarded(()=>scan()));
  $('#overview-developer').addEventListener('change',guarded(async()=>{$('#overview-game').value=''; await gameOptions($('#overview-developer'),$('#overview-game')); await loadOverview();}));
  $('#overview-game').addEventListener('change',guarded(loadOverview)); $('#range').addEventListener('change',guarded(loadOverview));
  $('#filters').addEventListener('submit',guarded(applyFilters)); $('#sort').addEventListener('change',guarded(applyFilters));
  $('#filter-developer').addEventListener('change',guarded(async()=>{$('#filter-game').value=''; await gameOptions($('#filter-developer'),$('#filter-game'));}));
  $('#filters').addEventListener('reset',()=>{setTimeout(()=>{options($('#filter-game'),games,'全部作品','title'); filterParams=new URLSearchParams(); offset=0; loadFeedback().catch(error=>notify(error.message));},0);});
  $('#previous').addEventListener('click',guarded(async()=>{offset=Math.max(0,offset-20); await loadFeedback();}));
  $('#next').addEventListener('click',guarded(async()=>{offset+=20; await loadFeedback();}));
  for (const [id,fn] of [['export-csv',exportCsv],['export-ai',exportAi]]) $('#'+id).addEventListener('click',guarded(async()=>{const node=$('#'+id); node.disabled=true; try {await fn();} finally {node.disabled=false;}}));
  $('#close-dialog').addEventListener('click',()=>$('#item-dialog').close());
  $('#add-developer').addEventListener('submit',guarded(async()=>{
    const value=$('#developer-url').value.trim(); let id;
    if (/^\d+$/.test(value)) id=Number(value);
    else {let url; try {url=new URL(value);} catch {throw new Error('请输入 TapTap 厂商主页链接或数字 ID。');}
      if (!['www.taptap.cn','taptap.cn'].includes(url.hostname)) throw new Error('请输入 TapTap 厂商主页链接。');
      id=Number(url.pathname.match(/^\/developer\/(\d+)\/?$/)?.[1]);}
    if (!Number.isSafeInteger(id) || id<1) throw new Error('无法识别厂商 ID。');
    await api('developers','POST',{id}); developerId=id; $('#developer-url').value=''; await loadCatalog(); renderDevelopers(); await loadSite(); await loadStatus(); notify('厂商已添加并安排同步，作品和官网内容将自动生成。');
  }));
  $('#rename-form').addEventListener('submit',guarded(async()=>{await api('developers/'+developerId,'PATCH',{name:field($('#rename-form'),'name').value.trim()}); await loadCatalog(); renderDevelopers(); $('#developer-title').textContent=developers.find(d=>d.id===developerId).name; notify('厂商名称已保存。');}));
  $('#site-form').addEventListener('submit',guarded(async()=>{const body={}, form=$('#site-form'); ['display_name','tagline','intro','about'].forEach(key=>body[key]=field(form,key).value); body.published=field(form,'published').checked; await api('sites/'+developerId,'PATCH',body); notify('官网设置已保存。');}));
  $('#subscription-form').addEventListener('submit',guarded(async()=>{const form=$('#subscription-form'); await api('developers/'+developerId,'PATCH',{enabled:field(form,'enabled').checked,target_umo:field(form,'target_umo').value.trim()}); await loadCatalog(); renderDevelopers(); notify('监测与推送设置已保存。');}));
  $('#upload-logo').addEventListener('click',guarded(async()=>{const file=$('#studio-logo').files[0]; if (!file) throw new Error('请先选择图标文件。'); if (file.size>8*1024*1024) throw new Error('图标不能超过 8 MB。'); await api(`sites/${developerId}/logo`,'PUT',file); await loadSite(); await loadCatalog(); renderDevelopers(); $('#studio-logo').value=''; notify('厂商图标已更新。');}));
  $('#reset-logo').addEventListener('click',guarded(async()=>{await api(`sites/${developerId}/logo`,'DELETE'); await loadSite(); await loadCatalog(); renderDevelopers(); notify('已恢复采用 TapTap 厂商图标。');}));
  $('#set-primary').addEventListener('click',guarded(async()=>{await api('site-default/'+developerId,'PUT'); await loadSite(); notify('默认官网已更新。');}));
  $('#refresh-runs').addEventListener('click',guarded(()=>loadRuns()));
  $('#runtime-form').addEventListener('submit',guarded(async()=>{const save=$('#runtime-form button.primary'); save.disabled=true; try {await saveSettings();} finally {save.disabled=false;}}));
  window.addEventListener('hashchange',guarded(()=>route(true)));
  let resizeTimer;
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>{
    if (view==='overview' && overviewData) {AdminCharts.trend($('#trend'),overviewData.daily); AdminCharts.scores($('#scores'),overviewData.score_distribution);}
  },100);});
  async function poll() {
    if (document.hidden || $('#workspace').hidden || polling) return; polling=true;
    try {await loadStatus(); if (view==='runs') await loadRuns(false);}
    catch(error) {notify(error.message);} finally {polling=false;}
  }
  setInterval(poll,10000); document.addEventListener('visibilitychange',()=>{if (!document.hidden) poll();});
  if (token) connect().catch(error=>{$('#login-error').textContent=error.message;});
})();
