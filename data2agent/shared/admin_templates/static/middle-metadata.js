var escAttr=esc;
var currentScan=null, selected=null, planSource=null, planRevision=null;
var visibleRows=[], selectedKeys={};
var listOffset=0, listTotal=0; var LIST_LIMIT=50;
var pendingConfirm=null; // {src, revision, existing, incoming, hints, validated}
var LEGACY_DRAFT_KEY='d2a_extraction_draft';
(function(){
  var q=new URLSearchParams(location.search);
  if(q.get('from')==='config'){
    var b=document.getElementById('from-config-banner');
    if(b) b.style.display='block';
  }
})();

function draftKey(src){
  if(!src) throw new Error('draft key requires source');
  return 'd2a_extraction_draft:'+src;
}
function clearDraft(src){
  try{
    if(src) sessionStorage.removeItem(draftKey(src));
    sessionStorage.removeItem(LEGACY_DRAFT_KEY);
  }catch(e){}
}
function rememberSource(src){
  if(src) planSource=src;
}
function rowKey(t){ return String(t.schema)+'\0'+String(t.name); }
function selectedCount(){ return Object.keys(selectedKeys).length; }

function syncBatchBar(){
  var n=selectedCount();
  var bar=document.getElementById('batch-bar');
  var btn=document.getElementById('btn-batch-add');
  var meta=document.getElementById('batch-meta');
  var page=document.getElementById('chk-page');
  if(!visibleRows.length){ bar.hidden=true; return; }
  bar.hidden=false;
  meta.textContent='已选 '+n+' 张表';
  btn.disabled=n===0;
  btn.textContent=n?('批量加入计划 ('+n+')'):'批量加入计划';
  var visibleKeys=visibleRows.map(rowKey);
  var allOn=visibleKeys.length>0 && visibleKeys.every(function(k){ return !!selectedKeys[k]; });
  var someOn=!allOn && visibleKeys.some(function(k){ return !!selectedKeys[k]; });
  page.checked=allOn;
  page.indeterminate=someOn;
}

function setRowSelected(t, on){
  var k=rowKey(t);
  if(on) selectedKeys[k]=t;
  else delete selectedKeys[k];
}

function cleanSpec(spec){
  var out={mode:spec.mode};
  if(spec.schema) out.schema=spec.schema;
  if(spec.key_columns&&spec.key_columns.length) out.key_columns=spec.key_columns;
  if(spec.watermark) out.watermark=spec.watermark;
  if(spec.schema_fingerprint) out.schema_fingerprint=spec.schema_fingerprint;
  if(spec.validated_at) out.validated_at=spec.validated_at;
  return out;
}
function specFromTable(t){
  var uniq=(t.unique_keys&&t.unique_keys[0]&&t.unique_keys[0].columns)||[];
  var keys=(t.primary_key&&t.primary_key.length)?t.primary_key:
    ((t.key_suggestions&&t.key_suggestions[0]&&t.key_suggestions[0].columns)||uniq||[]);
  var spec={
    mode: keys.length?'incremental':'full_refresh',
    schema: t.schema,
    key_columns: keys.length?keys:null,
    watermark: (t.watermark_candidates&&t.watermark_candidates[0])||null,
    schema_fingerprint: t.schema_fingerprint||null
  };
  if(spec.mode==='full_refresh'){
    delete spec.watermark;
    delete spec.key_columns;
  }
  return cleanSpec(spec);
}

async function ensurePlanContext(){
  var r=await apiFetch('/api/extraction-tables',{headers:authHeaders()});
  var body=await r.json();
  if(!r.ok) throw new Error(JSON.stringify(body));
  rememberSource(body.source);
  planRevision=body.revision;
  return body;
}

function closeConfirmModal(){
  pendingConfirm=null;
  closeModal('confirm-modal');
  document.getElementById('btn-confirm-save').disabled=true;
  document.getElementById('btn-confirm-revalidate').disabled=true;
}
function openConfirmModal(){
  openModal('confirm-modal');
}

function markConfirmDirty(){
  if(!pendingConfirm) return;
  pendingConfirm.validated=false;
  document.getElementById('btn-confirm-save').disabled=true;
  document.getElementById('btn-confirm-revalidate').disabled=false;
  document.getElementById('confirm-status').textContent='已修改，请重新校验后再保存';
  document.getElementById('confirm-err').textContent='';
  syncConfirmModeFields();
}

