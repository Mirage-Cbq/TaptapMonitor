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
  const views = {
    overview:['概览','玩家反馈与采集状态一览'], data:['反馈数据','检索、查看与导出收录的玩家反馈'],
    developers:['厂商与官网','管理监测厂商、官网内容与作品展示'], runs:['采集运行','采集任务、数据源与推送记录'], settings:['系统设置','采集与推送参数，保存后立即生效'],
  };
  const KINDS = ['review','topic','review_comment','topic_comment'];
  const date = value => value ? new Date(value * 1000).toLocaleString('zh-CN', {hour12:false}) : '—';
  const shortDate = value => value ? new Date(value * 1000).toLocaleDateString('zh-CN', {month:'2-digit', day:'2-digit'}) : '—';
  const ago = value => {
    if (!value) return '—'; const s = Date.now() / 1000 - value;
    if (s < 60) return '刚刚'; if (s < 3600) return Math.floor(s / 60) + ' 分钟前'; if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
    if (s < 86400 * 30) return Math.floor(s / 86400) + ' 天前'; return shortDate(value);
  };
  const number = value => Number(value || 0).toLocaleString('zh-CN');
  const ten = stars => stars == null ? null : stars * 2;  // 1–5 星 → 十分制
  const field = (form, key) => form.elements.namedItem(key);
  let token = sessionStorage.getItem('monitor-token') || '';
  let developers = [], games = [], developerId = null, site = null, view = 'overview', days = 30, tab = 'site';
  let offset = 0, total = 0, filterParams = new URLSearchParams(), status = null;
  let statsTicket = 0, searchTicket = 0, siteTicket = 0, polling = false, runtime = null, overviewData = null;
  const resets = new Set();
  const PAGE = 20;
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
      const error = new Error(Array.isArray(detail) ? '输入内容有误，请检查标注字段。' : translations[detail] || (typeof detail === 'string' && /[一-鿿]/.test(detail) ? detail : '请求失败，请检查服务状态。'));
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
  function link(text, url, className='') {
    const a=el('a',className,text); a.href=safeUrl(url) || '#'; a.target='_blank'; a.rel='noopener noreferrer'; return a;
  }
  function image(url, alt, size=48) {
    const node=el('img'); node.src=safeUrl(url,true) || '/favicon.svg'; node.alt=alt;
    node.width=node.height=size; node.referrerPolicy='no-referrer'; node.loading='lazy';
    node.addEventListener('error', () => {if (!node.src.endsWith('/favicon.svg')) node.src='/favicon.svg';}, {once:true}); return node;
  }
  function labeled(text, node, className='') {const label=el('label',className,text); label.append(node); return label;}
  function switchControl(text, checked) {
    const label=el('label','switch'), input=el('input'); input.type='checkbox'; input.checked=checked;
    label.append(input, el('span','track'), document.createTextNode(text)); return {label, input};
  }
  function chip(value) {return el('span','chip '+value,statuses[value] || '未知');}
  function kindTag(kind) {return el('span','kind '+kind,labels[kind] || kind);}
  function scoreBadge(stars) {
    if (stars == null) return null;
    const node=el('span','score '+(stars>=4?'hi':stars===3?'mid':'lo'),String(ten(stars))); node.append(el('small','','分')); node.title=`${ten(stars)} 分（${stars} 星）`; return node;
  }
  const palette=['#1f8a6d','#3a7fc2','#c4862a','#8a6bc8','#c0566b','#4f8f3a','#2f8f9d','#a0663a'];
  function avatar(name) {
    const text=(name || '匿').trim()[0] || '匿', node=el('span','avatar',text);
    let h=0; for (const c of name || '') h=(h*31+c.charCodeAt(0))>>>0; node.style.setProperty('--av',palette[h%palette.length]); return node;
  }
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
    const pill=$('#collecting'); pill.textContent=status.collecting ? '正在同步…' : '等待下次同步';
    pill.classList.toggle('busy',status.collecting); $('#scan').disabled=status.collecting; $('#sync-site').disabled=status.collecting;
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
    try {const result=await api('scan'+(id ? '?developer_id='+id : ''),'POST'); notify(`同步任务 #${result.run_id} 已开始，完成后数据会自动刷新。`); await loadStatus();}
    finally {$('#scan').disabled=!!status?.collecting;}
  }

  /* ---------- 概览 ---------- */
  function kpi(label, value, sub, extra) {
    const node=el('div','kpi'); node.append(el('span','label',label));
    const v=el('div','value'); if (Array.isArray(value)) v.append(value[0], el('small','',value[1])); else v.textContent=value; node.append(v);
    if (sub) {const s=el('div','sub'); [].concat(sub).filter(Boolean).forEach((part,i)=>{if (i && typeof part==='string') s.append(el('span','muted','·')); s.append(typeof part==='string'?el('span','',part):part);}); node.append(s);}
    if (extra) node.append(extra);
    return node;
  }
  function delta(now, before) {
    if (!before && !now) return el('span','delta flat','持平');
    if (!before) return el('span','delta up','新增');
    const change=(now-before)/before*100, node=el('span','delta '+(change>0?'up':change<0?'down':'flat'),(change>0?'▲ ':change<0?'▼ ':'')+Math.abs(change).toFixed(0)+'%');
    return node;
  }
  function overviewQuery() {
    const params=new URLSearchParams();
    if ($('#overview-developer').value) params.set('developer_id',$('#overview-developer').value);
    if ($('#overview-game').value) params.set('app_id',$('#overview-game').value);
    return params;
  }
  async function loadOverview() {
    const ticket=++statsTicket, params=overviewQuery(), latestParams=new URLSearchParams(params);
    params.set('days',days); latestParams.set('limit','6');
    const [data,latest]=await Promise.all([api('stats?'+params),api('items/search?'+latestParams)]); if (ticket !== statsTicket) return;
    overviewData=data;
    $('#view-subtitle').textContent=`监测 ${number(data.totals.developers)} 个厂商 · ${number(data.totals.games)} 款作品`;
    const t=data.totals, reviewCount=Object.values(data.score_distribution).reduce((a,b)=>a+b,0);
    const totalsByDay=data.daily.map(day=>KINDS.reduce((s,k)=>s+day[k],0));
    const scoreMeter=el('div','meter'), fill=el('i'); fill.style.width=`${(t.average_score||0)/5*100}%`; scoreMeter.append(fill);
    const last=data.runs.items[0], rate=data.runs.success_rate;
    $('#kpis').replaceChildren(
      kpi('反馈总数',number(t.items),[`评价 ${number(data.counts.review)} · 帖子 ${number(data.counts.topic)}`],AdminCharts.sparkline(totalsByDay)),
      kpi('近 7 天新增',number(t.recent_7_days),[delta(t.recent_7_days,t.previous_7_days),`上周 ${number(t.previous_7_days)}`]),
      kpi('玩家均分',t.average_score==null?'—':[(t.average_score*2).toFixed(1),'/10'],[`基于 ${number(reviewCount)} 条评价`],scoreMeter),
      kpi('最近采集',last?statuses[last.status]||'未知':'尚未运行',[last?ago(last.started_at):'等待首次运行',rate==null?'':`成功率 ${(rate*100).toFixed(0)}%`]),
    );
    AdminCharts.trend($('#trend'),data.daily); AdminCharts.scores($('#scores'),data.score_distribution,t.average_score);
    renderGameTable(data.games); renderLatest(latest.items);
    $('#success-rate').textContent=`最近 ${data.runs.items.length} 次 · 成功率 ${rate == null ? '—' : (rate*100).toFixed(0)+'%'}`;
    $('#run-strip').replaceChildren();
    if (!data.runs.items.length) AdminCharts.empty($('#run-strip'),'尚无运行记录。');
    data.runs.items.slice().reverse().forEach(run => {
      const text=`#${run.id} · ${date(run.started_at)} · ${statuses[run.status] || '未知'} · 新增 ${run.new_items} · 修改 ${run.updated_items} · 异常 ${run.error_count}`;
      const node=button('',async()=>{location.hash='#/runs';},run.status); node.title=text; node.setAttribute('aria-label',text); $('#run-strip').append(node);
    });
  }
  function renderGameTable(rows) {
    if (!rows.length) {AdminCharts.empty($('#game-comparison'),'暂无作品，添加厂商并同步后可查看。'); return;}
    const table=el('table','game-table'), head=el('tr');
    ['作品','TapTap 评分','玩家均分','评价','反馈'].forEach((h,i)=>{const th=el('th',i?'num':'',h); th.scope='col'; head.append(th);});
    const thead=el('thead'); thead.append(head); const body=el('tbody'); table.append(thead,body);
    const max=Math.max(1,...rows.map(g=>KINDS.reduce((s,k)=>s+g.counts[k],0)));
    rows.forEach(game=>{
      const tr=el('tr'), sum=KINDS.reduce((s,k)=>s+game.counts[k],0), cell=el('div','game-cell'), info=el('div');
      const bar=el('div','stack-bar'); bar.style.width=`${Math.max(8,sum/max*100)}%`;
      KINDS.forEach(k=>{if (!game.counts[k]) return; const seg=el('i'); seg.style.width=`${game.counts[k]/sum*100}%`; seg.style.background=`var(--c-${k})`; seg.title=`${labels[k]} ${game.counts[k]}`; bar.append(seg);});
      info.append(el('strong','',game.title),bar); cell.append(image(game.icon,'',38),info);
      const td=el('td'); td.append(cell); tr.append(td);
      const rating=el('td','num'); rating.append(game.rating==null?'—':el('strong','',game.rating.toFixed(1))); tr.append(rating);
      tr.append(el('td','num',game.average_score==null?'—':(game.average_score*2).toFixed(1)),el('td','num',number(game.review_count)),el('td','num',number(sum)));
      body.append(tr);
    });
    const scroller=el('div','table-scroll'); scroller.append(table); $('#game-comparison').replaceChildren(scroller);
  }
  function renderLatest(items) {
    if (!items.length) {AdminCharts.empty($('#latest'),'暂无反馈。'); return;}
    $('#latest').replaceChildren(...items.map(item=>{
      const node=el('button','latest-item'); node.type='button'; const row=el('div','row');
      row.append(kindTag(item.kind)); const badge=scoreBadge(item.score); if (badge) row.append(badge);
      row.append(el('span','',item.game_title || ''),el('span','when',ago(item.published_at)));
      node.append(row,el('p','',item.body || item.title || '（无文字内容）')); node.addEventListener('click',()=>showItem(item)); return node;
    }));
  }

  /* ---------- 反馈数据 ---------- */
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
    const ticket=++searchTicket, params=new URLSearchParams(filterParams); params.set('limit',PAGE); params.set('offset',offset);
    const data=await api('items/search?'+params); if (ticket !== searchTicket) return;
    total=data.total; $('#feedback-total').textContent=`共 ${number(total)} 条反馈`;
    if (!data.items.length) AdminCharts.empty($('#feedback'),'当前筛选下没有反馈。');
    else $('#feedback').replaceChildren(...data.items.map(feedItem));
    $('#previous').disabled=offset===0; $('#next').disabled=offset+PAGE>=total;
    $('#page-label').textContent=total ? `第 ${offset/PAGE+1} / ${Math.ceil(total/PAGE)} 页` : '';
  }
  function feedItem(item) {
    const node=el('article','feed-item'); node.tabIndex=0; node.setAttribute('role','button'); node.setAttribute('aria-label',`查看${item.author || '匿名'}的${labels[item.kind]}`);
    const main=el('div'), meta=el('div','feed-meta');
    meta.append(el('strong','',item.author || '匿名'),kindTag(item.kind)); const badge=scoreBadge(item.score); if (badge) meta.append(badge);
    if (item.official) meta.append(el('span','official','官方'));
    meta.append(el('span','',item.game_title || `作品 ${item.app_id}`),el('span','when',date(item.published_at)));
    main.append(meta); if (item.title) main.append(el('h3','',item.title)); main.append(el('p','body',item.body || '（无文字内容）'));
    const media=(item.media || []).filter(url=>safeUrl(url,true)).slice(0,4);
    if (media.length) {const thumbs=el('div','thumbs'); media.forEach(url=>thumbs.append(image(url,'',64))); main.append(thumbs);}
    node.append(avatar(item.author),main);
    node.addEventListener('click',()=>showItem(item)); node.addEventListener('keydown',event=>{if (event.key==='Enter' || event.key===' ') {event.preventDefault(); showItem(item);}});
    return node;
  }
  function showItem(item) {
    const body=$('#item-detail'); body.replaceChildren(); $('#item-title').textContent=item.title || labels[item.kind]+'详情';
    const head=el('div','feed-meta'); head.append(avatar(item.author),el('strong','',item.author || '匿名'),kindTag(item.kind)); const badge=scoreBadge(item.score); if (badge) head.append(badge); if (item.official) head.append(el('span','official','官方'));
    const meta=el('dl','detail-meta');
    [['作品',item.game_title || item.app_id],['发布时间',date(item.published_at)],['首次收录',date(item.first_seen)],['评分',item.score==null?'—':`${ten(item.score)} 分（十分制）`]].forEach(([k,v])=>meta.append(el('dt','',k),el('dd','',v)));
    body.append(head,el('p','body',item.body || '（无文字内容）'));
    const media=el('div','media-grid');
    (item.media || []).forEach(url=>{if (!safeUrl(url,true)) return; const a=link('',url); a.href=safeUrl(url,true); a.append(image(url,'反馈附件，点击查看原图',120)); media.append(a);});
    if (media.childElementCount) body.append(media);
    body.append(meta); if (safeUrl(item.url)) body.append(link('在 TapTap 查看原文 ↗',item.url,'link'));
    $('#item-dialog').showModal();
  }
  function download(content, name, type) {
    const url=URL.createObjectURL(new Blob([content],{type})), a=el('a'); a.href=url; a.download=name; document.body.append(a); a.click(); a.remove(); setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  async function exportCsv() {
    const params=new URLSearchParams(filterParams); params.set('limit','500'); let start=0, rows=[], count;
    do {params.set('offset',start); const batch=await api('items/search?'+params); rows.push(...batch.items); count=batch.total; start+=batch.items.length; if (!batch.items.length) break;} while (start<count);
    const quote=value=>{let text=String(value ?? ''); if (/^[\s]*[=+\-@\t\r]/.test(text)) text="'"+text; return '"'+text.replaceAll('"','""')+'"';};
    const records=[['作品','类型','标题','正文','作者','评分（十分制）','官方','发布时间','原文链接'],...rows.map(item=>[item.game_title,labels[item.kind],item.title,item.body,item.author,ten(item.score),item.official?'是':'否',date(item.published_at),item.url])];
    download('﻿'+records.map(row=>row.map(quote).join(',')).join('\r\n'),'反馈筛选结果.csv','text/csv;charset=utf-8'); notify(`已导出 ${rows.length} 条反馈。`);
  }
  async function exportAi() {
    const id=filterParams.get('developer_id') || developerId;
    if (!id) throw new Error('请先在筛选栏选择厂商并查询，再导出该厂商的 AI 汇总数据。');
    let cursor=0, events=[], more;
    do {const batch=await api(`digest-input?developer_id=${id}&after=${cursor}&limit=500&include_baseline=true`); events.push(...batch.events); cursor=batch.next_cursor; more=batch.has_more;} while (more);
    download(JSON.stringify({developer_id:Number(id),events},null,2),`厂商-${id}-AI汇总.json`,'application/json;charset=utf-8'); notify(`已导出该厂商全部历史反馈事件 ${events.length} 条（含修改与基线记录）。`);
  }

  /* ---------- 厂商与官网 ---------- */
  function renderDevelopers() {
    $('#developers').replaceChildren();
    if (!developers.length) {AdminCharts.empty($('#developers'),'尚未添加厂商。'); $('#developer-detail').hidden=true; $('#developer-empty').hidden=false; return;}
    developers.forEach(developer=>{
      const card=el('button','dev-card'); card.type='button'; card.setAttribute('aria-current',String(developer.id===developerId));
      const info=el('div'); info.append(el('strong','',developer.name),el('span','muted',`${number(developer.game_count)} 款作品 · ${number(developer.feedback_count)} 条反馈`));
      card.append(image(developer.logo,'',44),info,chip(developer.enabled?'ok':'interrupted'));
      card.lastChild.textContent=developer.enabled?'监测中':'已暂停';
      card.addEventListener('click',guarded(async()=>{developerId=developer.id; renderDevelopers(); await loadSite();}));
      $('#developers').append(card);
    });
  }
  function selectTab(name) {
    tab=name;
    document.querySelectorAll('.tabs [role=tab]').forEach(b=>b.setAttribute('aria-selected',String(b.dataset.tab===name)));
    document.querySelectorAll('.tab-panel').forEach(p=>p.hidden=p.dataset.panel!==name);
  }
  async function loadSite() {
    const ticket=++siteTicket, id=developerId, data=await api('sites/'+id);
    if (ticket!==siteTicket || id!==developerId) return; site=data;
    $('#developer-detail').hidden=false; $('#developer-empty').hidden=true;
    const developer=developers.find(d=>d.id===id);
    $('#developer-title').textContent=developer?.name || site.name;
    $('#developer-sync').textContent=`厂商 ID ${id} · 官网数据最近同步：${site.synced_at ? date(site.synced_at) : '尚未同步'}${site.is_primary ? ' · 默认官网' : ''}`;
    $('#studio-link').href='/studio/'+id; $('#logo-preview').src=safeUrl(site.logo,true) || '/favicon.svg';
    const form=$('#site-form'), auto={display_name:site.name,tagline:site.tagline,intro:site.intro,about:site.about};
    ['display_name','tagline','intro','about'].forEach(key=>{const input=field(form,key); input.value=site.custom[key] || ''; input.placeholder=site.custom[key] ? '' : '自动：'+auto[key];});
    field(form,'published').checked=site.custom.published!==false;
    $('#set-primary').disabled=site.is_primary; $('#set-primary').textContent=site.is_primary?'当前为默认官网':'设为默认官网';
    field($('#rename-form'),'name').value=developer.name; field($('#subscription-form'),'enabled').checked=!!developer.enabled; field($('#subscription-form'),'target_umo').value=developer.target_umo;
    renderGameEditors(id); selectTab(tab);
  }
  function renderGameEditors(id) {
    $('#games').replaceChildren();
    if (!site.games.length) {AdminCharts.empty($('#games'),'还没有同步到作品，点击“同步官网数据”开始获取。'); return;}
    site.games.forEach(game=>{
      const details=el('details','game-editor'), summary=el('summary'), badges=el('span','badges'), name=el('span','name');
      const paintSummary=()=>{name.textContent=title.value || game.source.title; badges.replaceChildren(); if (!visible.input.checked) badges.append(el('span','chip interrupted','已隐藏')); if (featured.input.checked) badges.append(el('span','chip running','精选'));};
      const title=el('input'); title.maxLength=200; title.value=game.overrides.title || ''; title.placeholder=game.source.title;
      const description=el('textarea'); description.rows=4; description.maxLength=10000; description.value=game.overrides.description || ''; description.placeholder='留空则使用 TapTap 介绍';
      const visible=switchControl('在官网展示',game.visible), featured=switchControl('精选推荐',game.featured);
      const sort=el('input'); sort.type='number'; sort.min=-100000; sort.max=100000; sort.value=game.sort_order;
      summary.append(image(game.icon,'',40),name,badges); paintSummary();
      const editor=el('form'), toggles=el('div','toggles span-all'); toggles.append(visible.label,featured.label,labeled('排序（越小越靠前）',sort));
      const source=el('div','span-all'); source.append(el('p','muted small','TapTap 原始介绍'),el('p','source-description',game.source.description || '（无）'));
      const actions=el('div','actions span-all'), save=el('button','primary','保存');
      const persist=async reset=>{await api(`sites/${id}/games/${game.id}`,'PATCH',{title:reset?'':title.value,description:reset?'':description.value,visible:visible.input.checked,featured:featured.input.checked,sort_order:Number(sort.value)}); if (reset) title.value=description.value=''; paintSummary(); notify('作品展示设置已保存。');};
      actions.append(save,button('恢复 TapTap 文案',()=>persist(true),'ghost'));
      editor.append(labeled('官网标题',title),labeled('作品页链接',link(`TapTap 作品页 ↗`,game.url,'link')),labeled('官网介绍',description,'span-all'),source,toggles,actions);
      editor.addEventListener('submit',guarded(async()=>{save.disabled=true; try {await persist(false);} finally {save.disabled=false;}}));
      details.append(summary,editor); $('#games').append(details);
    });
  }

  /* ---------- 采集运行 ---------- */
  function runErrors(run) {try {const errors=JSON.parse(run.errors); return Array.isArray(errors)?errors:[];} catch {return ['异常记录格式无法读取'];}}
  async function loadRuns(refreshStatus=true) {
    const [runs,deliveries] = await Promise.all([api('runs?limit=100'),api('deliveries?limit=20'),...(refreshStatus?[loadStatus()]:[])]);
    const finished=runs.filter(r=>r.status!=='running'), okRate=finished.length?finished.filter(r=>r.status==='ok').length/finished.length:null;
    const durations=finished.filter(r=>r.finished_at).map(r=>r.finished_at-r.started_at), avg=durations.length?durations.reduce((a,b)=>a+b,0)/durations.length:null;
    $('#run-summary').replaceChildren(
      kpi('成功率',okRate==null?'—':[(okRate*100).toFixed(0),'%'],[`最近 ${finished.length} 次已结束运行`]),
      kpi('最近运行',runs[0]?statuses[runs[0].status]||'未知':'—',[runs[0]?ago(runs[0].started_at):'尚无记录']),
      kpi('平均耗时',avg==null?'—':[Math.round(avg),'秒']),
      kpi('待处理数据源',number(status?.streams.length||0),['分页进度或异常的数据源']),
    );
    if (!runs.length) AdminCharts.empty($('#runs'),'尚无运行记录。');
    else {
      const table=el('table','runs'), head=el('tr'); ['运行','范围','开始时间','耗时','状态','新增 / 修改','异常'].forEach(h=>{const th=el('th','',h); th.scope='col'; head.append(th);});
      const thead=el('thead'); thead.append(head); const body=el('tbody'); table.append(thead,body);
      runs.forEach(run=>{
        const row=el('tr'), td=(v,c='')=>{const x=el('td',c); if (v instanceof Node) x.append(v); else x.textContent=v; row.append(x);};
        td('#'+run.id,'nowrap'); td(run.developer_id ? developers.find(d=>d.id===run.developer_id)?.name || '厂商 '+run.developer_id : '全部厂商');
        td(date(run.started_at),'nowrap'); td(`${Math.max(0,Math.round((run.finished_at || Date.now()/1000)-run.started_at))} 秒`,'nowrap'); td(chip(run.status)); td(`${run.new_items} / ${run.updated_items}`,'nowrap');
        const errors=runErrors(run);
        if (errors.length) {const details=el('details'), list=el('ul','run-errors'); errors.forEach(error=>list.append(el('li','',typeof error==='string'?error:JSON.stringify(error)))); details.append(el('summary','',errors.length+' 项'),list); td(details);}
        else td('—'); body.append(row);
      }); $('#runs').replaceChildren(table);
    }
    $('#streams').replaceChildren();
    if (!status?.streams.length) AdminCharts.empty($('#streams'),'所有数据源正常。');
    (status?.streams || []).forEach(stream=>{
      const node=el('article','stream'), top=el('div','actions'); top.append(chip(stream.status),el('code','',stream.key));
      const wording=stream.status==='pending'?'已保存分页进度，下轮继续采集':stream.status==='restricted'?'TapTap 要求登录才能展开部分楼中楼，已保存公开预览':stream.error || '数据源异常';
      node.append(top,el('p','muted small',wording),el('p','muted small','最近尝试：'+date(stream.last_attempt))); $('#streams').append(node);
    });
    $('#deliveries').replaceChildren();
    if (!deliveries.length) AdminCharts.empty($('#deliveries'),'暂无推送记录。');
    deliveries.forEach(delivery=>{const node=el('article','stream'), top=el('div','actions'); top.append(chip(delivery.status),el('span','',`厂商 ${delivery.developer_id} · 尝试 ${delivery.attempts} 次 · ${date(delivery.created_at)}`)); node.append(top); if (delivery.error) node.append(el('p','error small',delivery.error)); $('#deliveries').append(node);});
  }

  /* ---------- 系统设置 ---------- */
  const settingGroups=[
    ['采集任务','控制采集频率与单次规模。请求过快可能被 TapTap 限流。',[
      ['poll_seconds','采集周期','两次自动采集的间隔，至少 60 秒。','秒',60,null,1],
      ['request_interval','请求间隔','相邻两次 TapTap 请求之间的等待时间。','秒',0,60,'any'],
      ['request_timeout','请求超时','单个请求最长等待时间，最多 120 秒。','秒',0,120,'any'],
      ['max_pages','单次分页上限','每个数据源单次最多翻页数，达到后保存进度下轮继续。','页',1,1000,1]]],
    ['采集内容','选择需要额外采集与保存的内容。',[
      ['collect_comments','采集评论回复','同时采集评价回复与论坛回复。'],
      ['cache_images','缓存作品图片','把作品封面与图标缓存到服务器，官网加载更快。']]],
    ['简报推送','通过 AstrBot 把新增反馈推送给厂商接收会话。',[
      ['digest_enabled','启用简报推送','需在服务器配置 AstrBot，并在厂商设置中填写接收会话。'],
      ['digest_interval','推送间隔','两次推送之间的间隔，至少 60 秒。','秒',60,null,1],
      ['digest_batch_size','每批条数','每次推送最多包含的反馈事件数，1–500。','条',1,500,1]]],
  ];
  const settingDefinitions=settingGroups.flatMap(([, , items])=>items);
  function settingMeta(key) {
    const node=$('#meta-'+key), show=value=>typeof value==='boolean'?(value?'开启':'关闭'):value;
    const overridden=!resets.has(key) && Object.hasOwn(runtime.overrides,key);
    node.textContent=resets.has(key)?'保存后恢复默认':overridden?`已修改 · 默认 ${show(runtime.defaults[key])}`:'使用默认值';
    node.classList.toggle('overridden',overridden);
  }
  async function loadSettings() {
    runtime=await api('settings'); resets.clear(); $('#runtime-fields').replaceChildren(); $('#settings-message').textContent='';
    settingGroups.forEach(([title,help,items])=>{
      const group=el('section','card setting-group'); group.append(el('h2','',title),el('p','muted small',help));
      items.forEach(([key,label,hint,unit,min,max,step])=>{
        const row=el('div','setting-row'), id='setting-'+key; let input, control;
        if (min === undefined) {const sw=switchControl('',runtime.values[key]); input=sw.input; control=el('div','control'); control.append(sw.label);}
        else {input=el('input'); input.type='number'; input.min=min; if (max!==null) input.max=max; input.step=step; input.required=true; input.value=runtime.values[key]; control=el('div','control'); control.append(input,el('span','unit',unit));}
        input.id=id; input.name=key;
        const name=el('label','',label); name.htmlFor=id; name.append(el('small','',hint));
        const meta=el('span','setting-meta'); meta.id='meta-'+key;
        const error=el('p','field-error'); error.id='error-'+key; error.hidden=true; input.setAttribute('aria-describedby',meta.id+' '+error.id);
        input.addEventListener('input',()=>{resets.delete(key); error.hidden=true; input.removeAttribute('aria-invalid'); settingMeta(key);});
        input.addEventListener('change',()=>{resets.delete(key); settingMeta(key);});
        const reset=button('恢复默认',async()=>{resets.add(key); if (min===undefined) input.checked=runtime.defaults[key]; else input.value=runtime.defaults[key]; input.removeAttribute('aria-invalid'); error.hidden=true; settingMeta(key); $('#settings-message').textContent='默认值已填入，保存后生效。';},'ghost');
        const side=el('div','side'); side.append(meta,reset);
        row.append(name,control,side,error); group.append(row);
      });
      $('#runtime-fields').append(group); items.forEach(([key])=>settingMeta(key));
    });
    $('#system-info').replaceChildren();
    [['应用版本',runtime.info.version],['数据库路径',runtime.info.database],['AstrBot 接入',runtime.info.astrbot_configured?'已配置':'未配置']].forEach(([key,value])=>$('#system-info').append(el('dt','',key),el('dd','',value)));
  }
  async function saveSettings() {
    const body={}; let invalid=null;
    settingDefinitions.forEach(([key,,,,min])=>{
      const input=$('#setting-'+key), value=input.type==='checkbox'?input.checked:Number(input.value), error=$('#error-'+key);
      error.hidden=true; input.removeAttribute('aria-invalid');
      if (!input.validity.valid || (key==='request_timeout' && value<=0)) {
        error.textContent='请输入符合说明的'+(min===undefined?'内容。':'数值。'); error.hidden=false; input.setAttribute('aria-invalid','true'); invalid ||= input;
      }
      if (resets.has(key)) body[key]=null; else if (value!==runtime.values[key]) body[key]=value;
    });
    if (invalid) {invalid.focus(); throw new Error('请检查标注字段后保存。');}
    if (!Object.keys(body).length) {$('#settings-message').textContent='没有需要保存的修改。'; return;}
    try {await api('settings','PATCH',body); await loadSettings(); $('#notice').hidden=true; $('#settings-message').textContent='已保存，设置已生效。';}
    catch(error) {error.details?.forEach(detail=>{const key=detail.loc[detail.loc.length-1], node=$('#error-'+key); if (node) {node.textContent='数值不符合要求，请按说明填写。'; node.hidden=false; $('#setting-'+key).setAttribute('aria-invalid','true');}}); throw error;}
  }

  /* ---------- 系统升级 ---------- */
  const short=sha=>(sha || '').slice(0,7);
  const when=iso=>iso ? new Date(iso).toLocaleString('zh-CN',{hour12:false}) : '';
  const INSTALL='curl -fsSL https://raw.githubusercontent.com/Mirage-Cbq/TaptapMonitor/main/scripts/install.sh | sudo sh';
  const results={success:['ok','升级成功'],rolled_back:['interrupted','已自动回滚'],failed:['error','失败'],up_to_date:['ok','无需升级'],checked:['ok','检查完成']};
  let upgradeTimer=null, upgradeWatching=false;
  const busy=info=>info.pending || info.state==='checking' || info.state==='updating';
  function versionBox(label, version, className='') {
    const box=el('div','version-box '+className); box.append(el('span','label',label));
    if (!version) {box.append(el('span','muted','未知')); return box;}
    box.append(el('span','sha',short(version.commit)),el('span','msg',version.message || ''),
      el('span','muted small',`提交于 ${when(version.date)}`+(version.deployed_at?` · 部署于 ${when(version.deployed_at)}`:'')));
    return box;
  }
  async function requestUpgrade(ref, label) {
    if (!confirm(`确定${label}吗？\n\n服务器会从 GitHub 拉取该版本并重启服务（约 10–30 秒），升级前自动备份数据库，启动失败会自动回滚。`)) return;
    renderUpgrade(await api('system/upgrade','POST',{ref})); watchUpgrade();
  }
  function watchUpgrade() {
    clearTimeout(upgradeTimer); upgradeWatching=true;
    upgradeTimer=setTimeout(async()=>{
      let info;
      try {info=await api('system');}
      catch {const node=$('#upgrade-progress'); if (node) node.textContent='服务正在重启，稍候自动重连…'; watchUpgrade(); return;}
      if (view==='settings') renderUpgrade(info);
      if (busy(info)) {watchUpgrade(); return;}
      upgradeWatching=false; if (info.message) notify(info.message);
    },3000);
  }
  function commitRow(commit, tag, tagClass) {
    const row=el('div','deploy-run');
    row.append(el('span','chip '+tagClass,tag),el('span','title',commit.message),el('code','',`${short(commit.commit)} · ${ago(Date.parse(commit.date)/1000)}`));
    return row;
  }
  function renderUpgrade(info) {
    const body=$('#upgrade-body');
    $('#check-updates').hidden=!info.installed;
    if (!info.installed) {
      const steps=el('ol','setup-steps'), first=el('li'), second=el('li');
      first.append('在服务器上以 root 运行安装脚本（已有安装会原地转换，数据与配置保留）：',el('pre','command',INSTALL));
      second.append('完成后刷新本页，即可在这里检查更新、一键升级与回滚。');
      steps.append(first,second);
      body.replaceChildren(versionBox('当前运行版本',info.version),el('p','muted','服务器尚未安装更新服务。更新服务以独立的系统任务运行，从 GitHub 拉取官方版本：'),steps);
      return;
    }
    const current=info.current || info.version, latest=info.latest, behind=info.behind || [], running=busy(info);
    const grid=el('div','version-grid');
    grid.append(versionBox('当前运行版本',current),versionBox(behind.length?`最新版本（落后 ${behind.length} 个更新）`:'最新版本',latest,behind.length?'new':''));
    const nodes=[grid];
    if (running) {const progress=el('p','upgrade-progress',info.pending?'已提交，等待更新服务启动…':info.message || '处理中…'); progress.id='upgrade-progress'; nodes.push(progress);}
    else if (results[info.result]) {
      const line=el('p','upgrade-result'), [cls,text]=results[info.result];
      line.append(el('span','chip '+cls,text),el('span','',info.message || ''),el('span','muted small',info.finished_at?ago(Date.parse(info.finished_at)/1000):''));
      nodes.push(line);
    }
    const actions=el('div','actions upgrade-actions');
    if (behind.length && !running) actions.append(button(`升级到最新版 ${short(latest.commit)}`,()=>requestUpgrade('latest',`升级到最新版 ${short(latest.commit)}`),'primary'));
    else if (!running && latest) actions.append(el('span','chip ok','已是最新版本'));
    const auto=switchControl('每天凌晨自动升级到最新版',!!info.auto);
    auto.input.disabled=running;
    auto.input.addEventListener('change',guarded(async()=>{
      try {renderUpgrade(await api('system/auto','PUT',{enabled:auto.input.checked})); notify(auto.input.checked?'已开启自动升级。':'已关闭自动升级。');}
      catch(error) {auto.input.checked=!auto.input.checked; throw error;}
    }));
    actions.append(auto.label); nodes.push(actions);
    if (info.checked_at) nodes.push(el('p','muted small',`上次检查：${when(info.checked_at)} · 分支 ${info.branch || 'main'}`));
    if (behind.length) {
      const list=el('div','deploy-runs'); list.append(el('h3','small muted','待升级的更新'));
      behind.slice(0,10).forEach(commit=>list.append(commitRow(commit,'新','new')));
      nodes.push(list);
    }
    const history=(info.history || []).filter(commit=>!behind.some(item=>item.commit===commit.commit));
    if (history.length) {
      const list=el('details','deploy-runs history'); list.append(el('summary','small muted','版本历史与回滚'));
      history.forEach(commit=>{
        const isCurrent=current && commit.commit===current.commit, row=commitRow(commit,isCurrent?'当前':'历史',isCurrent?'ok':'muted');
        if (!isCurrent && !running) row.append(button('回滚到此版本',()=>requestUpgrade(commit.commit,`回滚到 ${short(commit.commit)}`),'ghost'));
        list.append(row);
      });
      nodes.push(list);
    }
    if (info.log?.length) {
      const details=el('details','upgrade-log'); details.append(el('summary','small muted','更新日志'),el('pre','',info.log.slice(-30).join('\n')));
      details.open=running; nodes.push(details);
    }
    body.replaceChildren(...nodes);
  }
  async function loadUpgrade() {
    const body=$('#upgrade-body'); if (!body.childElementCount) body.replaceChildren(el('p','muted','正在读取版本信息…'));
    const info=await api('system'); renderUpgrade(info);
    if (busy(info) && !upgradeWatching) watchUpgrade();
  }
  async function checkUpdates() {renderUpgrade(await api('system/check','POST')); watchUpgrade();}

  /* ---------- 路由与事件 ---------- */
  async function route(focus=false) {
    view=location.hash.replace(/^#\//,'').split('/')[0]; if (!views[view]) {location.hash='#/overview'; return;}
    $('#notice').hidden=true;
    Object.keys(views).forEach(key=>{$('#view-'+key).hidden=key!==view; const nav=document.querySelector(`[data-view="${key}"]`); if (key===view) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');});
    $('#view-title').textContent=views[view][0]; $('#view-subtitle').textContent=views[view][1]; document.title=views[view][0]+' · 作品监测管理'; if (focus) $('#view-title').focus();
    if ($('#workspace').hidden) return;
    if (view==='overview') await loadOverview();
    if (view==='data') await loadFeedback();
    if (view==='developers') {await loadCatalog(); if (!developerId && developers.length) developerId=developers[0].id; renderDevelopers(); if (developerId) await loadSite();}
    if (view==='runs') await loadRuns();
    if (view==='settings') {await loadSettings(); await loadUpgrade();}
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
  $('#sync-site').addEventListener('click',guarded(async()=>{if (developerId) await scan(developerId);}));
  $('#overview-developer').addEventListener('change',guarded(async()=>{$('#overview-game').value=''; await gameOptions($('#overview-developer'),$('#overview-game')); await loadOverview();}));
  $('#overview-game').addEventListener('change',guarded(loadOverview));
  $('#range').addEventListener('click',guarded(async event=>{const b=event.target.closest('button[data-days]'); if (!b) return; days=Number(b.dataset.days); $('#range').querySelectorAll('button').forEach(x=>x.setAttribute('aria-checked',String(x===b))); await loadOverview();}));
  $('#filters').addEventListener('submit',guarded(applyFilters)); $('#sort').addEventListener('change',guarded(applyFilters));
  $('#filter-developer').addEventListener('change',guarded(async()=>{$('#filter-game').value=''; await gameOptions($('#filter-developer'),$('#filter-game'));}));
  $('#filters').addEventListener('reset',()=>{setTimeout(()=>{options($('#filter-game'),games,'全部作品','title'); filterParams=new URLSearchParams(); offset=0; loadFeedback().catch(error=>notify(error.message));},0);});
  $('#previous').addEventListener('click',guarded(async()=>{offset=Math.max(0,offset-PAGE); await loadFeedback(); scrollTo({top:0});}));
  $('#next').addEventListener('click',guarded(async()=>{offset+=PAGE; await loadFeedback(); scrollTo({top:0});}));
  for (const [id,fn] of [['export-csv',exportCsv],['export-ai',exportAi]]) $('#'+id).addEventListener('click',guarded(async()=>{const node=$('#'+id); node.disabled=true; try {await fn();} finally {node.disabled=false;}}));
  $('#close-dialog').addEventListener('click',()=>$('#item-dialog').close());
  $('#item-dialog').addEventListener('click',event=>{if (event.target===$('#item-dialog')) $('#item-dialog').close();});
  document.querySelectorAll('.tabs [role=tab]').forEach(b=>b.addEventListener('click',()=>selectTab(b.dataset.tab)));
  $('#add-developer').addEventListener('submit',guarded(async()=>{
    const value=$('#developer-url').value.trim(); let id;
    if (/^\d+$/.test(value)) id=Number(value);
    else {let url; try {url=new URL(value);} catch {throw new Error('请输入 TapTap 厂商主页链接或数字 ID。');}
      if (!['www.taptap.cn','taptap.cn'].includes(url.hostname)) throw new Error('请输入 TapTap 厂商主页链接。');
      id=Number(url.pathname.match(/^\/developer\/(\d+)\/?$/)?.[1]);}
    if (!Number.isSafeInteger(id) || id<1) throw new Error('无法识别厂商 ID。');
    await api('developers','POST',{id}); developerId=id; $('#developer-url').value=''; await loadCatalog(); renderDevelopers(); await loadSite(); await loadStatus(); notify('厂商已添加并开始同步，作品和官网内容将自动生成。');
  }));
  $('#rename-form').addEventListener('submit',guarded(async()=>{await api('developers/'+developerId,'PATCH',{name:field($('#rename-form'),'name').value.trim()}); await loadCatalog(); renderDevelopers(); $('#developer-title').textContent=developers.find(d=>d.id===developerId).name; notify('监测名称已保存。');}));
  $('#site-form').addEventListener('submit',guarded(async()=>{const body={}, form=$('#site-form'); ['display_name','tagline','intro','about'].forEach(key=>body[key]=field(form,key).value); body.published=field(form,'published').checked; await api('sites/'+developerId,'PATCH',body); notify('官网内容已保存，刷新官网即可看到。');}));
  $('#subscription-form').addEventListener('submit',guarded(async()=>{const form=$('#subscription-form'); await api('developers/'+developerId,'PATCH',{enabled:field(form,'enabled').checked,target_umo:field(form,'target_umo').value.trim()}); await loadCatalog(); renderDevelopers(); notify('监测与推送设置已保存。');}));
  $('#upload-logo').addEventListener('click',guarded(async()=>{const file=$('#studio-logo').files[0]; if (!file) throw new Error('请先选择图标文件。'); if (file.size>8*1024*1024) throw new Error('图标不能超过 8 MB。'); await api(`sites/${developerId}/logo`,'PUT',file); await loadCatalog(); await loadSite(); renderDevelopers(); $('#studio-logo').value=''; notify('厂商图标已更新。');}));
  $('#reset-logo').addEventListener('click',guarded(async()=>{await api(`sites/${developerId}/logo`,'DELETE'); await loadCatalog(); await loadSite(); renderDevelopers(); notify('已恢复使用 TapTap 厂商图标。');}));
  $('#set-primary').addEventListener('click',guarded(async()=>{await api('site-default/'+developerId,'PUT'); await loadSite(); notify('默认官网已更新。');}));
  $('#refresh-runs').addEventListener('click',guarded(()=>loadRuns()));
  $('#check-updates').addEventListener('click',guarded(checkUpdates));
  $('#runtime-form').addEventListener('submit',guarded(async()=>{const save=$('#runtime-form button.primary'); save.disabled=true; try {await saveSettings();} finally {save.disabled=false;}}));
  window.addEventListener('hashchange',guarded(()=>route(true)));
  let resizeTimer;
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>{if (view==='overview' && overviewData) AdminCharts.trend($('#trend'),overviewData.daily);},120);});
  async function poll() {
    if (document.hidden || $('#workspace').hidden || polling) return; polling=true;
    try {await loadStatus(); if (view==='runs') await loadRuns(false);}
    catch(error) {notify(error.message);} finally {polling=false;}
  }
  setInterval(poll,10000); document.addEventListener('visibilitychange',()=>{if (!document.hidden) poll();});
  if (token) connect().catch(error=>{$('#login-error').textContent=error.message;});
})();
