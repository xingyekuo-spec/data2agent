var escAttr=esc;

var sourceName=null, revision=null, tables={}, results={};
var selectedKeys={}, editNames=[], editMeta={};
// 清理历史草稿键（本页已不再使用草稿）
try{
  Object.keys(sessionStorage).forEach(function(k){
    if(k==='d2a_extraction_draft' || k.indexOf('d2a_extraction_draft:')===0){
      sessionStorage.removeItem(k);
    }
  });
}catch(e){}

function setMsg(text, ok){
  var el=document.getElementById('msg');
  el.className=ok===false?'err':(ok?'ok':'meta');
  el.textContent=text||'';
}
function showDiff(diff){
  var d=document.getElementById('diff');
  diff=diff||{added:[], removed:[], changed:[]};
  d.style.display='block';
  d.textContent='差异 — 新增: '+(diff.added||[]).join(', ')+
    ' · 修改: '+(diff.changed||[]).join(', ')+
    ' · 删除: '+(diff.removed||[]).join(', ');
  return diff;
}
function cleanSpec(spec){
  var out={mode:spec.mode};
  if(spec.schema) out.schema=spec.schema;
  if(spec.key_columns&&spec.key_columns.length) out.key_columns=spec.key_columns;
  if(spec.watermark) out.watermark=spec.watermark;
  if(spec.start_date) out.start_date=spec.start_date;
  if(spec.schema_fingerprint) out.schema_fingerprint=spec.schema_fingerprint;
  if(spec.validated_at) out.validated_at=spec.validated_at;
  return out;
}
function parseKeyOptionValue(v){
  if(!v) return [];
  try{ var cols=JSON.parse(v); return Array.isArray(cols)?cols.filter(Boolean):[]; }
  catch(e){ return String(v).split(',').map(function(s){return s.trim();}).filter(Boolean); }
}
function buildKeyOptions(meta, selectedCols){
  var opts=[], seen={};
  function add(cols, label){
    if(!cols||!cols.length) return;
    var key=cols.join('\0');
    if(seen[key]) return;
    seen[key]=true;
    opts.push({value: JSON.stringify(cols), label: cols.join(', ') + (label?(' · '+label):'')});
  }
  add((meta&&meta.primary_key)||[], 'PK');
  ((meta&&meta.unique_keys)||[]).forEach(function(uk){
    add(uk.columns||[], uk.kind==='primary'?'PK':(uk.name||'唯一'));
  });
  ((meta&&meta.key_suggestions)||[]).forEach(function(ks){
    add(ks.columns||[], ks.name||'建议');
  });
  if(selectedCols&&selectedCols.length) add(selectedCols, null);
  return opts;
}
function buildWmOptions(meta, selected){
  var opts=[], seen={};
  function add(col){
    if(!col||seen[col]) return;
    seen[col]=true;
    opts.push(col);
  }
  ((meta&&meta.watermark_candidates)||[]).forEach(add);
  if(selected) add(selected);
  return opts;
}
function keySelectHtml(meta, selectedCols){
  var opts=buildKeyOptions(meta||{}, selectedCols||[]);
  // incremental 契约要求显式非空 key_columns；默认选中发现主键/首个候选
  var selVal=selectedCols&&selectedCols.length?JSON.stringify(selectedCols):(opts[0]?opts[0].value:'');
  var html='<select class="field-in cf-keys" title="键">';
  if(!opts.length) html+='<option value="">（无可用键）</option>';
  else {
    opts.forEach(function(o){
      html+='<option value="'+escAttr(o.value)+'"'+(o.value===selVal?' selected':'')+'>'+esc(o.label)+'</option>';
    });
  }
  return html+'</select>';
}
function wmSelectHtml(meta, selected){
  var opts=buildWmOptions(meta||{}, selected||'');
  var html='<select class="field-in cf-wm" title="水位">';
  if(!opts.length) html+='<option value="">（无水位候选）</option>';
  else {
    opts.forEach(function(col){
      html+='<option value="'+escAttr(col)+'"'+(col===selected?' selected':'')+'>'+esc(col)+'</option>';
    });
  }
  return html+'</select>';
}