function syncConfirmModeFields(){
  document.querySelectorAll('#confirm-table tr[data-table]').forEach(function(tr){
    var mode=tr.querySelector('select.cf-mode');
    var wm=tr.querySelector('.wm-wrap');
    var dash=tr.querySelector('.wm-dash');
    if(!mode||!wm) return;
    var inc=mode.value==='incremental';
    wm.hidden=!inc;
    if(dash) dash.hidden=inc;
  });
}

function parseKeyOptionValue(v){
  if(!v) return [];
  try{ var cols=JSON.parse(v); return Array.isArray(cols)?cols.filter(Boolean):[]; }
  catch(e){ return String(v).split(',').map(function(s){return s.trim();}).filter(Boolean); }
}
function buildKeyOptions(hint, selectedCols){
  var opts=[], seen={};
  function add(cols, label){
    if(!cols||!cols.length) return;
    var key=cols.join('\0');
    if(seen[key]) return;
    seen[key]=true;
    opts.push({
      value: JSON.stringify(cols),
      label: cols.join(', ') + (label?(' · '+label):'')
    });
  }
  add(hint.primary_key, 'PK');
  (hint.unique_keys||[]).forEach(function(uk){
    add(uk.columns||[], uk.kind==='primary'?'PK':(uk.name||'唯一'));
  });
  (hint.key_suggestions||[]).forEach(function(ks){
    add(ks.columns||[], ks.name||'建议');
  });
  if(selectedCols&&selectedCols.length) add(selectedCols, null);
  return opts;
}
function buildWmOptions(hint, selected){
  var opts=[], seen={};
  function add(col){
    if(!col||seen[col]) return;
    seen[col]=true;
    opts.push(col);
  }
  (hint.watermark_candidates||[]).forEach(add);
  if(selected) add(selected);
  return opts;
}

function collectIncomingFromForm(){
  var incoming={};
  document.querySelectorAll('#confirm-table tr[data-table]').forEach(function(tr){
    var name=tr.getAttribute('data-table');
    var mode=(tr.querySelector('select.cf-mode')||{}).value||'full_refresh';
    var schema=(tr.getAttribute('data-schema')||'').trim();
    var keys=parseKeyOptionValue((tr.querySelector('select.cf-keys')||{}).value||'');
    var wm=((tr.querySelector('select.cf-wm')||{}).value||'').trim();
    var fp=tr.getAttribute('data-fp')||'';
    var spec={mode:mode};
    if(schema) spec.schema=schema;
    if(keys.length) spec.key_columns=keys;
    if(mode==='incremental' && wm) spec.watermark=wm;
    if(fp) spec.schema_fingerprint=fp;
    incoming[name]=cleanSpec(spec);
  });
  return incoming;
}

function buildMergedPlan(existing, incoming){
  var merged={};
  Object.keys(existing||{}).forEach(function(k){ merged[k]=cleanSpec(existing[k]); });
  Object.keys(incoming||{}).forEach(function(k){ merged[k]=incoming[k]; });
  return merged;
}

function updateConfirmDiff(existing, incoming){
  var added=[], changed=[];
  Object.keys(incoming).forEach(function(n){ (existing[n]?changed:added).push(n); });
  var diffEl=document.getElementById('confirm-diff');
  diffEl.style.display='block';
  diffEl.textContent='新增 '+(added.join(', ')||'（无）')+'；覆盖已有 '+(changed.join(', ')||'（无）')+
    '。可修改模式/键/水位后点「重新校验」。确认保存后写入 connect.yaml，connector 从下一轮生效。';
}

