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
    overview:['概览','作品表现与玩家反馈一览'], data:['反馈数据','检索、查看与导出收录的玩家反馈'], insights:['数据洞察','评分概况、异常预警、关键词与回复分析'],
    developers:['厂商与官网','管理监测厂商、官网内容与作品展示'], sources:['数据源','官方授权、开发者后台登录与采集记录'], settings:['系统设置','采集与推送参数，保存后立即生效'],
  };
  const KINDS = ['review','topic'];  // 回复计入所属的评价/帖子，不单独统计
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
  const KEY='monitor-token';
  const forget=()=>{try {sessionStorage.removeItem(KEY); localStorage.removeItem(KEY);} catch {}};
  let token=''; try {token=sessionStorage.getItem(KEY) || localStorage.getItem(KEY) || '';} catch {}
  let developers = [], games = [], developerId = null, site = null, view = 'overview', days = 30, tab = 'site';
  let offset = 0, total = 0, filterParams = new URLSearchParams(), status = null;
  let statsTicket = 0, searchTicket = 0, siteTicket = 0, polling = false, runtime = null, overviewData = null;
  let coreData = null, insightTicket = 0, insightDays = 30, insightData = null, officialTimer = null, defaultsApplied = false;
  const primaryId = () => developers.find(d => d.is_primary)?.id ?? developers[0]?.id ?? null;
  const resets = new Set();
  const PAGE = 20;
  const notify = message => {$('#notice').textContent = message; $('#notice').hidden = false;};
  const translations = {
    'Valid Bearer token required':'管理令牌无效，请重新登录', 'Developer already subscribed':'该厂商已在监测列表中',
    'A collection run is already active':'已有同步任务正在运行，请稍后再试', 'Developer not found':'未找到厂商',
    'Game not found for developer':'未找到该厂商的作品', 'Invalid logo image':'图标不是有效图片',
    'Logo must be smaller than 8 MB':'图标不能超过 8 MB', 'Use PNG, JPEG or WebP, up to 4096 pixels per side':'请使用 PNG、JPEG 或 WebP，每边不超过 4096 像素',
    'Parent item not found':'未找到这条内容'
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
      if (response.status === 401) {forget(); $('#workspace').hidden = true; $('#login').hidden = false; $('#login-error').textContent = error.message;}
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
    const named=developers.map(d=>({...d,name:d.is_primary?d.name+'（默认）':d.name}));
    ['#overview-developer','#filter-developer','#insight-developer'].forEach(id=>options($(id),named,'全部厂商'));
  }
  // 默认厂商是主要关注对象：首次进入时各处筛选默认选中它
  function applyDefaults() {
    const id=primaryId(); if (defaultsApplied || !id) return; defaultsApplied=true;
    ['#overview-developer','#filter-developer','#insight-developer'].forEach(sel=>{$(sel).value=String(id);});
    filterParams=new URLSearchParams({developer_id:String(id)});
    if (!developerId) developerId=id;
  }
  async function gameOptions(developerSelect, gameSelect) {
    const rows=developerSelect.value ? await api('games?developer_id='+developerSelect.value) : games;
    options(gameSelect,rows,'全部作品','title');
  }
  async function loadStatus() {
    const previous=status?.collecting;
    status=await api('status');
    const pill=$('#collecting'); pill.textContent=status.collecting ? '正在同步…' : '等待下次同步';
    pill.classList.toggle('busy',status.collecting); ['#scan','#scan-full','#sync-site','#dev-incremental','#dev-full'].forEach(id=>{$(id).disabled=status.collecting;});
    if (previous && !status.collecting) {
      await loadCatalog(); notify('本轮同步已结束，数据已更新。');
      if (view === 'overview') await loadOverview();
      if (view === 'data') {await gameOptions($('#filter-developer'),$('#filter-game')); await loadFeedback();}
      if (view === 'developers') {renderDevelopers(); if (developerId) await loadSite();}
    }
    return status;
  }
  async function scan(id=null, mode='incremental') {
    const scope=id ? `「${developers.find(d=>d.id===id)?.name || '厂商 '+id}」` : '全部厂商';
    if (mode==='full' && !confirm(`确定对${scope}进行全量同步吗？\n\n全量同步会重新扫描全部评价、帖子与回复，可能需要数分钟到数十分钟。日常的定时同步均为增量，通常不需要手动全量同步。`)) return;
    $('#scan').disabled=$('#scan-full').disabled=true;
    try {
      const params=new URLSearchParams({mode}); if (id) params.set('developer_id',id);
      const result=await api('scan?'+params,'POST');
      notify(`${mode==='full'?'全量':'增量'}同步任务 #${result.run_id} 已开始，完成后数据会自动刷新。`); await loadStatus();
    } finally {$('#scan').disabled=$('#scan-full').disabled=!!status?.collecting;}
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
    const ticket=++statsTicket, params=overviewQuery(), latestParams=new URLSearchParams(params), metricParams=new URLSearchParams(params);
    params.set('days',days); latestParams.set('limit','6'); metricParams.set('days',Math.max(7,days));
    const [data,latest,metrics]=await Promise.all([api('stats?'+params),api('items/search?'+latestParams),api('metrics?'+metricParams)]);
    if (ticket !== statsTicket) return;
    overviewData=data; coreData=metrics;
    $('#view-subtitle').textContent=`监测 ${number(data.totals.developers)} 个厂商 · ${number(data.totals.games)} 款作品`;
    const t=data.totals, reviewCount=Object.values(data.score_distribution).reduce((a,b)=>a+b,0);
    const since=metrics.days[0], rows=metrics.games.flatMap(g=>(g.console_daily||[]).filter(r=>r.day>=since));
    const has=key=>rows.some(r=>r[key]!=null), sum=key=>rows.reduce((total,r)=>total+(r[key]||0),0);
    const byDay=key=>{const map=new Map(); rows.forEach(r=>{if (r[key]!=null) map.set(r.day,(map.get(r.day)||0)+r[key]);}); return [...map.entries()].sort((a,b)=>a[0]<b[0]?-1:1).map(([,v])=>v);};
    const feedbackDaily=data.daily.map(day=>KINDS.reduce((total,k)=>total+(day[k]||0),0));
    const scoreMeter=el('div','meter'), fill=el('i'); fill.style.width=`${(t.average_score||0)/5*100}%`; scoreMeter.append(fill);
    const cards=[];
    if (has('impressions')) {
      const shown=sum('impressions'), visits=sum('detail_visits'), launches=sum('launches');
      cards.push(kpi('曝光',number(shown),[`点击率 ${rate(shown?visits/shown:null)}`,`详情页 ${number(visits)}`],AdminCharts.sparkline(byDay('impressions'))),
        kpi('转化（启动）',number(launches),[`详情页转化率 ${rate(visits?launches/visits:null)}`],AdminCharts.sparkline(byDay('launches'))));
    }
    if (has('ad_revenue')) cards.push(kpi('广告收益',yuan(sum('ad_revenue')),['平台预估'],AdminCharts.sparkline(byDay('ad_revenue'))));
    if (cards.length<2) cards.push(kpi('评价与帖子',number(t.items),[`评价 ${number(data.counts.review)}`,`帖子 ${number(data.counts.topic)}`]));
    cards.push(kpi('新增反馈',number(feedbackDaily.reduce((a,b)=>a+b,0)),[delta(t.recent_7_days,t.previous_7_days),`近 7 天 ${number(t.recent_7_days)}`],AdminCharts.sparkline(feedbackDaily)),
      kpi('玩家均分',t.average_score==null?'—':[(t.average_score*2).toFixed(1),'/10'],[`基于 ${number(reviewCount)} 条评价`],scoreMeter));
    $('#kpis').replaceChildren(...cards); $('#kpis').dataset.count=String(cards.length);
    const single=!!$('#overview-game').value;
    $('#games-card').hidden=single; $('#core-card').hidden=!single;
    if (single) renderCore(); else renderGameOverview(data.games, metrics);
    drawConsoleCharts();
    AdminCharts.trend($('#trend'),data.daily); AdminCharts.scores($('#scores'),data.score_distribution,t.average_score);
    renderLatest(latest.items);
  }
  async function openGame(appId) {
    if (![...$('#overview-game').options].some(o=>o.value===String(appId))) {$('#overview-developer').value=''; await gameOptions($('#overview-developer'),$('#overview-game'));}
    $('#overview-game').value=String(appId); await loadOverview(); scrollTo({top:0});
  }
  function renderGameOverview(statsGames, metrics) {
    const host=$('#game-overview'), since=metrics.days[0], info=new Map(statsGames.map(g=>[g.app_id ?? g.id,g]));
    $('#core-auth').hidden=!metrics.games.length || metrics.games.some(g=>g.official);
    if (!metrics.games.length) {AdminCharts.empty(host,'暂无作品，添加厂商并同步后可查看。'); return;}
    const rows=metrics.games.map(game=>{
      const daily=(game.console_daily||[]).filter(r=>r.day>=since).sort((a,b)=>a.day<b.day?-1:1), s=info.get(game.app_id)||{};
      const total=key=>daily.some(r=>r[key]!=null)?daily.reduce((sum,r)=>sum+(r[key]||0),0):null;
      const last=key=>{for (let i=daily.length-1;i>=0;i--) if (daily[i][key]!=null) return daily[i][key]; return null;};
      const shown=total('impressions'), visits=total('detail_visits'), launches=total('launches');
      return {game, icon:game.icon || s.icon, shown, ctr:shown?visits/shown:null, launches, cvr:visits?launches/visits:null,
        active:last('active_devices'), retention:last('retention_1d'), revenue:total('ad_revenue'),
        score:game.summary?.rating_score ?? s.rating ?? null, feedback:s.main_count ?? (s.counts?KINDS.reduce((sum,k)=>sum+(s.counts[k]||0),0):null)};
    });
    const columns=[['曝光','shown',number],['点击率','ctr',rate],['启动','launches',number],['启动转化率','cvr',rate],['活跃设备','active',number],
      ['次日留存','retention',rate],['广告收益','revenue',yuan],['评分','score',v=>Number(v).toFixed(1)],['反馈','feedback',number]].filter(([,key])=>rows.some(r=>r[key]!=null));
    const table=el('table','game-table'), head=el('tr'), thead=el('thead'), body=el('tbody');
    [['作品'],...columns].forEach(([name],i)=>{const th=el('th',i?'num':'',name); th.scope='col'; head.append(th);});
    thead.append(head); table.append(thead,body);
    rows.forEach(row=>{
      const tr=el('tr','clickable'), cell=el('div','game-cell'), name=el('div'), td=el('td');
      name.append(el('strong','',row.game.title),el('span','muted small',row.game.official?'官方数据':'公开数据'));
      cell.append(image(row.icon,'',36),name); td.append(cell); tr.append(td);
      columns.forEach(([,key,format])=>tr.append(el('td','num',row[key]==null?'—':format(row[key]))));
      tr.tabIndex=0; tr.title='查看作品详情';
      const open=guarded(()=>openGame(row.game.app_id)); tr.addEventListener('click',open); tr.addEventListener('keydown',e=>{if (e.key==='Enter') open(e);});
      body.append(tr);
    });
    const scroller=el('div','table-scroll'); scroller.append(table); host.replaceChildren(scroller);
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
    if (KINDS.includes(item.kind)) main.append(replyToggle(item));
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
    if (KINDS.includes(item.kind) && replyTotal(item)) {const host=el('div','replies'); body.append(el('h3','replies-title',`回复 ${number(replyTotal(item))}`),host); loadReplies(item,host).catch(error=>host.replaceChildren(el('p','error',error.message)));}
    $('#item-dialog').showModal();
  }
  /* 回复挂在评价/帖子下面：点击回复数展开 */
  const replyTotal=item=>Math.max(item.source_reply_count || 0, item.reply_count || 0);
  function replyToggle(item) {
    const wrap=el('div','reply-wrap'), total=replyTotal(item), toggle=el('button','reply-toggle'); toggle.type='button';
    toggle.innerHTML='<svg><use href="#i-reply"/></svg>'; toggle.append(el('span','',number(total)));
    toggle.setAttribute('aria-label',`${number(total)} 条回复`); toggle.setAttribute('aria-expanded','false'); toggle.disabled=!total;
    const host=el('div','replies'); host.hidden=true;
    toggle.addEventListener('click',guarded(async event=>{
      event.stopPropagation(); const open=host.hidden; host.hidden=!open; toggle.setAttribute('aria-expanded',String(open));
      if (open && !host.childElementCount) {host.append(el('p','muted small','正在加载回复…')); try {await loadReplies(item,host);} catch(error) {host.replaceChildren(el('p','error',error.message));}}
    }));
    host.addEventListener('click',event=>event.stopPropagation()); host.addEventListener('keydown',event=>event.stopPropagation());
    wrap.append(toggle,host); return wrap;
  }
  async function loadReplies(item, host, offset=0) {
    const data=await api(`items/${item.app_id}/${item.kind}/${encodeURIComponent(item.source_id)}/replies?limit=50&offset=${offset}`);
    if (!offset) host.replaceChildren();
    host.querySelector('.more-replies')?.remove();
    if (!data.items.length && !offset) {host.append(el('p','muted small',item.source_reply_count?`TapTap 显示 ${number(item.source_reply_count)} 条回复，尚未收录，下次同步会补全。`:'暂无回复。')); return;}
    data.items.forEach(reply=>{
      const row=el('div','reply'), head=el('div','reply-head');
      head.append(avatar(reply.author),el('strong','',reply.author || '匿名'));
      if (reply.official) head.append(el('span','official','官方'));
      head.append(el('span','when',date(reply.published_at)));
      row.append(head,el('p','',reply.body || '（无文字内容）')); host.append(row);
    });
    const shown=offset+data.items.length;
    if (shown<data.total) host.append(button(`加载更多回复（还有 ${number(data.total-shown)} 条）`,()=>loadReplies(item,host,shown),'ghost more-replies'));
    else if (item.source_reply_count>data.total) host.append(el('p','muted small more-replies',`已收录 ${number(data.total)} 条，TapTap 显示 ${number(item.source_reply_count)} 条，其余会在后续同步中补全。`));
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
      if (developer.is_primary) info.firstChild.append(el('span','official primary-badge','默认'));
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

  /* ---------- 数据源：采集记录 ---------- */
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
      const table=el('table','runs'), head=el('tr'); ['运行','范围','方式','开始时间','耗时','状态','新增 / 修改','异常'].forEach(h=>{const th=el('th','',h); th.scope='col'; head.append(th);});
      const thead=el('thead'); thead.append(head); const body=el('tbody'); table.append(thead,body);
      runs.forEach(run=>{
        const row=el('tr'), td=(v,c='')=>{const x=el('td',c); if (v instanceof Node) x.append(v); else x.textContent=v; row.append(x);};
        td('#'+run.id,'nowrap'); td(run.developer_id ? developers.find(d=>d.id===run.developer_id)?.name || '厂商 '+run.developer_id : '全部厂商');
        const mode=el('span','chip '+(run.mode==='full'?'running':'muted'),run.mode==='full'?'全量':'增量'); td(mode);
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
      const wording=stream.status==='pending'?'已保存分页进度，下轮继续采集':stream.status==='restricted'?'部分楼中楼暂时只能获取公开预览，下次同步会重试':stream.error || '数据源异常';
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
  const INSTALL='git clone https://github.com/Mirage-Cbq/TaptapMonitor.git /tmp/TaptapMonitor && sudo sh /tmp/TaptapMonitor/scripts/install.sh';
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

  /* ---------- 核心数据（官方优先，公开数据兜底） ---------- */
  const observed=(rows,key)=>rows.filter(r=>r[key]!=null);
  // 从第一次观测开始画线，避免把尚未记录的日子当成 0
  const series=(rows,key,days)=>{const map=new Map(rows.map(r=>[r.day,r[key]])), start=days.findIndex(d=>map.get(d)!=null); let prev=null; return start<0?[]:days.slice(start).map(d=>{const v=map.get(d); if (v!=null) prev=v; return prev;});};
  const spanDays=(a,b)=>Math.round((Date.parse(b)-Date.parse(a))/86400000);
  const relative=values=>{const min=Math.min(...values); return values.map(v=>v-min);};  // 累计值以区间最小值为基线，才能看出变化
  const increments=(rows,key,days)=>{const map=new Map(rows.map(r=>[r.day,r[key]])); let prev=null; return days.map(d=>{const v=map.get(d); if (v==null) return 0; const out=prev==null?0:Math.max(0,v-prev); prev=v; return out;});};
  function figure(label, value, spark, sub) {
    const node=el('div','core-fig'); node.append(el('span','label',label),el('strong','',value));
    if (sub) node.append(el('span','muted small',sub)); if (spark) node.append(spark); return node;
  }
  function renderCore() {
    const data=coreData, host=$('#core-games'); host.replaceChildren();
    $('#core-auth').hidden=!data.games.length || data.games.some(g=>g.official);
    if (!data.games.length || data.games.every(g=>!g.official && !g.public_daily.length && !hasConsole(g))) {AdminCharts.empty(host,'数据会在下一次同步后出现。'); $('#core-trend').replaceChildren(); return;}
    data.games.forEach(game=>{
      const card=el('article','core-game'), head=el('div','core-head'), figs=el('div','core-figs');
      head.append(image(game.icon,'',36),el('strong','',game.title),el('span','chip '+(game.official?'ok':'muted'),game.official?'官方':'公开'));
      if (game.official) {
        const s=game.summary, d=game.official_daily, pub=game.public_daily||[];
        const val=(...keys)=>keys.map(k=>s[k]).find(v=>v!=null);
        // 官方趋势是每日新增（非累计），直接画；公开数据是累计值，以区间最小值为基线
        const daily=key=>{const map=new Map(d.map(r=>[r.day,r[key]])); if (!d.some(r=>r[key]!=null)) return null; const line=data.days.map(day=>map.get(day)||0); return {line,total:line.reduce((a,b)=>a+b,0)};};
        const cumul=key=>{const line=series(pub,key,data.days); return line.length>1?AdminCharts.sparkline(relative(line)):null;};
        const fav=daily('favorite_trend_value'), fans=val('fans_count','favorite_count');
        const reviews=[['好评',s.positive_review_count],['中评',s.neutral_review_count],['差评',s.negative_review_count]].filter(([,v])=>v!=null);
        const downloads=val('download_count'), reserves=val('reserve_count');
        const third=downloads>0 || reserves==null
          ? figure('下载量',downloads==null?'—':number(downloads),null,s.play_total?`游玩 ${number(s.play_total)}`:'')
          : figure('预约量',number(reserves),cumul('reserve_count'),s.wish_count!=null?`想玩 ${number(s.wish_count)}`:'');
        figs.append(figure('关注',fans==null?'—':number(fans),fav&&fav.total?AdminCharts.sparkline(fav.line):cumul('fans_count'),fav?`近 ${data.days.length} 天 +${number(fav.total)}`:''),
          figure('浏览量',val('hits_total','page_view_count')==null?'—':number(val('hits_total','page_view_count')),cumul('hits_total')),
          third,
          figure('官方评分',s.rating_score==null?'暂无':Number(s.rating_score).toFixed(1)+' /10',null,
            reviews.length?reviews.map(([n,v])=>`${n} ${number(v)}`).join(' · '):(s.review_count!=null?`评价 ${number(s.review_count)}`:'')));
        head.append(el('span','muted small',`官方数据更新于 ${ago(game.official_fetched_at)}`));
      } else {
        const d=game.public_daily, fig=(label,key,format=number)=>{
          const rows=observed(d,key), first=rows[0], last=rows[rows.length-1], span=first&&last?spanDays(first.day,last.day):0;
          const diff=span?last[key]-first[key]:null, line=series(d,key,data.days);
          return figure(label,last?format(last[key]):'—',key!=='rating' && line.length>1?AdminCharts.sparkline(relative(line)):null,diff==null||key==='rating'?'':`近 ${span} 天 ${diff>=0?'+':''}${number(diff)}`);
        };
        figs.append(fig('关注','fans_count'),fig('浏览量','hits_total'),fig('评价数','review_count'),fig('TapTap 评分','rating',v=>Number(v).toFixed(1)+' /10'));
      }
      card.append(head,figs);
      if (hasConsole(game)) card.append(...consoleFigures(game));
      host.append(card);
    });
    drawCoreTrend();
  }
  /* 开发者后台：曝光转化漏斗、活跃留存、广告收益（按中国日历日） */
  const hasConsole=g=>(g.console_daily||[]).some(r=>r.impressions!=null || r.ad_revenue!=null || r.active_devices!=null);
  const yuan=v=>v==null?'—':'¥'+Number(v).toLocaleString('zh-CN',{minimumFractionDigits:2,maximumFractionDigits:2});
  const rate=v=>v==null?'—':(v*100).toFixed(1)+'%';
  const chinaToday=()=>new Date(Date.now()+8*3600000).toISOString().slice(0,10);
  const prevDay=day=>new Date(Date.parse(day+'T00:00:00Z')-86400000).toISOString().slice(0,10);
  const dayName=day=>day===chinaToday()?'今日':day===prevDay(chinaToday())?'昨日':day.slice(5).replace('-','/');
  function consoleFigures(game) {
    const rows=(game.console_daily||[]).slice().sort((a,b)=>a.day<b.day?-1:1), byDay=new Map(rows.map(r=>[r.day,r]));
    const latest=key=>{for (let i=rows.length-1;i>=0;i--) if (rows[i][key]!=null) return rows[i]; return null;};
    const spark=key=>{const line=rows.filter(r=>r[key]!=null).map(r=>r[key]); return line.length>1?AdminCharts.sparkline(line):null;};
    const change=(row,key,format=number,relative=true)=>{
      const before=row&&byDay.get(prevDay(row.day))?.[key]; if (before==null) return '';
      const delta=relative&&before?Math.round((row[key]-before)/before*100):null;
      return `${delta==null?'':(delta>=0?'+':'')+delta+'% · '}前一日 ${format(before)}`;
    };
    const count=(label,key)=>{const row=latest(key); return figure(row?`${dayName(row.day)}${label}`:label,row?number(row[key]):'—',spark(key),change(row,key));};
    const ratio=(label,key)=>{const row=latest(key); return figure(label,row?rate(row[key]):'—',null,row?[dayName(row.day),change(row,key,rate,false)].filter(Boolean).join(' · '):'');};
    const nodes=[];
    if (rows.some(r=>r.impressions!=null)) {
      const funnel=el('div','core-figs funnel');
      funnel.append(count('曝光','impressions'),ratio('商店页点击率','click_rate'),count('访问详情页','detail_visits'),ratio('详情页启动转化率','launch_rate'),count('转化（启动）','launches'));
      const total=key=>rows.reduce((sum,r)=>sum+(r[key]||0),0), span=rows.filter(r=>r.impressions!=null).length;
      funnel.append(figure(`近 ${span} 天曝光`,number(total('impressions')),null,`启动 ${number(total('launches'))} · 整体转化 ${rate(total('impressions')?total('launches')/total('impressions'):null)}`));
      nodes.push(el('span','core-sub','曝光转化漏斗'),funnel);
    }
    const extra=el('div','core-figs');
    if (rows.some(r=>r.ad_revenue!=null)) {
      const row=latest('ad_revenue'), month=rows.filter(r=>r.ad_revenue!=null);
      extra.append(figure(`${dayName(row.day)}广告收益`,yuan(row.ad_revenue),spark('ad_revenue'),change(row,'ad_revenue',yuan)),
        figure(`近 ${month.length} 天广告收益`,yuan(month.reduce((sum,r)=>sum+r.ad_revenue,0)),null,'预估收益，以每月结算为准'));
    }
    if (rows.some(r=>r.active_devices!=null)) {
      const row=latest('active_devices');
      extra.append(figure(`${dayName(row.day)}活跃设备`,number(row.active_devices),spark('active_devices'),row.new_devices!=null?`新增 ${number(row.new_devices)}`:''));
      const ret=latest('retention_1d'), ret7=latest('retention_7d');
      extra.append(figure('次日留存',ret?rate(ret.retention_1d):'—',null,[ret?dayName(ret.day)+'新增':'',ret7?`7 日留存 ${rate(ret7.retention_7d)}`:''].filter(Boolean).join(' · ')));
    }
    if (extra.childElementCount) nodes.push(el('span','core-sub','广告收益与活跃'),extra);
    return nodes;
  }
  function drawCoreTrend() {
    const data=coreData, host=$('#core-trend'); if (!data) return;
    if (data.games.every(g=>!g.official_daily.length && g.public_daily.length<2)) {host.replaceChildren(); return;}
    const official=data.games.filter(g=>g.official && g.official_daily.length);
    if (official.length) {
      // 官方接口按天返回的是新增量：关注、好评、差评、下载请求
      const names=[['favorite_trend_value','新增关注','var(--c-topic)'],['positive_count','新增好评','var(--ok)'],['negative_count','新增差评','var(--danger)'],['download_request_count','下载请求','var(--warn)']];
      const use=names.filter(([k])=>official.some(g=>g.official_daily.some(r=>r[k])));
      const daily=data.days.map(day=>{const row={date:day}; use.forEach(([k])=>{row[k]=official.reduce((sum,g)=>sum+(g.official_daily.find(r=>r.day===day)?.[k]||0),0);}); return row;});
      AdminCharts.trend(host,daily,{series:use.length?use:names.slice(0,1),unit:'',label:'每日官方数据',emptyText:'官方数据暂无每日趋势。'});
    } else {
      const sum=key=>data.days.map((day,i)=>data.games.reduce((total,g)=>total+increments(g.public_daily,key,data.days)[i],0));
      const fans=sum('fans_count'), reviews=sum('review_count');
      AdminCharts.trend(host,data.days.map((day,i)=>({date:day,fans:fans[i],reviews:reviews[i]})),
        {series:[['fans','新增关注','var(--c-review)'],['reviews','新增评价','var(--c-topic)']],unit:'',label:'每日公开数据增量',emptyText:'公开数据需要累计两天以上才会显示每日增量。'});
    }
  }

  function drawConsoleCharts() {
    const wrap=$('#console-charts'), data=coreData; if (!data) return;
    const since=data.days[0], games=data.games.filter(g=>(g.console_daily||[]).some(r=>r.day>=since && (r.launches!=null || r.ad_revenue!=null)));
    wrap.hidden=!games.length; if (!games.length) return;
    const axis=[...new Set(games.flatMap(g=>g.console_daily.map(r=>r.day)))].filter(day=>day>=since).sort();
    const palette=['var(--c-review)','var(--c-topic)','var(--c-review_comment)','var(--c-topic_comment)','var(--danger)','var(--warn)'];
    const chart=(host,key,label,unit,emptyText)=>{
      const owners=games.filter(g=>g.console_daily.some(r=>r.day>=since && r[key]!=null));
      if (!owners.length) {AdminCharts.empty(host,emptyText); return;}
      const series=owners.map(g=>['g'+g.app_id,g.title,palette[games.indexOf(g)%palette.length]]);
      const daily=axis.map(day=>{const row={date:day}; owners.forEach(g=>{row['g'+g.app_id]=g.console_daily.find(r=>r.day===day)?.[key]||0;}); return row;});
      AdminCharts.trend(host,daily,{series,unit,label,emptyText});
    };
    chart($('#launch-trend'),'launches','每日转化（启动）',' 次','暂无启动数据。');
    chart($('#revenue-trend'),'ad_revenue','每日广告收益',' 元','暂无广告收益（作品需开通广告变现）。');
  }

  /* ---------- 官方数据授权 ---------- */
  async function loadOfficial() {
    clearTimeout(officialTimer);
    const [data,consoleData]=await Promise.all([api('official'),api('console')]); renderOfficial(data); renderConsole(consoleData);
    const waiting=data.auth.status==='waiting' || data.syncing || consoleData.syncing || ['waiting','scanned','connecting'].includes(consoleData.auth?.status);
    if (waiting && view==='sources') officialTimer=setTimeout(()=>loadOfficial().catch(error=>notify(error.message)),3000);
  }
  function renderOfficial(data) {
    const host=$('#official-panel'), nodes=[];
    if (!data.installed) {
      const box=el('div','sub-card'); box.append(el('h3','','未安装官方数据组件'),el('p','muted','官方数据需要服务器上的 Node.js 组件。请在服务器上重新运行安装脚本（数据与配置会保留）：'),el('pre','command','sudo sh /opt/taptap-monitor/taptap-monitor/scripts/install.sh'));
      host.replaceChildren(...nodes,box); return;
    }
    if (!data.authorized) {
      const box=el('div','sub-card official-auth');
      box.append(el('h3','','授权 TapTap 官方数据'),el('p','muted','授权后可获得下载量、曝光、预约、官方评分与评价趋势等公开页面上看不到的数据。只需用 TapTap App 扫码，不需要密码，授权信息只保存在你的服务器上，可随时解绑。'));
      const auth=data.auth;
      if (auth.status==='waiting' && /^data:image\/svg\+xml;base64,/.test(auth.qr || '')) {
        const qr=el('div','qr-box'), img=el('img'); img.src=auth.qr; img.alt='TapTap 授权二维码'; img.width=img.height=200; qr.append(img);
        const left=Math.max(0,Math.round((auth.expires_at || 0)-Date.now()/1000));
        box.append(qr,el('p','muted small',`请使用 TapTap App 扫码并确认授权 · 二维码 ${Math.floor(left/60)} 分 ${left%60} 秒后过期`));
        if (safeUrl(auth.auth_url) || /^https:\/\/[\w.-]*taptap\.(cn|com)\//.test(auth.auth_url || '')) {const a=el('a','link','在手机上打开授权链接 ↗'); a.href=auth.auth_url; a.target='_blank'; a.rel='noopener noreferrer'; box.append(a);}
      } else {
        if (auth.message && auth.status!=='idle') box.append(el('p',auth.status==='failed'||auth.status==='expired'?'error':'muted',auth.message));
        const actions=el('div','actions'); actions.append(button(auth.status==='expired'||auth.status==='failed'?'重新获取二维码':'扫码授权',async()=>{renderOfficial(await api('official/auth','POST')); await loadOfficial();},'primary')); box.append(actions);
      }
      if (data.error) box.append(el('p','error',data.error));
      host.replaceChildren(...nodes,box); return;
    }
    const box=el('div','sub-card'), head=el('div','official-account');
    if (data.account?.avatar) head.append(image(data.account.avatar,'',40));
    const who=el('div'); who.append(el('strong','',data.account?.name || 'TapTap 开发者账号'),el('span','muted small',data.syncing?'正在同步官方数据…':`最近同步：${data.synced_at?date(data.synced_at):'尚未同步'}`)); head.append(who,el('span','chip ok','已授权'));
    const actions=el('div','actions');
    actions.append(button(data.syncing?'同步中…':'立即同步',async()=>{renderOfficial(await api('official/sync','POST')); await loadOfficial();},'primary'),
      button('解绑',async()=>{if (!confirm('确定解绑 TapTap 官方数据吗？已同步的历史数据会保留，之后将改用公开数据。')) return; renderOfficial(await api('official/auth','DELETE')); notify('已解绑官方数据。');},'ghost'));
    if (data.syncing) actions.firstChild.disabled=true;
    box.append(el('h3','','TapTap 官方数据'),el('p','muted small','对整个系统生效：授权账号下的作品使用官方开放接口数据（下载、预约、评分趋势等），其余作品使用公开数据。'),head,actions);
    if (data.error) box.append(el('p','error',data.error));
    const list=el('div','official-apps');
    if (!data.apps.length) list.append(el('p','muted','该账号下没有可用的作品。'));
    data.apps.slice().sort((a,b)=>b.monitored-a.monitored).forEach(app=>{
      const row=el('div','official-app'); row.append(el('strong','',app.title),el('span','muted small',app.developer_name),el('span','chip '+(app.monitored?'ok':'muted'),app.monitored?'已监测':'未监测')); list.append(row);
    });
    const apps=el('details','app-list'); apps.append(el('summary','muted small',`授权账号下的作品（${data.apps.length}）`),list,el('p','muted small','只有同时在监测列表中的作品才会同步官方数据；未监测的作品可在「厂商与官网」添加其厂商。'));
    box.append(apps);
    host.replaceChildren(...nodes,box);
  }

  /* ---------- 开发者后台数据（Cookie 会话） ---------- */
  function renderConsole(data) {
    const host=$('#console-panel'), box=el('div','sub-card console-card'), auth=data.auth || {};
    box.append(el('h3','','开发者后台：曝光漏斗 · 广告收益 · 活跃留存'),
      el('p','muted small','官方开放接口不提供曝光、商店页点击率、启动转化和广告收益。用 TapTap App 扫码登录开发者后台后，系统每 2 小时读取一次后台页面的数据。登录信息只保存在服务器的数据库中，不会返回到浏览器；后台会话过期时会自动续期，续不上才需要重新扫码。这些是后台内部接口，改版后可能失效。'));
    if (data.configured) {
      const head=el('div','official-account'), who=el('div');
      who.append(el('strong','',data.account?.name || '开发者后台账号'),el('span','muted small',data.syncing?'正在同步开发者后台数据…':`最近同步：${data.synced_at?date(data.synced_at):'尚未同步'}${data.method==='qr'?' · 扫码登录':data.method==='cookie'?' · Cookie':''}`));
      head.append(who,el('span','chip '+(data.valid?'ok':'muted'),data.valid?'已连接':'已失效')); box.append(head);
      const actions=el('div','actions');
      if (data.valid) {
        const sync=button(data.syncing?'同步中…':'立即同步',async()=>{renderConsole(await api('console/sync','POST')); await loadOfficial();},'primary');
        const full=button('重新获取全部历史',async()=>{if (!confirm('将从各作品上线日起重新获取全部开发者后台数据，可能需要一两分钟。确定吗？')) return; renderConsole(await api('console/sync?full=true','POST')); await loadOfficial();},'ghost');
        sync.disabled=full.disabled=data.syncing; actions.append(sync,full);
      }
      actions.append(button('退出登录',async()=>{if (!confirm('确定退出开发者后台登录吗？已同步的数据会保留。')) return; renderConsole(await api('console','DELETE')); notify('已退出开发者后台登录。');},'ghost'));
      box.append(actions);
    }
    if (data.error) box.append(el('p','error',data.error));
    if (!data.configured || !data.valid) {
      const login=el('div','official-auth');
      if (['waiting','scanned'].includes(auth.status) && /^data:image\/svg\+xml;base64,/.test(auth.qr || '')) {
        const qr=el('div','qr-box'), img=el('img'); img.src=auth.qr; img.alt='TapTap 登录二维码'; img.width=img.height=200; qr.append(img);
        const left=Math.max(0,Math.round((auth.expires_at || 0)-Date.now()/1000));
        login.append(qr,el('p',auth.status==='scanned'?'':'muted small',auth.status==='scanned'?auth.message:`${auth.message} · 二维码 ${Math.floor(left/60)} 分 ${left%60} 秒后过期`));
      } else if (auth.status==='connecting') {
        login.append(el('p','muted',auth.message));
      } else {
        if (auth.message && auth.status!=='idle') login.append(el('p',auth.status==='success'?'muted':'error',auth.message));
        const actions=el('div','actions'); actions.append(button(auth.status==='expired'||auth.status==='failed'?'重新获取二维码':'扫码登录开发者后台',async()=>{renderConsole(await api('console/login','POST')); await loadOfficial();},'primary'));
        login.append(actions);
      }
      const manual=el('details','console-manual'), summary=el('summary','muted small','备用：手动粘贴 Cookie');
      const input=el('textarea'); input.rows=3; input.maxLength=16384; input.autocomplete='off'; input.spellcheck=false;
      input.placeholder='在已登录的 developer.taptap.cn 按 F12 → Network，刷新后点任意 /api/ 请求，复制请求头里的 Cookie 粘贴到这里';
      const save=button('保存并连接',async()=>{
        if (!input.value.trim()) throw new Error('请先粘贴 Cookie');
        const result=await api('console','PUT',{cookie:input.value.trim()}); input.value=''; renderConsole(result); notify('已连接开发者后台，正在同步数据。'); await loadOfficial();
      },'ghost');
      manual.append(summary,input,save);
      box.append(login,manual);
    }
    if (data.apps?.length) {
      const list=el('div','official-apps');
      data.apps.slice().sort((a,b)=>b.monitored-a.monitored).forEach(app=>{const row=el('div','official-app'); row.append(el('strong','',app.title),el('span','muted small',app.developer_name),el('span','chip '+(app.monitored?'ok':'muted'),app.monitored?'已监测':'未监测')); list.append(row);});
      const apps=el('details','app-list'); apps.append(el('summary','muted small',`后台账号下的作品（${data.apps.length}）`),list); box.append(apps);
    }
    host.replaceChildren(box);
  }

  /* ---------- 数据洞察 ---------- */
  const pct=v=>v==null?'—':(v*100).toFixed(v<0.1&&v>0?1:0)+'%';
  const ten1=v=>v==null?'—':(v*2).toFixed(1);
  async function loadInsights() {
    const ticket=++insightTicket, params=new URLSearchParams({days:insightDays});
    if ($('#insight-developer').value) params.set('developer_id',$('#insight-developer').value);
    if ($('#insight-game').value) params.set('app_id',$('#insight-game').value);
    const data=await api('insights?'+params); if (ticket!==insightTicket) return;
    insightData=data; renderInsights();
  }
  function renderInsights() {
    const data=insightData, r=data.rating, resp=data.response;
    $('#insight-alert').hidden=!r.alert; $('#insight-alert').textContent=r.alert ? '⚠ '+r.alert : '';
    const meter=v=>{const m=el('div','meter'), f=el('i'); f.style.width=`${(v||0)*100}%`; m.append(f); return m;};
    $('#insight-kpis').replaceChildren(
      kpi('玩家均分',r.mean==null?'—':[ten1(r.mean),'/10'],[`${number(r.count)} 条带评分的评价`]),
      kpi('好评率',pct(r.positive_rate),[`Wilson 下界 ${pct(r.wilson_lower)}`],meter(r.positive_rate)),
      kpi('差评率',pct(r.negative_rate),[r.recent_mean==null?'近 7 天暂无评分':`近 7 天均分 ${ten1(r.recent_mean)}`,r.previous_mean==null?'':`前 7 天 ${ten1(r.previous_mean)}`]),
      kpi('官方回复率',pct(resp.official_reply_rate),[`${number(resp.replied)} / ${number(resp.total)} 条评价`,resp.median_hours==null?'':`中位响应 ${resp.median_hours<1?Math.round(resp.median_hours*60)+' 分钟':resp.median_hours.toFixed(1)+' 小时'}`]),
    );
    drawWeekly();
    const rating=$('#insight-rating'); rating.replaceChildren();
    if (!r.count) AdminCharts.empty(rating,'时间范围内暂无带评分的评价。');
    else {
      const rows=[['玩家均分',ten1(r.mean)+' /10',`${number(r.count)} 条评价的平均分`],
        ['好评 / 差评',`${pct(r.positive_rate)} / ${pct(r.negative_rate)}`,'好评 8–10 分，差评 2–4 分'],
        ['好评率下界',pct(r.wilson_lower),`95% 置信下，真实好评率至少为该值（好评率 ${pct(r.positive_rate)}）`],
        ['近 7 天 vs 前 7 天',`${ten1(r.recent_mean)} / ${ten1(r.previous_mean)}`,'均分下降 1 分（十分制）以上或差评率超过 40% 会触发预警']];
      rows.forEach(([k,v,hint])=>{const row=el('div','rating-row'); row.append(el('span','muted',k),el('strong','',v),el('span','muted small',hint)); rating.append(row);});
    }
    const words=(host,list,tone)=>{
      host.replaceChildren(); if (!list.length) {AdminCharts.empty(host,'样本不足，暂无显著关键词。'); return;}
      const max=Math.max(...list.map(w=>w.count));
      list.forEach(w=>{const row=el('div','kw '+tone), bar=el('i'); bar.style.width=`${w.count/max*100}%`; row.title=`出现在 ${w.count} 条评价中，相对倍数 ${w.lift.toFixed(1)}`; row.append(el('span','term',w.term),el('span','bar-track'),el('span','count',`${w.count} · ×${w.lift.toFixed(1)}`)); row.children[1].append(bar); host.append(row);});
    };
    words($('#kw-negative'),data.keywords.negative,'neg'); words($('#kw-positive'),data.keywords.positive,'pos');
    const anomalies=$('#insight-anomalies'); anomalies.replaceChildren();
    if (!data.anomalies.length) AdminCharts.empty(anomalies,'时间范围内没有发现异常波动。');
    else {const list=el('div','anomaly-list'); data.anomalies.slice().reverse().forEach(a=>{const row=el('div','anomaly '+a.type); row.append(el('strong','',a.day),el('span','chip '+(a.type==='negative'?'error':'partial'),a.type==='negative'?'差评激增':'反馈激增'),el('span','',`${number(a.count)} 条，常态约 ${a.baseline.toFixed(1)} 条`),el('span','muted small',`偏离指数 ${a.score.toFixed(1)}`)); list.append(row);}); anomalies.append(list);}
    const response=$('#insight-response'); response.replaceChildren();
    if (!resp.total) AdminCharts.empty(response,'时间范围内暂无评价。');
    else {const big=el('div','score-summary'); big.append(el('strong','',pct(resp.official_reply_rate)),el('span','muted',`已回复 ${number(resp.replied)} / ${number(resp.total)}`)); response.append(big,meter(resp.official_reply_rate),
      el('p','muted small',resp.median_hours==null?'暂无官方回复记录。':`从评价发布到首次官方回复的中位时长：${resp.median_hours<1?Math.round(resp.median_hours*60)+' 分钟':resp.median_hours.toFixed(1)+' 小时'}。及时回复差评通常有助于挽回口碑。`));}
    const hot=$('#insight-hot'); hot.replaceChildren();
    if (!data.hot_topics.length) AdminCharts.empty(hot,'暂无回复数据。');
    data.hot_topics.forEach(item=>{
      const text=item.title || item.excerpt || labels[item.kind], row=el('div','hot-item'), title=safeUrl(item.url)?link(text,item.url,'link'):el('span','',text);
      const info=el('div'); info.append(title,el('span','muted small',`${item.game_title || ''} · ${labels[item.kind]} · ${date(item.published_at)}`));
      const count=el('span','reply-count'); count.innerHTML='<svg><use href="#i-reply"/></svg>'; count.append(number(Math.max(item.reply_count||0,item.source_reply_count||0)));
      row.append(info,count); hot.append(row);
    });
    const growth=$('#insight-growth'); growth.replaceChildren();
    if (!data.growth.length) AdminCharts.empty(growth,'需要累计两天以上的数据。');
    else {
      const table=el('table','game-table'), head=el('tr'); ['作品','来源','关注','浏览','评价','下载'].forEach((h,i)=>{const th=el('th',i?'num':'',h); th.scope='col'; head.append(th);});
      const thead=el('thead'); thead.append(head); const body=el('tbody'); table.append(thead,body);
      const fmt=v=>v==null?'—':(v>0?'+':'')+number(v);
      data.growth.forEach(g=>{const tr=el('tr'); tr.append(el('td','',g.title),el('td','num',g.source==='official'?'官方':'公开'),el('td','num',fmt(g.fans_delta)),el('td','num',fmt(g.hits_delta)),el('td','num',fmt(g.reviews_delta)),el('td','num',fmt(g.downloads_delta))); body.append(tr);});
      const scroller=el('div','table-scroll'); scroller.append(table); growth.append(scroller);
    }
    $('#insight-methods').replaceChildren(...data.methods.map(m=>{const row=el('div','method'); row.append(el('strong','',m.name),el('p','muted small',m.description)); return row;}));
  }
  function drawWeekly() {
    const weekly=insightData?.weekly || [];
    const rows=weekly.map(w=>{const pos=Math.round(w.reviews*(w.positive_rate||0)), neg=Math.round(w.reviews*(w.negative_rate||0)); return {date:w.week_start,positive:pos,neutral:Math.max(0,w.reviews-pos-neg),negative:neg};});
    AdminCharts.trend($('#insight-weekly'),rows,{series:[['positive','好评','var(--ok)'],['neutral','中评','var(--c-review_comment)'],['negative','差评','var(--danger)']],label:'每周评价构成',emptyText:'时间范围内暂无带评分的评价。'});
  }

  /* ---------- 路由与事件 ---------- */
  async function route(focus=false) {
    view=location.hash.replace(/^#\//,'').split('/')[0];
    if (view==='runs') {location.hash='#/sources'; return;}  // 旧链接
    if (!views[view]) {location.hash='#/overview'; return;}
    $('#notice').hidden=true; clearTimeout(officialTimer);
    Object.keys(views).forEach(key=>{$('#view-'+key).hidden=key!==view; const nav=document.querySelector(`[data-view="${key}"]`); if (key===view) nav.setAttribute('aria-current','page'); else nav.removeAttribute('aria-current');});
    $('#view-title').textContent=views[view][0]; $('#view-subtitle').textContent=views[view][1]; document.title=views[view][0]+' · 作品监测管理'; if (focus) $('#view-title').focus();
    if ($('#workspace').hidden) return;
    if (view==='overview') await loadOverview();
    if (view==='data') await loadFeedback();
    if (view==='insights') await loadInsights();
    if (view==='developers') {await loadCatalog(); if (!developerId && developers.length) developerId=primaryId(); renderDevelopers(); if (developerId) await loadSite();}
    if (view==='sources') await Promise.all([loadRuns(),loadOfficial().catch(error=>$('#official-panel').replaceChildren(el('p','error',error.message)))]);
    if (view==='settings') {await loadSettings(); await loadUpgrade();}
  }
  async function connect() {
    await loadCatalog(); applyDefaults();
    try {if ($('#remember').checked || localStorage.getItem(KEY)) localStorage.setItem(KEY,token); else sessionStorage.setItem(KEY,token);} catch {}
    await Promise.all([gameOptions($('#overview-developer'),$('#overview-game')),gameOptions($('#filter-developer'),$('#filter-game')),gameOptions($('#insight-developer'),$('#insight-game'))]);
    $('#login').hidden=true; $('#workspace').hidden=false; $('#login-error').textContent=''; await loadStatus();
    if (!views[location.hash.replace(/^#\//,'')]) location.hash='#/'+view; else await route();
  }
  $('#login').addEventListener('submit',guarded(async()=>{token=$('#token').value.trim(); await connect();}));
  $('#logout').addEventListener('click',()=>{forget(); token=''; location.reload();});
  $('#scan').addEventListener('click',guarded(()=>scan()));
  $('#scan-full').addEventListener('click',guarded(()=>scan(null,'full')));
  $('#sync-site').addEventListener('click',guarded(async()=>{if (developerId) await scan(developerId);}));
  $('#dev-incremental').addEventListener('click',guarded(async()=>{if (developerId) await scan(developerId);}));
  $('#dev-full').addEventListener('click',guarded(async()=>{if (developerId) await scan(developerId,'full');}));
  $('#dev-delete').addEventListener('click',guarded(async()=>{
    const developer=developers.find(d=>d.id===developerId); if (!developer) return;
    if (!confirm(`确定删除厂商「${developer.name}」吗？\n\n将删除它的官网设置，以及只属于它的 ${number(developer.game_count)} 款作品和约 ${number(developer.feedback_count)} 条反馈等数据。此操作无法撤销。`)) return;
    const result=await api('developers/'+developerId,'DELETE');
    developerId=null; await loadCatalog(); if (developers.length) developerId=primaryId(); renderDevelopers(); if (developerId) await loadSite(); await loadStatus();
    notify(`已删除厂商「${developer.name}」，清理了 ${number(result.games)} 款作品、${number(result.items)} 条反馈。`);
  }));
  $('#core-auth').addEventListener('click',()=>{location.hash='#/sources';});
  $('#core-back').addEventListener('click',guarded(async()=>{$('#overview-game').value=''; await loadOverview();}));
  $('#insight-developer').addEventListener('change',guarded(async()=>{$('#insight-game').value=''; await gameOptions($('#insight-developer'),$('#insight-game')); await loadInsights();}));
  $('#insight-game').addEventListener('change',guarded(loadInsights));
  $('#insight-range').addEventListener('click',guarded(async event=>{const b=event.target.closest('button[data-days]'); if (!b) return; insightDays=Number(b.dataset.days); $('#insight-range').querySelectorAll('button').forEach(x=>x.setAttribute('aria-checked',String(x===b))); await loadInsights();}));
  $('#overview-developer').addEventListener('change',guarded(async()=>{$('#overview-game').value=''; await gameOptions($('#overview-developer'),$('#overview-game')); await loadOverview();}));
  $('#overview-game').addEventListener('change',guarded(loadOverview));
  $('#range').addEventListener('click',guarded(async event=>{const b=event.target.closest('button[data-days]'); if (!b) return; days=Number(b.dataset.days); $('#range').querySelectorAll('button').forEach(x=>x.setAttribute('aria-checked',String(x===b))); await loadOverview();}));
  $('#filters').addEventListener('submit',guarded(applyFilters)); $('#sort').addEventListener('change',guarded(applyFilters));
  $('#filter-developer').addEventListener('change',guarded(async()=>{$('#filter-game').value=''; await gameOptions($('#filter-developer'),$('#filter-game'));}));
  // 清空筛选后恢复为默认厂商
  $('#filters').addEventListener('reset',()=>{setTimeout(async()=>{try {const id=primaryId(); $('#filter-developer').value=id?String(id):''; await gameOptions($('#filter-developer'),$('#filter-game')); filterParams=readFilters(); offset=0; await loadFeedback();} catch(error) {notify(error.message);}},0);});
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
    if (!confirm(`添加厂商 ${id} 后会立即对它进行一次全量同步，获取全部作品、评价、帖子与回复，可能需要数分钟。确定添加吗？`)) return;
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
  window.addEventListener('resize',()=>{clearTimeout(resizeTimer); resizeTimer=setTimeout(()=>{
    if (view==='overview' && overviewData) {AdminCharts.trend($('#trend'),overviewData.daily); drawCoreTrend(); drawConsoleCharts();}
    if (view==='insights' && insightData) drawWeekly();
  },120);});
  async function poll() {
    if (document.hidden || $('#workspace').hidden || polling) return; polling=true;
    try {await loadStatus(); if (view==='sources') await loadRuns(false);}
    catch(error) {notify(error.message);} finally {polling=false;}
  }
  setInterval(poll,10000); document.addEventListener('visibilitychange',()=>{if (!document.hidden) poll();});
  if (token) connect().catch(error=>{$('#login-error').textContent=error.message;});
})();