function selectedCount(){ return Object.keys(selectedKeys).length; }
function syncBatchBar(){
  var names=Object.keys(tables);
  var bar=document.getElementById('batch-bar');
  var n=selectedCount();
  if(!names.length){ bar.hidden=true; return; }
  bar.hidden=false;
  document.getElementById('batch-meta').textContent='已选 '+n+' 张表';
  document.getElementById('btn-batch-edit').disabled=n===0;
  document.getElementById('btn-batch-remove').disabled=n===0;
  document.getElementById('btn-batch-edit').textContent=n?('批量编辑 ('+n+')'):'批量编辑';
  var page=document.getElementById('chk-page');
  var allOn=names.length>0 && names.every(function(k){ return !!selectedKeys[k]; });
  var someOn=!allOn && names.some(function(k){ return !!selectedKeys[k]; });
  page.checked=allOn;
  page.indeterminate=someOn;
}

function render(){
  document.getElementById('rev').textContent=revision?('revision '+revision.slice(0,18)+'…'):'';
  var names=Object.keys(tables).sort();
  var guide=document.getElementById('empty-guide');
  if(guide) guide.style.display=!names.length?'block':'none';
  if(!names.length){
    document.getElementById('tbody').innerHTML=
      '<tr><td colspan="7" class="meta">暂无抽取表 — 请先到元数据页选表</td></tr>';
    selectedKeys={};
    syncBatchBar();
    return;
  }
  var html='';
  names.forEach(function(name){
    var s=tables[name]||{};
    var st=results[name];
    var checked=!!selectedKeys[name];
    html+='<tr class="'+(checked?'selected':'')+'">'+
      '<td class="check"><input type="checkbox" class="row-check" data-name="'+esc(name)+'"'+
        (checked?' checked':'')+'></td>'+
      '<td>'+esc((s.schema||'')+'.'+name)+'</td><td>'+esc(s.mode)+'</td><td>'+
      esc((s.key_columns||[]).join(', ')||'—')+'</td><td>'+esc(s.watermark||'—')+'</td><td>'+
      esc(s.start_date||'—')+'</td><td>'+
      esc(st?(st.status+(st.suggestion?(' · '+st.suggestion):'')):'—')+
      '</td><td class="actions">'+
      '<button type="button" class="text" data-edit="'+esc(name)+'">编辑</button>'+
      '<button type="button" class="text danger-text" data-remove="'+esc(name)+'">移除</button>'+
      '</td></tr>';
  });
  document.getElementById('tbody').innerHTML=html;
  syncBatchBar();
}