function renderConfirmTable(incoming, existing, resultsByTable, hints){
  hints=hints||{};
  resultsByTable=resultsByTable||{};
  var html='<table><thead><tr><th>表</th><th>变更</th><th>模式</th><th>键</th><th>水位</th><th>校验</th></tr></thead><tbody>';
  Object.keys(incoming).sort().forEach(function(name){
    var spec=incoming[name]||{};
    var was=!!(existing&&existing[name]);
    var res=resultsByTable[name];
    var st=res?(res.status||'—'):'—';
    var ok=st==='ready';
    var hint=hints[name]||{};
    var selKeys=spec.key_columns||[];
    var keyOpts=buildKeyOptions(hint, selKeys);
    // incremental 契约要求显式非空 key_columns；无选项时只能切 full_refresh
    var selKeyVal=selKeys.length?JSON.stringify(selKeys):(keyOpts[0]?keyOpts[0].value:'');
    var wmOpts=buildWmOptions(hint, spec.watermark||'');
    var inc=spec.mode==='incremental';
    var keyHtml='<select class="field-in cf-keys" title="业务键 / 主键">';
    if(!keyOpts.length){
      keyHtml+='<option value="">（无可用键）</option>';
    } else {
      keyOpts.forEach(function(o){
        keyHtml+='<option value="'+escAttr(o.value)+'"'+(o.value===selKeyVal?' selected':'')+'>'+esc(o.label)+'</option>';
      });
    }
    keyHtml+='</select>';
    var wmHtml='<select class="field-in cf-wm" title="水位列">';
    if(!wmOpts.length){
      wmHtml+='<option value="">（无水位候选）</option>';
    } else {
      wmOpts.forEach(function(col){
        wmHtml+='<option value="'+escAttr(col)+'"'+(col===(spec.watermark||'')?' selected':'')+'>'+esc(col)+'</option>';
      });
    }
    wmHtml+='</select>';
    html+='<tr data-table="'+esc(name)+'" data-schema="'+esc(spec.schema||'')+
      '" data-fp="'+esc(spec.schema_fingerprint||'')+'">'+
      '<td title="'+esc(name)+'">'+esc(name)+'</td>'+
      '<td><span class="badge '+(was?'badge-chg':'badge-ok')+'">'+(was?'覆盖':'新增')+'</span></td>'+
      '<td><select class="field-in cf-mode">'+
        '<option value="incremental"'+(inc?' selected':'')+'>incremental</option>'+
        '<option value="full_refresh"'+(!inc?' selected':'')+'>full_refresh</option>'+
      '</select></td>'+
      '<td>'+keyHtml+'</td>'+
      '<td><span class="wm-wrap"'+(inc?'':' hidden')+'>'+wmHtml+
      '</span><span class="meta wm-dash"'+(inc?' hidden':'')+'>—</span></td>'+
      '<td class="'+(ok?'ok':(st==='—'?'meta':'err'))+'">'+esc(st)+
        (res&&res.detail&&!ok?(' · '+esc(res.detail)):'')+'</td></tr>';
  });
  html+='</tbody></table>';
  document.getElementById('confirm-table').innerHTML=html;
  syncConfirmModeFields();
}

async function validatePendingConfirm(){
  if(!pendingConfirm) return false;
  var incoming=collectIncomingFromForm();
  pendingConfirm.incoming=incoming;
  var merged=buildMergedPlan(pendingConfirm.existing, incoming);
  pendingConfirm.merged=merged;
  updateConfirmDiff(pendingConfirm.existing, incoming);
  document.getElementById('confirm-err').textContent='';
  document.getElementById('confirm-status').textContent='正在校验…';
  document.getElementById('btn-confirm-save').disabled=true;
  document.getElementById('btn-confirm-revalidate').disabled=true;

  var vr=await apiFetch('/api/extraction-tables/validate',{method:'POST',headers:authHeaders(),
    body:JSON.stringify({tables:merged, live:true})});
  var vbody=await vr.json().catch(function(){return {};});
  var resultsByTable={};
  (vbody.results||[]).forEach(function(x){ resultsByTable[x.table]=x; });
  renderConfirmTable(incoming, pendingConfirm.existing, resultsByTable, pendingConfirm.hints);
  document.getElementById('btn-confirm-revalidate').disabled=false;

  if(!vr.ok || !vbody.ok){
    var msg=typeof formatApiError==='function'
      ? formatApiError(vbody,'校验未通过')
      : ((vbody.errors||[]).map(function(e){return (e.field||'')+': '+(e.message||'');}).join('\n')
        || '校验未通过，请修改后重新校验');
    document.getElementById('confirm-err').textContent=msg;
    document.getElementById('confirm-status').textContent='校验未通过';
    pendingConfirm.validated=false;
    return false;
  }
  pendingConfirm.validated=true;
  document.getElementById('confirm-status').textContent='校验通过，可确认保存';
  document.getElementById('btn-confirm-save').disabled=false;
  return true;
}

