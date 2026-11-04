/* ===========================================================================
 *  公共脚本：主界面与设置页共用
 *  ---------------------------------------------------------------------------
 *  这里放「两页都要用」的底层能力：环境解析、API 调用、toast、转义、
 *  以及**设置面板的渲染与采集**（设置页是它的主战场，主界面也可能用到）。
 *
 *  设计约定：
 *  - 不依赖任何页面的具体 DOM（除了 toast 节点，取不到就静默降级）
 *  - 不引入框架：保持和原项目一致的零依赖风格
 * =========================================================================== */

/* ---------- 环境 ---------- */
// Electron 模式由 preload 注入 __WCS__（含服务端口与 token）；
// 浏览器模式（python backend/server.py）下 __WCS__ 不存在，走同源 + 不校验。
const WCS = window.__WCS__ || null;
const API_BASE = (WCS && WCS.baseUrl) ? WCS.baseUrl : '';
const API_TOKEN = (WCS && WCS.token) ? WCS.token : '';

/* ---------- 小工具 ---------- */
function $(id){ return document.getElementById(id); }

function escapeHtml(s){
  return (s == null ? '' : String(s)).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function toast(msg){
  const t = $('toast');
  if(!t) return;                       // 页面没放 toast 节点时静默降级
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), 2400);
}

async function api(path, opts){
  opts = opts || {};
  const headers = Object.assign({}, opts.headers || {});
  if(API_TOKEN) headers['X-Token'] = API_TOKEN;
  const r = await fetch(API_BASE + path, Object.assign({}, opts, {headers}));
  const j = await r.json().catch(() => ({}));
  if(!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
  return j;
}

/* 周一到周日（设置页的「投放日」勾选用，主界面预览也会用） */
const WEEKDAYS = [['mon','周一'],['tue','周二'],['wed','周三'],['thu','周四'],
                  ['fri','周五'],['sat','周六'],['sun','周日']];

/* ---------- 设置面板 ---------- */
let settingsSchema = [];

function settingsField(s){
  const id = 'set_' + s.key;
  const help = s.help ? `<div class="k" style="margin-top:2px">${escapeHtml(s.help)}</div>` : '';
  let ctl = '';
  if(s.type === 'bool'){
    ctl = `<label class="chk"><input type="checkbox" id="${id}" data-key="${s.key}"
      data-type="bool" ${s.value ? 'checked' : ''}><span>启用</span></label>`;
  }else if(s.type === 'number'){
    ctl = `<input type="number" id="${id}" data-key="${s.key}" data-type="number"
      value="${s.value}" min="${s.min ?? ''}" max="${s.max ?? ''}"
      step="${s.step ?? 'any'}" style="width:130px">`;
  }else if(s.type === 'time'){
    ctl = `<input type="time" id="${id}" data-key="${s.key}" data-type="time"
      value="${s.value}" style="width:130px">`;
  }else if(s.type === 'weekdays'){
    const on = s.value || [];
    ctl = WEEKDAYS.map(([k,cn]) => `<label class="chk" style="margin:0 10px 0 0">
      <input type="checkbox" data-key="${s.key}" data-type="weekdays" data-day="${k}"
        ${on.includes(k) ? 'checked' : ''}><span>${cn}</span></label>`).join('');
  }else if(s.type === 'list'){
    ctl = `<textarea id="${id}" data-key="${s.key}" data-type="list" rows="3"
      style="width:100%">${escapeHtml((s.value || []).join('\n'))}</textarea>`;
  }else{
    ctl = `<input type="text" id="${id}" data-key="${s.key}" data-type="text"
      value="${escapeHtml(s.value)}" style="width:190px">`;
  }
  return `<div class="srow">
    <div class="slabel">${escapeHtml(s.label)}${help}</div>
    <div class="sctl">${ctl}</div></div>`;
}

function renderSettings(d){
  settingsSchema = [];
  (d.groups || []).forEach(g => g.items.forEach(s => settingsSchema.push(s)));
  const box = $('settingsBox');
  if(!box) return;
  box.innerHTML = (d.groups || []).map(g => `
    <div class="sgroup">
      <div class="sgtitle">${escapeHtml(g.name)}</div>
      ${g.items.map(settingsField).join('')}
    </div>`).join('');
  const msg = $('settingsMsg');
  if(msg) msg.textContent = `共 ${settingsSchema.length} 项设置`;
}

function collectSettings(){
  const patch = {};
  document.querySelectorAll('#settingsBox [data-key]').forEach(el => {
    const key = el.dataset.key, type = el.dataset.type;
    if(type === 'weekdays'){
      if(!patch[key]) patch[key] = [];
      if(el.checked) patch[key].push(el.dataset.day);
    }else if(type === 'bool'){
      patch[key] = el.checked;
    }else if(type === 'number'){
      if(el.value !== '') patch[key] = Number(el.value);
    }else if(type === 'list'){
      patch[key] = el.value.split('\n').map(x => x.trim()).filter(Boolean);
    }else{
      patch[key] = el.value;
    }
  });
  return patch;
}

async function loadSettingsInto(boxId){
  const r = await api('/api/settings');
  if(boxId && $('settingsBox') !== $(boxId)){
    // 允许调用方指定容器（目前两页都用 settingsBox，留个口子）
    const tmp = document.createElement('div');
    tmp.id = 'settingsBox';
    tmp.innerHTML = '';
  }
  renderSettings(r);
  return r;
}

async function saveSettingsPatch(patch){
  const r = await api('/api/settings', {method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify(patch)});
  renderSettings(r);
  return r;
}