function syncEditModeFields(){
  document.querySelectorAll('#edit-table tr[data-table]').forEach(function(tr){
    var mode=tr.querySelector('select.cf-mode');
    var wm=tr.querySelector('.wm-wrap');
    var sd=tr.querySelector('.sd-wrap');
    var dash=tr.querySelector('.wm-dash');
    if(!mode||!wm) return;
    var inc=mode.value==='incremental';
    wm.hidden=!inc;
    if(sd) sd.hidden=!inc;
    if(dash) dash.hidden=inc;
  });
}
function renderEditTable(names){
  var html='<table><thead><tr><th>表</th><th>模式</th><th>键</th><th>水位</th><th>开始日期</th></tr></thead><tbody>';
  names.forEach(function(name){
    var s=tables[name]||{};
    var meta=editMeta[name]||{};
    var inc=(s.mode||'full_refresh')==='incremental';
    html+='<tr data-table="'+esc(name)+'" data-schema="'+escAttr(s.schema||'')+'">'+
      '<td title="'+esc((s.schema||'')+'.'+name)+'">'+esc(name)+
        '<div class="meta">Schema '+esc(s.schema||'—')+'</div></td>'+
      '<td><select class="field-in cf-mode">'+
        '<option value="incremental"'+(inc?' selected':'')+'>incremental</option>'+
        '<option value="full_refresh"'+(!inc?' selected':'')+'>full_refresh</option>'+
      '</select></td>'+
      '<td>'+keySelectHtml(meta, s.key_columns||[])+'</td>'+
      '<td><span class="wm-wrap"'+(inc?'':' hidden')+'>'+wmSelectHtml(meta, s.watermark||'')+
      '</span><span class="meta wm-dash"'+(inc?' hidden':'')+'>—</span></td>'+
      '<td><span class="sd-wrap"'+(inc?'':' hidden')+'>'+
        '<input type="text" class="field-in cf-start-date" placeholder="YYYY-MM-DD(可选)"'+
        ' value="'+escAttr(s.start_date||'')+'">'+
        '<div class="meta">从此日期起抽,不抽更早历史</div>'+
      '</span><span class="meta wm-dash"'+(inc?' hidden':'')+'>—</span></td></tr>';
  });
  html+='</tbody></table>';
  document.getElementById('edit-table').innerHTML=html;
  syncEditModeFields();
}
function collectEditsFromForm(){
  var out={};
  document.querySelectorAll('#edit-table tr[data-table]').forEach(function(tr){
    var name=tr.getAttribute('data-table');
    var mode=(tr.querySelector('select.cf-mode')||{}).value||'full_refresh';
    var schema=(tr.getAttribute('data-schema')||'').trim();
    var keys=parseKeyOptionValue((tr.querySelector('select.cf-keys')||{}).value||'');
    var wm=((tr.querySelector('select.cf-wm')||{}).value||'').trim();
    var startDate=((tr.querySelector('.cf-start-date')||{}).value||'').trim();
    var prev=tables[name]||{};
    var spec={mode:mode};
    if(schema) spec.schema=schema;
    if(keys.length) spec.key_columns=keys;
    if(mode==='incremental' && wm) spec.watermark=wm;
    if(mode==='incremental' && startDate) spec.start_date=startDate;
    if(prev.schema_fingerprint) spec.schema_fingerprint=prev.schema_fingerprint;
    out[name]=cleanSpec(spec);
  });
  return out;
}
async function fetchTableMeta(schema, name){
  var r=await apiFetch('/api/metadata/tables/'+encodeURIComponent(schema||'dbo')+'/'+encodeURIComponent(name),
    {headers:authHeaders()});
  if(!r.ok) return null;
  return r.json();
}
async function openEdit(names){
  names=(names||[]).filter(function(n){ return !!tables[n]; });
  if(!names.length) return;
  editNames=names;
  editMeta={};
  document.getElementById('ed-title').textContent=
    names.length===1?('编辑 '+names[0]):('批量编辑 '+names.length+' 张表');
  document.getElementById('ed-sub').textContent='Schema 不可改 · 保存并生效将写入磁盘';
  document.getElementById('ed-err').textContent='';
  document.getElementById('ed-msg').textContent='加载元数据选项…';
  document.getElementById('btn-apply').disabled=true;
  renderEditTable(names);
  openModal('edit-modal');

  await Promise.all(names.map(async function(name){
    var s=tables[name]||{};
    var meta=await fetchTableMeta(s.schema||'dbo', name);
    if(meta) editMeta[name]=meta;
    else editMeta[name]={
      primary_key: s.key_columns||[],
      unique_keys: [],
      watermark_candidates: s.watermark?[s.watermark]:[]
    };
  }));
  renderEditTable(names);
  document.getElementById('ed-msg').textContent='';
  document.getElementById('btn-apply').disabled=false;
}
function closeEdit(){
  editNames=[];
  editMeta={};
  closeModal('edit-modal');
  document.getElementById('ed-err').textContent='';
  document.getElementById('ed-msg').textContent='';
  document.getElementById('btn-apply').disabled=false;
}