async function openBatchConfirm(tables){
  if(!tables||!tables.length) return;
  var incoming={}, hints={};
  tables.forEach(function(t){
    if(!t||!t.name||t.error_code) return;
    incoming[t.name]=specFromTable(t);
    hints[t.name]={
      primary_key: t.primary_key||[],
      unique_keys: t.unique_keys||[],
      key_suggestions: t.key_suggestions||[],
      watermark_candidates: t.watermark_candidates||[]
    };
  });
  var names=Object.keys(incoming);
  if(!names.length){ announce('warning','没有可加入的表'); return; }

  document.getElementById('confirm-err').textContent='';
  document.getElementById('confirm-diff').style.display='none';
  document.getElementById('confirm-status').textContent='读取当前计划…';
  document.getElementById('btn-confirm-save').disabled=true;
  document.getElementById('btn-confirm-revalidate').disabled=true;
  document.getElementById('confirm-summary').textContent=
    '即将加入 '+names.length+' 张表（可编辑模式/键/水位）';
  renderConfirmTable(incoming, {}, {}, hints);
  openConfirmModal();

  var cur;
  try{ cur=await ensurePlanContext(); }
  catch(e){
    document.getElementById('confirm-err').textContent='无法读取抽取计划: '+e.message;
    document.getElementById('confirm-status').textContent='读取失败';
    return;
  }
  pendingConfirm={
    src: cur.source,
    revision: cur.revision,
    existing: cur.tables||{},
    incoming: incoming,
    hints: hints,
    merged: null,
    validated: false,
    count: names.length
  };
  updateConfirmDiff(pendingConfirm.existing, incoming);
  renderConfirmTable(incoming, pendingConfirm.existing, {}, hints);
  document.getElementById('btn-confirm-revalidate').disabled=false;
  await validatePendingConfirm();
}

async function confirmAndSave(){
  if(!pendingConfirm) return;
  if(!pendingConfirm.validated){
    var ok=await validatePendingConfirm();
    if(!ok) return;
  }
  var incoming=collectIncomingFromForm();
  var merged=buildMergedPlan(pendingConfirm.existing, incoming);
  pendingConfirm.incoming=incoming;
  pendingConfirm.merged=merged;
  var p=pendingConfirm;
  var btn=document.getElementById('btn-confirm-save');
  btn.disabled=true;
  document.getElementById('btn-confirm-revalidate').disabled=true;
  document.getElementById('confirm-status').textContent='正在保存…';
  document.getElementById('confirm-err').textContent='';
  var r=await apiFetch('/api/extraction-tables',{method:'PUT',headers:authHeaders(),
    body:JSON.stringify({tables:merged, revision:p.revision})});
  var saved=await r.json().catch(function(){return {};});
  if(r.status===409){
    document.getElementById('confirm-status').textContent='保存冲突，正在重新加载计划…';
    document.getElementById('confirm-err').textContent=typeof formatApiError==='function'
      ? formatApiError(saved,'配置已被其他会话修改')
      : '配置已被其他会话修改';
    try{
      var cur=await ensurePlanContext();
      p.revision=cur.revision;
      p.existing=cur.tables||{};
      planRevision=cur.revision;
      var latestIncoming=collectIncomingFromForm();
      p.incoming=latestIncoming;
      p.merged=buildMergedPlan(p.existing, latestIncoming);
      p.validated=false;
      updateConfirmDiff(p.existing, latestIncoming);
      renderConfirmTable(latestIncoming, p.existing, {}, p.hints);
      document.getElementById('confirm-err').textContent=
        (typeof formatApiError==='function'
          ? formatApiError(saved,'配置已被其他会话修改')
          : '配置已被其他会话修改')
        + '。已重新加载当前计划与 revision，正在重新校验…';
      document.getElementById('btn-confirm-revalidate').disabled=false;
      await validatePendingConfirm();
    }catch(e){
      document.getElementById('confirm-err').textContent=
        '配置冲突且重新加载失败: '+e.message+'。请关闭弹窗后重试。';
      document.getElementById('confirm-status').textContent='保存冲突';
      document.getElementById('btn-confirm-revalidate').disabled=false;
    }
    return;
  }
  if(!r.ok || !saved.ok){
    document.getElementById('confirm-err').textContent=typeof formatApiError==='function'
      ? formatApiError(saved,'保存失败')
      : ((saved.errors||[]).map(function(e){return (e.field||'')+': '+(e.message||'');}).join('\n')
        || JSON.stringify(saved));
    document.getElementById('confirm-status').textContent='保存失败';
    if(saved.results){
      var by={}; saved.results.forEach(function(x){ by[x.table]=x; });
      renderConfirmTable(incoming, p.existing, by, p.hints);
    }
    p.validated=false;
    document.getElementById('btn-confirm-revalidate').disabled=false;
    return;
  }
  clearDraft(p.src);
  planRevision=saved.revision||planRevision;
  selectedKeys={};
  var count=Object.keys(incoming).length;
  closeConfirmModal();
  document.getElementById('drawer').classList.remove('open');
  await loadTables();
  document.getElementById('batch-meta').textContent=
    '已保存 '+count+' 张表到抽取计划（已加入）';
}

async function addToPlan(){
  if(!selected) return;
  await openBatchConfirm([selected]);
}
async function addSelectedToPlan(){
  var list=Object.keys(selectedKeys).map(function(k){ return selectedKeys[k]; })
    .filter(function(t){ return t && t.name && !t.error_code; });
  if(!list.length) return;
  await openBatchConfirm(list);
}

document.getElementById('btn-scan').onclick=startScan;
document.getElementById('btn-filter').onclick=function(){ listOffset=0; loadTables(); };
var filterTimer=null;
function debouncedFilter(){ listOffset=0; clearTimeout(filterTimer); filterTimer=setTimeout(loadTables,300); }
document.getElementById('f-q').addEventListener('input', debouncedFilter);
document.getElementById('f-q').addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); clearTimeout(filterTimer); listOffset=0; loadTables(); } });
document.getElementById('f-schema').addEventListener('keydown', function(e){ if(e.key==='Enter'){ e.preventDefault(); listOffset=0; loadTables(); } });
['f-type','f-pk','f-planned'].forEach(function(id){ document.getElementById(id).addEventListener('change', function(){ listOffset=0; loadTables(); }); });
document.getElementById('btn-prev-page').onclick=function(){ listOffset=Math.max(0,listOffset-LIST_LIMIT); loadTables(); };
document.getElementById('btn-next-page').onclick=function(){ listOffset+=LIST_LIMIT; loadTables(); };
function closeDrawer(){ document.getElementById('drawer').classList.remove('open'); }
document.getElementById('btn-close').onclick=closeDrawer;
document.addEventListener('keydown', function(e){
  if(e.key==='Escape'){
    closeDrawer();
    var modal=document.getElementById('confirm-modal');
    if(modal && !modal.hidden) closeConfirmModal();
  }
});
document.addEventListener('click', function(e){
  var drawer=document.getElementById('drawer');
  if(drawer.classList.contains('open') && !drawer.contains(e.target) && !e.target.closest('#table-list')) closeDrawer();
});
document.getElementById('btn-add').onclick=addToPlan;
document.getElementById('btn-batch-add').onclick=addSelectedToPlan;
document.getElementById('btn-confirm-cancel').onclick=closeConfirmModal;
document.getElementById('btn-confirm-x').onclick=closeConfirmModal;
document.getElementById('btn-confirm-revalidate').onclick=function(){ validatePendingConfirm(); };
document.getElementById('btn-confirm-save').onclick=confirmAndSave;
document.getElementById('confirm-modal').addEventListener('click', function(e){
  if(e.target===this) closeConfirmModal();
});
document.getElementById('confirm-table').addEventListener('change', function(e){
  if(!e.target.closest('.cf-mode,.cf-keys,.cf-wm')) return;
  markConfirmDirty();
});
document.getElementById('btn-clear-sel').onclick=function(){
  selectedKeys={};
  document.querySelectorAll('#table-list input.row-check').forEach(function(c){ c.checked=false; });
  document.querySelectorAll('#table-list tr.selected').forEach(function(tr){ tr.classList.remove('selected'); });
  syncBatchBar();
};
document.getElementById('chk-page').onchange=function(){
  var on=this.checked;
  visibleRows.forEach(function(t){
    if(t.error_code) return;
    setRowSelected(t, on);
  });
  document.querySelectorAll('#table-list input.row-check').forEach(function(c){
    c.checked=on && !c.disabled;
    var tr=c.closest('tr');
    if(tr) tr.classList.toggle('selected', c.checked);
  });
  syncBatchBar();
};
document.getElementById('table-list').addEventListener('click', function(e){
  var b=e.target.closest('button[data-t]'); if(!b) return;
  openDetail(b.getAttribute('data-s'), b.getAttribute('data-t'));
});
document.getElementById('table-list').addEventListener('change', function(e){
  var c=e.target.closest('input.row-check'); if(!c) return;
  var schema=c.getAttribute('data-s');
  var name=c.getAttribute('data-t');
  var t=visibleRows.find(function(r){ return r.schema===schema && r.name===name; });
  if(!t) return;
  setRowSelected(t, c.checked);
  var tr=c.closest('tr');
  if(tr) tr.classList.toggle('selected', c.checked);
  syncBatchBar();
});
ensurePlanContext().catch(function(){});