function planDiff(before, after){
  var b=Object.keys(before||{}), a=Object.keys(after||{});
  var bs={}, as={};
  b.forEach(function(k){ bs[k]=1; });
  a.forEach(function(k){ as[k]=1; });
  var added=[], removed=[], changed=[];
  a.forEach(function(k){ if(!bs[k]) added.push(k); });
  b.forEach(function(k){ if(!as[k]) removed.push(k); });
  a.forEach(function(k){
    if(!bs[k]) return;
    if(JSON.stringify(before[k])!==JSON.stringify(after[k])) changed.push(k);
  });
  return {added:added.sort(), removed:removed.sort(), changed:changed.sort()};
}

async function saveTablesPlan(nextTables){
  if(!revision){ setMsg('缺少 revision，请重新加载', false); return false; }
  document.getElementById('ed-err').textContent='';
  setMsg('正在校验…', null);
  var vr=await apiFetch('/api/extraction-tables/validate',{method:'POST',headers:authHeaders(),
    body:JSON.stringify({tables:nextTables, live:true})});
  var vbody=await vr.json().catch(function(){return {};});
  results={}; (vbody.results||[]).forEach(function(x){ results[x.table]=x; });
  showDiff(vbody.diff||planDiff(tables, nextTables));
  if(!vr.ok || !vbody.ok){
    var msg=typeof formatApiError==='function'
      ? formatApiError(vbody,'校验未通过')
      : ((vbody.errors||[]).map(function(e){return (e.field||'')+': '+(e.message||'');}).join('\n')
        || '校验未通过，不能保存');
    document.getElementById('ed-err').textContent=msg;
    setMsg(msg, false);
    render();
    return false;
  }
  var diff=vbody.diff||planDiff(tables, nextTables);
  var summary='即将写入磁盘并使计划生效：\n'+
    '新增: '+(diff.added.join(', ')||'（无）')+'\n'+
    '修改: '+(diff.changed.join(', ')||'（无）')+'\n'+
    '删除: '+(diff.removed.join(', ')||'（无）')+'\n\n确认后替换 connect.yaml 中的 tables。';
  if(!confirm(summary)){
    setMsg('已取消，未写入磁盘', false);
    return false;
  }
  setMsg('正在写入磁盘…', null);
  var r=await apiFetch('/api/extraction-tables',{method:'PUT',headers:authHeaders(),
    body:JSON.stringify({tables:nextTables, revision:revision})});
  var saved=await r.json().catch(function(){return {};});
  if(r.status===409){
    setMsg(typeof formatApiError==='function'
      ? formatApiError(saved,'配置已被其他会话修改，正在重新加载…')
      : '配置已被其他会话修改，正在重新加载…', false);
    await reload();
    setMsg(typeof formatApiError==='function'
      ? formatApiError(saved,'配置已被其他会话修改')
      : '配置已被其他会话修改'
      + '。已重新加载当前计划与 revision，请再次保存并生效。', false);
    return false;
  }
  if(!r.ok || !saved.ok){
    var fail=typeof formatApiError==='function'
      ? formatApiError(saved,'保存失败')
      : ((saved.errors||[]).map(function(e){return (e.field||'')+': '+(e.message||'');}).join('\n')
        || JSON.stringify(saved));
    document.getElementById('ed-err').textContent=fail;
    setMsg(fail, false);
    if(saved.results){ results={}; saved.results.forEach(function(x){ results[x.table]=x; }); render(); }
    return false;
  }
  closeEdit();
  selectedKeys={};
  revision=saved.revision;
  setMsg((saved.message||'计划已写入磁盘')+'（connector 从下一轮开始使用）', true);
  await reload();
  return true;
}