async function startScan(){
  document.getElementById('scan-err').textContent='';
  var r=await apiFetch('/api/metadata/scans',{method:'POST',headers:authHeaders(),body:'{}'});
  var body=await r.json();
  if(!r.ok){ document.getElementById('scan-err').textContent=
    (typeof formatApiError==='function'?formatApiError(body,'扫描失败'):(body.detail||JSON.stringify(body))); return; }
  currentScan=body.scan_id;
  rememberSource(body.source);
  document.getElementById('scan-meta').textContent='扫描中 '+esc(currentScan)+'…';
  pollScan();
}
function syncScanSpin(running){
  var s=document.getElementById('scan-spin'); if(s) s.hidden=!running;
  var btn=document.getElementById('btn-scan'); if(btn) btn.disabled=!!running;
}
async function pollScan(){
  if(!currentScan) return;
  var r=await apiFetch('/api/metadata/scans/'+encodeURIComponent(currentScan),{headers:authHeaders()});
  var body=await r.json();
  if(!r.ok){ document.getElementById('scan-err').textContent=JSON.stringify(body); return; }
  document.getElementById('scan-meta').textContent=
    '状态 '+esc(body.status)+' · 表 '+esc(body.table_count||0)+
    (body.finished_at?(' · 完成于 '+esc(body.finished_at)):'');
  syncScanSpin(body.status==='running');
  if(body.status==='running'){ setTimeout(pollScan,800); return; }
  if(body.status==='failed'||body.status==='timeout'){
    var scanErrText=(typeof formatApiError==='function'
        ? formatApiError(body, body.error_detail||body.error_code||body.status)
        : ((body.error_detail||body.error_code||body.status)+
           (body.error_suggestion||body.suggestion?(' — 建议：'+(body.error_suggestion||body.suggestion)):'')));
    document.getElementById('scan-err').textContent=scanErrText;
    // 扫描失败:把真实原因显示在表列表区(主视觉),不再加载列表(避免次生 409 盖住原因)
    document.getElementById('table-list').innerHTML=
      '<p class="err">元数据扫描未成功:'+esc(scanErrText)+'</p>';
    syncBatchBar();
    return;
  }
  loadTables();
}
async function loadTables(){
  var qs=new URLSearchParams();
  var schema=document.getElementById('f-schema').value.trim();
  var q=document.getElementById('f-q').value.trim();
  var ot=document.getElementById('f-type').value;
  if(schema) qs.set('schema',schema);
  if(q) qs.set('q',q);
  if(ot) qs.set('object_type',ot);
  if(document.getElementById('f-pk').checked) qs.set('has_pk','true');
  qs.set('limit', String(LIST_LIMIT)); qs.set('offset', String(listOffset));
  var r=await apiFetch('/api/metadata/tables?'+qs.toString(),{headers:authHeaders()});
  var body=await r.json();
  if(!r.ok){
    var code=body&&body.detail&&body.detail.code;
    if(r.status===409 && code==='metadata_stale'){
      // 从未扫描(或服务重启后内存缓存已清):自动发起扫描,而非红字报错;
      // 但本次会话刚扫描过且未成功(currentScan 仍在),不再自动重扫
      if(currentScan){
        document.getElementById('list-pager').hidden=true;
        document.getElementById('table-list').innerHTML=
          '<p class="err">最近一次扫描未成功,请查看上方扫描错误后重试。</p>';
      } else {
        document.getElementById('table-list').innerHTML=
          '<p class="meta">尚未扫描 ERP 元数据,正在自动开始扫描…</p>';
        startScan();
      }
      syncBatchBar();
      return;
    }
    visibleRows=[];
    document.getElementById('list-pager').hidden=true;
    document.getElementById('table-list').innerHTML='<p class="err">'+
      esc(typeof formatApiError==='function'?formatApiError(body,'加载失败'):
        (body.detail&&body.detail.detail?body.detail.detail:JSON.stringify(body)))+'</p>';
    syncBatchBar();
    return;
  }
  listTotal=body.total||0;
  rememberSource(body.source);
  var onlyPlanned=document.getElementById('f-planned').checked;
  var rows=(body.tables||[]).filter(function(t){
    return !onlyPlanned || t.in_extraction_plan;
  });
  visibleRows=rows;
  if(!rows.length){
    document.getElementById('table-list').innerHTML='<p class="meta">无匹配表</p>';
    syncPager();
    syncBatchBar();
    return;
  }
  var html='<table><thead><tr><th class="check"><span class="meta">选</span></th><th>表</th><th>类型</th><th title="来自数据库元数据估算,非精确行数,仅供选表参考量级">行数≈</th><th>PK</th><th>抽取</th><th></th></tr></thead><tbody>';
  rows.forEach(function(t){
    var k=rowKey(t);
    var disabled=!!t.error_code;
    if(disabled) delete selectedKeys[k];
    var checked=!disabled && !!selectedKeys[k];
    if(checked) selectedKeys[k]=t;
    var planned=!!t.in_extraction_plan;
    html+='<tr class="'+(checked?'selected':'')+'"><td class="check"><input type="checkbox" class="row-check" data-s="'+
      esc(t.schema)+'" data-t="'+esc(t.name)+'"'+(checked?' checked':'')+(disabled?' disabled':'')+
      '></td><td>'+esc(t.schema)+'.'+esc(t.name)+'</td><td>'+esc(t.object_type)+'</td><td>'+
      esc(t.estimated_rows==null?'—':t.estimated_rows)+'</td><td>'+esc((t.primary_key||[]).join(', ')||'—')+
      '</td><td><span class="badge '+(planned?'badge-ok':'badge-off')+'">'+
      (planned?'已加入':'未加入')+'</span></td><td><button type="button" class="text" data-s="'+
      esc(t.schema)+'" data-t="'+esc(t.name)+'">详情</button></td></tr>';
  });
  html+='</tbody></table>';
  document.getElementById('table-list').innerHTML=html;
  syncPager();
  syncBatchBar();
}
function syncPager(){
  var pager=document.getElementById('list-pager');
  if(!pager) return;
  // 单页也显示:分页信息本身就是状态(总数一目了然),按钮自动禁用
  pager.hidden = false;
  var from=listTotal===0?0:listOffset+1, to=Math.min(listTotal,listOffset+LIST_LIMIT);
  document.getElementById('page-info').textContent='共 '+fmtNumber(listTotal)+' 张 · 第 '+from+'-'+to+' 张';
  document.getElementById('btn-prev-page').disabled = listOffset===0;
  document.getElementById('btn-next-page').disabled = listOffset+LIST_LIMIT>=listTotal;
}
async function openDetail(schema, table){
  var r=await apiFetch('/api/metadata/tables/'+encodeURIComponent(schema)+'/'+encodeURIComponent(table),{headers:authHeaders()});
  var body=await r.json();
  if(!r.ok){ announce('error',formatApiError(body,'详情加载失败'),{persistent:true}); return; }
  selected=body;
  rememberSource(body.source);
  document.getElementById('drawer-title').textContent=body.schema+'.'+body.name;
  var html='<p>类型 '+esc(body.object_type)+' · 指纹 '+esc(body.schema_fingerprint||'—')+'</p>';
  html+='<p>PK: '+esc((body.primary_key||[]).join(', ')||'—')+'</p>';
  html+='<p>水位候选: '+esc((body.watermark_candidates||[]).join(', ')||'—')+'</p>';
  html+='<table><thead><tr><th>列</th><th>类型</th><th>NULL</th></tr></thead><tbody>';
  (body.columns||[]).forEach(function(c){
    html+='<tr><td>'+esc(c.name)+'</td><td>'+esc(c.sql_type)+'</td><td>'+esc(c.nullable?'是':'否')+'</td></tr>';
  });
  html+='</tbody></table>';
  document.getElementById('drawer-body').innerHTML=html;
  document.getElementById('drawer').classList.add('open');
}