async function reload(){
  var r=await apiFetch('/api/extraction-tables',{headers:authHeaders()});
  var body=await r.json();
  if(!r.ok){ setMsg(typeof formatApiError==='function'?formatApiError(body,'加载失败'):JSON.stringify(body), false); return; }
  sourceName=body.source;
  revision=body.revision;
  tables={};
  Object.keys(body.tables||{}).forEach(function(k){ tables[k]=cleanSpec(body.tables[k]); });
  results={};
  // 清理已不存在的勾选
  Object.keys(selectedKeys).forEach(function(k){ if(!tables[k]) delete selectedKeys[k]; });
  render();
  setMsg('已加载 '+Object.keys(tables).length+' 张表', true);
}
async function validatePlan(){
  var r=await apiFetch('/api/extraction-tables/validate',{method:'POST',headers:authHeaders(),
    body:JSON.stringify({tables:tables, live:true})});
  var body=await r.json();
  results={}; (body.results||[]).forEach(function(x){ results[x.table]=x; });
  render();
  showDiff(body.diff);
  setMsg(body.ok?'校验通过':(typeof formatApiError==='function'
    ? formatApiError({errors:(body.results||[]).filter(function(x){return x.status!=='ready';}).map(function(x){
        return {field:x.table,message:(x.status||'')+': '+(x.detail||''),suggestion:x.suggestion};
      })}, '校验未通过')
    : '校验未通过'), !!body.ok);
  return body;
}

async function removeNames(names){
  names=(names||[]).filter(function(n){ return !!tables[n]; });
  if(!names.length) return;
  if(!confirm('确认移除 '+names.length+' 张表并立即写入磁盘？\n'+names.join(', '))) return;
  var next={};
  Object.keys(tables).forEach(function(k){
    if(names.indexOf(k)<0) next[k]=tables[k];
  });
  await saveTablesPlan(next);
}

document.getElementById('btn-reload').onclick=function(){ reload(); };
document.getElementById('btn-validate').onclick=validatePlan;
document.getElementById('btn-edit-cancel').onclick=closeEdit;
document.getElementById('edit-modal').addEventListener('click', function(e){
  if(e.target===this) closeEdit();
});
document.getElementById('edit-table').addEventListener('change', function(e){
  if(e.target.closest('.cf-mode')) syncEditModeFields();
});
document.getElementById('btn-apply').onclick=async function(){
  if(!editNames.length) return;
  var edits=collectEditsFromForm();
  var next={};
  Object.keys(tables).forEach(function(k){ next[k]=tables[k]; });
  Object.keys(edits).forEach(function(k){ next[k]=edits[k]; });
  document.getElementById('btn-apply').disabled=true;
  document.getElementById('ed-msg').textContent='正在校验并写入…';
  var ok=await saveTablesPlan(next);
  if(!ok){
    document.getElementById('btn-apply').disabled=false;
    document.getElementById('ed-msg').textContent='保存失败，可修改后重试';
  }
};
document.getElementById('btn-batch-edit').onclick=function(){
  openEdit(Object.keys(selectedKeys));
};
document.getElementById('btn-batch-remove').onclick=function(){
  removeNames(Object.keys(selectedKeys));
};
document.getElementById('btn-clear-sel').onclick=function(){
  selectedKeys={};
  render();
};
document.getElementById('chk-page').onchange=function(){
  var on=this.checked;
  selectedKeys={};
  if(on) Object.keys(tables).forEach(function(n){ selectedKeys[n]=true; });
  render();
};
document.getElementById('tbody').addEventListener('click', function(e){
  var edit=e.target.closest('button[data-edit]');
  if(edit){ openEdit([edit.getAttribute('data-edit')]); return; }
  var rm=e.target.closest('button[data-remove]');
  if(rm){ removeNames([rm.getAttribute('data-remove')]); return; }
});
document.getElementById('tbody').addEventListener('change', function(e){
  var c=e.target.closest('input.row-check'); if(!c) return;
  var name=c.getAttribute('data-name');
  if(c.checked) selectedKeys[name]=true;
  else delete selectedKeys[name];
  var tr=c.closest('tr');
  if(tr) tr.classList.toggle('selected', c.checked);
  syncBatchBar();
});

reload();
