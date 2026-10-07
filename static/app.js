/* Academia BJJ - app.js */
const BELTS_ADULT = window.BELTS_ADULT || ['Blanco', 'Azul', 'Púrpura', 'Marrón', 'Negro'];
const TIPOS_ACTIVIDAD = window.ACTIVIDADES || ['Gi', 'NoGi', 'JJ Kids', 'MMA', 'Muay Thai', 'Sipalki', 'Clase personalizada'];
// "Muay Thai" -> "muay-thai". Sin esto, toLowerCase() deja un espacio y el
// tipo queda como dos clases de CSS ("muay" y "thai"): ni el color del tag
// ni el borde del bloque se aplicaban y la clase se veia sin estilo.
const slugTipo = (t) => String(t || 'Gi').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '');
const BELTS_KIDS = window.BELTS_KIDS || ['Gris', 'Amarillo', 'Naranja', 'Verde', 'Blanco'];
const BELTS_JUV = window.BELTS_JUV || ['Blanco', 'Gris', 'Amarillo', 'Naranja', 'Verde'];
const catLabel = (c) => esc({ adulto: 'Adulto', juveniles: 'Juveniles', kids: 'Kids' })[c] || esc(c);
// Destinos de una cuota que no le tocan a un profesor (reparto 60/30/10).
const DESTINO_LABEL = { tatami: 'Tatami y academia', administrativo: 'Administrativo' };
window._secCache = window._secCache || {};
let filtroAlumnosCat = 'todos';
const BELTS_POR_CAT = { kids: BELTS_KIDS, juveniles: BELTS_JUV, adulto: BELTS_ADULT };
const CATS_VIDEOS = ['kids', 'juveniles', 'adulto']; // todas las categorías para staff
function beltOptionsPorCategoriaConTodos(catSeleccionada, beltSel) {
  const opts = BELTS_POR_CAT[catSeleccionada] || BELTS_ADULT;
  return `<option value="Todos">Todos</option>` + opts.map(b => `<option ${b === beltSel ? 'selected' : ''}>${esc(b)}</option>`).join('');
}

function verTerminos() {
  const m = $('#tycModal');
  if (!m) return;
  m.style.display = 'flex';
}
function cerrarTerminos() {
  const m = $('#tycModal');
  if (m) m.style.display = 'none';
}
async function aceptarTerminosHoy() {
  try {
    const r = await api('/api/terminos/aceptar', { method: 'POST', body: {} });
    if (r.ok && r.acepto_tyc && window.USER) window.USER.acepto_tyc = r.acepto_tyc;
    toast('Gracias por aceptar los términos ✓');
    cerrarTerminos();
  } catch (e) { toast(e.message); }
}

const $ = (s, e) => (e || document).querySelector(s);
const $$ = (s, e) => [...(e || document).querySelectorAll(s)];
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const escJs = (s) => String(s == null ? '' : s)
  .replace(/\\/g, '\\\\')
  .replace(/'/g, '\\u0027')
  .replace(/"/g, '\\u0022')
  .replace(/&/g, '&amp;')
  .replace(/</g, '&lt;')
  .replace(/>/g, '&gt;');
const num = (s) => (s == null ? '' : Number(s).toLocaleString('es-AR'));
const normBelt = (s) => (s || '').normalize('NFD').replace(/[\u0300-\u036f]/g, '').toLowerCase();
let CHAT_ACTIVO = null;
let CHAT_TIMER = null;
let CHAT_ADJ = null;
let CHAT_LAST_MSG = 0;
let deferredPrompt = null;
let AVISO_AUMENTO = true;

function beltHTML(cinturon) {
  if (!cinturon) return '—';
  return `<span class="belt"><span class="belt-dot bel-${normBelt(cinturon)}"></span>${esc(cinturon)}</span>`;
}

function toast(msg, ms = 3200) {
  const t = $('#toast');
  if (!t) return;
  t.textContent = msg;
  t.className = 'toast show';
  if (/✓|✅|correcto|guardad|actualizad|enviad|cread/.test(msg)) t.classList.add('ok');
  else if (/error|inválid|no se puede|rechazad|falta|conecta/i.test(msg)) t.classList.add('err');
  clearTimeout(t._h);
  t._h = setTimeout(() => t.classList.remove('show'), ms);
}

function vib(ms) { if (navigator.vibrate) { try { navigator.vibrate(ms || 12); } catch (e) {} } }

/* =====================================================================
   MODO ACCESIBLE (personas no videntes / baja visión)
   ===================================================================== */
const ACC_KEY = 'nexo_acc';
function initAcc() {
  let on = false;
  try { on = localStorage.getItem(ACC_KEY) === '1'; } catch (e) {}
  document.body.classList.toggle('acc', on);
  $$('.acc-toggle').forEach(b => {
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
}
function toggleAcc() {
  const on = !document.body.classList.contains('acc');
  document.body.classList.toggle('acc', on);
  try { localStorage.setItem(ACC_KEY, on ? '1' : '0'); } catch (e) {}
  $$('.acc-toggle').forEach(b => {
    b.classList.toggle('on', on);
    b.setAttribute('aria-pressed', on ? 'true' : 'false');
  });
  toast(on ? 'Modo accesible activado. Podés navegar con el lector de pantalla.' : 'Modo accesible desactivado.');
  const act = $('.sec.active');
  if (typeof showSec === 'function' && act) showSec(act.id.replace('sec-', ''));
}

function secHeader(title, sub) {
  return `<div class="sec-head"><span class="brand">NEXO MADRYN · JIU JITSU</span>
    <h2 class="sec-title">${title}</h2>${sub ? `<div class="sec-sub">${sub}</div>` : ''}</div>`;
}

function msgShow(el, text, ok) {
  el.textContent = text;
  el.className = 'msg ' + (ok ? 'ok' : 'error');
}

function fechaLocalDe(d) {
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  return d.getFullYear() + '-' + mm + '-' + dd;
}
function fechaHoyLocal() { return fechaLocalDe(new Date()); }

async function api(path, opts = {}) {
  // cualquier escritura invalida el cache de secciones: si no, se verian datos viejos
  if ((opts.method || 'GET').toUpperCase() !== 'GET' && window._secCache) window._secCache = {};
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = {};
  try { data = await res.json(); } catch (e) {}
  // Sesion vencida (401) dentro de la app: volver al login en vez de mostrar
  // un error generico. Se excluye /api/login para no pisar su mensaje.
  if (res.status === 401 && !path.startsWith('/api/login') && location.pathname.startsWith('/app')) {
    location.href = '/';
    throw new Error('Tu sesion se vencio. Volve a iniciar sesion.');
  }
  if (!res.ok && !data.ok) throw new Error(data.error || 'Error de servidor');
  return data;
}

function focusFirst(el) {
  if (!el) return null;
  const f = el.querySelector('button, input, select, textarea, a[href], [tabindex]:not([tabindex="-1"]), summary');
  if (f) f.focus();
  return f;
}
let _modalLastFocus = null;
function openModal(html) {
  _modalLastFocus = document.activeElement;
  $('#modalBody').innerHTML = html;
  $('#modal').hidden = false;
  // accesibilidad: mover el foco al diálogo para que TalkBack lo lea
  const f = focusFirst($('#modalBody')) || $('#modalClose');
  if (f) f.focus();
}
function closeModal() {
  const mc = $('#modalClose');
  if (mc) mc.style.display = '';
  $('#modal').hidden = true;
  if (_modalLastFocus && document.body.contains(_modalLastFocus)) _modalLastFocus.focus();
}
$('#modalClose') && $('#modalClose').addEventListener('click', closeModal);
$('#modal') && $('#modal').addEventListener('click', (e) => { if (e.target === $('#modal')) closeModal(); });
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && $('#modal') && !$('#modal').hidden) closeModal();
});

/* =====================================================================
   PAGINA DE LOGIN / REGISTRO
   ===================================================================== */
if ($('#tab-login')) {
  initLogin();
  setupFirmas();
  initAcc();
}
function setupFirmas() {
  const conf = [
    { canvas: 'firmaCanvasTyC', hidden: 'regFirmaTyC', done: 'firmaTyCHecha' },
    { canvas: 'firmaCanvasFoto', hidden: 'regFirmaFoto', done: 'firmaFotoHecha' }
  ];
  conf.forEach(c => {
    const cv = document.getElementById(c.canvas);
    if (!cv) return;
    const ctx = cv.getContext('2d');
    let drawing = false, last = null;
    function pos(e) {
      const r = cv.getBoundingClientRect();
      const x = (e.touches ? e.touches[0].clientX : e.clientX) - r.left;
      const y = (e.touches ? e.touches[0].clientY : e.clientY) - r.top;
      return { x, y };
    }
    function startGrabar() {
      const hid = document.getElementById(c.hidden);
      if (hid && !hid.value) {
        hid.value = cv.toDataURL('image/png');
        cv.style.outline = '2px solid var(--good)';
      }
    }
    cv.addEventListener('mousedown', e => {
      drawing = true; last = pos(e);
      cv.setPointerCapture && cv.setPointerCapture(e.pointerId);
      e.preventDefault();
      startGrabar();
    });
    cv.addEventListener('mousemove', e => {
      if (!drawing) return;
      const p = pos(e);
      ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.strokeStyle = '#11151c';
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p;
      startGrabar();
    });
    const stop = () => { drawing = false; last = null; };
    cv.addEventListener('mouseup', stop);
    cv.addEventListener('mouseleave', stop);
    cv.addEventListener('touchstart', e => { e.preventDefault(); drawing = true; last = pos(e); startGrabar(); }, { passive: false });
    cv.addEventListener('touchmove', e => {
      e.preventDefault();
      if (!drawing) return;
      const p = pos(e);
      ctx.lineWidth = 3; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
      ctx.strokeStyle = '#11151c';
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p;
      startGrabar();
    }, { passive: false });
    cv.addEventListener('touchend', stop);
  });
}
function limpiarFirma(quien) {
  const map = { TyC: { cv: 'firmaCanvasTyC', hid: 'regFirmaTyC' }, Foto: { cv: 'firmaCanvasFoto', hid: 'regFirmaFoto' } };
  const m = map[quien];
  if (!m) return;
  const cv = document.getElementById(m.cv);
  const hid = document.getElementById(m.hid);
  if (cv) { cv.getContext('2d').clearRect(0, 0, cv.width, cv.height); cv.style.outline = ''; }
  if (hid) hid.value = '';
}
function firmaPorNombre(quien) {
  const map = { TyC: { cv: 'firmaCanvasTyC', hid: 'regFirmaTyC', inp: 'firmaTyCNombre' }, Foto: { cv: 'firmaCanvasFoto', hid: 'regFirmaFoto', inp: 'firmaFotoNombre' } };
  const m = map[quien];
  if (!m) return;
  const cv = document.getElementById(m.cv);
  const hid = document.getElementById(m.hid);
  const inp = document.getElementById(m.inp);
  if (!cv || !hid) return;
  const nombre = (inp ? inp.value : '').trim();
  if (!nombre) { toast('Escribí tu nombre y apellido en el recuadro para firmar'); if (inp) inp.focus(); return; }
  const ctx = cv.getContext('2d');
  ctx.clearRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#ffffff';
  ctx.fillRect(0, 0, cv.width, cv.height);
  ctx.fillStyle = '#11151c';
  ctx.font = (nombre.length > 24 ? '30px' : '44px') + ' cursive';
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillText(nombre.split(' ')[0] + (nombre.length > 24 ? '…' : ''), cv.width / 2, cv.height / 2);
  hid.value = cv.toDataURL('image/png');
  cv.style.outline = '2px solid var(--good)';
}
function initLogin() {
  const cats = ['adulto', 'juveniles', 'kids'];
  function fillBelts(sel, cat) {
    const opts = BELTS_POR_CAT[cat === 'adulto' ? 'adulto' : (cat === 'juveniles' ? 'juveniles' : 'kids')] || BELTS_ADULT;
    sel.innerHTML = opts.map(b => `<option value="${esc(b)}">${esc(b)}</option>`).join('');
  }
  fillBelts($('#regAlumnoCinturon'), 'adulto');
  fillBelts($('#regProfeCinturon'), 'adulto');
  $('#regAlumnoCat').addEventListener('change', (e) => {
    fillBelts($('#regAlumnoCinturon'), e.target.value);
    actualizarMenorFirma();
  });
  actualizarMenorFirma();

  function actualizarMenorFirma() {
    const sel = $('#regAlumnoCat');
    if (!sel) return;
    const menor = sel.value === 'kids' || sel.value === 'juveniles';
    const tf = $('#tutorFields');
    if (tf) tf.style.display = menor ? '' : 'none';
    const tt = $('#regAlumnoTelTutor');
    if (tt) tt.required = menor;
    const req = $('#firmaFotoReq');
    if (req) req.textContent = menor ? '(obligatorio para menores)' : '(opcional para adultos)';
    const reqTyC = $('#firmaTyCReq');
    if (reqTyC) reqTyC.textContent = menor ? '(obligatorio para menores)' : '(opcional para adultos)';
  }

  $$('.tab').forEach(t => t.addEventListener('click', () => {
    $$('.tab').forEach(x => {
      x.classList.remove('active');
      x.setAttribute('aria-selected', 'false');
    });
    $$('.tabpanel').forEach(x => x.classList.remove('active'));
    t.classList.add('active');
    t.setAttribute('aria-selected', 'true');
    $('#tab-' + t.dataset.tab).classList.add('active');
    $('#loginMsg').className = 'msg';
  }));

  try { fetch('/api/settings').then(r => r.json()).then(s => {
    if (s.academy_name) { $('#academyTitle').textContent = s.academy_name; document.title = s.academy_name; }
  }).catch(() => {}); } catch (e) {}

  /* Preview de cuota según actividades elegidas (alumno).
     window.NEXO_PRECIOS = [precio_1, precio_2, precio_3] desde el servidor:
     1 actividad -> precio_1 (45k), 2 -> precio_2 (60k), 3 o mas -> precio_3 (80k). */
  function actualizarCuotaPreview() {
    const el = $('#regCuotaPreview');
    if (!el) return;
    const n = $$('input[name="actividad"]:checked').length;
    const p1 = Number(window.NEXO_PRECIOS?.[0]) || 0;
    const p2 = Number(window.NEXO_PRECIOS?.[1]) || 0;
    const p3 = Number(window.NEXO_PRECIOS?.[2]) || 0;
    let precio = 0;
    if (n === 1) precio = p1;
    else if (n === 2) precio = p2;
    else if (n >= 3) precio = p3;
    if (precio) el.textContent = 'Tu cuota será $' + precio.toLocaleString('es-AR');
    else el.textContent = 'Tildá una actividad para ver tu cuota.';
  }
  $$('input[name="actividad"]').forEach(cb => cb.addEventListener('change', actualizarCuotaPreview));
  actualizarCuotaPreview();

  $('#tab-login').addEventListener('submit', async (e) => {
    e.preventDefault();
    const m = $('#loginMsg');
    try {
      const d = await api('/api/login', { method: 'POST', body: {
        username: $('#loginUser').value.trim(), password: $('#loginPass').value } });
      if (d.ok) location.href = '/app';
    } catch (err) { msgShow(m, err.message, false); }
  });

  $('#btnOlvidada').addEventListener('click', () => abrirModalRecuperar());

  $('#tab-reg-alumno').addEventListener('submit', async (e) => {
    e.preventDefault();
    const m = $('#loginMsg');
    const cat = $('#regAlumnoCat').value;
    const firmaTyC = $('#regFirmaTyC')?.value || '';
    const firmaFoto = $('#regFirmaFoto')?.value || '';
    const menor = cat === 'kids' || cat === 'juveniles';
    if (menor && !firmaTyC) { msgShow(m, 'Firmá en el recuadro de Términos y Condiciones para crear tu cuenta.', false); return; }
    if (menor && !firmaFoto) { msgShow(m, 'Para menores, el padre, madre o tutor debe firmar la autorización de fotos.', false); return; }
    try {
      const d = await api('/api/register', { method: 'POST', body: {
        role: 'alumno', username: $('#regAlumnoUser').value.trim(),
        password: $('#regAlumnoPass').value, nombre: $('#regAlumnoNombre').value.trim(),
        edad: $('#regAlumnoEdad').value, peso: $('#regAlumnoPeso').value, nacimiento: $('#regAlumnoNac').value,
        categoria: cat, cinturon: $('#regAlumnoCinturon').value,
        actividades: $$('input[name="actividad"]:checked').map(x => x.value),
        tel: $('#regAlumnoTel')?.value || '',
        dni: $('#regAlumnoDni')?.value.trim() || '', direccion: $('#regAlumnoDir')?.value.trim() || '',
        tel_tutor: $('#regAlumnoTelTutor')?.value || '', tel_2: $('#regAlumnoTel2')?.value || '',
        foto_ok: !!($('#regAlumnoFoto')?.checked || false),
        acepto_tyc: !!($('#regAlumnoTyC')?.checked || false),
        firma_tyc: firmaTyC, firma_foto: firmaFoto } });
      if (d.ok) location.href = '/app';
    } catch (err) { msgShow(m, err.message, false); }
  });

  $('#tab-reg-profesor').addEventListener('submit', async (e) => {
    e.preventDefault();
    const m = $('#loginMsg');
    try {
      const d = await api('/api/register', { method: 'POST', body: {
        role: 'profesor', username: $('#regProfeUser').value.trim(),
        password: $('#regProfePass').value, nombre: $('#regProfeNombre').value.trim(),
        codigo: $('#regProfeCodigo').value.trim(), edad: $('#regProfeEdad').value,
        nacimiento: $('#regProfeNac').value, peso: $('#regProfePeso').value, cinturon: $('#regProfeCinturon').value,
        actividades: $$('input[name="actividadProfe"]:checked').map(c => c.value).join(','),
        acepto_tyc: !!($('#regProfeTyC')?.checked || false) } });
      if (d.ok) location.href = '/app';
    } catch (err) { msgShow(m, err.message, false); }
  });
}

/* =====================================================================
   DASHBOARD
   ===================================================================== */
if ($('#content')) initDashboard();

async function obligarNacimiento() {
  const R = USER.role;
  if (esAdmin()) return;
  if (USER.nacimiento && String(USER.nacimiento).trim() !== '') return;
  openModal(`
    <h3>📅 Completá tu fecha de nacimiento</h3>
    <p class="small" style="color:var(--muted)">A tu perfil le falta la fecha de nacimiento. Es obligatoria para la ficha y la categoría del gimnasio.</p>
    <form id="nacForm">
      <div class="field"><label>Fecha de nacimiento</label><input type="date" id="nacInput" required></div>
      <button type="submit" class="btn primary btn-block" id="nacBtn">Guardar</button>
    </form>`);
  $('#modalClose').style.display = 'none';
  $('#nacForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const nac = $('#nacInput').value;
    if (!nac) return;
    const btn = $('#nacBtn'); btn.disabled = true; btn.textContent = 'Guardando...';
    try {
      await api('/api/perfil', { method: 'PUT', body: { nacimiento: nac } });
      USER.nacimiento = nac;
      $('#modalClose').style.display = '';
      closeModal();
      if ($('#sec-perfil')) renderPerfil($('#sec-perfil')).catch(() => {});
      toast('Fecha de nacimiento guardada ✓');
    } catch (err) {
      btn.disabled = false; btn.textContent = 'Guardar';
      toast(err.message);
    }
  });
}

function esAdmin(u){
  u = u || USER;
  if (!u) return false;
  if (u.role === 'admin') return true;
  try { if (u.es_admin) return true; } catch(e){}
  return false;
}

// Seleccion de profesores con checkboxes: el 60% se divide en partes iguales
// SOLO entre los marcados. id = unico por formulario para poder leerlo despues.
function profeChecksHTML(id, profesores, sel) {
  sel = sel || [];
  if (!profesores || !profesores.length) return '<span class="small">Todavía no hay profesores cargados.</span>';
  return `<div id="${id}" style="display:flex;flex-wrap:wrap;gap:8px">
    ${profesores.map(p => `<label style="display:flex;gap:6px;align-items:center;border:1px solid rgba(255,255,255,.16);border-radius:8px;padding:7px 11px;cursor:pointer">
      <input type="checkbox" class="pchk_${id}" value="${p.id}" ${sel.indexOf(p.id) >= 0 ? 'checked' : ''} style="width:16px;height:16px">${esc(p.nombre)}</label>`).join('')}
  </div>`;
}

function profeElegidos(id) {
  return Array.prototype.slice.call(document.querySelectorAll('.pchk_' + id))
    .filter(x => x.checked)
    .map(x => +x.value);
}

function initDashboard() {
  const R = USER.role;
  const A = esAdmin();
  initAcc();
  $('#userRoleLabel').textContent = A ? 'Administrador' : R === 'profesor' ? 'Profesor' : R === 'alumno' ? 'Alumno' : (R || '');
  $('#academyName').textContent = window.ACADEMY_NAME || 'NEXO MADRYN JIU JITSU';

  // barra de navegación inferior estilo Instagram
  const items = [
    { sec: 'inicio', ico: '🏠', lbl: 'Inicio' },
    { sec: 'horarios', ico: '📅', lbl: 'Horarios' },
  ];
  if (R === 'alumno' || R === 'profesor') items.push({ sec: 'mispagos', ico: '🧾', lbl: 'Cuota' });
  else items.push({ sec: 'pagos', ico: '💳', lbl: 'Pagos' });
  items.push({ sec: 'videos', ico: '🎥', lbl: 'Videos' });
  items.push({ sec: 'perfil', ico: '👤', lbl: 'Perfil' });

  $('#bottombar').innerHTML = items.map(i =>
    `<button class="bb-item" data-sec="${i.sec}"><span class="bb-ico">${i.ico}</span><span>${i.lbl}</span></button>`
  ).join('');
  $$('#bottombar .bb-item').forEach(b => b.addEventListener('click', () => {
    $$('#bottombar .bb-item').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    showSec(b.dataset.sec);
  }));

  // gesto táctil: deslizar para cambiar de sección
  const secOrder = items.map(i => i.sec);
  let tX = 0, tY = 0, tEl = null;
  const content = $('#content');
  content.addEventListener('touchstart', (e) => {
    if (e.touches.length !== 1) return;
    tX = e.touches[0].clientX; tY = e.touches[0].clientY;
    tEl = e.target;
  }, { passive: true });
  content.addEventListener('touchend', (e) => {
    if (tEl == null) return;
    if (e.changedTouches.length !== 1) return;
    const dx = e.changedTouches[0].clientX - tX;
    const dy = e.changedTouches[0].clientY - tY;
    tEl = null;
    if (Math.abs(dx) < 65 || Math.abs(dx) < Math.abs(dy)) return;
    if (e.target.closest && e.target.closest('table, .semana, [style*="overflow"], input, select, .asCheck, .alum-actions')) return;
    const activeEl = Array.from($$('.sec')).find(s => s.classList.contains('active'));
    const cur = secOrder.indexOf(activeEl ? activeEl.id.replace('sec-', '') : 'inicio');
    const next = dx < 0 ? cur + 1 : cur - 1;
    if (next < 0 || next >= secOrder.length) return;
    vib(10);
    showSec(secOrder[next]);
  }, { passive: true });

  $('#logoutBtn').addEventListener('click', async () => {
    await api('/api/logout', { method: 'POST' }).catch(() => {});
    location.href = '/';
  });

  // notificaciones
  $('#bellBtn').addEventListener('click', () => {
    const p = $('#notifPanel');
    p.hidden = !p.hidden;
    if (!p.hidden) loadNotifs();
  });
  // tema claro/oscuro
  applyTheme(localStorage.getItem('nexo_tema') || 'dark');
  $('#themeBtn').addEventListener('click', () => {
    applyTheme(document.documentElement.dataset.theme === 'light' ? 'dark' : 'light');
  });
  $('#markAllRead').addEventListener('click', async () => {
    await api('/api/notificaciones/leer_todas', { method: 'POST' }).catch(() => {});
    loadNotifs();
  });
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#bellBtn') && !e.target.closest('#notifPanel')) $('#notifPanel').hidden = true;
  });
  setInterval(() => { if ($('#notifPanel').hidden) refreshBadge(); }, 20000);
  refreshBadge();

  // push + instalación PWA
  setupPush();
  setupInstall();

  obligarNacimiento();

  showSec('inicio');

  // abrir sección indicada en la URL (?sec=...) al volver de una notificación push
  const secParam = new URLSearchParams(location.search).get('sec');
  const seccionesValidas = ['inicio', 'perfil', 'horarios', 'pagos', 'mispagos', 'alumnos',
    'asistencia', 'deudores', 'profesores', 'config', 'mi_asistencia', 'videos', 'chat',
    'muro', 'galeria', 'ranking', 'metas', 'encuestas', 'eventos', 'historial', 'familias', 'diario',
    'planes', 'estadisticas', 'dinero', 'ingresos_extra', 'descuentos'];
  if (secParam && seccionesValidas.includes(secParam)) showSec(secParam);
  history.replaceState(null, '', location.pathname);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.addEventListener('message', (ev) => {
      if (ev.data && ev.data.type === 'nexo-nav' && ev.data.url) {
        const s = new URL(ev.data.url, location.origin).searchParams.get('sec');
        if (s && seccionesValidas.includes(s)) showSec(s);
      }
    });
  }

  // Si el usuario aún no aceptó los Términos y Condiciones, mostrarlos
  try {
    if (window.USER && !window.USER.acepto_tyc) {
      setTimeout(() => verTerminos(), 800);
    }
  } catch (e) {}

  // QR auto-asistencia: si la URL tiene ?qr=1, marcar presente automáticamente
  if (new URLSearchParams(location.search).get('qr') === '1' && (R === 'alumno' || R === 'profesor')) {
    marcarAsistenciaQR();
  }
}

function showSec(name) {
  if (name !== 'chat' && CHAT_TIMER) { clearInterval(CHAT_TIMER); CHAT_TIMER = null; CHAT_ACTIVO = null; }
  $$('.sec').forEach(s => s.classList.remove('active'));
  let el = $('#sec-' + name);
  if (!el) {
    const div = document.createElement('div');
    div.className = 'sec';
    div.id = 'sec-' + name;
    $('#content').appendChild(div);
    el = div;
  }
  el.classList.add('active');
  $$('#bottombar .bb-item').forEach(x => x.classList.toggle('active', x.dataset.sec === name));
  vib(8);
  const renderers = {
    inicio: renderInicio, perfil: renderPerfil, horarios: renderHorarios,
    pagos: renderPagos, mispagos: renderMisPagos, alumnos: renderAlumnos,
    asistencia: renderAsistencia, deudores: renderDeudores,
    profesores: renderProfesores, config: renderConfig,
    mi_asistencia: renderMiAsistencia, videos: renderVideos,
    chat: renderChat, muro: renderMuro, galeria: renderGaleria,
    ranking: renderRanking, metas: renderMetas, encuestas: renderEncuestas,
    eventos: renderEventos, historial: renderHistorial,
    torneos: renderTorneos,
    familias: renderFamilias, diario: renderDiario,
    planes: renderPlanes, estadisticas: renderEstadisticas,
    dinero: renderMiDinero, ingresos_extra: renderIngresosExtra, descuentos: renderDescuentos,
  };
  if (renderers[name]) {
    // cache corta por seccion: volver atras es instantaneo, sin datos viejos (TTL 20s)
    const c = window._secCache && window._secCache[name];
    if (c && (Date.now() - c.ts) < 20000) { el.innerHTML = c.html; return; }
    const p = renderers[name](el);
    if (p && p.then) {
      p.then(() => { if (window._secCache) window._secCache[name] = { html: el.innerHTML, ts: Date.now() }; })
       .catch((err) => {
         // Antes el error se comia con .catch(() => {}) y la seccion se quedaba
         // en "Cargando" para siempre, sin dar ninguna pista. Ahora se muestra.
         console.error('Error renderizando #' + name, err);
         el.innerHTML = `<div class="card">
           <b>No se pudo cargar esta sección.</b>
           <div class="small" style="color:var(--bad);margin-top:6px;word-break:break-word">${esc(err && err.message || err)}</div>
           <button class="btn ghost" style="margin-top:12px" onclick="location.reload()">Reintentar</button>
         </div>`;
       });
    }
  }
}

/* ---------- NOTIFICACIONES ---------- */
async function refreshBadge() {
  try {
    const d = await api('/api/notificaciones');
    const b = $('#bellBadge');
    if (d.no_leidas > 0) { b.textContent = d.no_leidas; b.hidden = false; }
    else b.hidden = true;
  } catch (e) {}
}

async function loadNotifs() {
  const d = await api('/api/notificaciones');
  refreshBadge();
  const el = $('#notifList');
  if (!d.notificaciones.length) { el.innerHTML = '<div class="empty">Sin notificaciones</div>'; return; }
  el.innerHTML = d.notificaciones.map(n => `
    <button class="notif-item ${n.leida ? '' : 'unread'}" onclick="abrirNotif(${n.id},'${escJs(n.link || '')}')">
      <span class="n-title">${esc(n.titulo)}</span>
      <span class="n-msg">${esc(n.mensaje)}</span>
      ${n.link ? `<small style="color:var(--accent2)">Tocá para abrir →</small>` : `<small>${esc(n.fecha)}</small>`}
    </button>`).join('');
}
async function markRead(id) {
  await api('/api/notificaciones/' + id, { method: 'POST' }).catch(() => {});
  loadNotifs();
}
async function abrirNotif(id, link) {
  await api('/api/notificaciones/' + id, { method: 'POST' }).catch(() => {});
  $('#notifPanel').hidden = true;
  if (link) showSec(link);
  else loadNotifs();
}

/* ---------- PUSH ---------- */
function pushDiagEl() {
  const act = document.querySelector('.sec.active #pushDiag');
  return act || $('#pushDiag');
}
async function _pushDiagLine(txt) {
  const el = pushDiagEl();
  if (el) el.textContent = txt;
}
async function setupPush(verbose) {
  if (!('serviceWorker' in navigator)) {
    if (verbose) _pushDiagLine('✗ Este navegador no soporta Service Worker (necesitás Android/Chrome o navegador actualizado).');
    return 0;
  }
  if (!('PushManager' in window)) {
    if (verbose) _pushDiagLine('✗ Este navegador no soporta notificaciones push. Probá con Safari y con la app instalada.');
    return 0;
  }
  const esiOS = /iPad|iPhone|iPod/.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  const instaladaPWA = window.matchMedia && window.matchMedia('(display-mode: standalone)').matches;
  if (esiOS && !instaladaPWA && verbose) {
    _pushDiagLine('✗ En iPhone/iPad las notificaciones solo funcionan con la app INSTALADA. En Safari tocá Compartir ✓ → "Agregar a pantalla de inicio", abrí la app desde ese ícono y volvé a "Activar notificaciones". Requiere iOS 16.4 o superior.');
  }
  try {
    if (verbose) _pushDiagLine('Registrando service worker…');
    await navigator.serviceWorker.register('/sw.js');
    if (verbose) _pushDiagLine('Pidiendo clave VAPID…');
    const keyRes = await (await fetch('/api/vapid_public_key')).json();
    if (!keyRes.key) { if (verbose) _pushDiagLine('✗ El servidor no devolvió la clave VAPID.'); return 0; }
    const reg = await navigator.serviceWorker.ready;
    let sub = await reg.pushManager.getSubscription();
    if (verbose) _pushDiagLine(sub ? 'Suscripción ya existía en el navegador.' : 'Creando nueva suscripción (pedí el permiso)…');
    if (!sub) {
      sub = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(keyRes.key),
      });
    }
    // si el permiso no está concedido, subscribe puede devolver null o lanzar
    if (!sub) { if (verbose) _pushDiagLine('✗ No se pudo crear la suscripción (sin permiso). Activá las notificaciones en Ajustes del sitio.'); return 0; }
    if (Notification && Notification.permission !== 'granted') {
      if (verbose) _pushDiagLine('✗ Falta el permiso de notificaciones en el navegador. Tocá "Permitir" cuando Chrome lo pida.');
      return 0;
    }
    if (verbose) _pushDiagLine('Guardando suscripción en el servidor…');
    const r = await api('/api/push_subscribe', { method: 'POST', body: { subscription: sub.toJSON() } });
    if (verbose) _pushDiagLine('✓ Suscripción guardada correctamente. Ahora probá una notificación.');
    return 1;
  } catch (e) {
    if (verbose) _pushDiagLine('✗ Error al activar: ' + (e && e.message ? e.message : e));
    return 0;
  }
}
function urlBase64ToUint8Array(base64) {
  const pad = '='.repeat((4 - base64.length % 4) % 4);
  const b64 = (base64 + pad).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(b64);
  return Uint8Array.from([...raw].map(c => c.charCodeAt(0)));
}

/* ---------- AVATAR / FOTO ---------- */
function avatarHTML(foto, nombre, size) {
  const initial = esc((nombre || '?')[0].toUpperCase());
  const cls = 'avatar ' + (size || '');
  return `<span class="${cls}">${foto ? `<img loading="lazy" decoding="async" src="${esc(foto)}" onerror="this.parentNode.innerHTML='${initial}'">` : initial}</span>`;
}

function setupFoto() {
  const input = $('#fotoInput');
  if (!input) return;
  input.addEventListener('change', async () => {
    const file = input.files && input.files[0];
    if (!file) return;
    if (!/^image\/(png|jpe?g|webp)/.test(file.type)) { toast('Elegí una imagen (JPG o PNG)'); return; }
    if (file.size > 5 * 1024 * 1024) { toast('La imagen es muy grande (máx 5MB)'); return; }
    const reader = new FileReader();
    reader.onload = async () => {
      try {
        const d = await api('/api/foto', { method: 'POST', body: { foto: reader.result } });
        toast('Foto de perfil actualizada ✓');
        USER.foto = d.foto;
        renderPerfil($('#sec-perfil'));
      } catch (err) { toast(err.message); }
    };
    reader.readAsDataURL(file);
  });
}

/* ---------- INSTALAR APP (PWA) ---------- */
function setupInstall() {
  window.addEventListener('beforeinstallprompt', (e) => {
    e.preventDefault();
    deferredPrompt = e;
    const btn = $('#instalarBtn');
    if (btn) btn.hidden = false;
  });
  const btn = $('#instalarBtn');
  if (btn) btn.addEventListener('click', () => {
    if (deferredPrompt) { deferredPrompt.prompt(); }
    else if (/Android/i.test(navigator.userAgent) || /iPhone|iPad/i.test(navigator.userAgent)) {
      toast('En el teléfono: menú ⋮ o compartir → "Agregar a pantalla de inicio"');
    } else {
      toast('En tu navegador: mirá el ícono de instalar en la barra de direcciones, o usá el menú → "Instalar app"');
    }
  });
}

/* ---------- MI ASISTENCIA (alumno) ---------- */
async function renderMiAsistencia(el) {
  const [d, stat] = await Promise.all([
    api('/api/mi_asistencia'),
    api('/api/mi_estadistica_asistencia').catch(() => null)]);
  const acc = document.body.classList.contains('acc');
  el.innerHTML = `
    ${secHeader('Mis asistencias')}
    ${stat ? `<div class="feed-card">
      <div class="small mb">📊 Mi constancia · % de las clases a las que asistí (últimos 6 meses)</div>
      <div style="display:flex;gap:6px;align-items:flex-end;height:90px">${stat.serie.map((s, i) => {
        const h = s.pct == null ? 6 : Math.max(6, Math.round(s.pct));
        return `<div style="flex:1;display:flex;flex-direction:column;justify-content:flex-end;align-items:center;height:100%">
          <div title="${s.pct == null ? 'Sin clases ese mes' : s.pct + '%'} (${s.asist}/${s.dias})" style="width:70%;height:${h}%;background:var(--accent2);border-radius:4px 4px 0 0;min-height:6px"></div>
          <div class="small" style="margin-top:4px">${esc(stat.meses[i])}</div>
        </div>`;
      }).join('')}</div>
      <div class="small" style="color:var(--muted);margin-top:6px">Asististe a <b>${stat.total}</b> clases en total.</div>
    </div>` : ''}
    <button class="btn primary btn-block mb" onclick="marcarAsistenciaDirecta()">📋 Marcar asistencia de hoy</button>
    <div class="feed">
      <div class="feed-card">
        <div class="stat-card" style="margin-bottom:12px"><div class="num">${d.total}</div><div class="lbl">Clases a las que asistí</div></div>
        ${d.asistencia.length ? d.asistencia.map(a => `
          <div style="padding:8px 0;border-bottom:1px solid var(--line)">
            <div class="flex space-between">
              <div><b>${esc(a.dia)} ${esc(a.hora)}</b> · <span class="tag ${slugTipo(a.tipo)}">${esc(a.tipo)}</span></div>
              <div class="small">${esc(a.fecha)} · ${esc(a.profesor || 'Sin profesor')}</div>
            </div>
            ${a.valorada
              ? `<div class="small" style="color:var(--good)">⭐ Valorada</div>`
              : acc
                  ? `<div class="flex space-between" style="align-items:center;margin-top:6px">
                      <div class="stars" data-cid="${a.clase_id}" data-fecha="${esc(a.fecha)}" role="radiogroup" aria-label="Valoración de esta clase, de 1 a 5">
                        ${[1,2,3,4,5].map(n => `<button type="button" class="star acc-star" data-n="${n}" role="radio" aria-pressed="false" aria-label="${n} de 5">${n}</button>`).join('')}
                      </div>
                      <button type="button" class="btn ghost small" onclick="valorarClase(${a.clase_id})">Guardar ⭐</button>
                    </div>
                    <input class="comentario small" style="margin-top:4px;width:100%" placeholder="Comentario (opcional)">`
                  : `<div class="flex space-between" style="align-items:center;margin-top:6px">
                      <div class="stars" data-cid="${a.clase_id}" data-fecha="${esc(a.fecha)}">
                        ${[1,2,3,4,5].map(n => `<button type="button" class="star" data-n="${n}" aria-label="${n} estrellas">☆</button>`).join('')}
                      </div>
                      <button type="button" class="btn ghost small" onclick="valorarClase(${a.clase_id})">Guardar ⭐</button>
                    </div>
                    <input class="comentario small" style="margin-top:4px;width:100%" placeholder="Comentario (opcional)">`}
            ${a.fecha === d.fecha_hoy ? `<button type="button" class="btn ghost small" style="margin-top:6px" onclick="desmarcarAsistencia(${a.clase_id})">↩ Desmarcar de hoy</button>` : ''}
          </div>`).join('') : '<div class="empty">Todavía no tenés asistencias registradas.</div>'}
      </div>
    </div>`;
  $$('.stars').forEach(s => {
    s.addEventListener('click', (e) => {
      const btn = e.target.closest('.star');
      if (!btn) return;
      const n = parseInt(btn.dataset.n, 10);
      s.dataset.n = n;
      $$('.star', s).forEach((b, i) => {
        const sel = (i + 1) <= n;
        if (b.classList.contains('acc-star')) {
          b.classList.toggle('sel', sel);
          b.setAttribute('aria-pressed', sel ? 'true' : 'false');
        } else {
          b.textContent = i < n ? '★' : '☆';
        }
      });
    });
  });
}

async function valorarClase(claseId, fecha) {
  const starsEl = document.querySelector('.stars[data-cid="' + claseId + '"]');
  const estrellas = parseInt((starsEl ? starsEl.dataset.n : 0) || 0, 10);
  if (!estrellas) { toast('Elegí la cantidad de estrellas (1 a 5)'); return; }
  const fechaSel = fecha || (starsEl ? starsEl.dataset.fecha : '');
  const comentario = starsEl ? starsEl.querySelector('.comentario')?.value || '' : '';
  try {
    await api('/api/clase_valorar', { method: 'POST', body: { clase_id: claseId, fecha: fechaSel, estrellas: estrellas, comentario: comentario } });
    toast('¡Gracias por valorar la clase! ⭐');
    renderMiAsistencia($('#sec-mi_asistencia')).catch(() => {});
  } catch (err) { toast(err.message); }
}

async function marcarAsistencia(claseId) {
  try {
    await api('/api/asistencia_yo', { method: 'POST', body: { clase_id: claseId } });
    toast('¡Asistencia marcada! ✓');
    renderInicio($('#sec-inicio'));
    renderMiAsistencia($('#sec-mi_asistencia')).catch(() => {});
  } catch (err) { toast(err.message); }
}

async function marcarAsistenciaQR(qrToken) {
  try {
    const token = qrToken || new URLSearchParams(location.search).get('t') || '';
    if (!token) { toast('QR no válido. Escaneá el QR físico del gimnasio.'); return; }
    const [horarios] = await Promise.all([api('/api/horarios')]);
    const hoyIdx = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
    const hoyClases = (horarios.horarios || []).filter(h => h.dia === hoyIdx);
    if (hoyClases.length === 0) { toast('No hay clases programadas para hoy'); return; }
    mostrarSelectorClase(hoyClases, async (cid) => {
      await api('/api/asistencia_yo', { method: 'POST', body: { clase_id: cid, qr_token: token } });
    });
  } catch (err) { toast('Error al marcar asistencia'); }
}

async function marcarAsistenciaDirecta() {
  try {
    const [horarios] = await Promise.all([api('/api/horarios')]);
    const hoyIdx = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
    const hoyClases = (horarios.horarios || []).filter(h => h.dia === hoyIdx);
    if (hoyClases.length === 0) { toast('No hay clases programadas para hoy'); return; }
    mostrarSelectorClase(hoyClases, async (cid) => {
      await api('/api/asistencia_directo', { method: 'POST', body: { clase_id: cid } });
    });
  } catch (err) { toast('Error al marcar asistencia'); }
}

async function desmarcarAsistencia(claseId) {
  if (!confirm('¿Querés desmarcar tu asistencia de hoy? Sirve si la marcaste por error.')) return;
  try {
    await api('/api/asistencia_desmarcar', { method: 'POST', body: { clase_id: claseId } });
    toast('Asistencia desmarcada ✓');
    renderMiAsistencia($('#sec-mi_asistencia')).catch(() => {});
    renderInicio($('#sec-inicio')).catch(() => {});
  } catch (err) { toast(err.message); }
}

function mostrarSelectorClase(hoyClases, marcar) {
  const ov = document.createElement('div');
  ov.setAttribute('role', 'dialog');
  ov.setAttribute('aria-modal', 'true');
  ov.setAttribute('aria-label', 'Elegir la clase a la que asistís hoy');
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.88);z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:12px;padding:20px';
  ov.innerHTML = `
    <p style="color:#fff;font-weight:700;font-size:16px;text-align:center;margin:0">¿A qué clase asistís hoy?</p>
    <div role="status" aria-live="polite" style="position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0,0,0,0)" id="selLive"></div>
    <div style="display:flex;flex-direction:column;gap:10px;width:100%;max-width:360px">
      ${hoyClases.map(c => `<button class="selclase" data-id="${c.id}" data-txt="${esc(c.tipo)} · ${esc(c.hora)}" aria-label="${esc(c.tipo)} a las ${esc(c.hora)}, tocar para marcar asistencia" style="background:#e84393;color:#fff;border:none;padding:14px 18px;border-radius:12px;font-size:15px;font-weight:600">${esc(c.tipo)} · ${esc(c.hora)}</button>`).join('')}
    </div>
    <button id="selCerrar" style="background:#d63031;color:#fff;border:none;padding:10px 24px;border-radius:12px">Cerrar</button>`;
  document.body.appendChild(ov);
  ov.querySelectorAll('.selclase').forEach(btn => {
    btn.onclick = async () => {
      try {
        btn.disabled = true;
        btn.textContent = 'Marcando…';
        const cid = parseInt(btn.dataset.id, 10);
        await marcar(cid);
        $('#selLive').textContent = 'Asistencia marcada';
        toast('¡Asistencia marcada! ✓');
        ov.remove();
        renderInicio($('#sec-inicio')).catch(() => {});
        renderMiAsistencia($('#sec-mi_asistencia')).catch(() => {});
      } catch (e) {
        btn.disabled = false;
        btn.textContent = btn.dataset.txt || '';
        $('#selLive').textContent = e && e.message ? e.message : 'Ya tenías asistencia marcada';
        toast(e && e.message ? e.message : 'Ya tenías asistencia marcada');
      }
    };
  });
  ov.querySelector('#selCerrar').onclick = () => ov.remove();
  const primer = ov.querySelector('.selclase');
  if (primer) { try { primer.focus(); } catch (e) {} }
}

/* ---------- ESCÁNER DE QR CON CÁMARA ---------- */
async function abrirScannerQR() {
  if (typeof jsQR === 'undefined') {
    try {
        await new Promise((ok, ko) => { const t = document.createElement('script'); t.src = '/static/jsQR.js'; t.onload = ok; t.onerror = ko; document.head.appendChild(t); });
    } catch (e) { toast('No se pudo cargar el escáner'); return; }
}
  const overlay = document.createElement('div');
  overlay.style.cssText = 'position:fixed;inset:0;background:#000;z-index:9999;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:16px;padding:20px';
  overlay.innerHTML = `
    <video playsinline muted style="width:100%;max-width:420px;border-radius:14px;max-height:65vh"></video>
    <div style="color:#fff;font-size:14px" id="qrMsg">Apuntá la cámara al código QR del gimnasio</div>
    <button class="btn" style="background:#d63031;color:#fff;border:none;padding:12px 22px;border-radius:12px" id="qrCerrar">Cerrar</button>`;
  document.body.appendChild(overlay);
  let stream = null, timer = null, cerrado = false;

  function cerrar() {
    if (cerrado) return;
    cerrado = true;
    if (timer) clearInterval(timer);
    if (stream) stream.getTracks().forEach(t => t.stop());
    overlay.remove();
  }
  overlay.querySelector('#qrCerrar').onclick = cerrar;

  const video = overlay.querySelector('video');
  const msj = (t) => { overlay.querySelector('#qrMsg').textContent = t; };
  const esIOS = /iPhone|iPad|iPod/i.test(navigator.userAgent);
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    msj('Este navegador no soporta cámara o falta HTTPS. Probá recargar (Ctrl+F5) en la URL principal.');
  } else {
    navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
      .then(s => {
        stream = s;
        video.srcObject = s;
        video.setAttribute('autoplay', 'true');
        video.play();
        timer = setInterval(leer, 250);
      })
      .catch(err => {
        let m = '';
        if (err && (err.name === 'NotAllowedError' || err.name === 'PermissionDeniedError')) {
          m = 'Permiso de cámara denegado. Tenés que habilitar la Cámara para este sitio en los ajustes del navegador Chrome, no de una app. En Chrome: Configuración > Sitios > Cámara; o borra el permiso y recargá.';
        } else if (err && (err.name === 'NotFoundError' || err.name === 'OverconstrainedError')) {
          m = 'No se encontró cámara. Probá con la otra cámara (girar el celular).';
        } else if (err && err.name === 'NotReadableError') {
          m = 'La cámara está siendo usada por otra app. Cerrá otras apps y reintentá.';
        } else if (err && err.name === 'SecurityError') {
          m = 'Bloqueado por seguridad: necesitás HTTPS o permisos de cámara en el navegador.';
        } else {
          m = 'Error de cámara: ' + (err && err.message ? err.message : 'desconocido');
        }
        msj(m);
      });
  }

  function leer() {
    const v = video;
    if (!v.videoWidth) return;
    const c = document.createElement('canvas');
    c.width = v.videoWidth; c.height = v.videoHeight;
    c.getContext('2d').drawImage(v, 0, 0, c.width, c.height);
    const img = c.getContext('2d').getImageData(0, 0, c.width, c.height);
    const code = jsQR(img.data, img.width, img.height, { inversionAttempts: 'dontInvert' });
    if (code && code.data && code.data.indexOf('qr=1') !== -1) {
      clearInterval(timer);
      timer = null;
      cerrar();
      const tParam = new URLSearchParams(code.data.split('?')[1] || '').get('t') || '';
      marcarAsistenciaQR(tParam);
    }
  }
}

/* =====================================================================
   VIDEOS (por cinturón)
   ===================================================================== */
function youtubeId(url) {
  const m = String(url || '').match(/(?:youtube\.com\/(?:watch\?v=|embed\/|shorts\/)|youtu\.be\/)([\w-]{6,})/);
  return m ? m[1] : null;
}

function videoMediaHTML(v) {
  if (v.tipo === 'link') {
    const yid = youtubeId(v.url);
    if (yid) return `<iframe src="https://www.youtube.com/embed/${yid}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe>`;
    return `<div class="post-media-link"><a href="${esc(v.url)}" target="_blank" rel="noopener">🔗 ${esc(v.url)}</a></div>`;
  }
  return `<video controls preload="metadata" playsinline data-vid="${v.id}" ontimeupdate="trackProgreso(event)" onpause="flushProgreso(event)" onended="videoTerminado(event)"><source src="${esc(v.url)}"></video>`;
}

function applyTheme(t) {
  if (t !== 'light') t = 'dark';
  document.documentElement.dataset.theme = t;
  localStorage.setItem('nexo_tema', t);
  const ic = document.querySelector('#themeIcon');
  if (ic) ic.innerHTML = t === 'light'
    ? '<path fill="currentColor" d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0 .39-.39.39-1.03 0-1.41l-1.06-1.06zm1.06-10.96c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z"/>'
    : '<path fill="currentColor" d="M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36-.98 1.37-2.58 2.26-4.4 2.26-2.98 0-5.4-2.42-5.4-5.4 0-1.81.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z"/>';
}

const _watchState = {};
function _onTick(video) {
  const id = video.dataset.vid;
  if (!id) return;
  const st = _watchState[id] || (_watchState[id] = { last: -1, watched: 0 });
  const t = video.currentTime;
  if (st.last >= 0) {
    const d = t - st.last;
    if (d > 0 && d <= 3) st.watched += d;
  }
  st.last = t;
}
function _durDe(video) {
  return (isFinite(video.duration) && video.duration > 0) ? video.duration : (video.currentTime || 0);
}
async function trackProgreso(e) {
  const vid = e.currentTarget;
  const id = vid.dataset.vid;
  if (!id) return;
  _onTick(vid);
  const dur = _durDe(vid);
  const seg = Math.floor(vid.currentTime);
  const cerca = dur > 0 && seg >= dur * 0.9;
  if (!cerca && trackProgreso._last === id && (Date.now() - trackProgreso._time) < 5000) return;
  trackProgreso._last = id; trackProgreso._time = Date.now();
  try {
    await api('/api/videos/' + id + '/progress', { method: 'POST',
      body: { segundos: seg, duracion: Math.floor(dur), watched: Math.floor(_watchState[id] ? _watchState[id].watched : 0) } });
  } catch (e) {}
}
async function flushProgreso(e) {
  const vid = e.currentTarget;
  const id = vid.dataset.vid;
  if (!id) return;
  _onTick(vid);
  const dur = _durDe(vid);
  try {
    await api('/api/videos/' + id + '/progress', { method: 'POST',
      body: { segundos: Math.floor(vid.currentTime || dur || 0), duracion: Math.floor(dur), watched: Math.floor(_watchState[id] ? _watchState[id].watched : 0) } });
  } catch (e) {}
}
async function videoTerminado(e) {
  const vid = e.currentTarget;
  const id = vid.dataset.vid;
  if (!id) return;
  try {
    const dur = _durDe(vid);
    const r = await api('/api/videos/' + id + '/progress', { method: 'POST',
      body: { segundos: Math.floor(vid.duration || vid.currentTime || 0), duracion: Math.floor(dur), watched: Math.floor(_watchState[id] ? _watchState[id].watched : 0) } });
    const ok = r && r.completado;
    if (ok) {
      const b = $('#modalMarcarVisto');
      if (b) {
        b.disabled = false;
        b.style.opacity = '';
        b.classList.add('visto');
        b.textContent = '✓ Ya lo vi';
      }
      toast('🎉 Video completado');
    } else {
      toast('Todavía no completaste el video: miralo hasta el final sin saltar.');
    }
    const sec = $('#sec-videos');
    if (sec && sec.classList.contains('active')) renderVideos(sec);
  } catch (err) {}
}

function videoCardHTML(v, isStaff) {
  const beltCls = 'tag ' + (v.belt === 'Todos' ? 'alumno' : 'nogi');
  const visto = v.visto ? ' visto' : '';
  const staffBtns = isStaff ? `<div class="post-views" id="views-${v.id}" hidden></div>` : '';
  const vistoBtn = isStaff || v.tipo === 'link'
    ? `<button class="post-btn${visto}" onclick="marcarVisto(${v.id}, this, ${v.tipo === 'link'})">✓ Visto</button>`
    : `<button class="post-btn${visto}" ${v.completado ? '' : 'disabled style=opacity:.5'} onclick="marcarVisto(${v.id}, this)">✓ Visto</button>
       <div class="small" style="color:var(--muted)">${v.completado ? 'Completado ✓ (podés marcar visto)' : (v.progreso_pct ? 'Progreso ' + v.progreso_pct + '% — mirá el video hasta el final para poder marcarlo' : 'Mirá el video hasta el final para poder marcarlo como visto')}</div>`;
  return `<div class="post-card" id="video-${v.id}">
    <div class="post-head">
      ${avatarHTML('', v.subidor_nombre || 'Profesor', 'sm')}
      <div style="flex:1">
        <b>${esc(v.subidor_nombre || 'Profesor')}</b>
        <div class="small">${esc(v.fecha || '')} · <span class="${beltCls}">${esc(v.belt)}</span> ${v.categoria ? '<span class="tag alumno">' + catLabel(v.categoria) + '</span>' : ''} ${v.actividad ? '<span class="tag nogi">' + esc(v.actividad) + '</span>' : ''}</div>
      </div>
      ${isStaff && v.subido_por === USER.id || esAdmin() ? `<button class="btn bad small" onclick="borrarVideo(${v.id})">🗑</button>` : ''}
    </div>
    <div class="post-media">${videoMediaHTML(v)}</div>
    <div class="post-actions">
      ${vistoBtn}
      <span class="post-count">👁 ${v.vistas} visto${v.vistas === 1 ? '' : 's'}</span>
      ${isStaff ? `<button class="post-btn" onclick="toggleVistos(${v.id}, this)">Quién lo vio</button>` : ''}
    </div>
    <div class="post-caption">
      <b>${esc(v.titulo)}</b>${v.descripcion ? '<div>' + esc(v.descripcion) + '</div>' : ''}
    </div>
    ${staffBtns}
  </div>`;
}

async function renderVideos(el) {
  const R = USER.role;
  const isStaff = R !== 'alumno';
  const qCat = isStaff ? (($('#videoCat') && $('#videoCat').value) || 'Todas') : '';
  const qBelt = isStaff ? (($('#videoBelt') && $('#videoBelt').value) || 'Todos') : '';
  const qAct = isStaff ? (($('#videoAct') && $('#videoAct').value) || 'Todas') : '';
  const all = await api('/api/videos' + (isStaff
    ? '?categoria=' + encodeURIComponent(qCat) + '&belt=' + encodeURIComponent(qBelt) + '&actividad=' + encodeURIComponent(qAct)
    : ''));
  const videos = all.videos;
  const ACTS_VIDEOS = (window.ACTIVIDADES || []).slice();
  const filtros = isStaff ? `
    <div class="flex space-between mb" style="gap:6px;flex-wrap:wrap">
      <select id="videoCat" class="search" style="max-width:120px;padding:9px">
        <option value="Todas">Todas</option>
        ${CATS_VIDEOS.map(c => `<option value="${c}" ${qCat === c ? 'selected' : ''}>${catLabel(c)}</option>`).join('')}
      </select>
      <select id="videoBelt" class="search" style="max-width:150px;padding:9px">
        <option value="Todos">Todos los cinturones</option>
        ${(BELTS_POR_CAT[qCat] || []).map(b => `<option ${b === qBelt ? 'selected' : ''}>${esc(b)}</option>`).join('')}
      </select>
      <select id="videoAct" class="search" style="max-width:150px;padding:9px">
        <option value="Todas">Todas las clases</option>
        ${ACTS_VIDEOS.map(a => `<option ${a === qAct ? 'selected' : ''}>${esc(a)}</option>`).join('')}
      </select>
      <button class="btn primary small" onclick="subirVideo()">＋ Subir video</button>
    </div>` : '<span></span>';
  el.innerHTML = `
    ${secHeader('Videos', isStaff ? 'Subí técnicas para cada cinturón y mirá quién las vio.' : 'Técnicas de las clases que entrenás.')}
    ${filtros}
    <div class="feed" id="videoFeed">
      ${videos.length ? videos.map(v => videoCardHTML(v, isStaff)).join('') : '<div class="feed-card empty">Todavía no hay videos para esta categoría.</div>'}
    </div>`;
  if (isStaff) {
    const catSel = $('#videoCat'), beltSel = $('#videoBelt'), actSel = $('#videoAct');
    catSel.addEventListener('change', () => {
      const c = catSel.value;
      beltSel.innerHTML = `<option value="Todos">Todos los cinturones</option>` +
        (BELTS_POR_CAT[c] || []).map(b => `<option>${esc(b)}</option>`).join('');
      beltSel.value = 'Todos';
      renderVideos(el);
    });
    beltSel.addEventListener('change', () => renderVideos(el));
    if (actSel) actSel.addEventListener('change', () => renderVideos(el));
  }
}

function toggleVistos(vid, btn) {
  const el = (btn ? btn.closest('.post-card').querySelector('#views-' + vid) : $('#views-' + vid));
  if (!el) return;
  if (!el.dataset.cargado) {
    el.dataset.cargado = '1';
    api('/api/videos/' + vid + '/views').then(d => {
      el.innerHTML = (d.vistos && d.vistos.length)
        ? '<div class="small" style="color:var(--muted)">👁 Vieron: ' + d.vistos.map(x => '<b>' + esc(x.nombre) + '</b>').join(', ') + '</div>'
        : '<div class="small" style="color:var(--muted)">Todavía nadie lo vio.</div>';
    }).catch(() => {
      el.innerHTML = '<div class="small" style="color:var(--muted)">No se pudo cargar quién lo vio.</div>';
    });
  }
  el.hidden = !el.hidden;
}

async function marcarVisto(vid, btn) {
  try {
    await api('/api/videos/' + vid + '/view', { method: 'POST' });
    vib(12);
    if (btn) btn.classList.add('visto');
    toast('Marcado como visto ✓');
    const sec = $('#sec-videos');
    if (sec && sec.classList.contains('active')) renderVideos(sec);
  } catch (err) { toast(err.message); }
}

function subirVideo() {
  openModal(`
    <h3>Subir video</h3>
    <form id="vForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>Título</label><input id="vTitulo" required placeholder="Ej: Armbar desde guardia"></div>
      <div class="field" style="grid-column:1/-1"><label>Descripción (opcional)</label><input id="vDesc" placeholder="Qué técnica es, nivel, consejos..."></div>
      <div class="field"><label>Categoría</label><select id="vCat">
        ${CATS_VIDEOS.map(c => `<option value="${c}">${catLabel(c)}</option>`).join('')}</select></div>
      <div class="field"><label>Cinturón para el que es</label><select id="vBelt"></select></div>
      <div class="field"><label>Clase (actividad)</label><select id="vAct">
        <option value="">— Todas las clases —</option>
        ${(window.ACTIVIDADES || []).map(a => `<option>${esc(a)}</option>`).join('')}</select></div>
      <div class="field" style="grid-column:1/-1"><label>O link de YouTube</label><input id="vLink" placeholder="https://youtube.com/watch?v=..."></div>
      <div class="field" style="grid-column:1/-1"><label>O subí un archivo (MP4)</label>
        <input type="file" id="vFile" accept="video/mp4,video/webm,video/ogg,video/quicktime"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Publicar video</button></div>
    </form>`);
  function fillBelt() {
    const cat = $('#vCat').value;
    $('#vBelt').innerHTML = beltOptionsPorCategoriaConTodos(cat, 'Todos');
  }
  fillBelt();
  $('#vCat').addEventListener('change', fillBelt);
  $('#vForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const titulo = $('#vTitulo').value.trim();
    const desc = $('#vDesc').value.trim();
    const belt = $('#vBelt').value;
    const categoria = $('#vCat').value;
    const actividad = $('#vAct') ? $('#vAct').value : '';
    const file = $('#vFile').files && $('#vFile').files[0];
    const link = $('#vLink').value.trim();
    if (!file && !link) { toast('Subí un archivo o pegá un link'); return; }
    const btn = $('button[type="submit"]', $('#vForm'));
    btn.disabled = true; btn.textContent = 'Publicando...';
    try {
      if (file) {
        if (file.size > 150 * 1024 * 1024) { toast('El video es muy grande (máx 150MB). Para videos largos usá un link de YouTube.'); btn.disabled = false; btn.textContent = 'Publicar video'; return; }
        const fd = new FormData();
        fd.append('video', file); fd.append('titulo', titulo); fd.append('descripcion', desc); fd.append('belt', belt); fd.append('categoria', categoria);
        if (actividad) fd.append('actividad', actividad);
        const ctrl = new AbortController();
        const timer = setTimeout(() => ctrl.abort(), 5 * 60 * 1000);
        let res;
        try {
          res = await fetch('/api/videos/upload', { method: 'POST', body: fd, signal: ctrl.signal });
        } catch (e) {
          if (e && e.name === 'AbortError') throw new Error('El envío tardó demasiado. Probá con un video más corto o un link de YouTube.');
          throw new Error('No se pudo conectar al servidor. Revisá tu conexión e intentá de nuevo.');
        }
        clearTimeout(timer);
        let d = {};
        try { d = await res.json(); } catch (e) { d = {}; }
        if (!res.ok) throw new Error(d.error || ('Error al subir (código ' + res.status + '). Probá con un video más chico o un link de YouTube.'));
      } else {
        await api('/api/videos', { method: 'POST', body: { titulo, descripcion: desc, belt, categoria, url: link, actividad } });
      }
      closeModal(); toast('Video publicado ✓');
      renderVideos($('#sec-videos'));
    } catch (err) { toast(err.message); btn.disabled = false; btn.textContent = 'Publicar video'; }
  });
}

async function borrarVideo(vid) {
  if (!confirm('¿Eliminar este video?')) return;
  try {
    await api('/api/videos/' + vid, { method: 'DELETE' });
    toast('Video eliminado');
    const sec = $('#sec-videos');
    if (sec && sec.classList.contains('active')) renderVideos(sec);
    if ($('#sec-perfil')) renderPerfil($('#sec-perfil')).catch(() => {});
  } catch (err) { toast(err.message); }
}

function vidThumb(v) {
  return `<div class="vid-thumb" onclick="verVideo(${v.id})" title="${esc(v.titulo)}">
    <video muted playsinline preload="none"><source src="${esc(v.url)}"></video>
    <span class="play">▶</span>
    <span class="vid-belt">${esc(v.belt)}</span>
  </div>`;
}

async function verVideo(vid) {
  const d = await api('/api/videos');
  const v = d.videos.find(x => x.id === vid);
  if (!v) return;
  const esAlumno = USER.role === 'alumno';
  const conCondicion = esAlumno && v.tipo !== 'link';
  const habilitado = !conCondicion || v.completado;
  const btnTxt = v.visto ? 'Ya lo vi' : 'Marcar como visto';
  openModal(`<h3>${esc(v.titulo)}</h3>
    <div class="post-media" style="margin:10px 0">${videoMediaHTML(v)}</div>
    <div class="flex space-between">
      <span class="${v.belt === 'Todos' ? 'tag alumno' : 'tag nogi'}">${esc(v.belt)}</span>
      <button class="btn primary small" id="modalMarcarVisto" ${habilitado ? '' : 'disabled style=opacity:.5'} onclick="marcarVisto(${v.id}, this)">✓ ${btnTxt}</button>
    </div>
    ${conCondicion && !v.completado ? '<div class="small" style="color:var(--muted);margin-top:6px">Mirá el video hasta el final para poder marcarlo como visto.</div>' : ''}`);
  if (conCondicion && !v.completado) {
    const vidEl = document.querySelector('#modalBody video');
    if (vidEl) {
      const recheck = async (intentos = 5) => {
        const b = $('#modalMarcarVisto');
        try {
          const d2 = await api('/api/videos');
          const v2 = d2.videos.find(x => x.id === vid);
          if (b && v2 && v2.completado) {
            b.disabled = false;
            b.style.opacity = '';
            b.classList.add('visto');
            b.textContent = '✓ Ya lo vi';
            toast('Terminaste el video ✓');
          } else if (intentos > 0) {
            setTimeout(() => recheck(intentos - 1), 700);
          } else if (b) {
            toast('Todavía no completaste el video: miralo hasta el final sin saltar.');
          }
        } catch (err) {
          if (intentos) setTimeout(() => recheck(intentos - 1), 700);
          else if (b) { b.disabled = false; b.style.opacity = ''; }
        }
      };
      vidEl.addEventListener('ended', recheck);
      vidEl.addEventListener('pause', recheck);
    }
  }
}

/* =====================================================================
   INICIO
   ===================================================================== */
async function renderInicio(el) {
  const R = USER.role;
  if (R === 'alumno') {
    const [me, asis, horarios, vids] = await Promise.all([
      api('/api/me'), api('/api/mi_asistencia'), api('/api/horarios'), api('/api/videos')]);
    const c = me.cuota || {};
    const estado = c.estado;
    const tagMap = { al_dia: ['tag-al-dia', 'Al día'], deuda: ['tag-deuda', 'Debe la cuota'], por_vencer: ['tag-por-vencer', 'Por vencer'], becado: ['tag-al-dia', 'Becado'] };
    const [cls, lbl] = tagMap[estado] || ['tag-al-dia', 'Al día'];
    const hoyIdx = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
    const hoyClases = horarios.horarios.filter(h => h.dia === hoyIdx);
    const videos = vids.videos.slice(0, 3);
    el.innerHTML = `
      <div class="feed">
        <div class="feed-card">
          <div class="profile-top">
            ${avatarHTML(me.foto, me.nombre, 'lg')}
            <div style="flex:1">
              <h2 style="margin:0;font-size:20px">${esc(me.nombre)}</h2>
              <div class="small">${beltHTML(me.cinturon)}${me.peso ? ' · ' + esc(me.peso) + ' kg' : ''}${me.edad ? ' · ' + esc(me.edad) + ' años' : ''}</div>
              <div class="profile-stats">
                <div class="pstat"><b>${asis.total}</b><span>clases</span></div>
                <div class="pstat"><b>${me.cuota_mensual ? '$' + num(me.cuota_mensual) : '—'}</b><span>cuota</span></div>
                <div class="pstat"><b>${videos.length}</b><span>técnicas</span></div>
              </div>
            </div>
          </div>
        </div>

        <div class="feed-card">
          <div class="flex space-between">
            <div>
              <div class="lbl small">Estado de cuota (${c.mes}/${c.anio})</div>
              <div class="tag ${cls}" style="margin-top:6px">${lbl}</div>
            </div>
            <button class="btn ghost small" onclick="showSec('mispagos')">Ver mi cuenta</button>
          </div>
          ${estado !== 'al_dia' ? `<p class="small" style="color:#ff9b8f;margin-bottom:0">⚠️ Aboná tu cuota y <b>mandá el comprobante de pago</b>${me.pago_alias ? ' (por transferencia al alias/CVU de la academia)' : ''}. Queda acreditado apenas lo recibimos.</p><button class="btn primary btn-block mt" onclick="avisarPago()">🧾 Mandar comprobante de pago</button>${me.pago_link ? `<a class="btn primary btn-block mt" href="${esc(me.pago_link)}" target="_blank" rel="noopener noreferrer" onclick="marcarLinkPago(this)">🔗 Pagar online</a>` : ''}` : ''}
        </div>

        <div class="feed-card">
          <div class="small mb">📅 Clases de hoy</div>
          ${hoyClases.length ? hoyClases.map(h => `
            <div class="clase-item ${slugTipo(h.tipo)}">
              <span class="hora">${esc(h.hora)}</span> · <span class="tag ${slugTipo(h.tipo)}">${esc(h.tipo || 'Gi')}</span> · <span class="profe">${esc(h.profesor_nombre || 'Sin profesor')}</span>
              <div style="margin-top:6px">${(asis.hoy || []).includes(h.id) ? '<span class="tag tag-al-dia">✓ Asistencia marcada</span>' : `<button class="btn primary small" onclick="abrirScannerQR()">📷 Marcar con QR</button>`}</div>
            </div>`).join('') : '<div class="small" style="color:var(--muted)">Hoy no hay clases cargadas. Mirá la sección Horarios.</div>'}
        </div>

        ${videos.length ? `<div class="post-card" style="padding:0;overflow:hidden">
          <div class="post-head" style="padding:10px 14px 0"><b style="color:var(--accent2)">🎥 Técnicas para vos (${esc(me.cinturon)})</b></div>
          <div class="feed" style="margin:0;padding:10px 14px 14px">${videos.map(v => videoCardHTML(v, false)).join('')}</div>
        </div>` : ''}

        <div class="chips">
          <button class="chip" onclick="showSec('horarios')">📅 Horarios</button>
          <button class="chip" onclick="showSec('videos')">🎥 Videos</button>
          <button class="chip" onclick="showSec('mispagos')">🧾 Mi cuota</button>
          <button class="chip" onclick="showSec('mi_asistencia')">✅ Mi asistencia</button>
          <button class="chip" onclick="showSec('planes')">📋 Planes</button>
          <button class="chip" onclick="showSec('metas')">🎯 Mis metas</button>
          <button class="chip" onclick="showSec('muro')">📢 Muro</button>
          <button class="chip" onclick="showSec('chat')">💬 Chat</button>
          <button class="chip" onclick="showSec('eventos')">🗓️ Eventos</button>
          <button class="chip" onclick="showSec('torneos')">🏆 Torneos</button>
          <button class="chip" onclick="showSec('encuestas')">📊 Encuestas</button>
          <button class="chip" onclick="showSec('diario')">📓 Diario</button>
          <button class="chip" onclick="abrirScannerQR()">📷 Escanear QR</button>
        </div>
      </div>`;
  } else {
    const promesas = [
      api('/api/estadisticas'), api('/api/horarios'), api('/api/videos'), api('/api/cumpleanios'),
      ...(R === 'profesor' ? [api('/api/mi_asistencia')] : [])];
    const r = await Promise.all(promesas);
    const stats = r[0], horarios = r[1], vids = r[2], cums = r[3];
    const asis = R === 'profesor' ? r[4] : null;
    const hoyIdx = new Date().getDay() === 0 ? 6 : new Date().getDay() - 1;
    const hoyClases = horarios.horarios.filter(h => h.dia === hoyIdx);
    const videos = vids.videos.slice(0, 3);
    const chips = [
      `<button class="chip" onclick="showSec('horarios')">📅 Horarios</button>`,
      `<button class="chip" onclick="showSec('pagos')">💳 Registrar pago</button>`,
      `<button class="chip" onclick="showSec('alumnos')">🥋 Alumnos</button>`,
      `<button class="chip" onclick="showSec('asistencia')">✅ Asistencia</button>`,
      `<button class="chip" onclick="showSec('estadisticas')">📊 Asistencias</button>`,
      `<button class="chip" onclick="showSec('planes')">📋 Planes</button>`,
      `<button class="chip" onclick="showSec('deudores')">⚠️ Deudas</button>`,
      `<button class="chip" onclick="showSec('videos')">🎥 Videos</button>`];
    if (esAdmin()) chips.push(`<button class="chip" onclick="showSec('profesores')">🧑‍🏫 Profesores</button>`, `<button class="chip" onclick="showSec('config')">⚙️ Configuración</button>`);
    chips.push(`<button class="chip" onclick="showSec('familias')">👨‍👩‍👧 Familias</button>`);
    chips.push(`<button class="chip" onclick="showSec('diario')">📓 Diario</button>`);
    chips.push(`<button class="chip" onclick="showSec('muro')">📢 Muro</button>`);
    chips.push(`<button class="chip" onclick="showSec('chat')">💬 Chat</button>`);
    chips.push(`<button class="chip" onclick="showSec('ranking')">🏆 Ranking</button>`);
    chips.push(`<button class="chip" onclick="showSec('eventos')">🗓️ Eventos</button>`);
    chips.push(`<button class="chip" onclick="showSec('torneos')">🏆 Torneos</button>`);
    chips.push(`<button class="chip" onclick="showSec('encuestas')">📊 Encuestas</button>`);
    chips.push(`<button class="chip" onclick="showSec('historial')">📈 Historial</button>`);
    chips.push(`<button class="chip" onclick="showSec('galeria')">🖼️ Galería</button>`);
    chips.push(`<button class="chip" onclick="abrirMensajeMasivo()">📣 Mandar mensaje</button>`);
    chips.push(`<button class="chip" onclick="window.open('/qr_print','_blank')">📱 QR de asistencia</button>`);
    if (R === 'profesor') chips.push(`<button class="chip" onclick="showSec('mi_asistencia')">✅ Mi asistencia</button>`, `<button class="chip" onclick="abrirScannerQR()">📷 Escanear QR</button>`);
    chips.push(`<button class="chip" onclick="showSec('dinero')">💰 ${esAdmin() ? 'Reparto de dinero' : 'Mi dinero'}</button>`);
    chips.push(`<button class="chip" onclick="showSec('ingresos_extra')">🎁 Ingresos extra</button>`);
    chips.push(`<button class="chip" onclick="showSec('descuentos')">🏷️ Descuentos</button>`);
    el.innerHTML = `
      <div class="feed">
        ${secHeader('Inicio')}
        <div class="home-grid">
          <div class="stat-card"><div class="num">${stats.total_alumnos}</div><div class="lbl">Activos</div></div>
          <div class="stat-card"><div class="num">$${num(stats.ingresos_mes)}</div><div class="lbl">Cobrado este mes</div></div>
          <div class="stat-card"><div class="num">${stats.clases}</div><div class="lbl">Clases/semana</div></div>
          ${R === 'profesor' ? `<div class="stat-card"><div class="num" style="color:var(--good)">$${num(stats.mi_ingreso_mes)}</div><div class="lbl">Tu dinero este mes</div></div>
          <div class="stat-card"><div class="num" style="color:var(--good)">$${num(stats.mi_ingreso_total)}</div><div class="lbl">Tu dinero total</div></div>` : ''}
        </div>
        <div class="chips">${chips.join('')}</div>
        <div class="feed-card">
          <div class="small mb">📅 Clases de hoy</div>
          ${hoyClases.length ? hoyClases.map(h => `<div class="clase-item ${slugTipo(h.tipo)}"><span class="hora">${esc(h.hora)}</span> · <span class="tag ${slugTipo(h.tipo)}">${esc(h.tipo || 'Gi')}</span> · ${esc(h.nivel || 'Todos')} · <span class="profe">${esc(h.profesor_nombre || 'Sin profesor')}</span>${R === 'profesor' ? `<div style="margin-top:6px">${(asis.hoy || []).includes(h.id) ? '<span class="tag tag-al-dia">✓ Asistencia marcada</span>' : `<button class="btn primary small" onclick="abrirScannerQR()">📷 Marcar con QR</button>`}</div>` : ''}</div>`).join('') : '<div class="small" style="color:var(--muted)">Hoy no hay clases cargadas.</div>'}
        </div>
        ${cums.cumpleanios.length ? `<div class="feed-card">
          <div class="small mb">🎂 Cumpleaños de ${['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'][cums.mes - 1]} <span class="small" style="color:var(--muted)">(${cums.cumpleanios.length})</span></div>
          ${cums.cumpleanios.map(x => `<div class="flex space-between" style="padding:6px 0;border-bottom:1px solid var(--line)"><span>${avatarHTML('', x.nombre, 'sm')} ${esc(x.nombre)}</span><b>${x.hoy ? '🎉 Hoy! · ' : ''}${x.dia}/${cums.mes}${x.edad ? ' · ' + x.edad + ' años' : ''}</b></div>`).join('')}
        </div>` : ''}
        ${videos.length ? `<div class="post-card" style="padding:0;overflow:hidden">
          <div class="post-head" style="padding:10px 14px 0"><b style="color:var(--accent2)">🎥 Últimos videos subidos</b> <button class="btn primary small" onclick="showSec('videos')">Subir video</button></div>
          <div class="feed" style="margin:0;padding:10px 14px 14px">${videos.map(v => videoCardHTML(v, true)).join('')}</div>
        </div>` : `<button class="btn primary btn-block" onclick="showSec('videos')">🎥 Subir el primer video</button>`}
      </div>`;
  }
}

/* =====================================================================
   PERFIL
   ===================================================================== */
let BJJ_TABLAS = null;

function _bjjTablaHTML(tabla) {
  return tabla.map(([nombre, limite]) => {
    const gi = limite === null ? 'sin límite' : `hasta ${limite} kg`;
    const ng = limite === null ? 'sin límite' : `hasta ${(limite - 2.5).toFixed(2).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1')} kg`;
    return `<tr><td style="padding:6px 8px;font-weight:600">${esc(nombre)}</td>
      <td style="padding:6px 8px;text-align:right">${esc(gi)}</td>
      <td style="padding:6px 8px;text-align:right">${esc(ng)}</td></tr>`;
  }).join('');
}

// 'grupo|genero|modalidad|peso|edad' -> 'Medio · Adulto · Con Gi'
// Misma regla que bjj_clave_label() en app.py. Devuelve '' si la clave no
// tiene el formato esperado, para no romper la pantalla con undefined.
function bjjLabelDeClave(clave) {
  const p = String(clave || '').split('|');
  if (p.length !== 5) return '';
  return `${p[3]} · ${p[4]} · ${p[2] === 'nogi' ? 'No-Gi' : 'Con Gi'}`;
}

// Arma la lista de claves validas con la misma forma que hace bjj_claves() en
// Python. Si un dia la IBJJF agrega una division, aparece sola en el selector.
async function bjjListaClaves() {
  if (!BJJ_TABLAS) BJJ_TABLAS = await api('/api/bjj/categorias');
  const P = BJJ_TABLAS.pesos || {};
  const divs = BJJ_TABLAS.divisiones || [];
  const out = [];
  Object.keys(P).forEach(grupo => {
    Object.keys(P[grupo]).forEach(g => {
      ['gi', 'nogi'].forEach(mod => {
        (P[grupo][g][mod] || []).forEach(([nombre]) => {
          divs.forEach(div => out.push({ clave: `${grupo}|${g}|${mod}|${nombre}|${div}`, grupo, g, mod, nombre, div }));
        });
      });
    });
  });
  return out;
}

function pintarBjjElegida(clave) {
  const box = document.getElementById('bjjElegida');
  if (!box) return;
  box.innerHTML = `
    <div style="margin-top:14px;padding-top:12px;border-top:1px solid var(--line)">
      <div class="small mb">🎯 Con qué categoría competís</div>
      ${clave ? `<div class="tag tag-al-dia" style="font-size:.95rem">${esc(bjjLabelDeClave(clave))}</div>`
        : `<div class="small" style="color:var(--muted)">Todavía no elegiste categoría.</div>`}
      <button class="btn ghost btn-block" style="margin-top:10px" onclick="elegirCategoriaBjj()">
        ${clave ? 'Cambiar' : 'Elegir mi categoría'}
      </button>
      <small class="hint">La app la calcula con tu edad y peso, pero si sabés en qué categoría
        te inscribís, elegila acá: al inscripción a un torneo se usa esta.</small>
    </div>`;
}

async function elegirCategoriaBjj() {
  let lista;
  try { lista = await bjjListaClaves(); } catch (e) { return toast(e.message); }
  const me = await api('/api/me').catch(() => ({}));
  const actual = me.bjj_categoria || '';
  const gi = ($$('input[name="bjjGi"]').find(r => r.checked) || {}).value || 'gi';
  const vis = lista.filter(x => x.mod === gi);
  const grupos = {};
  vis.forEach(x => { (grupos[x.grupo + '|' + x.g] = grupos[x.grupo + '|' + x.g] || []).push(x); });

  openModal(`
    <h3>Elegí tu categoría</h3>
    <div class="small" style="color:var(--muted);margin-bottom:10px">
      Andá a la pestaña ${gi === 'nogi' ? 'No-Gi' : 'Con Gi'}.
    </div>
    <div class="field"><label>Buscar</label><input id="bkBuscar" placeholder="Ej: Medio, Master, Pluma" autocomplete="off"></div>
    <div id="bkLista" style="max-height:46vh;overflow:auto">
      ${Object.keys(grupos).map(k => `
        <div class="small" style="margin:12px 0 4px;font-weight:600">${esc(grupos[k][0].div)}</div>
        <div class="chips" data-grupo="${esc(k)}">
          ${grupos[k].map(x => `<label class="chip"><input type="radio" name="bkCat" value="${esc(x.clave)}"${x.clave === actual ? ' checked' : ''}><span>${esc(x.nombre)}</span></label>`).join('')}
        </div>`).join('')}
    </div>
    <div style="margin-top:12px;display:flex;gap:8px">
      <button class="btn ghost" style="flex:1" onclick="elegirCategoriaBjj()">${gi === 'nogi' ? 'Con Gi' : 'No-Gi'}</button>
      ${actual ? `<button class="btn ghost" style="flex:1" onclick="guardarCategoriaBjj('')">Quitar</button>` : ''}
      <button class="btn primary" style="flex:2" onclick="guardarCategoriaBjj((document.querySelector('input[name=bkCat]:checked')||{}).value||'')">Guardar</button>
    </div>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cancelar</button>`);

  const b = document.getElementById('bkBuscar');
  if (b) b.addEventListener('input', () => {
    const q = b.value.trim().toLowerCase();
    $$('#bkLista .chip').forEach(c => {
      c.style.display = !q || c.textContent.toLowerCase().includes(q) ? '' : 'none';
    });
  });
}

async function guardarCategoriaBjj(clave) {
  try {
    const r = await api('/api/bjj/categoria', { method: 'POST', body: { clave: clave || '' } });
    closeModal();
    toast(r.label ? 'Categoría guardada: ' + r.label : 'Categoría quitada');
    pintarBjjElegida(r.clave);
    if (window.USER) USER.bjj_categoria = r.clave;
  } catch (e) { toast(e.message); }
}

async function toggleBjjTabla() {
  const box = document.getElementById('bjjTabla');
  if (!box) return;
  if (box.innerHTML) { box.innerHTML = ''; return; }
  box.innerHTML = '<div class="small" style="color:var(--muted);padding:8px 0">Cargando tabla…</div>';
  try {
    if (!BJJ_TABLAS) BJJ_TABLAS = await api('/api/bjj/categorias');
    const P = BJJ_TABLAS.pesos;
    const divs = (BJJ_TABLAS.divisiones || []);
    const fmt = (v) => (v === null || v === undefined ? 'sin limite'
      : 'hasta ' + String(v).replace(/\.00$/, '').replace(/(\.\d)0$/, '$1') + ' kg');
    const bloque = (titulo, grupo, g) => {
      const gi = P[grupo][g].gi, ng = P[grupo][g].nogi;
      const filas = gi.map(([nombre], i) => `<tr style="border-bottom:1px solid var(--line)">
        <td style="padding:6px 8px;font-weight:600">${esc(nombre)}</td>
        <td style="padding:6px 8px;text-align:right">${esc(fmt(gi[i][1]))}</td>
        <td style="padding:6px 8px;text-align:right">${esc(fmt(ng[i] ? ng[i][1] : null))}</td>
      </tr>`).join('');
      return `<div style="margin-top:14px">
        <div style="font-weight:600;margin-bottom:4px">${esc(titulo)}</div>
        <table style="width:100%;border-collapse:collapse;font-size:.9rem">
          <thead><tr style="border-bottom:1px solid var(--line);color:var(--muted);font-size:.8rem">
            <th style="text-align:left;padding:6px 8px">Categoria</th>
            <th style="text-align:right;padding:6px 8px">Con Gi</th>
            <th style="text-align:right;padding:6px 8px">No-Gi</th></tr></thead>
          <tbody>${filas}</tbody>
        </table></div>`;
    };
    box.innerHTML = `
      <div class="small" style="color:var(--muted);margin-top:12px">
        Tablas oficiales IBJJF. En No-Gi los límites bajan ~2,5 kg porque no se pesa con el kimono puesto.
        La edad de categoría es <b>año del torneo − año de nacimiento</b>.
      </div>
      ${bloque('Masculino · Adulto (18 a 29)', 'adulto', 'M')}
      ${bloque('Femenino · Adulto (18 a 29)', 'adulto', 'F')}
      ${bloque('Masculino · Juvenil (16 y 17)', 'juvenil', 'M')}
      ${bloque('Femenino · Juvenil (16 y 17)', 'juvenil', 'F')}
      <div style="margin-top:16px">
        <div style="font-weight:600;margin-bottom:4px">Divisiones por edad</div>
        <div class="small" style="color:var(--muted)">${divs.join(' · ')}</div>
      </div>`;
  } catch (e) {
    box.innerHTML = '<div class="small" style="color:var(--bad);padding:8px 0">No se pudo cargar la tabla.</div>';
  }
}

async function renderBjj() {
  const out = document.getElementById('bjjOut');
  if (!out) return;
  const gi = ($$('input[name="bjjGi"]').find(r => r.checked) || {}).value || 'gi';
  const val = (s) => { const e = $(s); return e ? e.value : ''; };
  try {
    const r = await api('/api/bjj/calcular', { method: 'POST', body: {
      nacimiento: val('#pNac'), peso: val('#pPeso'), genero: val('#pGenero'), gi } });
    if (!r.division) {
      out.innerHTML = `<span style="color:var(--muted)">Cargá tu fecha de nacimiento, peso y género para ver tu categoría.</span>`;
      return;
    }
    if (!r.ok) {
      out.innerHTML = `<b>${esc(r.division)}</b> (${r.edad} años) — falta: ${esc(r.motivo || 'datos')}`;
      return;
    }
    const tope = r.limite ? `hasta ${r.limite} kg` : 'sin límite de peso';
    out.innerHTML = `<div style="font-size:1.15rem;font-weight:600">${esc(r.division_peso)} · ${esc(r.division)}</div>
      <div style="color:var(--muted);margin-top:4px">${esc(gi === 'nogi' ? 'No-Gi' : 'Con Gi')} · ${esc(tope)} · ${r.edad} años</div>
      <div style="color:var(--muted);margin-top:4px;font-size:.85rem">La edad se cuenta como año del torneo − año de nacimiento, sin importar el día.</div>`;
  } catch (e) {
    out.innerHTML = `<span style="color:var(--muted)">No se pudo calcular la categoría.</span>`;
  }
}

async function renderPerfil(el) {
  const me = await api('/api/me');
  const cat = me.categoria || 'adulto';
  const belts = BELTS_POR_CAT[cat] || BELTS_ADULT;
  const vids = await api('/api/videos').catch(() => ({ videos: [] }));
  let stats = '';
  if (me.role === 'alumno') {
    const asis = await api('/api/mi_asistencia').catch(() => ({ total: 0 }));
    const pagos = await api('/api/mis_pagos').catch(() => ({ pagos: [] }));
    stats = `<div class="profile-stats">
      <div class="pstat"><b>${asis.total}</b><span>clases</span></div>
      <div class="pstat"><b>${pagos.pagos.length}</b><span>pagos</span></div>
      <div class="pstat"><b>${me.peso ? esc(me.peso) + 'kg' : '—'}</b><span>peso</span></div>
    </div>`;
  } else {
    stats = `<div class="profile-stats">
      <div class="pstat"><b>${me.edad || '—'}</b><span>edad</span></div>
      <div class="pstat"><b>${me.peso ? esc(me.peso) + 'kg' : '—'}</b><span>peso</span></div>
      <div class="pstat"><b>${beltHTML(me.cinturon)}</b><span>faixa</span></div>
    </div>`;
  }
  let grid = '';
  if (me.role === 'alumno') {
    const visto = vids.videos.filter(v => v.visto);
    grid = `<div class="feed-card">
      <div class="small mb">📼 Técnicas que viste (${visto.length})</div>
      <div class="vid-grid">${visto.length ? visto.map(v => vidThumb(v)).join('') : '<div class="empty">Todavía no marcaste videos como vistos. Entrá a la sección 🎥 Videos.</div>'}</div>
    </div>`;
  } else {
    const mios = vids.videos.filter(v => v.subido_por === me.id);
    grid = `<div class="feed-card">
      <div class="flex space-between mb"><div class="small">🎥 Videos que subí (${mios.length})</div>
        <button class="btn primary small" onclick="showSec('videos')">＋ Subir</button></div>
      <div class="vid-grid">${mios.length ? mios.map(v => vidThumb(v)).join('') : '<div class="empty">Todavía no subiste videos.</div>'}</div>
    </div>`;
  }
  el.innerHTML = `
    ${secHeader('Mi perfil')}
    <div class="feed">
      <div class="feed-card">
        <div class="profile-top">
          <div style="position:relative">
            ${avatarHTML(me.foto, me.nombre, 'lg')}
            <label style="position:absolute;bottom:-4px;right:-4px;background:var(--accent);color:#fff;width:26px;height:26px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;border:2px solid #000">📷
              <input type="file" id="fotoInput" accept="image/*" style="display:none">
            </label>
          </div>
          <div style="flex:1">
            <h2 style="margin:0;font-size:20px">${esc(me.nombre)}</h2>
            <div class="small">@${esc(me.username)} · ${me.role === 'alumno' ? 'Alumno' : me.role === 'profesor' ? 'Profesor' : 'Administrador'}${me.es_admin && me.role !== 'admin' ? ' · Administrador' : ''}</div>
            ${stats}
          </div>
        </div>
        <div class="small" style="color:var(--muted)">Tocá la cámara 📷 sobre tu foto para cambiarla.</div>
      </div>

      ${grid}

      ${me.role === 'alumno' ? `<div class="feed-card">
        <div class="small mb">🥋 Mi camino (grados)</div>
        <div class="small" style="margin-bottom:8px">Cinturón actual: ${beltHTML(me.cinturon)}${me.proximo_examen ? ' · <b style="color:var(--accent2)">Próximo examen: ' + esc(me.proximo_examen) + '</b>' : ''}</div>
        <div id="misGrados">Cargando…</div>
      </div>` : ''}

      <div class="feed-card" id="miFamiliaCard"></div>

      <div class="feed-card">
        <form id="perfilForm" class="grid2">
          <div class="field"><label>Nombre y apellido</label><input type="text" id="pNombre" value="${esc(me.nombre)}"></div>
          <div class="field"><label>Usuario</label><input type="text" value="${esc(me.username)}" disabled></div>
          <div class="field"><label>DNI</label><input type="text" id="pDni" value="${esc(me.dni || '')}"></div>
          <div class="field"><label>Dirección / domicilio</label><input type="text" id="pDir" placeholder="Ej: Calle 1 N° 123, Madryn" value="${esc(me.direccion || '')}"></div>
          <div class="field"><label>Edad</label><input type="number" id="pEdad" value="${me.edad != null ? me.edad : ''}"></div>
          <div class="field"><label>Peso (kg)</label><input type="number" step="0.1" id="pPeso" value="${me.peso != null ? me.peso : ''}"></div>
          <div class="field"><label>Género (para categorías de competición)</label><select id="pGenero">
            <option value="">Sin definir</option>
            <option value="M" ${me.genero === 'M' ? 'selected' : ''}>Masculino</option>
            <option value="F" ${me.genero === 'F' ? 'selected' : ''}>Femenino</option></select></div>
          <div class="field"><label>Teléfono</label><input type="tel" id="pTel" value="${esc(me.tel || '')}"></div>
          <div class="field"><label>📞 Teléfono del padre/madre/tutor ${cat === 'kids' || cat === 'juveniles' ? '<span style="color:#ff9b8f">(obligatorio)</span>' : '(opcional)'}</label><input type="tel" id="pTelTutor" placeholder="Ej: 299 1234567" value="${esc(me.tel_tutor || '')}"></div>
          <div class="field"><label>📞 Segundo teléfono (opcional)</label><input type="tel" id="pTel2" placeholder="Otro teléfono de contacto" value="${esc(me.tel_2 || '')}"></div>
          <div class="field"><label>Fecha de nacimiento</label><input type="date" id="pNac" value="${me.nacimiento || ''}"></div>
          <div class="field"><label>Categoría</label><select id="pCat">
            ${CATEGORIAS.map(c => `<option value="${c}" ${c === cat ? 'selected' : ''}>${catLabel(c)}</option>`).join('')}</select></div>
          <div class="field"><label>Cinturón / Faixa${me.role === 'alumno' ? ' <span class="small">(lo define la academia)</span>' : ''}</label><select id="pCinturon" ${me.role === 'alumno' ? 'disabled' : ''}>
            ${belts.map(b => `<option ${b === me.cinturon ? 'selected' : ''}>${esc(b)}</option>`).join('')}</select></div>
          <div class="field" style="grid-column:1/-1"><label>Actividades</label>
            <div class="chips">
              ${TIPOS_ACTIVIDAD.map(ac => `<label class="chip"><input type="checkbox" name="pAct" value="${ac}" ${(me.actividades || '').split(',').map(s => s.trim()).includes(ac) ? 'checked' : ''}><span>${ac}</span></label>`).join('')}
            </div>
          <div class="field"><label>Cambiar contraseña (opcional)</label><input type="password" id="pPass" placeholder="Nueva contraseña"></div>
          <div class="field" style="grid-column:1/-1"><label>🩺 Ficha médica (opcional)</label><textarea id="pMedic" rows="2" placeholder="Lesiones, alergias, medicación, operaciones...">${esc(me.medic_info || '')}</textarea></div>
          ${cat === 'kids' || cat === 'juveniles' ? `<div class="field" style="grid-column:1/-1"><label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" id="pFotoOk" style="width:18px;height:18px" ${me.foto_ok ? 'checked' : ''}>
            <span>Autorizo como mayor/padre/madre/tutor que <b>las fotos de este/a menor puedan ser expuestas</b> (redes y muro). <span style="color:#ff9b8f">(obligatorio para menores)</span></span></label></div>` : ''}
          <div class="field" style="grid-column:1/-1"><label>📞 Contacto de emergencia (opcional)</label><input type="text" id="pEmer" placeholder="Nombre y teléfono" value="${esc(me.emergency_contact || '')}"></div>
          <div class="field" style="grid-column:1/-1">
            <div class="flex space-between" style="align-items:center">
              <label>🩺 Mi ficha médica (responsable: vos)</label>
              ${me.ficha_fecha ? `<span class="small" style="color:var(--muted)">Actualizada el ${esc(me.ficha_fecha)}</span>` : `<span class="tag tag-deuda">Sin completar</span>`}
            </div>
            <div class="grid2" style="margin-top:6px">
              <div class="field" style="margin:0"><label style="font-weight:500">Enfermedades / condiciones</label><input id="pMedEnf" placeholder="Ej: asma, presión alta" value="${esc(me.medic_enfermedades || '')}"></div>
              <div class="field" style="margin:0"><label style="font-weight:500">Alergias</label><input id="pMedAlergias" placeholder="Ej: penicilina, polvo" value="${esc(me.medic_alergias || '')}"></div>
              <div class="field" style="margin:0"><label style="font-weight:500">Medicación actual</label><input id="pMedMed" placeholder="Ej: salbutamol, insulina" value="${esc(me.medic_medicacion || '')}"></div>
              <div class="field" style="margin:0"><label style="font-weight:500">Lesiones / operaciones</label><input id="pMedLes" placeholder="Ej: rodilla operada 2024" value="${esc(me.medic_lesiones || '')}"></div>
            </div>
          </div>
          <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Guardar cambios</button></div>
        </form>
      </div>

      <div class="feed-card">
        <div class="small mb">🥋 Mi categoría de competición (BJJ · IBJJF)</div>
        <div class="chips" style="margin-bottom:10px">
          <label class="chip"><input type="radio" name="bjjGi" value="gi" checked><span>Con Gi</span></label>
          <label class="chip"><input type="radio" name="bjjGi" value="nogi"><span>No-Gi</span></label>
        </div>
        <div id="bjjOut" class="small">Cargando…</div>
        <div id="bjjElegida"></div>
        <button class="btn ghost btn-block" style="margin-top:10px" onclick="toggleBjjTabla()">📋 Ver todas las categorías de peso</button>
        <div id="bjjTabla"></div>
      </div>

      ${(me.medic_enfermedades || me.medic_alergias || me.medic_medicacion || me.medic_lesiones || me.medic_info) ? `<div class="feed-card">
        <div class="small mb">🩺 Mi ficha médica</div>
        ${me.ficha_fecha ? `<div class="small" style="color:var(--muted);margin-bottom:6px">Última actualización: ${esc(me.ficha_fecha)}</div>` : ''}
        ${me.medic_enfermedades ? `<div class="small" style="margin-bottom:4px"><b>Enfermedades:</b> ${esc(me.medic_enfermedades)}</div>` : ''}
        ${me.medic_alergias ? `<div class="small" style="margin-bottom:4px"><b>Alergias:</b> ${esc(me.medic_alergias)}</div>` : ''}
        ${me.medic_medicacion ? `<div class="small" style="margin-bottom:4px"><b>Medicación:</b> ${esc(me.medic_medicacion)}</div>` : ''}
        ${me.medic_lesiones ? `<div class="small" style="margin-bottom:4px"><b>Lesiones:</b> ${esc(me.medic_lesiones)}</div>` : ''}
        ${me.medic_info ? `<p class="small" style="white-space:pre-wrap;margin:6px 0 0">${esc(me.medic_info)}</p>` : ''}
      </div>` : ''}
      ${me.emergency_contact ? `<div class="feed-card">
        <div class="small mb">📞 Contacto de emergencia</div>
        <p class="small" style="margin-bottom:0">${esc(me.emergency_contact)}</p>
      </div>` : ''}

      <div class="feed-card">
        <div class="small mb">📲 Notificaciones push</div>
        <p class="small" style="margin-bottom:8px">Activá las notificaciones para que te lleguen avisos de mensajes y novedades al celular, aun con la app cerrada.</p>
        <button class="btn primary btn-block" onclick="perfilActivarPush()">🔔 Activar notificaciones</button>
        <button class="btn ghost btn-block mt" onclick="testPush()">🧪 Probar notificación</button>
        <p class="small mt" id="pushDiag" style="color:var(--muted);margin-bottom:0"></p>
      </div>

      ${me.role === 'alumno' ? `<div class="feed-card">
        <div class="small mb">⏸ Pausa temporal</div>
        ${me.en_pausa
          ? `<p class="small" style="margin-bottom:8px">Estás de pausa${me.pausa_hasta ? ' <b>hasta el ' + esc(me.pausa_hasta) + '</b>' : ''}. Mientras dure la pausa <b>no se te cobra la cuota</b> ni contás como deudor.</p>
             <button class="btn primary btn-block" onclick="cancelarPausaMi()">✅ Terminar mi pausa</button>`
          : `<p class="small" style="color:var(--muted);margin-bottom:8px">¿Te vas de viaje o no vas a poder entrenar un tiempo? Activá una pausa y no se te cobra ni contás como deudor.</p>
             <div class="grid2" style="margin-bottom:8px">
               <div class="field" style="margin:0"><label>Desde</label><input type="date" id="pausaDesde" value="${fechaHoyLocal()}"></div>
               <div class="field" style="margin:0"><label>Hasta</label><input type="date" id="pausaHasta" value=""></div>
             </div>
             <button class="btn primary btn-block" onclick="activarPausaMi()">⏸ Activar pausa</button>`}
      </div>` : ''}

      <div class="feed-card">
        <div class="small mb">📲 ¿Querés la app como si fuera de tu teléfono?</div>
        <button class="btn ghost" id="instalarBtn" onclick="instalarManual()">📲 Instalar la app</button>
        <p class="small" style="margin-bottom:0">Se instala en tu pantalla de inicio sin pasar por Google. (En el celular: menú → "Agregar a pantalla de inicio".)</p>
      </div>

      <div class="feed-card">
        <div class="small mb">🔐 Seguridad de la cuenta</div>
        <p class="small" style="margin-bottom:6px">Configurá una <b>pregunta de seguridad</b> para poder recuperar tu contraseña si alguna vez la olvidás. También podés cambiar tu contraseña acá.</p>
        <div class="field"><label>Pregunta de seguridad</label><input id="pSecQ" placeholder="Ej: ¿Nombre de tu mascota?" value="${esc(me.security_q || '')}"></div>
        <div class="field"><label>Respuesta</label><input id="pSecA" placeholder="Tu respuesta (se guarda visible solo para vos)"></div>
        <div class="field"><label>Nueva contraseña (opcional)</label><input type="password" id="pSecPass" placeholder="Dejalo en blanco para no cambiarla"></div>
        <button class="btn primary btn-block" onclick="guardarSeguridad()">Guardar seguridad</button>
      </div>
      <div class="feed-card">
        <div class="small mb">📄 Términos y Condiciones</div>
        ${me.acepto_tyc
          ? `<p class="small" style="margin-bottom:0">✔ Aceptaste los <b>Términos y Condiciones y la Política de Privacidad</b> el <b>${esc(me.acepto_tyc)}</b>. <a href="javascript:void(0)" onclick="verTerminos()" style="color:var(--accent2)">Ver términos</a></p>`
          : `<p class="small" style="margin-bottom:6px">Todavía no aceptaste los Términos y Condiciones.</p><button class="btn primary btn-block" onclick="verTerminos()">📄 Aceptar términos y condiciones</button>`}
      </div>
      ${me.role === 'alumno' ? `
      <div class="feed-card">
        <div class="small mb" style="color:var(--bad)">¿No vas a seguir entrenando?</div>
        <button class="btn bad btn-block" onclick="desactivarMiCuenta()">🚫 Desactivar mi cuenta</button>
        <p class="small" style="margin-bottom:0">Tus datos se guardan; si algún día volvés, el administrador puede reactivarte.</p>
      </div>` : ''}
    </div>`;
  ['#pNac', '#pPeso', '#pGenero'].forEach(sel => {
    const el = $(sel);
    if (el) { el.addEventListener('change', renderBjj); el.addEventListener('input', renderBjj); }
  });
  $$('input[name="bjjGi"]').forEach(r => r.addEventListener('change', renderBjj));
  renderBjj();
  pintarBjjElegida(me.bjj_categoria || '');
  $('#pCat').addEventListener('change', (e) => {
    const b = BELTS_POR_CAT[e.target.value] || BELTS_ADULT;
    $('#pCinturon').innerHTML = b.map(x => `<option>${esc(x)}</option>`).join('');
  });
  $('#perfilForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/perfil', { method: 'PUT', body: {
        nombre: $('#pNombre').value.trim(), edad: $('#pEdad').value,
        peso: $('#pPeso').value, genero: $('#pGenero').value, cinturon: $('#pCinturon').value,
        categoria: $('#pCat').value,
        actividades: $$('input[name="pAct"]:checked').map(x => x.value),
        tel: $('#pTel').value, nacimiento: $('#pNac').value,
        medic_info: $('#pMedic')?.value || '', emergency_contact: $('#pEmer').value,
        medic_enfermedades: $('#pMedEnf')?.value || '', medic_alergias: $('#pMedAlergias')?.value || '',
        medic_medicacion: $('#pMedMed')?.value || '', medic_lesiones: $('#pMedLes')?.value || '',
        ficha_fecha: new Date().toLocaleDateString('es-AR'),
        tel_tutor: $('#pTelTutor')?.value || '', tel_2: $('#pTel2')?.value || '',
        dni: $('#pDni')?.value.trim() || '', direccion: $('#pDir')?.value.trim() || '',
        foto_ok: !!($('#pFotoOk')?.checked || false),
        password: $('#pPass').value } });
      toast('Perfil actualizado ✓'); renderPerfil(el);
    } catch (err) { toast(err.message); }
  });
  setupFoto();
  if (me.role === 'alumno') {
    api('/api/mis_grados').then(d => {
      const box = $('#misGrados');
      if (!box) return;
      box.innerHTML = (d.grados && d.grados.length
        ? d.grados.map(g => `<div class="flex space-between small" style="padding:4px 0;border-bottom:1px dashed var(--line)">
            <span>🥋 ${esc(g.cinturon)}</span><span style="color:var(--muted)">${esc(g.fecha || '')}${g.notas ? ' · ' + esc(g.notas) : ''}</span>
          </div>`).join('')
        : '<div class="small" style="color:var(--muted)">Todavía no tenés grados registrados.</div>');
    }).catch(() => { const b = $('#misGrados'); if (b) b.textContent = '—'; });
    renderMiFamilia($('#miFamiliaCard'));
  }
}

async function renderMiFamilia(box) {
  if (!box) return;
  try {
    const [d, hijos] = await Promise.all([api('/api/mi_familia'), api('/api/mis_hijos').catch(() => ({ familia: null, hijos: [] }))]);
    if (!d.familia) {
      box.innerHTML = `
        <div class="small mb">👨‍👩‍👧 Plan familiar (modo Padre)</div>
        <p class="small" style="color:var(--muted);margin:0">Si entrenás con tu hija/o menor, podés crear un grupo familiar y gestionar su cuenta desde tu perfil.</p>
        <button class="btn primary btn-block mt" onclick="activarModoPadre()">🤝 Soy padre/madre: crear grupo</button>`;
      return;
    }
    const soyTitular = d.familia.titular_id === USER.id;
    box.innerHTML = `
      <div class="small mb">👨‍👩‍👧 Mi familia</div>
      <div class="small" style="margin-bottom:8px"><b>${esc(d.familia.nombre)}</b> · ${d.familia.miembros.length} miembro${d.familia.miembros.length === 1 ? '' : 's'}</div>
      ${d.familia.miembros.map(m => `
        <div class="flex space-between" style="align-items:center;padding:6px 0;border-bottom:1px dashed var(--line)">
          <span>${avatarHTML(m.foto, m.nombre, 'sm')} <b>${esc(m.nombre)}</b> ${m.es_titular ? '<span class="tag tag-al-dia">Titular</span>' : ''}
            <span class="small" style="color:var(--muted)">· ${esc(m.relacion)}</span></span>
          <span class="small">$${num(m.cuota_final)}/mes${m.descuento ? ' <span style="color:var(--good)">(-' + num(m.descuento) + ')</span>' : ''}</span>
        </div>`).join('')}
      ${soyTitular && hijos.hijos && hijos.hijos.length ? `
        <div class="small mb mt" style="font-weight:700">👶 Hijos/as a mi cargo</div>
        ${hijos.hijos.map(h => `
          <div style="padding:6px 0;border-bottom:1px dashed var(--line)">
            <div class="flex space-between" style="align-items:center">
              <span>${avatarHTML(h.foto, h.nombre, 'sm')} <b>${esc(h.nombre)}</b>
                ${h.en_pausa ? '<span class="tag tag-pausa">⏸ En pausa</span>' : ''}
                <span class="small" style="color:var(--muted)">· ${catLabel(h.categoria)} · ${beltHTML(h.cinturon)}</span></span>
              <button class="btn ghost small" onclick="quitarHijo(${h.id})" title="Desvincular">🗑</button>
            </div>
            <div class="small" style="margin-top:4px">
              <span class="tag ${h.cuota ? (h.cuota.estado === 'al_dia' ? 'tag-al-dia' : h.cuota.estado === 'por_vencer' ? 'tag-por-vencer' : 'tag-deuda') : 'tag-al-dia'}">${h.cuota && h.cuota.estado === 'al_dia' ? '💰 Cuota al día' : h.cuota && h.cuota.estado === 'por_vencer' ? '💰 Cuota por vencer' : '💰 Debe la cuota'}</span>
              <span class="tag tag-alumno">🥋 ${h.asistencias} clases</span>
              ${h.ultima_fecha ? `<span style="color:var(--muted)">última: ${esc(h.ultima_fecha)}</span>` : ''}
            </div>
          </div>`).join('')}` : ''}
      <div class="flex mt" style="gap:8px;flex-wrap:wrap">
        ${soyTitular ? `<button class="btn primary" onclick="abrirAltaHijo()">➕ Alta de hijo/a menor</button>
        <button class="btn ghost" onclick="vincularHijo()">🔗 Vincular cuenta existente</button>` : ''}
      </div>
      <p class="small" style="color:var(--muted);margin-bottom:0;margin-top:6px">Descuento familiar: ${(d.escala || []).map(e => `<b>${e.integrantes} ${e.integrantes === 4 ? 'o más' : ''}:</b> ${e.pct}%`).join(' · ')}. Con 2 o más integrantes, <b>todos</b> pagan con descuento${soyTitular && d.descuento ? ` (este grupo: <b>${d.descuento}%</b>)` : ''}.</p>`;
  } catch (e) {
    box.innerHTML = '';
  }
}

async function activarModoPadre() {
  try {
    await api('/api/familia', { method: 'POST' });
    toast('Grupo familiar creado 👨‍👩‍👧');
    renderMiFamilia($('#miFamiliaCard'));
  } catch (err) { toast(err.message); }
}

function abrirAltaHijo() {
  openModal(`
    <h3>👶 Alta de hijo/a menor</h3>
    <form id="hijoForm" class="grid2" autocomplete="off">
      <div class="field" style="grid-column:1/-1"><label>Nombre y apellido del menor</label><input id="hNombre" required placeholder="Ej: Martina Pérez" autocomplete="off"></div>
      <div class="field"><label>Usuario (para que ingrese)</label><input id="hUsuario" required placeholder="Ej: martina2026" autocomplete="off"></div>
      <div class="field"><label>Contraseña</label><input type="password" id="hPassword" required placeholder="Mínimo 4 caracteres" autocomplete="new-password"></div>
      <div class="field"><label>Edad</label><input type="number" id="hEdad" required min="3" max="17" autocomplete="off"></div>
      <div class="field"><label>Fecha de nacimiento</label><input type="date" id="hNac" required max="${fechaHoyLocal()}" autocomplete="off"></div>
      <div class="field"><label>Categoría</label><select id="hCat">
        <option value="kids">Kids (niños/as)</option>
        <option value="juveniles">Juveniles</option></select></div>
      <div class="field"><label>Cinturón / Faixa</label><select id="hBelt">
        ${BELTS_ADULT.map(b => `<option>${esc(b)}</option>`).join('')}</select></div>
      <div class="field" style="grid-column:1/-1"><label>Actividades</label>
        <div class="chips">
          ${TIPOS_ACTIVIDAD.map(ac => `<label class="chip"><input type="checkbox" name="hAct" value="${ac}"><span>${ac}</span></label>`).join('')}
        </div>
      <div class="field" style="grid-column:1/-1"><label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="hFotoOk" style="width:18px;height:18px">
        <span>Autorizo como mayor/padre/madre que <b>las fotos de este/a menor puedan ser expuestas</b> (redes y muro). <span style="color:#ff9b8f">(obligatorio)</span></span></label></div>
      <div class="field" style="grid-column:1/-1"><label>✍️ Firmá Términos y Condiciones (nombre del padre/madre)</label><input id="hFirmaTyC" required placeholder="Tu nombre y apellido"></div>
      <div class="field" style="grid-column:1/-1"><label>✍️ Firmá autorización de fotos (nombre del padre/madre)</label><input id="hFirmaFoto" required placeholder="Tu nombre y apellido"></div>
      <p class="small" style="grid-column:1/-1;color:var(--muted);margin:0">El teléfono del tutor responsable será el de tu perfil.</p>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Crear cuenta del menor</button></div>
    </form>`);
  const CATS = { kids: BELTS_KIDS, juveniles: BELTS_JUV };
  $('#hCat').addEventListener('change', () => {
    const opts = CATS[$('#hCat').value] || BELTS_ADULT;
    $('#hBelt').innerHTML = opts.map(b => `<option>${esc(b)}</option>`).join('');
  });
  $('#hijoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const nombre = $('#hNombre').value.trim();
    const usuario = $('#hUsuario').value.trim();
    if (nombre && window.USER && nombre.toLowerCase() === (window.USER.nombre || '').toLowerCase()) {
      toast('⚠️ El campo "Nombre y apellido del menor" quedó con TU nombre (autocompletado por el navegador). Cambialo por el nombre del niño/a.'); return;
    }
    if (usuario && window.USER && usuario.toLowerCase() === (window.USER.username || '').toLowerCase()) {
      toast('⚠️ El usuario quedó con el tuyo (autocompletado). Poné un usuario nuevo para el niño/a.'); return;
    }
    try {
      await api('/api/familia/hijos', { method: 'POST', body: {
        nombre: nombre, username: usuario,
        password: $('#hPassword').value, edad: +$('#hEdad').value,
        categoria: $('#hCat').value, cinturon: $('#hBelt').value,
        nacimiento: $('#hNac').value,
        foto_ok: $('#hFotoOk').checked, firma_tyc: $('#hFirmaTyC').value.trim(),
        firma_foto: $('#hFirmaFoto').value.trim(),
        actividades: $$('input[name="hAct"]:checked').map(x => x.value) } });
      closeModal(); toast('Cuenta del menor creada y vinculada ✓');
      renderMiFamilia($('#miFamiliaCard'));
    } catch (err) { toast(err.message); }
  });
}

function vincularHijo() {
  openModal(`
    <h3>🔗 Vincular cuenta existente</h3>
    <div class="small" style="color:var(--muted);margin-bottom:10px">Ingresá el <b>usuario</b> con el que tu hijo/a ya se registró (Kids/Juveniles). El teléfono del tutor de esa cuenta debe coincidir con el de tu perfil.</div>
    <form id="vincForm">
      <div class="field"><label>Usuario del menor</label><input id="vkUsuario" required placeholder="Ej: martina2026"></div>
      <div class="field"><button class="btn primary btn-block" type="submit">Vincular</button></div>
    </form>`);
  $('#vincForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/familia/vincular', { method: 'POST', body: { username: $('#vkUsuario').value.trim() } });
      closeModal(); toast('Cuenta vinculada ✓');
      renderMiFamilia($('#miFamiliaCard'));
    } catch (err) { toast(err.message); }
  });
}

async function quitarHijo(uid) {
  if (!confirm('¿Querés desvincular a este menor de tu grupo familiar?')) return;
  try {
    await api('/api/familia/hijos/' + uid, { method: 'DELETE' });
    toast('Desvinculado ✓');
    renderMiFamilia($('#miFamiliaCard'));
  } catch (err) { toast(err.message); }
}

function instalarManual() {
  if (deferredPrompt) { deferredPrompt.prompt(); return; }
  toast('En el navegador: mirá el ícono de instalación (🚀) en la barra de direcciones, o menú → "Instalar". En el celular: menú → "Agregar a pantalla de inicio".');
}

async function desactivarMiCuenta() {
  if (!confirm('¿Seguro que querés desactivar tu cuenta?\n\nNo vas a poder entrar hasta que el administrador te reactive.')) return;
  try {
    await api('/api/perfil/desactivar', { method: 'POST' });
    toast('Tu cuenta fue desactivada. ¡Esperamos verte pronto!');
    setTimeout(() => { location.href = '/'; }, 800);
  } catch (e) { toast(e.message); }
}

async function activarPausaMi() {
  const desde = $('#pausaDesde').value;
  const hasta = $('#pausaHasta').value;
  if (!hasta) { toast('Indicá hasta qué día estás de pausa'); return; }
  if (hasta < desde) { toast('La fecha "hasta" no puede ser anterior a "desde"'); return; }
  try {
    await api('/api/pausa', { method: 'POST', body: { desde: desde, hasta: hasta } });
    toast('Pausa activada ✓ El profe fue avisado.');
    renderPerfil($('#sec-perfil'));
  } catch (err) { toast(err.message); }
}

async function cancelarPausaMi() {
  try {
    await api('/api/pausa', { method: 'DELETE' });
    toast('Pausa terminada ✓');
    renderPerfil($('#sec-perfil'));
  } catch (err) { toast(err.message); }
}

/* =====================================================================
   HORARIOS
   ===================================================================== */
async function renderHorarios(el) {
  const R = USER.role;
  const d = await api('/api/horarios');
  const profesores = R !== 'alumno' ? (await api('/api/profesores').catch(() => ({ profesores: [] }))).profesores : [];
  PROFESORES_CACHE = profesores;
  const canEdit = R !== 'alumno';
  const cols = DIAS.map((dia, i) => {
    const items = d.horarios.filter(h => h.dia === i);
    return `<div class="dia-col"><h4>${dia}</h4>
      ${items.length ? items.map(h => `
<div class="clase-item ${slugTipo(h.tipo)}">
          <span class="hora">${esc(h.hora)}</span> · <span class="tag ${slugTipo(h.tipo)}">${esc(h.tipo || 'Gi')}</span>
          <div class="small">${esc(h.nivel || 'Todos')} · ${h.duracion || 60}min</div>
          <div class="profe">🧑‍🏫 ${esc(h.profesor_nombre || 'Sin profesor')}</div>
          ${R !== 'alumno' && h.rating ? `<button class="btn ghost small" style="margin-top:6px" onclick="verValoraciones(${h.id})">⭐ ${h.rating.promedio} (${h.rating.n})</button>` : ''}
          ${canEdit ? `<div class="flex" style="margin-top:6px">
            <button class="btn ghost small" onclick="editarHorario(${h.id},${h.dia},'${escJs(h.hora)}','${escJs(h.tipo || 'Gi')}','${escJs(h.nivel || 'Todos')}',${h.profesor_id != null ? h.profesor_id : 'null'},${h.duracion || 60})">✏️ Editar</button>
            ${esAdmin() ? `<button class="btn bad small" onclick="borrarHorario(${h.id})">🗑</button>` : ''}
          </div>` : ''}
        </div>`).join('') : '<p class="small" style="color:var(--muted)">Sin clases</p>'}
    </div>`;
  });
  el.innerHTML = `
    ${secHeader('Horarios semanales')}
    ${d.avisos && d.avisos.length ? `<div class="card" style="border-color:var(--bad)">
      ${d.avisos.map(a => `<div class="small" style="color:var(--bad)">⚠ ${esc(a)}</div>`).join('')}
    </div>` : ''}
    ${R !== 'alumno' ? `<div class="card flex space-between"><span class="small">Profesores pueden editar la tabla de horarios (${esAdmin() ? 'solo admin puede eliminar' : 'edición permitida'}).</span>
      <button class="btn primary small" onclick="formHorario()">+ Agregar clase</button></div>` : ''}
    <div class="semana">${cols.join('')}</div>`;
}

function formHorario(h = null) {
  const horarios = [];
  const isEdit = !!h;
  openModal(`
    <h3>${isEdit ? 'Editar clase' : 'Nueva clase'}</h3>
    <form id="hForm" class="grid2">
      <div class="field"><label>Día</label><select id="hDia">
        ${DIAS.map((dd, i) => `<option value="${i}" ${h && h[1] === i ? 'selected' : ''}>${dd}</option>`).join('')}</select></div>
      <div class="field"><label>Hora</label><input type="time" id="hHora" value="${h ? h[2] : ''}" required></div>
      <div class="field"><label>Tipo</label><select id="hTipo">
        ${TIPOS_CLASE.map(t => `<option ${h && h[3] === t ? 'selected' : ''}>${t}</option>`).join('')}</select></div>
      <div class="field"><label>Nivel</label><select id="hNivel">
        ${['Todos', 'Principiantes', 'Intermedios', 'Avanzados', 'Competencia', 'Femenino'].map(n => `<option ${h && h[4] === n ? 'selected' : ''}>${n}</option>`).join('')}</select></div>
      <div class="field"><label>Profesor</label><select id="hProfesor">
        <option value="">Sin asignar</option>
        ${PROFESORES_CACHE.length ? PROFESORES_CACHE.map(p => `<option value="${p.id}" ${h && h[5] === p.id ? 'selected' : ''}>${esc(p.nombre)}</option>`).join('') : ''}</select></div>
      <div class="field"><label>Duración (min)</label><input type="number" id="hDur" value="${h ? h[6] : 60}"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Guardar</button></div>
    </form>`);
  $('#hForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { dia: +$('#hDia').value, hora: $('#hHora').value, tipo: $('#hTipo').value,
      nivel: $('#hNivel').value, profesor_id: $('#hProfesor').value ? +$('#hProfesor').value : null,
      duracion: +$('#hDur').value };
    try {
      if (isEdit) await api('/api/horarios/' + h[0], { method: 'PUT', body });
      else await api('/api/horarios', { method: 'POST', body });
      closeModal(); toast('Horario guardado ✓'); renderHorarios($('#sec-horarios'));
    } catch (err) { toast(err.message); }
  });
}
function editarHorario(id, dia, hora, tipo, nivel, prof, dur) { formHorario([id, dia, hora, tipo, nivel, prof, dur]); }
async function borrarHorario(id) {
  if (!confirm('¿Eliminar esta clase?')) return;
  await api('/api/horarios/' + id, { method: 'DELETE' }).catch(e => toast(e.message));
  toast('Clase eliminada'); renderHorarios($('#sec-horarios'));
}

async function verValoraciones(claseId) {
  const d = await api('/api/clase_valoraciones/' + claseId);
  openModal(`
    <h3>⭐ Valoraciones de la clase</h3>
    <div class="stat-card">
      <div class="num">${d.promedio} <span class="small">/ 5</span></div>
      <div class="lbl">Promedio · ${d.n} valoración${d.n === 1 ? '' : 'es'}</div>
    </div>
    ${d.comentarios.length ? d.comentarios.map(c => `
      <div class="small" style="padding:8px;border-bottom:1px solid var(--line)">
        <div><b>${'★'.repeat(c.estrellas)}${'☆'.repeat(5 - c.estrellas)}</b> · ${esc(c.nombre)} <span style="color:var(--muted)">· ${esc(c.fecha)}</span></div>
        ${c.comentario ? `<div style="margin-top:4px">${esc(c.comentario)}</div>` : ''}
      </div>`).join('') : '<div class="empty">Todavía no hay valoraciones para esta clase.</div>'}
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
}
let PROFESORES_CACHE = [];
let AVISOS_CACHE = {};

/* =====================================================================
   TORNEOS: calendario manual + ranking por asistencia y medallas
   =====================================================================
   No hay integracion con ningun calendario externo: el admin/profe carga
   cada torneo a mano y marca Participatinges y medals. El ranking se arma
   entero en el backend desde esas inscripciones, asi que la UI nunca inventa
   numeros: solo los muestra.
   ===================================================================== */
let TORNEOS_CACHE = [];
let TORNEOS_VISTA = 'calendario';

const MEDALLAS = [
  { v: 'oro', lbl: '🥇 Oro' },
  { v: 'plata', lbl: '🥈 Plata' },
  { v: 'bronce', lbl: '🥉 Bronce' },
];
const MESES_TORNEO = ['enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
  'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre'];
const ESTADOS = [
  { v: 'programado', lbl: 'Programado' },
  { v: 'inscripcion', lbl: 'Inscripción abierta' },
  { v: 'confirmado', lbl: 'Confirmado' },
  { v: 'finalizado', lbl: 'Finalizado' },
  { v: 'cancelado', lbl: 'Cancelado' },
];

function torneoFechaLarga(f) {
  if (!f) return 'Sin fecha';
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(f);
  if (!m) return f;
  const mi = parseInt(m[2], 10) - 1;
  if (mi < 0 || mi > 11) return f;
  return `${parseInt(m[3], 10)} de ${MESES_TORNEO[mi]} de ${m[1]}`;
}

function torneoMes(f) {
  return /^\d{4}-\d{2}/.test(f || '') ? f.slice(0, 7) : 'sin-fecha';
}

function torneoMesLbl(ym) {
  if (ym === 'sin-fecha') return 'Sin fecha';
  const mi = parseInt(ym.slice(5, 7), 10) - 1;
  return `${MESES_TORNEO[mi] || ym} ${ym.slice(0, 4)}`;
}

function medallaLbl(m) {
  const h = MEDALLAS.find(x => x.v === m);
  return h ? h.lbl : '';
}

function estadoTag(e) {
  const c = { programado: '', inscripcion: 'tag-por-vencer', confirmado: 'tag-al-dia',
    finalizado: 'tag-al-dia', cancelado: 'tag-deuda' }[e] || '';
  const h = ESTADOS.find(x => x.v === e);
  return `<span class="tag ${c}">${esc(h ? h.lbl : (e || 'Programado'))}</span>`;
}

function torneoInscripcionesHTML(t) {
  if (!t.inscripciones.length) {
    return `<div class="small" style="color:var(--muted);margin-top:8px">Todavía nadie inscrito.</div>`;
  }
  return `<div class="small" style="margin-top:10px"><b>Participantes (${t.inscripciones.length})</b></div>
    ${t.inscripciones.map(i => `
      <div class="flex space-between" style="padding:7px 0;border-bottom:1px solid var(--line)">
        <div style="display:flex;align-items:center;gap:8px;min-width:0">
          ${avatarHTML(i.foto, i.nombre, 'sm')}
          <div style="min-width:0">
            <div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(i.nombre)} ${beltHTML(i.cinturon)}</div>
            ${i.categoria ? `<div class="small" style="color:var(--muted)">${esc(i.categoria)}</div>` : ''}
            ${i.nota ? `<div class="small" style="color:var(--muted)">${esc(i.nota)}</div>` : ''}
          </div>
        </div>
        <span class="flex" style="gap:6px;align-items:center">
          ${medallaLbl(i.medalla) ? `<span class="tag tag-al-dia">${medallaLbl(i.medalla)}</span>` : ''}
          ${USER.role !== 'alumno' ? `<button class="btn ghost small" onclick="inscribirEnTorneo(${t.id}, ${i.alumno_id})">✏️</button>
            <button class="btn bad small" onclick="quitarInscripcion(${i.id})">🗑</button>` : ''}
        </span>
      </div>`).join('')}`;
}

function torneoCalendarioHTML() {
  if (!TORNEOS_CACHE.length) {
    return `<div class="card"><div class="empty">
      Todavía no hay torneos cargados.
      ${USER.role !== 'alumno' ? '<br>Usá <b>+ Nuevo torneo</b> para empezar.' : ''}
    </div></div>`;
  }
  // agrupar por mes, conservando el orden que ya dio el backend (mas nuevo primero)
  const grupos = [];
  TORNEOS_CACHE.forEach(t => {
    const ym = torneoMes(t.fecha);
    let g = grupos.find(x => x.ym === ym);
    if (!g) { g = { ym: ym, trs: [] }; grupos.push(g); }
    g.trs.push(t);
  });
  return grupos.map(g => `
    <div class="card">
      <h3>${esc( torneoMesLbl(g.ym) )}</h3>
      ${g.trs.map(t => `
        <div style="padding:12px 0;border-bottom:1px solid var(--line)">
          <div class="flex space-between" style="align-items:flex-start">
            <div style="min-width:0">
              <b style="font-size:16px">${esc(t.nombre)}</b>
              <div class="small" style="color:var(--muted)">
                📅 ${esc( torneoFechaLarga(t.fecha) )}
                ${t.ciudad ? ' · 📍 ' + esc(t.ciudad) : ''}
                ${t.lugar ? ' · ' + esc(t.lugar) : ''}
              </div>
              <div class="small" style="margin-top:4px">${estadoTag(t.estado)} ${t.tipo ? `<span class="tag">${esc(t.tipo)}</span>` : ''}</div>
              ${t.descripcion ? `<div class="small" style="margin-top:6px">${esc(t.descripcion)}</div>` : ''}
              ${t.url ? `<a class="small" href="${esc(t.url)}" target="_blank" rel="noopener noreferrer">🔗 Más info</a>` : ''}
            </div>
            ${USER.role !== 'alumno' ? `<div class="flex" style="gap:6px;flex-shrink:0">
              <button class="btn primary small" onclick="inscribirEnTorneo(${t.id})">+ Inscribir</button>
              <button class="btn ghost small" onclick="editarTorneo(${t.id})">✏️</button>
              ${esAdmin() ? `<button class="btn bad small" onclick="borrarTorneo(${t.id})">🗑</button>` : ''}
            </div>` : ''}
          </div>
          ${torneoInscripcionesHTML(t)}
        </div>`).join('')}
    </div>`).join('');
}

function torneoRankingHTML(ranking) {
  if (!ranking.length) {
    return `<div class="card"><div class="empty">
      Todavía no hay resultados.<br>
      A partir de la segunda inscripción con fecha ya aparecen posiciones.
    </div></div>`;
  }
  const medals = rk => rk.oro + rk.plata + rk.bronce;
  return `<div class="card">
    <h3>🏆 Ranking de torneos</h3>
    <p class="small" style="color:var(--muted)">Ordenado por cantidad de torneos y, en empate, por medallas. Los torneos sin fecha todavía no cuentan.</p>
    <div style="overflow:auto"><table>
      <thead><tr>
        <th style="text-align:left;padding:6px 8px">#</th>
        <th style="text-align:left;padding:6px 8px">Alumno</th>
        <th style="text-align:right;padding:6px 8px">Torneos</th>
        <th style="text-align:right;padding:6px 8px">🥇</th>
        <th style="text-align:right;padding:6px 8px">🥈</th>
        <th style="text-align:right;padding:6px 8px">🥉</th>
        <th style="text-align:right;padding:6px 8px">Total</th>
      </tr></thead>
      <tbody>
        ${ranking.map((rk, i) => `
          <tr>
            <td style="padding:6px 8px"><b>${i + 1}</b></td>
            <td style="padding:6px 8px"><div class="flex" style="gap:8px;align-items:center">
              ${avatarHTML(rk.foto, rk.nombre, 'sm')}<span>${esc(rk.nombre)}</span>${beltHTML(rk.cinturon)}
            </div></td>
            <td style="text-align:right;padding:6px 8px"><b>${rk.torneos}</b></td>
            <td style="text-align:right;padding:6px 8px">${rk.oro || ''}</td>
            <td style="text-align:right;padding:6px 8px">${rk.plata || ''}</td>
            <td style="text-align:right;padding:6px 8px">${rk.bronce || ''}</td>
            <td style="text-align:right;padding:6px 8px"><b>${medals(rk)}</b></td>
          </tr>`).join('')}
      </tbody>
    </table></div>
    <div class="small" style="color:var(--muted);margin-top:10px">
      🥇 Oro ${ranking.reduce((a, x) => a + x.oro, 0)} ·
      🥈 Plata ${ranking.reduce((a, x) => a + x.plata, 0)} ·
      🥉 Bronce ${ranking.reduce((a, x) => a + x.bronce, 0)}
    </div>
  </div>`;
}

async function renderTorneos(el) {
  const esStaff = USER.role !== 'alumno';
  const [d, r] = await Promise.all([
    api('/api/torneos'),
    api('/api/torneos/ranking').catch(() => ({ ranking: [] })),
  ]);
  TORNEOS_CACHE = d.torneos;
  const tab = (v, lbl) => `<button class="btn ${TORNEOS_VISTA === v ? 'primary' : 'ghost'} small"
      onclick="TORNEOS_VISTA='${v}';renderTorneos($('#sec-torneos'))">${lbl}</button>`;
  el.innerHTML = `
    ${secHeader('🏆 Torneos', 'Calendario manual y ranking de la academia')}
    <div class="flex" style="gap:8px;margin-bottom:12px">
      ${tab('calendario', '🗓️ Calendario')}
      ${tab('ranking', '🏅 Ranking')}
      ${esStaff ? `<button class="btn primary small" style="margin-left:auto" onclick="editarTorneo(null)">+ Nuevo torneo</button>` : ''}
    </div>
    ${TORNEOS_VISTA === 'ranking' ? torneoRankingHTML(r.ranking || []) : torneoCalendarioHTML()}`;
}

function formTorneo(id) {
  const t = id ? TORNEOS_CACHE.find(x => x.id === id) : null;
  const val = (k, def = '') => esc(t ? (t[k] || def) : def);
  openModal(`
    <h3>${t ? 'Editar torneo' : 'Nuevo torneo'}</h3>
    <form id="trForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>Nombre</label>
        <input id="trNombre" required value="${val('nombre')}" placeholder="Copa Invierno"></div>
      <div class="field"><label>Fecha</label><input type="date" id="trFecha" value="${val('fecha')}"></div>
      <div class="field"><label>Estado</label>
        <select id="trEstado">${ESTADOS.map(e =>
          `<option value="${e.v}"${t && t.estado === e.v ? ' selected' : ''}>${e.lbl}</option>`).join('')}</select></div>
      <div class="field"><label>Ciudad</label><input id="trCiudad" value="${val('ciudad')}"></div>
      <div class="field"><label>Lugar</label><input id="trLugar" value="${val('lugar')}" placeholder="Polideportivo / Gym"></div>
      <div class="field"><label>Tipo</label><input id="trTipo" value="${val('tipo', 'IBJJF')}" placeholder="IBJJF, Gi, No-Gi..."></div>
      <div class="field"><label>Link</label><input id="trUrl" value="${val('url')}" placeholder="ibjjf.com/..."></div>
      <div class="field" style="grid-column:1/-1"><label>Descripción</label>
        <textarea id="trDesc" style="width:100%;min-height:60px">${val('descripcion')}</textarea></div>
      <div class="field" style="grid-column:1/-1">
        <button class="btn primary btn-block" type="submit">${t ? 'Guardar cambios' : 'Crear torneo'}</button></div>
    </form>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cancelar</button>`);
  $('#trForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const body = {
      nombre: $('#trNombre').value.trim(),
      fecha: $('#trFecha').value,
      ciudad: $('#trCiudad').value.trim(),
      lugar: $('#trLugar').value.trim(),
      tipo: $('#trTipo').value.trim(),
      estado: $('#trEstado').value,
      url: $('#trUrl').value.trim(),
      descripcion: $('#trDesc').value.trim(),
    };
    if (!body.nombre) { toast('Falta el nombre'); return; }
    const btn = ev.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Guardando...';
    try {
      await api(t ? '/api/torneos/' + t.id : '/api/torneos',
        { method: t ? 'PUT' : 'POST', body: body });
      closeModal();
      toast(t ? 'Torneo actualizado' : 'Torneo creado');
      renderTorneos($('#sec-torneos'));
    } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = 'Guardar'; }
  });
}

function editarTorneo(id) { formTorneo(id); }

async function borrarTorneo(id) {
  const t = TORNEOS_CACHE.find(x => x.id === id);
  if (!confirm(`¿Eliminar "${t ? t.nombre : 'este torneo'}"? Se borran también sus inscripciones.`)) return;
  try {
    await api('/api/torneos/' + id, { method: 'DELETE' });
    toast('Torneo eliminado');
    renderTorneos($('#sec-torneos'));
  } catch (e) { toast(e.message); }
}

async function inscribirEnTorneo(torneoId, alumnoId) {
  const t = TORNEOS_CACHE.find(x => x.id === torneoId);
  if (!t) return;
  const lista = await api('/api/alumnos').catch(() => ({ alumnos: [] }));
  const alumnos = lista.alumnos || [];
  const actual = alumnoId ? t.inscripciones.find(i => i.alumno_id === alumnoId) : null;
  const sel = alumnoId || (actual ? actual.alumno_id : '');
  // la categoria propia del alumno viene precargada: es lo que eligio en su perfil
  const cat = actual ? actual.categoria : '';
  openModal(`
    <h3>${actual ? 'Editar inscripción' : 'Inscribir en'}<br>${esc(t.nombre)}</h3>
    <form id="insForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>Alumno</label>
        <select id="inAlumno" ${alumnoId ? 'disabled' : ''} required>
          <option value="">Elegí un alumno</option>
          ${alumnos.map(a => `<option value="${a.id}"${a.id === sel ? ' selected' : ''}>${esc(a.nombre)}${a.cinturon ? ' · ' + esc(a.cinturon) : ''}</option>`).join('')}
        </select></div>
      <div class="field" style="grid-column:1/-1"><label>Categoría de inscripción</label>
        <input id="inCat" value="${esc(cat)}" placeholder="Ej: Medio · Adulto · Con Gi">
        <small class="hint">Se completa con la categoría que eligió el alumno en su Perfil.</small></div>
      <div class="field" style="grid-column:1/-1"><label>Medalla</label>
        <select id="inMedalla">
          <option value="">Sin medalla (todavía no compite)</option>
          ${MEDALLAS.map(m => `<option value="${m.v}"${actual && actual.medalla === m.v ? ' selected' : ''}>${m.lbl}</option>`).join('')}
        </select></div>
      <div class="field" style="grid-column:1/-1"><label>Nota</label>
        <input id="inNota" value="${esc(actual ? actual.nota : '')}" placeholder="Opcional"></div>
      <div class="field" style="grid-column:1/-1">
        <button class="btn primary btn-block" type="submit">${actual ? 'Guardar' : 'Inscribir'}</button></div>
    </form>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cancelar</button>`);

  // si elegiste un alumno distinto, Autocompletar su categoria elegida
  const selAl = $('#inAlumno');
  if (!alumnoId && selAl) selAl.addEventListener('change', () => {
    // selAl.value es un string y a.id un numero: con === el find nunca
    // encontraba a nadie y la categoria se quedaba siempre vacia.
    const idElegido = parseInt(selAl.value, 10);
    const a = alumnos.find(x => x.id === idElegido);
    const label = a && a.bjj_categoria
      ? bjjLabelDeClave(a.bjj_categoria) : '';
    $('#inCat').value = label || '';
  });

  $('#insForm').addEventListener('submit', async (ev) => {
    ev.preventDefault();
    const id = alumnoId || parseInt($('#inAlumno').value, 10);
    if (!id) { toast('Elegí un alumno'); return; }
    const btn = ev.target.querySelector('button[type=submit]');
    btn.disabled = true; btn.textContent = 'Guardando...';
    try {
      await api(`/api/torneos/${torneoId}/inscripcion`, {
        method: 'POST',
        body: {
          alumno_id: id,
          categoria: $('#inCat').value.trim(),
          medalla: $('#inMedalla').value,
          nota: $('#inNota').value.trim(),
        },
      });
      closeModal();
      toast('Inscripción guardada');
      renderTorneos($('#sec-torneos'));
    } catch (e) { toast(e.message); btn.disabled = false; btn.textContent = 'Guardar'; }
  });
}

async function quitarInscripcion(iid) {
  if (!confirm('¿Quitar a este alumno del torneo?')) return;
  try {
    await api('/api/torneos/torneo/' + iid, { method: 'DELETE' });
    toast('Inscripción quitada');
    renderTorneos($('#sec-torneos'));
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   PAGOS (admin / profesor)
   ===================================================================== */
function filaAviso(a) {
  const pend = a.estado === 'pendiente';
  return `<div style="display:flex;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid rgba(255,255,255,.08)">
    <button class="btn ghost" onclick="verComprobante(${a.id})" style="width:44px;height:44px;padding:0;font-size:19px;border-radius:8px;flex-shrink:0" title="Ver comprobante">🧾</button>
    <div style="flex:1;min-width:0">
      <div><b>${esc(a.alumno_nombre)}</b> · ${a.mes}/${a.anio} · <b>$${num(a.monto)}</b></div>
      <div class="small" style="color:var(--muted)">${esc(a.fecha)}${a.nota && a.nota !== 'Cuota mensual' ? ' · ' + esc(a.nota) : ''}${a.tiene_comprobante ? '' : ' · sin comprobante'}${a.profesor_id ? ` · 💰 cobra ${esc(a.profesor_nombre || 'profesor')}` : ''}</div>
    </div>
    <div class="flex" style="gap:6px;flex-shrink:0">
      ${pend ? `<button class="btn primary small" onclick="verComprobante(${a.id})">Revisar</button>` : ''}
      ${esAdmin() ? `<button class="btn bad small" onclick="descartarAviso(${a.id})">🗑</button>` : ''}
    </div>
  </div>`;
}
async function renderPagos(el) {
  const R = USER.role;
  const [pagos, alumnos, profesores, avisos] = await Promise.all([
    api('/api/pagos'), api('/api/alumnos'),
    esAdmin() ? api('/api/profesores') : Promise.resolve({ profesores: [USER] }),
    api('/api/avisos_pago')]);
  PROFESORES_CACHE = profesores.profesores;
  const mes = new Date().getMonth() + 1, anio = new Date().getFullYear();
  const lista = avisos.avisos || [];
  const pendientes = lista.filter(a => a.estado === 'pendiente');
  const acreditados = lista.filter(a => a.estado === 'confirmado' && a.tiene_comprobante);
  AVISOS_CACHE = {};
  lista.forEach(a => { AVISOS_CACHE[a.id] = a; });
  el.innerHTML = `
    ${secHeader('Registrar pago')}
    ${pendientes.length ? `
    <div class="card">
      <h3>⏳ Comprobantes para revisar (${pendientes.length})</h3>
      <p class="small">Llegaron después del vencimiento, así que no se acreditaron solos. Decidí si se acreditan y si va con recargo por demora.</p>
      ${pendientes.map(filaAviso).join('')}
    </div>` : ''}
    ${acreditados.length ? `
    <div class="card">
      <h3>🗂 Comprobantes acreditados (${acreditados.length})</h3>
      <p class="small">Los que el sistema acreditó solo al recibirlos. Quedan guardados por si los necesitás revisar.</p>
      ${acreditados.map(filaAviso).join('')}
    </div>` : ''}
    <div class="card">
      <form id="pagoForm" class="grid2">
        <div class="field"><label>Alumno</label><select id="pAlumno" required>
          <option value="">— Elegí el alumno —</option>
          ${alumnos.alumnos.map(a => `<option value="${a.id}" data-cuota="${a.cuota_mensual || 0}" data-acts="${esc(a.actividades || '')}">${esc(a.nombre)}</option>`).join('')}</select></div>
        <div class="field" style="grid-column:1/-1"><label>¿A qué profesor(es) le pagás? El 60% se divide en partes iguales entre los marcados. Sin marcar ninguno, el reparto es automático por actividades.</label>${profeChecksHTML('pProfe', profesores.profesores, [])}</div>
        <div class="field" style="grid-column:1/-1" id="pRepartoBox"></div>
        <div class="field"><label>Monto ($)</label><input type="number" step="0.01" id="pMonto" required></div>
        <div class="field" style="grid-column:1/-1"><label style="display:flex;gap:8px;align-items:center;cursor:pointer"><input type="checkbox" id="pAum" style="width:18px;height:18px"> Sumar aumento (recargo por demora) — desmarcalo si el alumno pagó antes del vencimiento 📅</label></div>
        <div class="field"><label>Método</label><select id="pMetodo">
          ${METODOS.map(m => `<option>${m}</option>`).join('')}</select></div>
        <div class="field"><label>Mes</label><select id="pMes">
          ${Array.from({ length: 12 }, (_, i) => `<option value="${i + 1}" ${i + 1 === mes ? 'selected' : ''}>${i + 1}</option>`).join('')}</select></div>
        <div class="field"><label>Año</label><input type="number" id="pAnio" value="${anio}"></div>
        <div class="field" style="grid-column:1/-1"><label>Nota (opcional)</label><input type="text" id="pNota" placeholder="Ej: cuota agosto"></div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">💳 Registrar pago y notificar</button></div>
        <div class="field" style="grid-column:1/-1"><button class="btn warn btn-block" type="button" style="margin-top:6px" onclick="abrirPagoFamilia()">👨‍👩‍👧 Pagar familia completa</button></div>
      </form>
    </div>
    <div class="card">
      <div class="flex space-between">
        <div><h3 style="margin:0">📊 Reporte mensual de ingresos</h3><p class="small">Total cobrado, métodos usados y deudores del mes.</p></div>
        <button class="btn primary small" onclick="abrirReporte()">Ver reporte</button>
      </div>
      <div class="flex mt" style="gap:8px">
        <button class="btn ghost small" onclick="abrirMetricas()">📈 Métricas del año</button>
        <button class="btn ghost small" onclick="exportarPagosExcel()">📥 Exportar pagos a Excel</button>
      </div>
    </div>
    <div class="card">
      <h3>${R === 'profesor' ? 'Mis pagos recibidos' : 'Historial de pagos'}</h3>
      <div style="overflow:auto"><table>
        <tr><th>Fecha</th><th>Alumno</th><th>Profesor</th><th>Mes</th><th>Método</th><th>Monto</th><th>Recibo</th>${esAdmin() ? '<th></th>' : ''}</tr>
        ${pagos.pagos.length ? pagos.pagos.map(p => `<tr>
          <td>${esc(p.fecha)}</td><td><div class="flex" style="gap:8px">${avatarHTML('', p.alumno_nombre, 'sm')}<span>${esc(p.alumno_nombre)}</span></div></td>
          <td>${esc(p.profesor_nombre || '—')}</td><td>${p.mes}/${p.anio}</td>
          <td>${esc(p.metodo)}</td><td><b>$${num(p.monto)}</b></td>
          <td><a class="btn ghost small" href="/recibo/${p.id}" target="_blank" rel="noopener">🧾</a></td>
          ${esAdmin() ? `<td><button class="btn bad small" onclick="borrarPago(${p.id})">🗑</button></td>` : ''}</tr>`).join('')
          : '<tr><td colspan="7" class="empty">Todavía no hay pagos registrados</td></tr>'}
      </table></div>
    </div>`;

  function mostrarRepartoPrevisto() {
    const box = $('#pRepartoBox');
    if (!box) return;
    const sel = $('#pAlumno');
    const opt = sel && sel.selectedOptions[0];
    if (!opt) { box.innerHTML = ''; return; }
    const acts = (opt.dataset.acts || '').split(',').map(s => s.trim()).filter(Boolean);
    const elegidos = profeElegidos('pProfe');
    const monto = parseFloat(($('#pMonto') || {}).value || 0);
    const r2 = (x) => Math.round(x * 100) / 100;
    const mTatami = monto ? r2(monto * 30 / 100) : 0;
    const mAdmin = monto ? r2(monto * 10 / 100) : 0;
    const mProfes = monto ? r2(monto - mTatami - mAdmin) : 0;
    // Marcados a mano: el 60% se divide en partes iguales SOLO entre ellos.
    // Es lo mismo que hace el backend.
    if (elegidos.length) {
      const nombres = elegidos.map(id => {
        const p = (PROFESORES_CACHE || []).find(x => x.id === id);
        return p ? esc(p.nombre) : 'profesor';
      });
      const n = elegidos.length;
      const cada = n ? r2(mProfes / n) : 0;
      box.innerHTML = `<small style="color:var(--muted)">Reparto elegido a mano: ${monto
        ? `el 60% (<b style="color:var(--good)">$${num(mProfes)}</b>) en ${n} parte${n > 1 ? 's' : ''} iguales de <b>$${num(cada)}</b> para ${nombres.join(' · ')}`
        : `el 60% para ${nombres.join(' · ')}`} · Tatami y academia $${num(mTatami)} · Administrativo $${num(mAdmin)}.</small>`;
      return;
    }
    if (!acts.length) { box.innerHTML = ''; return; }
    const profs = (PROFESORES_CACHE || []).filter(p => {
      const pa = (p.actividades || '').split(',').map(s => s.trim());
      return acts.some(a => pa.includes(a));
    });
    if (!profs.length) {
      box.innerHTML = '<small style="color:var(--muted)">Ningún profesor da las actividades de este alumno, el pago quedará sin repartir (o asignalo a mano arriba).</small>';
      return;
    }
    const partes = `El 60% se reparte en partes iguales entre ${profs.length} profesor${profs.length > 1 ? 'es' : ''}: ${profs.map(p => esc(p.nombre)).join('  ·  ')}.`;
    let mitad = '';
    if (monto) {
      const cadaUno = r2(mProfes / profs.length);
      mitad = `<br><small style="color:var(--good)">Profes $${num(mProfes)} ($${num(cadaUno)} c/u) · Tatami y academia $${num(mTatami)} · Administrativo $${num(mAdmin)}</small>`;
    }
    box.innerHTML = `<small style="color:var(--muted)">Actividades: ${acts.map(a => esc(a)).join(', ')}. ${partes}${mitad}</small>`;
  }
  $('#pAlumno').addEventListener('change', (e) => {
    const opt = e.target.selectedOptions[0];
    if (opt && opt.dataset.cuota) $('#pMonto').value = opt.dataset.cuota;
    mostrarRepartoPrevisto();
  });
  const pProfeBox = $('#pProfe');
  if (pProfeBox) pProfeBox.addEventListener('change', mostrarRepartoPrevisto);
  $('#pMonto').addEventListener('input', mostrarRepartoPrevisto);
  mostrarRepartoPrevisto();
  $('#pagoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = e.target.querySelector('button[type="submit"]');
    if (btn) { if (btn.disabled) return; btn.disabled = true; btn.textContent = 'Guardando...'; }
    const body = { alumno_id: +$('#pAlumno').value, profesor_ids: profeElegidos('pProfe'),
      monto: +$('#pMonto').value, metodo: $('#pMetodo').value, mes: +$('#pMes').value,
      anio: +$('#pAnio').value, nota: $('#pNota').value,
      aplicar_cargo: $('#pAum') ? $('#pAum').checked : false };
    try {
      const res = await api('/api/pagos', { method: 'POST', body });
      let msg = res.cargo ? `Pago registrado ✓ (incluye $${num(res.cargo)} de recargo por demora)` : 'Pago registrado ✓';
      if (res.reparto && res.reparto.length > 1) {
        msg += ` · Profes: ${res.reparto.map(r => `${r.profesor} $${num(r.monto)}`).join(' / ')}`;
      } else if (res.reparto && res.reparto.length === 1) {
        msg += ` · Profes: todo para ${res.reparto[0].profesor}`;
      }
      if (res.destinos && res.destinos.length) {
        msg += ` · ${res.destinos.map(d => `${DESTINO_LABEL[d.destino] || d.destino} $${num(d.monto)}`).join(' · ')}`;
      }
      toast(msg);
      renderPagos($('#sec-pagos'));
    } catch (err) {
      if (btn) { btn.disabled = false; btn.textContent = 'Registrar pago'; }
      toast(err.message);
    }
  });
}
async function abrirPagoFamilia() {
  const d = await api('/api/familias').catch(() => ({ familias: [] }));
  const fams = (d.familias || []).filter(f => f.titular_id);
  if (!fams.length) { toast('Todavía no hay grupos familiares con titular'); return; }
  const mes = new Date().getMonth() + 1;
  openModal(`
    <h3>👨‍👩‍👧 Pagar familia completa</h3>
    <p class="small" style="color:var(--muted);margin:0">Registra la cuota (con descuento familiar) de todos los integrantes de una vez. Saltea becados y los que ya pagaron ese mes.</p>
    <div class="field"><label>Familia</label><select id="pfTitular">
      <option value="">— elegí la familia —</option>
      ${fams.map(f => {
        const t = (f.miembros || []).find(m => m.id === f.titular_id) || {};
        return `<option value="${f.titular_id}">${esc(t.nombre || '¿?')} · ${esc(f.nombre)} (${(f.miembros || []).length} integrantes · total $${num(f.total)}/mes)</option>`;
      }).join('')}
    </select></div>
    <div class="field"><label>¿A qué profesor(es) le pagan? El 60% se divide entre los marcados; sin marcar, automático por actividades.</label>${profeChecksHTML('pfProfe', PROFESORES_CACHE || [], [])}</div>
    <div class="grid2">
      <div class="field"><label>Método</label><select id="pfMetodo">${METODOS.map(m => `<option>${m}</option>`).join('')}</select></div>
      <div class="field"><label>Mes</label><select id="pfMes">${Array.from({ length: 12 }, (_, i) => `<option value="${i + 1}" ${i + 1 === mes ? 'selected' : ''}>${i + 1}</option>`).join('')}</select></div>
    </div>
    <div class="field"><label>Año</label><input type="number" id="pfAnio" value="${new Date().getFullYear()}"></div>
    <div class="field"><label style="display:flex;gap:8px;align-items:center;cursor:pointer"><input type="checkbox" id="pfAum" style="width:18px;height:18px"> Sumar aumento (recargo por demora) — desmarcalo si pagaron antes del vencimiento 📅</label></div>
    <div class="field"><label>Nota (opcional)</label><input type="text" id="pfNota" placeholder="Ej: cuota familiar agosto"></div>
    <button class="btn primary btn-block mt" onclick="pagarFamilia()">💳 Registrar pago de toda la familia</button>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
}
async function pagarFamilia() {
  const titular_id = +$('#pfTitular').value;
  if (!titular_id) { toast('Elegí la familia'); return; }
  try {
    const res = await api('/api/pagos/familia', { method: 'POST', body: {
      titular_id,
      profesor_ids: profeElegidos('pfProfe'),
      mes: +$('#pfMes').value,
      anio: +$('#pfAnio').value,
      metodo: $('#pfMetodo').value,
      nota: $('#pfNota').value,
      aplicar_cargo: $('#pfAum') ? $('#pfAum').checked : false } });
    toast(`💳 ${res.cantidad} pagos registrados de ${esc(res.familia)} por $${num(res.total)}`);
    closeModal();
    renderPagos($('#sec-pagos'));
  } catch (e) { toast(e.message); }
}
async function borrarPago(id) {
  if (!confirm('¿Eliminar este pago?')) return;
  await api('/api/pagos/' + id, { method: 'DELETE' }).catch(e => toast(e.message));
  renderPagos($('#sec-pagos'));
}
const MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
function abrirMensajeMasivo() {
  openModal(`
    <h3>📣 Mandar mensaje</h3>
    <p class="small">Les llega como notificación (app + push si tienen permisos).</p>
    <div class="field"><label>Mensaje</label><textarea id="mMsg" rows="3" maxlength="500" placeholder='Ej: HOY A ENTRENAR 🥋'></textarea></div>
    <div class="field"><label>Título (opcional)</label><input id="mTit" value="📣 Mensaje de la academia"></div>
    <div class="field"><label>Enviar a</label><select id="mDesti">
      <option value="alumnos">A todos los alumnos</option>
      <option value="especifico">A un alumno específico...</option>
    </select></div>
    <div class="field" id="mEspecificoWrap" style="display:none"><label>Alumno</label><select id="mEspecifico"></select></div>
    <button class="btn primary btn-block" id="mBtn">Enviar</button>`);
  $('#mDesti').addEventListener('change', async (e) => {
    const w = $('#mEspecificoWrap');
    w.style.display = e.target.value === 'especifico' ? '' : 'none';
    if (e.target.value === 'especifico' && $('#mEspecifico').options.length === 0) {
      try {
        const d = await api('/api/alumnos');
        $('#mEspecifico').innerHTML = d.alumnos.filter(a => a.activo).map(a => `<option value="${a.id}">${esc(a.nombre)}</option>`).join('');
      } catch (err) {}
    }
  });
  $('#mBtn').addEventListener('click', async () => {
    const texto = $('#mMsg').value.trim();
    if (!texto) { toast('Escribí el mensaje'); return; }
    const body = { texto, titulo: $('#mTit').value };
    if ($('#mDesti').value === 'especifico') {
      const aid = +$('#mEspecifico').value;
      if (!aid) { toast('Elegí un alumno'); return; }
      body.alumno_id = aid;
    }
    closeModal();
    try {
      const r = await api('/api/mensajes/broadcast', { method: 'POST', body });
      toast('Mensaje enviado ✓');
    } catch (err) { toast(err.message); }
  });
}
function abrirReporte() {
  const hoy = new Date();
  openModal(`
    <h3>📊 Reporte mensual</h3>
    <div class="grid2">
      <div class="field"><label>Mes</label><select id="rMes">${MESES.map((m, i) => `<option value="${i + 1}" ${i + 1 === hoy.getMonth() + 1 ? 'selected' : ''}>${m}</option>`).join('')}</select></div>
      <div class="field"><label>Año</label><input type="number" id="rAnio" value="${hoy.getFullYear()}"></div>
    </div>
    <button class="btn primary btn-block" id="rBtn">Ver reporte</button>`);
  $('#rBtn').addEventListener('click', async () => {
    try {
      const rep = await api('/api/reporte?mes=' + $('#rMes').value + '&anio=' + $('#rAnio').value);
      const metodos = Object.entries(rep.por_metodo || {}).map(([k, v]) => `<div class="flex space-between" style="padding:6px 0;border-bottom:1px dashed var(--line)"><span>${esc(k)}</span><b>$${num(v)}</b></div>`).join('');
      const pctBar = (pct, color) => `<div style="background:${color};width:${pct}%;height:8px;border-radius:4px;min-width:${pct > 0 ? '8px' : '0'}"></div>`;
      $('#modalBody').innerHTML = `
        <h3>📊 Reporte ${MESES[rep.mes - 1]} ${rep.anio}</h3>
        <div class="stat-card"><div class="num">$${num(rep.total)}</div><div class="lbl">Total cobrado (${rep.cantidad} pago${rep.cantidad === 1 ? '' : 's'})</div></div>

        <div class="small mb mt" style="margin-top:14px"><b>💳 Pagos</b></div>
        <div style="display:flex;gap:4px;height:8px;border-radius:4px;overflow:hidden;margin-bottom:6px">
          ${pctBar(rep.pct_pagaron, 'var(--good)')}${pctBar(rep.pct_no_pagaron, 'var(--bad)')}
        </div>
        <div class="flex space-between small" style="margin-bottom:4px"><span style="color:var(--good)">✅ Pagaron: ${rep.cant_pagaron}/${rep.total_alumnos} (${rep.pct_pagaron}%)</span></div>
        <div class="flex space-between small" style="margin-bottom:8px"><span style="color:var(--bad)">❌ No pagaron: ${rep.cant_no_pagaron}/${rep.total_alumnos} (${rep.pct_no_pagaron}%)</span></div>
        ${rep.alumnos_que_pagaron && rep.alumnos_que_pagaron.length ? `<div style="max-height:120px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px 10px;margin-bottom:8px">${rep.alumnos_que_pagaron.map(a => `<div class="flex space-between" style="padding:3px 0;border-bottom:1px solid var(--line)"><span class="small">${esc(a.nombre)} <span style="color:var(--muted)">${esc(a.cinturon || '')}</span></span><span class="small">$${num(a.monto)} · ${esc(a.metodo)}</span></div>`).join('')}</div>` : ''}

        <div class="small mb mt" style="margin-top:14px"><b>✅ Asistencia</b></div>
        <div style="display:flex;gap:4px;height:8px;border-radius:4px;overflow:hidden;margin-bottom:6px">
          ${pctBar(rep.pct_asistieron, 'var(--accent2)')}${pctBar(rep.pct_no_asistieron, '#888')}
        </div>
        <div class="flex space-between small" style="margin-bottom:4px"><span style="color:var(--accent2)">🏃 Entrenaron: ${rep.cant_asistieron}/${rep.total_alumnos} (${rep.pct_asistieron}%)</span></div>
        <div class="flex space-between small" style="margin-bottom:8px"><span>💤 No entrenaron: ${rep.cant_no_asistieron}/${rep.total_alumnos} (${rep.pct_no_asistieron}%)</span></div>
        ${rep.alumnos_que_asistieron && rep.alumnos_que_asistieron.length ? `<div style="max-height:120px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px 10px;margin-bottom:8px">${rep.alumnos_que_asistieron.map(a => `<div class="flex space-between" style="padding:3px 0;border-bottom:1px solid var(--line)"><span class="small">${esc(a.nombre)} <span style="color:var(--muted)">${esc(a.cinturon || '')}</span></span><span class="small">${a.clases} clase${a.clases === 1 ? '' : 's'}</span></div>`).join('')}</div>` : ''}

        <div class="small mb mt" style="margin-top:14px"><b>💰 Por método de pago:</b></div>${metodos || '<div class="small" style="color:var(--muted)">Sin pagos este mes.</div>'}
        <div class="small mb mt" style="margin-top:12px">${rep.deudores.length} alumno${rep.deudores.length === 1 ? '' : 's'} debe${rep.deudores.length === 1 ? '' : 'n'} la cuota${rep.deudores.length ? ':' : ''}</div>
        ${rep.deudores.length ? `<div style="max-height:200px;overflow:auto;border:1px solid var(--line);border-radius:8px;padding:4px 10px">${rep.deudores.map(d => `<div class="flex space-between" style="padding:5px 0;border-bottom:1px solid var(--line)"><span>${esc(d.nombre)} <span class="small" style="color:var(--muted)">${esc(d.cinturon || '')}</span></span><b>$${num(d.cuota_mensual || 0)}</b></div>`).join('')}</div>` : ''}
        <p class="small" style="color:var(--warn)">${rep.avisos_pend} aviso(s) de pago pendiente(s) de revisar.</p>
        <button class="btn ghost btn-block" onclick="closeModal()">Cerrar</button>`;
    } catch (e) { toast(e.message); }
  });
}

function exportarPagosExcel() {
  const hoy = new Date();
  const anio = parseInt(prompt('Año a exportar:', String(hoy.getFullYear())), 10);
  if (isNaN(anio)) return;
  window.open('/api/exportar_pagos?anio=' + anio, '_blank');
}

async function abrirMetricas() {
  const hoy = new Date();
  const anio = parseInt(prompt('Año de las métricas:', String(hoy.getFullYear())), 10);
  if (isNaN(anio)) return;
  try {
    const d = await api('/api/metricas_pagos?anio=' + anio);
    const serie = d.serie || [];
    const max = Math.max(1, ...serie.map(s => s.ingresos));
    const MESES = ['E', 'F', 'M', 'A', 'M', 'J', 'J', 'A', 'S', 'O', 'N', 'D'];
    const bars = serie.map((s, i) => `
      <div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:4px;min-width:0">
        <div class="small" style="color:var(--muted);font-size:10px">$${num(s.ingresos)}</div>
        <div title="${MESES[i]}·${anio}" style="width:14px;background:var(--accent2);border-radius:3px;height:${Math.max(2, Math.round(s.ingresos * 120 / max))}px;opacity:${s.ingresos ? '1' : '.25'};min-height:2px"></div>
        <div class="small" style="color:var(--muted);font-size:9px">${MESES[i]}</div>
      </div>`).join('');
    openModal(`
      <h3>📈 Ingresos ${anio}</h3>
      <div class="stat-card"><div class="num">$${num(d.total_ingresos)}</div><div class="lbl">Total cobrado en el año</div></div>
      <div class="small mb mt"><b>💰 Por mes</b> <span style="color:var(--muted)">(máx $${num(max)})</span></div>
      <div style="display:flex;align-items:flex-end;gap:2px;height:150px;padding:8px 4px;border:1px solid var(--line);border-radius:8px">${bars}</div>
      <div class="small mb mt" style="margin-top:14px"><b>🎯 Morosidad por mes</b> <span style="color:var(--muted)">(${d.total_alumnos} activ${d.total_alumnos === 1 ? 'o' : 'os'})</span></div>
      ${serie.map((s, i) => `<div class="flex space-between small" style="padding:3px 0;border-bottom:1px dashed var(--line)">
        <span>${MESES[i]}. — ${s.deudores} debiendo</span>
        <b style="color:${s.pct_morosidad > 50 ? 'var(--bad)' : 'var(--good)'}">${s.pct_morosidad}%</b>
      </div>`).join('')}
      <button class="btn ghost btn-block" onclick="closeModal()">Cerrar</button>`);
  } catch (e) { toast(e.message); }
}
async function verComprobante(id) {
  const a = AVISOS_CACHE[id];
  if (!a) { toast('No hay comprobante'); return; }
  AVISO_AUMENTO = true;
  openModal('<h3>🧾 Comprobante</h3><p class="small" style="color:var(--muted)">Cargando…</p>');
  let d;
  try { d = await api('/api/avisos_pago/' + id + '/comprobante'); }
  catch (e) { closeModal(); toast(e.message); return; }
  const esImg = d.comprobante.indexOf('data:image/') === 0;
  const cuerpo = esImg
    ? `<img src="${esc(d.comprobante)}" style="width:100%;border-radius:10px;background:#fff">`
    : `<div class="flex center" style="flex-direction:column;gap:10px;padding:20px 0;color:var(--muted)"><div style="font-size:44px">📄</div><p style="margin:0">Comprobante en formato PDF</p>
       <a class="btn primary small" href="${esc(d.comprobante)}" download="comprobante-${esc(d.alumno_nombre || id)}.pdf" style="text-decoration:none">⬇ Descargar PDF</a>
       <a class="btn ghost small" href="${esc(d.comprobante)}" target="_blank" rel="noopener" style="text-decoration:none">👁 Ver PDF</a></div>`;
  const cabeza = `<h3>🧾 Comprobante · ${esc(d.alumno_nombre)}</h3>
    <p class="small">Cuota de <b>${d.mes}/${d.anio}</b> por <b>$${num(d.monto)}</b>${d.nota && d.nota !== 'Cuota mensual' ? ' · ' + esc(d.nota) : ''}${d.profesor_id ? ` · 💰 el 60% es para <b>${esc(d.profesor_nombre || 'profesor')}</b>` : ''}</p>`;

  // Ya acreditado: ficha de solo lectura. El sistema lo acreditó al recibir el
  // comprobante, asi que acá no hay nada que confirmar.
  if (d.estado === 'confirmado') {
    $('#modalBody').innerHTML = `${cabeza}
      <div class="tag tag-al-dia" style="display:inline-block;margin-bottom:10px">✓ Acreditado${d.confirmado_fecha ? ' el ' + esc(d.confirmado_fecha) : ''}</div>
      ${cuerpo}
      <div class="flex mt" style="gap:8px">
        ${esAdmin() ? `<button class="btn bad small" onclick="descartarAviso(${d.id})">🗑 Descartar</button>` : ''}
        <button class="btn ghost small" onclick="closeModal()">Cerrar</button>
      </div>`;
    return;
  }
  // Pendiente: el staff decide a qué profesor le paga antes de confirmar. El
  // alumno solo ve la eleccion que hizo al subir el comprobante.
  let campoProfe = '';
  if (USER.role !== 'alumno') {
    let profs = (await api('/api/profesores_disponibles').catch(() => ({ profesores: [] }))).profesores || [];
    const elegidos = (d.profesor_ids && d.profesor_ids.length) ? d.profesor_ids
      : (d.profesor_id ? [d.profesor_id] : []);
    // Si alguno quedo dado de baja no aparece en la lista: se agrega a mano
    // para que no se pierda lo que se eligio al subir el comprobante.
    elegidos.forEach(pid => {
      if (!profs.some(p => p.id === pid)) {
        const nom = elegidos.length === 1 ? (d.profesor_nombre || 'Profesor') : 'Profesor';
        profs = [{ id: pid, nombre: nom }].concat(profs);
      }
    });
    if (profs.length) {
      campoProfe = `<div class="field"><label>Profesor(es) que cobran el 60% (en partes iguales)</label>
        ${profeChecksHTML('avProfeSel', profs, elegidos)}
        <p class="small" style="margin:4px 0 0;color:var(--muted)">Si lo cambiás acá, manda sobre lo que eligió el alumno. Sin marcar ninguno, se reparte por actividades.</p></div>`;
    }
  } else if (d.profesor_id) {
    campoProfe = `<p class="small">💰 Le estás pagando a <b>${esc(d.profesor_nombre || 'profesor')}</b>.</p>`;
  }
  $('#modalBody').innerHTML = `${cabeza}
    ${cuerpo}
    <div class="field"><label>Monto a registrar (ajustalo si pagó el valor anterior)</label>
      <input type="number" id="avMonto" value="${d.monto || ''}" min="1">
      <p class="small" style="margin:2px 0 0;color:var(--muted)">El recargo por demora se calcula sobre este monto.</p></div>
    ${campoProfe}
    <button type="button" id="avAumBtn" class="btn small btn-block" style="margin:0 0 10px" onclick="toggleAvisoAumento()"></button>
    <div class="flex mt" style="gap:8px">
      <button class="btn primary small" onclick="confirmarAviso(${d.id})">✅ Confirmar y registrar</button>
      ${esAdmin() ? `<button class="btn bad small" onclick="descartarAviso(${d.id})">🗑 Descartar</button>` : ''}
    </div>
  `;
  actualizarBotonAumento();
}
function actualizarBotonAumento() {
  const b = $('#avAumBtn');
  if (!b) return;
  b.innerHTML = 'Sumar aumento (recargo por demora): <b style="color:' + (AVISO_AUMENTO ? 'var(--warn)' : '#7fd87f') + '">' + (AVISO_AUMENTO ? 'SÍ' : 'NO') + '</b>';
}
function toggleAvisoAumento() { AVISO_AUMENTO = !AVISO_AUMENTO; actualizarBotonAumento(); }
async function confirmarAviso(id) {
  const a = AVISOS_CACHE[id] || {};
  const mEl = $('#avMonto');
  let monto = mEl ? parseFloat(mEl.value) : (a.monto || 0);
  if (!monto || isNaN(monto) || monto <= 0) { toast('Ingresá un monto válido'); return; }
  // Se lee antes de cerrar el modal: define quién cobra el 60%.
  const hayCampo = !!$('#avProfeSel');
  const elegidos = hayCampo ? profeElegidos('avProfeSel') : undefined;
  closeModal();
  try {
    const body = { monto, aplicar_cargo: AVISO_AUMENTO };
    if (hayCampo) body.profesor_ids = elegidos;
    const res = await api('/api/avisos_pago/' + id + '/confirmar', { method: 'POST', body });
    toast(res.cargo ? 'Pago confirmado ✓ (incluye $' + num(res.cargo) + ' de recargo por demora)' : 'Pago confirmado y registrado ✓ (sin aumento)');
    renderPagos($('#sec-pagos'));
  } catch (e) { toast(e.message); }
}
async function descartarAviso(id) {
  if (!confirm('¿Descartar este aviso? No se registra ningún pago.')) return;
  closeModal();
  try {
    await api('/api/avisos_pago/' + id, { method: 'DELETE' });
    toast('Aviso descartado.');
    renderPagos($('#sec-pagos'));
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   MIS PAGOS (alumno)
   ===================================================================== */
async function renderMisPagos(el) {
  const [me, pagos] = await Promise.all([api('/api/me'), api('/api/mis_pagos')]);
  const c = me.cuota || {};
  const estado = c.estado;
  const cls = estado === 'al_dia' || estado === 'becado' ? 'tag-al-dia' : estado === 'por_vencer' ? 'tag-por-vencer' : 'tag-deuda';
  const lbl = estado === 'al_dia' ? 'Al día ✓' : estado === 'becado' ? '🎖 Becado' : estado === 'por_vencer' ? 'Por vencer' : 'Debe la cuota';
  const aviso = pagos.aviso_pendiente;
  const ultimo = pagos.ultimo_aviso;
  // Solo cuenta el acreditado si es de la cuota de hoy: si mando el
  // comprobante de un mes viejo y este sigue impago, igual tiene que ver
  // el boton para mandar el de ahora.
  const acreditado = ultimo && ultimo.estado === 'confirmado'
    && ultimo.mes === c.mes && ultimo.anio === c.anio ? ultimo : null;
  if (acreditado) { AVISOS_CACHE = {}; AVISOS_CACHE[acreditado.id] = acreditado; }
  const becado = estado === 'becado';
  const exento = becado;
  el.innerHTML = `
    ${secHeader('Mi estado de cuenta')}
    <div class="card">
      <div class="flex space-between">
        <div>
          <h3 style="margin:0">${exento ? '🎖 Estás becado' : 'Cuota de ' + c.mes + '/' + c.anio}</h3>
          <p class="small">${exento ? 'No pagás cuota mensual: la academia te cubre la inscripción. No necesitás mandar comprobantes.' : `Tu cuota mensual es <b>$${num(c.cuota)}</b> · se considera paga hasta el día ${c.due_day} del mes${c.cargo_demora_pct ? ` · <b style="color:var(--warn)">si pagás después, se suma un ${c.cargo_demora_pct}% de recargo</b>` : ''}.`}</p>
        </div>
        <div class="tag ${cls}" style="font-size:14px;padding:6px 14px">${lbl}</div>
      </div>
      ${exento ? '' : `<p class="small mt">💰 Aboná ${c.cuota ? '$' + num(c.cuota) : 'tu cuota'}${me.pago_alias ? ' por transferencia al alias/CVU de la academia' : ''} y <b>sí o sí mandá el comprobante de pago</b>: tu cuota queda acreditada apenas lo recibimos.</p>`}
      ${acreditado && !exento ? `<p class="small mt" style="color:var(--ok,#7fd87f)">✅ Comprobante de ${acreditado.mes}/${acreditado.anio} recibido y acreditado.${acreditado.profesor_id ? ` El 60% es para <b>${esc(acreditado.profesor_nombre || 'tu profesor')}</b>.` : ''}</p><button class="btn ghost btn-block" onclick="verComprobante(${acreditado.id})">🧾 Ver mi comprobante</button>` : ''}
      ${aviso && !exento ? `<p class="small mt" style="color:var(--warn)">⏳ Comprobante de ${aviso.mes}/${aviso.anio} enviado.${aviso.profesor_id ? ` Le estás pagando a <b>${esc(aviso.profesor_nombre || 'tu profesor')}</b>.` : ''} Tu cuota ya venció, así que el profe/admin lo revisa antes de acreditarlo.</p>` : ''}
      ${!exento && estado !== 'al_dia' && !aviso && !acreditado ? `<button class="btn primary btn-block" onclick="avisarPago()">🧾 Mandar comprobante de pago</button>` : ''}
      ${!exento && me.mp_habilitado && estado !== 'al_dia' && !aviso && !acreditado ? `<button class="btn primary btn-block" style="background:linear-gradient(90deg,#00c3ff,#0aa2e0);border:none" onclick="pagarMercadoPago()">💳 Pagar con MercadoPago</button>` : ''}
    </div>
    ${me.pago_alias ? `
    <div class="card">
      <h3>🏦 Pagar por transferencia</h3>
      <p class="small">Págale al alias/CVU de la academia y después <b>mandá el comprobante</b> (foto o captura). Queda acreditado al instante.</p>
      <div class="alias-box" id="aliasBox">${esc(me.pago_alias)}</div>
      <button class="btn ghost btn-block" onclick="copiarAlias()">📋 Copiar alias / CVU</button>
    </div>` : ''}
    ${me.pago_link ? `
    <div class="card">
      <h3>🔗 Pagar online</h3>
      <p class="small">Te llevamos al link de pago de la academia. MercadoPago nos avisa solo y acreditamos tu cuota al instante.</p>
      <a class="btn primary btn-block" href="${esc(me.pago_link)}" target="_blank" rel="noopener noreferrer" onclick="marcarLinkPago(this)">💳 Pagar con MercadoPago</a>
      <div class="small" style="color:var(--muted);margin-top:8px;word-break:break-all">${esc(me.pago_link)}</div>
    </div>` : ''}
    <div class="card"><h3>Mis pagos</h3>
      <div style="overflow:auto"><table>
        <tr><th>Fecha</th><th>Profesor que recibió</th><th>Mes</th><th>Método</th><th>Monto</th><th>Recibo</th></tr>
        ${pagos.pagos.length ? pagos.pagos.map(p => `<tr>
          <td>${esc(p.fecha)}</td><td>${esc(p.profesor_nombre || '—')}</td>
          <td>${p.mes}/${p.anio}</td><td>${esc(p.metodo)}</td><td><b>$${num(p.monto)}</b></td>
          <td><a class="btn ghost small" href="/recibo/${p.id}" target="_blank" rel="noopener">🧾</a></td></tr>`).join('')
          : '<tr><td colspan="6" class="empty">Aún no registraste pagos</td></tr>'}
      </table></div>
    </div>`;
}
async function pagarMercadoPago() {
  const btn = event && event.currentTarget;
  if (btn) { btn.disabled = true; btn.textContent = 'Abriendo MercadoPago…'; }
  try {
    const r = await api('/api/checkout', { method: 'POST' });
    if (r.init_point) {
      const w = window.open(r.init_point, '_blank', 'noopener');
      if (!w) location.href = r.init_point;
      else toast('Abrimos MercadoPago en una pestaña nueva 🛒');
    } else toast(r.error || 'No se pudo generar el pago');
  } catch (e) { toast(e.message); }
  finally { if (btn) { btn.disabled = false; btn.innerHTML = '💳 Pagar con MercadoPago'; } }
}
function marcarLinkPago(a) {
  try { a.dataset.clic = '1'; a.style.opacity = '.75'; } catch (e) {}
  toast('Abriendo el link de pago 🛒 Después mandá el comprobante.');
}
async function avisarPago() {
  // El alumno puede decirle a qué profesor le está pagando. Si no elige, el
  // sistema reparte entre los que dan sus actividades (como siempre).
  const profs = (await api('/api/profesores_disponibles').catch(() => ({ profesores: [] }))).profesores || [];
  openModal(`
    <h3>🧾 Mandar comprobante de pago</h3>
    <p class="small">Subí una <b>foto, captura o PDF</b> del comprobante de pago. Si tu cuota está a tiempo, queda acreditada al instante.</p>
    <div class="field"><label>Comprobante (JPG, PNG o PDF)</label>
      <input type="file" id="avComprobante" accept="image/*,application/pdf">
      <div id="avPreview" class="mt" style="display:none"><img id="avPreviewImg" style="max-width:100%;border-radius:10px;background:#fff"></div>
      <div id="avFileName" class="small mt" style="display:none;color:var(--muted)"></div>
    </div>
    <div class="field"><label>¿A qué profesor(es) le pagás? (obligatorio)</label>
      ${profeChecksHTML('avProfe', profs, [])}
      <p class="small" style="margin:4px 0 0;color:var(--muted)">El 60% de la cuota se divide en partes iguales entre los que marques (30% academia, 10% admin).</p>
    </div>
    <button class="btn primary btn-block" id="avEnviar">📤 Enviar aviso</button>
    <p class="small" style="color:var(--muted)">Si mandás el comprobante con la cuota vencida, el profe/admin lo revisa antes de acreditarlo.</p>
  `);
  const input = $('#avComprobante');
  input.addEventListener('change', () => {
    const f = input.files && input.files[0];
    if (!f) return;
    if (!/^image\/(png|jpe?g|webp)|^application\/pdf/.test(f.type)) { toast('Elegí una imagen (JPG/PNG) o un PDF'); input.value = ''; return; }
    if (f.size > 12 * 1024 * 1024) { toast('El archivo es muy grande (máx 12MB)'); input.value = ''; return; }
    $('#avFileName').style.display = 'none';
    $('#avFileName').textContent = '';
    if (f.type.indexOf('image/') === 0) {
      const r = new FileReader();
      r.onload = () => { $('#avPreview').style.display = ''; $('#avPreviewImg').src = r.result; };
      r.readAsDataURL(f);
    } else {
      $('#avPreview').style.display = 'none';
      $('#avFileName').style.display = '';
      $('#avFileName').textContent = '📄 ' + f.name + ' (' + (Math.round(f.size / 1024)) + ' KB)';
    }
  });
  $('#avEnviar').addEventListener('click', () => {
    const f = input.files && input.files[0];
    if (!f) { toast('Elegí el comprobante primero'); return; }
    const elegidos = profeElegidos('avProfe');
    if (!elegidos.length) { toast('Elegí al menos un profesor'); return; }
    const r = new FileReader();
    r.onload = async () => {
      try {
        const res = await api('/api/avisar_pago', {
          method: 'POST',
          body: { comprobante: r.result, profesor_ids: elegidos }
        });
        closeModal();
        const nombres = (res.profesor_ids || []).map(pid => {
          const p = profs.find(x => x.id === pid);
          return p ? p.nombre : '';
        }).filter(Boolean);
        if (res.auto) {
          toast('Pago acreditado ✓ Ya quedó registrado tu comprobante.'
            + (nombres.length ? ' El 60% es para ' + nombres.join(' y ') + '.' : '')
            + (res.cargo ? ' Incluye $' + num(res.cargo) + ' de recargo por demora.' : ''));
        } else {
          toast(res.msg || 'Comprobante enviado. Te avisamos cuando lo confirmen.');
        }
        renderMisPagos($('#sec-mispagos'));
      } catch (e) { toast(e.message); }
    };
    r.readAsDataURL(f);
  });
}
async function copiarAlias() {
  const box = $('#aliasBox');
  if (!box) return;
  const txt = box.textContent.trim();
  try {
    await navigator.clipboard.writeText(txt);
    toast('Alias/CVU copiado ✓');
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = txt;
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); toast('Alias/CVU copiado ✓'); }
    catch (e2) { toast('Copialo manualmente'); }
    document.body.removeChild(ta);
  }
}

/* =====================================================================
   ALUMNOS (admin / profesor)
   ===================================================================== */
function bjjBadge(a) {
  const b = a.bjj;
  if (!b || !b.division) return '';
  if (!b.ok) {
    const falta = { 'Falta el genero': '⚧ falta género', 'Falta el peso': '⚖️ falta peso', 'Falta la fecha de nacimiento': '🎂 falta fecha' };
    return `<div class="small" style="color:var(--muted)">🥋 Competición: ${esc(falta[b.motivo] || 'faltan datos')}</div>`;
  }
  return `<div class="small">🥋 Competición: <b>${esc(b.division_peso)}</b> · ${esc(b.division)}${b.gi ? '' : ' · No-Gi'}</div>`;
}

async function renderAlumnos(el) {
  const d = await api('/api/alumnos');
  const filtro = filtroAlumnosCat || 'todos';
  const catLbl = { adulto: 'Adultos', juveniles: 'Juveniles', kids: 'Kids' };
  const catOf = (a) => (a.categoria === 'juveniles' || a.categoria === 'kids') ? a.categoria : 'adulto';
  const grupos = ['adulto', 'juveniles', 'kids'].filter(c => filtro === 'todos' || c === filtro);
  const card = (a) => {
    const c = a.cuota;
    const cls = c.estado === 'al_dia' ? 'tag-al-dia' : c.estado === 'por_vencer' ? 'tag-por-vencer' : 'tag-deuda';
    const lbl = c.estado === 'al_dia' ? 'Al día' : c.estado === 'por_vencer' ? 'Por vencer' : 'Debe ' + c.mes + '/' + c.anio;
    return `<div class="alum-card" data-q="${esc((a.nombre + ' ' + (a.cinturon || '') + ' ' + ((a.bjj && a.bjj.division_peso) || '')).toLowerCase())}">
      ${avatarHTML(a.foto, a.nombre, 'lg')}
      <div class="al-nombre">${esc(a.nombre)}${a.role === 'profesor' ? '<span class="tag profesor">🧑‍🏫 Profesor</span>' : ''}${a.activo ? '' : '<div><span class="tag tag-deuda">inactivo</span></div>'}</div>
      <div class="small" style="margin-top:4px">👤 @${esc(a.username || '—')}</div>
      <div class="small" style="margin-top:4px">${beltHTML(a.cinturon)} · ${a.edad != null ? a.edad + ' años' : '—'}</div>
      <div class="small">Actividades: ${(a.actividades || (a.gi_pref === 'Gi' ? 'Gi' : a.gi_pref === 'NoGi' ? 'NoGi' : '')).split(',').filter(Boolean).map(x => `<span class="tag gi">${esc(x.trim())}</span>`).join(' ') || '—'}</div>
      ${a.beca ? `<div class="small">🎖 <b>Becado</b> · no paga cuota</div>` : `<div class="small">Cuota: <b>$${num(a.familia ? a.familia.cuota_final : a.cuota_mensual)}</b> · <span class="tag ${cls}">${lbl}</span></div>`}
      ${a.familia ? `<div class="small">👨‍👩‍👧 <b>${esc(a.familia.nombre)}</b> ${a.familia.es_titular ? '<span class="tag tag-al-dia">Titular</span>' : ''}${a.familia.descuento ? `<span class="tag tag-por-vencer">ahorra $${num(a.familia.descuento)}</span>` : ''}</div>` : ''}
      ${a.en_pausa ? `<div class="small"><span class="tag tag-por-vencer">⏸ En pausa${a.pausa_hasta ? ' hasta ' + esc(a.pausa_hasta) : ''}</span></div>` : ''}
      ${bjjBadge(a)}
      <div class="small">🥋 <b>${a.asistencias}</b> asistencias</div>
      <div class="al-actions">
        <button class="btn ghost small" onclick="formAlumno(${a.id})">✏️</button>
        <button class="btn ghost small" onclick="cambiarCuota(${a.id},'${escJs(a.nombre)}',${a.cuota_mensual || 0})">💲</button>
        <button class="btn ghost small" onclick="verFicha(${a.id},'${escJs(a.nombre)}')">🩺</button>
        <button class="btn ghost small" onclick="verGrado(${a.id},'${escJs(a.nombre)}')">🥋</button>
        <button class="btn ghost small" onclick="verNotas(${a.id},'${escJs(a.nombre)}')">📝</button>
        <button class="btn good small" onclick="notificarDeuda(${a.id})">🔔</button>
        ${USER.role !== 'alumno' ? `<button class="btn ${a.activo ? 'bad' : 'good'} small" onclick="toggleActivo(${a.id},${a.activo ? 1 : 0})">${a.activo ? '🚫 Desactivar' : '✅ Reactivar'}</button>` : ''}
        ${USER.role !== 'alumno' ? `<button class="btn ${a.beca ? 'bad' : 'good'} small" title="${a.beca ? 'Quitar beca' : 'Becar (no paga nada)'}" onclick="toggleBeca(${a.id},${a.beca ? 1 : 0},'${escJs(a.nombre)}')">🎖</button>` : ''}
        ${esAdmin() ? `<button class="btn ghost small" title="Convertir en profesor" onclick="hacerProfesor(${a.id},'${escJs(a.nombre)}')">👨‍🏫</button>` : ''}
        ${esAdmin() ? `<button class="btn ghost small" onclick="reiniciarPassword(${a.id},'${escJs(a.nombre)}')">🔑</button>` : ''}
        ${esAdmin() ? `<button class="btn bad small" onclick="eliminarAlumno(${a.id},'${escJs(a.nombre)}')">🗑</button>` : ''}
      </div>
    </div>`;
  };
  const cont = (c) => {
    const lis = d.alumnos.filter(a => catOf(a) === c);
    return `<div class="sec-grupo" data-cat="${c}">
      <div class="gr-header">${catLbl[c] || c} <span class="small" style="color:var(--muted)">(${lis.length})</span></div>
      <div class="alum-grid">${lis.map(card).join('') || '<div class="empty">No hay alumnos en esta categoría.</div>'}</div>
    </div>`;
  };
  const total = d.alumnos.length;
  const esStaff = esAdmin() || USER.role === 'profesor';
  el.innerHTML = `
    ${secHeader('Alumnos', esStaff ? 'Se registran solos en la pantalla de ingreso, o los creás vos acá' : 'Los alumnos se registran solos en la pantalla de ingreso')}
    ${esStaff ? `<div class="card">
      <p class="small">Alta de perfil: creás la cuenta y le generás usuario y contraseña. Si los dejás vacíos se generan solos.</p>
      <button class="btn good" onclick="formAlumno()">+ Nuevo alumno</button>
    </div>` : ''}
    ${esAdmin() || USER.role === 'profesor' ? `<div class="mb">
      <button class="btn good" onclick="exportarAlumnosExcel()">📥 Exportar alumnos a Excel (Adultos / Juveniles / Kids)</button>
      <p class="small" style="margin:6px 0 0">Descarga un archivo .xlsx con los datos de los alumnos activos (nombre, DNI, dirección, teléfonos, categoría, etc.). Solo alumnos <b>activos</b>.</p>
    </div>` : ''}
    <div class="chips" id="alumnoCats">${[
      ['todos', 'Todos'],
      ['adulto', 'Adultos'],
      ['juveniles', 'Juveniles'],
      ['kids', 'Kids'],
    ].map(([k, lbl]) => {
      const n = k === 'todos' ? total : d.alumnos.filter(a => catOf(a) === k).length;
      return `<button class="chip ${filtro === k ? 'active' : ''}" data-cat="${k}">${lbl} (${n})</button>`;
    }).join('')}</div>
    <div class="mb">
      <input class="search" style="max-width:100%" id="alumnoBusq" placeholder="🔍 Buscar alumno...">
    </div>
    ${filtro === 'todos' ? grupos.map(cont).join('') : cont(filtro)}`;
  $('#alumnoCats').querySelectorAll('.chip').forEach((b) => {
    b.addEventListener('click', () => {
      filtroAlumnosCat = b.dataset.cat;
      renderAlumnos($('#sec-alumnos')).catch(() => {});
    });
  });
  $('#alumnoBusq').addEventListener('input', (e) => {
    const q = e.target.value.toLowerCase();
    $$('#sec-alumnos .alum-card').forEach(c => { c.style.display = c.dataset.q.includes(q) ? '' : 'none'; });
    $$('#sec-alumnos .sec-grupo').forEach(g => {
      const visibles = Array.from(g.querySelectorAll('.alum-card')).filter(c => c.style.display !== 'none').length;
      g.style.display = q && visibles === 0 ? 'none' : '';
    });
  });
}

function exportarAlumnosExcel() {
  window.open('/api/exportar_alumnos', '_blank');
}

async function verFicha(id, nombre) {
  const a = (await api('/api/alumnos')).alumnos.find(x => x.id === id);
  const fichaLlena = a.medic_enfermedades || a.medic_alergias || a.medic_medicacion || a.medic_lesiones || a.medic_info;
  const fila = (label, val) => val ? `<p class="small" style="margin:6px 0"><b>${label}:</b> ${esc(val)}</p>` : '';
  openModal(`
    <h3>🩺 Ficha de ${esc(nombre)}</h3>
    ${a.tel ? `<div class="small mb">📱 ${esc(a.tel)}</div>` : ''}
    ${a.ficha_fecha ? `<div class="small mb" style="color:var(--muted)">Actualizada el ${esc(a.ficha_fecha)}</div>` : ''}
    ${fichaLlena ? `<div style="background:var(--bg2);border-radius:8px;padding:10px">
        ${fila('🫀 Enfermedades', a.medic_enfermedades)}
        ${fila('🤧 Alergias', a.medic_alergias)}
        ${fila('💊 Medicación', a.medic_medicacion)}
        ${fila('🦴 Lesiones', a.medic_lesiones)}
        ${a.medic_info ? `<p class="small" style="margin:6px 0;white-space:pre-wrap"><b>Observaciones:</b> ${esc(a.medic_info)}</p>` : ''}
      </div>` : '<div class="tag tag-deuda mb">⚠ Sin ficha médica cargada</div>'}
    ${a.emergency_contact ? `<p class="small" style="background:var(--bg2);border-radius:8px;padding:10px;margin-top:8px"><b>📞 Contacto de emergencia:</b> ${esc(a.emergency_contact)}</p>` : ''}
    <button class="btn ghost btn-block mt" onclick="editarFicha(${id},'${escJs(nombre)}')">✏️ Completar / actualizar ficha</button>
    <button class="btn ghost btn-block" onclick="closeModal()">Cerrar</button>`);
}

async function editarFicha(id, nombre) {
  const a = (await api('/api/alumnos')).alumnos.find(x => x.id === id);
  const escv = (v) => esc((v || '').replace(/"/g, '&quot;'));
  openModal(`
    <h3>🩺 Editar ficha de ${esc(nombre)}</h3>
    <form id="fichaForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>🫀 Enfermedades / condiciones</label><input id="fEnf" value="${escv(a.medic_enfermedades)}" placeholder="Ej: asma, presión alta"></div>
      <div class="field"><label>🤧 Alergias</label><input id="fAlergias" value="${escv(a.medic_alergias)}" placeholder="Ej: penicilina"></div>
      <div class="field"><label>💊 Medicación</label><input id="fMed" value="${escv(a.medic_medicacion)}" placeholder="Ej: salbutamol"></div>
      <div class="field"><label>🦴 Lesiones / operaciones</label><input id="fLes" value="${escv(a.medic_lesiones)}" placeholder="Ej: rodilla"></div>
      <div class="field" style="grid-column:1/-1"><label>Observaciones</label><textarea id="fObs" rows="3" placeholder="Cualquier otra cosa que debamos saber">${escv(a.medic_info)}</textarea></div>
      <div class="field"><label>📞 Contacto de emergencia</label><input id="fEmer" value="${escv(a.emergency_contact)}" placeholder="Nombre y teléfono"></div>
      <div class="field" style="grid-column:1/-1"><label>Fecha de la ficha</label><input type="date" id="fFecha" value="${a.ficha_fecha ? esc(a.ficha_fecha) : fechaHoyLocal()}"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">💾 Guardar ficha</button></div>
    </form>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
  $('#fichaForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/alumnos/' + id + '/ficha', { method: 'PUT', body: {
        medic_enfermedades: $('#fEnf').value, medic_alergias: $('#fAlergias').value,
        medic_medicacion: $('#fMed').value, medic_lesiones: $('#fLes').value,
        medic_info: $('#fObs').value, emergency_contact: $('#fEmer').value,
        ficha_fecha: $('#fFecha').value } });
      toast('Ficha guardada ✓');
      closeModal();
      verFicha(id, nombre);
    } catch (err) { toast(err.message); }
  });
}

async function verGrado(id, nombre) {
  const a = (await api('/api/alumnos')).alumnos.find(x => x.id === id);
  const belts = BELTS_POR_CAT[a.categoria] || BELTS_ADULT;
  openModal(`
    <h3>🥋 Examen de ${esc(nombre)}</h3>
    <div class="small mb">Cinturón actual: ${beltHTML(a.cinturon)} · Próximo examen: <b>${a.proximo_examen ? esc(a.proximo_examen) : 'No agendado'}</b></div>
    <form id="gradoForm" class="grid2">
      <div class="field"><label>Nuevo cinturón</label><select id="gCinturon">${belts.map(b => `<option ${a.cinturon === b ? 'selected' : ''}>${esc(b)}</option>`).join('')}</select></div>
      <div class="field"><label>Fecha del examen</label><input type="date" id="gFecha" value="${fechaHoyLocal()}"></div>
      <div class="field" style="grid-column:1/-1"><label>Notas</label><input type="text" id="gNotas" placeholder="Ej: aprobó katas perfecto"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">✅ Registrar promoción</button></div>
    </form>
    <form id="exForm" style="margin-top:10px">
      <div class="field"><label>Agendar próximo examen</label><div style="display:flex;gap:8px"><input type="date" id="gProx" class="flex:1"><button class="btn ghost" type="submit">Guardar</button></div></div>
    </form>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
  $('#gradoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/alumnos/' + id + '/grado', { method: 'POST', body: { cinturon: $('#gCinturon').value, fecha: $('#gFecha').value, notas: $('#gNotas').value } });
      toast('Promoción registrada ✓ Se notificó al alumno');
      closeModal();
      renderAlumnos($('#sec-alumnos'));
    } catch (err) { toast(err.message); }
  });
  $('#exForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/alumnos/' + id + '/proximo_examen', { method: 'POST', body: { fecha: $('#gProx').value } });
      toast('Examen agendado ✓ Se notificó al alumno');
      closeModal();
      renderAlumnos($('#sec-alumnos'));
    } catch (err) { toast(err.message); }
  });
}

async function verNotas(id, nombre) {
  const a = (await api('/api/alumnos')).alumnos.find(x => x.id === id);
  openModal(`
    <h3>📝 Notas internas de ${esc(nombre)}</h3>
    <p class="small">Visibles solo para admin y profesores.</p>
    <textarea id="notasTxt" rows="4" style="width:100%">${esc(a.notas_internas || '')}</textarea>
    <button class="btn primary btn-block mt" id="notasBtn">Guardar</button>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
  $('#notasBtn').addEventListener('click', async () => {
    try {
      await api('/api/alumnos/' + id + '/notas', { method: 'PUT', body: { notas: $('#notasTxt').value } });
      toast('Notas guardadas ✓');
      closeModal();
    } catch (err) { toast(err.message); }
  });
}

async function toggleActivo(id, activo) {
  const accion = activo ? 'desactivar' : 'reactivar';
  if (!confirm(activo ? '¿Desactivar a este alumno? No va a poder entrar hasta que lo reactives.' : '¿Reactivar a este alumno?')) return;
  try {
    await api('/api/alumnos/' + id, { method: 'PUT', body: { activo: activo ? 0 : 1 } });
    toast(activo ? 'Alumno desactivado.' : 'Alumno reactivado ✓');
    renderAlumnos($('#sec-alumnos'));
  } catch (err) { toast(err.message); }
}

async function formAlumno(id) {
  // Sin id = alta (botón "+ Nuevo alumno"); con id = edición, como siempre.
  const esNuevo = !id;
  const vacio = { nombre: '', dni: '', direccion: '', edad: '', peso: '', genero: '', tel: '',
    tel_tutor: '', tel_2: '', nacimiento: '', categoria: 'adulto', cinturon: '', actividades: '',
    cuota_mensual: null, pausa_desde: '', pausa_hasta: '', medic_info: '', emergency_contact: '',
    foto_ok: 0 };
  const a = esNuevo ? vacio : (await api('/api/alumnos')).alumnos.find(x => x.id === id);
  if (!a) { toast('Alumno no encontrado'); return; }
  openModal(`
    <h3>${esNuevo ? 'Nuevo alumno' : 'Editar alumno'}</h3>
    <form id="alForm" class="grid2">
      <div class="field"><label>Nombre y apellido</label><input id="aNombre" value="${esc(a.nombre)}" required></div>
      ${esNuevo ? `
      <div class="field"><label>Usuario</label><input id="aUser" placeholder="si lo dejás vacío se genera"></div>
      <div class="field"><label>Contraseña</label><input id="aPass" placeholder="si lo dejás vacío: alumno123"></div>` : ''}
      <div class="field"><label>DNI</label><input type="text" id="aDni" value="${esc(a.dni || '')}"></div>
      <div class="field"><label>Dirección / domicilio</label><input type="text" id="aDir" placeholder="Ej: Calle 1 N° 123, Madryn" value="${esc(a.direccion || '')}"></div>
      <div class="field"><label>Edad</label><input type="number" id="aEdad" value="${a.edad != null ? a.edad : ''}"></div>
      <div class="field"><label>Peso (kg)</label><input type="number" step="0.1" id="aPeso" value="${a.peso != null ? a.peso : ''}"></div>
      <div class="field"><label>Género</label><select id="aGenero">
        <option value="">Sin definir</option>
        <option value="M" ${a.genero === 'M' ? 'selected' : ''}>Masculino</option>
        <option value="F" ${a.genero === 'F' ? 'selected' : ''}>Femenino</option></select></div>
      <div class="field"><label>Teléfono</label><input type="tel" id="aTel" value="${esc(a.tel || '')}"></div>
      <div class="field"><label>📞 Tel. padre/madre/tutor ${a.categoria === 'kids' || a.categoria === 'juveniles' ? '<span style="color:#ff9b8f">(obligatorio)</span>' : ''}</label><input type="tel" id="aTutor" value="${esc(a.tel_tutor || '')}"></div>
      <div class="field"><label>📞 Segundo teléfono</label><input type="tel" id="aTel2" value="${esc(a.tel_2 || '')}"></div>
      <div class="field"><label>Fecha de nacimiento${esNuevo ? ' <span style="color:#ff9b8f">(obligatoria)</span>' : ''}</label><input type="date" id="aNac" value="${a.nacimiento || ''}" ${esNuevo ? 'required' : ''}></div>
      <div class="field"><label>Categoría</label><select id="aCat">${CATEGORIAS.map(c => `<option value="${c}" ${a.categoria === c ? 'selected' : ''}>${catLabel(c)}</option>`).join('')}</select></div>
      <div class="field"><label>Cinturón</label><select id="aCinturon"></select></div>
      <div class="field" style="grid-column:1/-1"><label>Actividades</label>
        <div class="chips">
          ${TIPOS_ACTIVIDAD.map(ac => `<label class="chip"><input type="checkbox" name="actAct" value="${ac}" ${(a.actividades || '').split(',').map(s => s.trim()).includes(ac) ? 'checked' : ''}><span>${ac}</span></label>`).join('')}
        </div>
      <div class="field"><label>Cuota mensual ($)</label><input type="number" step="0.01" id="aCuota" value="${a.cuota_mensual != null ? a.cuota_mensual : ''}" disabled></div>
      <div class="field"><label>⏸ Pausa desde (fechas vacías = sin pausa)</label><input type="date" id="aPausaDesde" value="${a.pausa_desde || ''}"></div>
      <div class="field"><label>⏸ Pausa hasta</label><input type="date" id="aPausaHasta" value="${a.pausa_hasta || ''}"></div>
      <div class="field" style="grid-column:1/-1"><label>🩺 Ficha médica (opcional)</label><textarea id="aMedic" rows="2" placeholder="Lesiones, alergias, medicación...">${esc(a.medic_info || '')}</textarea></div>
      <div class="field" style="grid-column:1/-1"><label>📞 Contacto de emergencia (opcional)</label><input type="text" id="aEmer" placeholder="Nombre y teléfono" value="${esc(a.emergency_contact || '')}"></div>
      ${a.categoria === 'kids' || a.categoria === 'juveniles' ? `<div class="field" style="grid-column:1/-1"><label style="display:flex;align-items:center;gap:8px;cursor:pointer">
        <input type="checkbox" id="aFotoOk" style="width:18px;height:18px" ${a.foto_ok ? 'checked' : ''}>
        <span>Autorizado por un mayor para exponer fotos del menor (redes y muro)</span></label></div>` : ''}
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Guardar</button></div>
    </form>`);
  function fillBelt() {
    const cat = $('#aCat').value;
    const belts = BELTS_POR_CAT[cat] || BELTS_ADULT;
    $('#aCinturon').innerHTML = belts.map(b => `<option ${a && a.cinturon === b ? 'selected' : ''}>${esc(b)}</option>`).join('');
  }
  fillBelt();
  $('#aCat').addEventListener('change', fillBelt);
  $('#alForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { nombre: $('#aNombre').value.trim(), edad: $('#aEdad').value, peso: $('#aPeso').value, genero: $('#aGenero').value,
      cinturon: $('#aCinturon').value, categoria: $('#aCat').value,
      actividades: $$('input[name="actAct"]:checked').map(x => x.value),
      tel: $('#aTel').value, nacimiento: $('#aNac').value,
      medic_info: $('#aMedic').value, emergency_contact: $('#aEmer').value,
      tel_tutor: $('#aTutor')?.value || '', tel_2: $('#aTel2')?.value || '',
      dni: $('#aDni')?.value.trim() || '', direccion: $('#aDir')?.value.trim() || '',
      foto_ok: !!($('#aFotoOk')?.checked || false),
      pausa_desde: $('#aPausaDesde')?.value || '', pausa_hasta: $('#aPausaHasta')?.value || '' };
    if (esNuevo) {
      if ($('#aUser')?.value.trim()) body.username = $('#aUser').value.trim();
      if ($('#aPass')?.value) body.password = $('#aPass').value;
    }
    try {
      if (esNuevo) {
        const r = await api('/api/alumnos', { method: 'POST', body });
        toast(`Alumno creado · usuario: ${r.username} · contraseña: ${r.password}`);
      } else {
        await api('/api/alumnos/' + a.id, { method: 'PUT', body });
        toast('Alumno actualizado ✓');
      }
      closeModal(); renderAlumnos($('#sec-alumnos'));
    } catch (err) { toast(err.message); }
  });
}

function cambiarCuotaProfe(id, nombre, actual) {
  openModal(`
    <h3>Cuota de ${esc(nombre)}</h3>
    <form id="cuotaProfeForm">
      <p class="small">Los profesores también pagan cuota. Cargale el monto mensual; si no lo cargás, el profesor no figura como deudor y no puede pagar con MercadoPago.</p>
      <div class="field"><label>Cuota mensual ($)</label>
        <input type="number" step="0.01" id="cpMonto" value="${actual || ''}" required></div>
      <button class="btn primary btn-block" type="submit">Guardar cuota</button>
    </form>`);
  $('#cuotaProfeForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/profesores/' + id + '/cuota', { method: 'PUT', body: { cuota_mensual: +$('#cpMonto').value } });
      closeModal(); toast('Cuota del profesor actualizada ✓');
      renderProfesores($('#sec-profesores'));
    } catch (err) { toast(err.message); }
  });
}

function cambiarCuota(id, nombre, actual) {
  openModal(`
    <h3>Cambiar cuota de ${esc(nombre)}</h3>
    <form id="cuotaForm">
      <div class="field"><label>Nueva cuota mensual ($) — solo admin/profesor puede cambiar</label>
        <input type="number" step="0.01" id="cMonto" value="${actual || ''}" required></div>
      <button class="btn primary btn-block" type="submit">Guardar cuota</button>
    </form>`);
  $('#cuotaForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/alumnos/' + id + '/cuota', { method: 'PUT', body: { cuota_mensual: +$('#cMonto').value } });
      closeModal(); toast('Cuota actualizada y alumno notificado ✓');
      renderAlumnos($('#sec-alumnos'));
    } catch (err) { toast(err.message); }
  });
}

async function eliminarAlumno(id, nombre) {
  if (!confirm(`¿Eliminar a ${nombre} y todos sus datos?`)) return;
  await api('/api/alumnos/' + id, { method: 'DELETE' }).catch(e => toast(e.message));
  toast('Alumno eliminado');
  renderAlumnos($('#sec-alumnos'));
}

async function hacerProfesor(id, nombre) {
  if (!confirm(`¿Convertir a ${nombre} en profesor? Conserva su usuario, contraseña, pagos y asistencias; deja de contar como alumno.`)) return;
  try {
    await api('/api/alumnos/' + id + '/profesor', { method: 'POST' });
    toast(`✅ ${nombre} ahora es profesor`);
    renderAlumnos($('#sec-alumnos')).catch(() => {});
    renderProfesores($('#sec-profesores')).catch(() => {});
  } catch (e) { toast(e.message); }
}

async function toggleBeca(id, actual, nombre) {
  if (actual ? !confirm(`¿Quitarle la beca a ${nombre}? Vuelve a pagar cuota.`) : !confirm(`¿Becar a ${nombre}? No paga más nada hasta que le quites la beca.`)) return;
  try {
    const r = await api('/api/alumnos/' + id + '/beca', { method: 'POST' });
    toast(r.beca ? `🎖 ${nombre} quedó becado (no paga)` : `${nombre} ya no está becado`);
    renderAlumnos($('#sec-alumnos')).catch(() => {});
  } catch (e) { toast(e.message); }
}

async function notificarDeuda(id) {
  try {
    const d = await api('/api/notify_deuda', { method: 'POST', body: { alumno_id: id } });
    toast(`Recordatorio enviado a ${d.avisados} alumno(s) 🔔`);
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   ASISTENCIA (admin / profesor)
   ===================================================================== */
async function renderAsistencia(el) {
  const [horarios, alumnos] = await Promise.all([api('/api/horarios'), api('/api/alumnos')]);
  const hoy = fechaHoyLocal();
  el.innerHTML = `
    ${secHeader('Tomar asistencia')}
    <div class="card">
      <div class="flex mb">
        <select id="asClase" style="padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--bg2);color:var(--txt);flex:1">
          <option value="">— Elegí la clase —</option>
          ${horarios.horarios.map(h => `<option value="${h.id}" data-profe="${h.profesor_id || ''}">${esc(h.dia_nombre)} ${esc(h.hora)} · ${esc(h.tipo || 'Gi')} · ${esc(h.nivel || 'Todos')}</option>`).join('')}
        </select>
        <input type="date" id="asFecha" value="${hoy}" style="padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--bg2);color:var(--txt)">
      </div>
      <div id="asLista" class="mt">
        <div class="empty">Elegí una clase para marcar los alumnos presentes.</div>
      </div>
      <div class="flex space-between mt">
        <span class="marcador">✅ Presentes hoy: <b id="asCount">0</b></span>
        <button class="btn good" id="asGuardar" hidden>Guardar asistencia</button>
      </div>
    </div>

    <div class="card mt">
      <div class="small mb">📅 Ver qué alumnos asistieron en un día (hoy, ayer, mañana o cualquier fecha)</div>
      <div class="flex" style="gap:8px">
        <input type="date" id="diaFecha" value="${hoy}" style="padding:10px;border-radius:9px;border:1px solid var(--line);background:var(--bg2);color:var(--txt);flex:1">
        <button class="btn primary" id="diaVer">Ver asistencia</button>
      </div>
      <div id="diaResult" class="mt"></div>
    </div>`;

  $('#diaVer').addEventListener('click', verAsistenciaDia);

  async function cargar() {
    const cid = +$('#asClase').value;
    const fecha = $('#asFecha').value;
    const lista = $('#asLista');
    if (!cid) { lista.innerHTML = '<div class="empty">Elegí una clase.</div>'; $('#asGuardar').hidden = true; return; }
    const presentes = await api('/api/asistencia_dia?clase_id=' + cid + '&fecha=' + fecha).catch(() => ({ presentes: [] }));
    const set = new Set(presentes.presentes);
    lista.innerHTML = alumnos.alumnos.map(a => `
      <label class="check-row ${set.has(a.id) ? 'presente' : ''}">
        <input type="checkbox" class="asCheck" value="${a.id}" ${set.has(a.id) ? 'checked' : ''} data-nombre="${esc(a.nombre)}">
        ${avatarHTML(a.foto, a.nombre, 'sm')}
        <span class="nom">${esc(a.nombre)}</span>
        <span class="meta">${beltHTML(a.cinturon)} · ${a.edad != null ? a.edad : ''} años · 🥋${a.asistencias}</span>
      </label>`).join('') || '<div class="empty">No hay alumnos cargados.</div>';
    $('#asGuardar').hidden = false;
    contar();
    $$('.asCheck').forEach(c => c.addEventListener('change', () => {
      c.closest('.check-row').classList.toggle('presente', c.checked);
      contar();
    }));
  }
  function contar() {
    const n = $$('.asCheck:checked').length;
    $('#asCount').textContent = n;
  }
  $('#asClase').addEventListener('change', cargar);
  $('#asFecha').addEventListener('change', cargar);
  $('#asGuardar').addEventListener('click', async () => {
    const cid = +$('#asClase').value;
    const fecha = $('#asFecha').value;
    const presentes = $$('.asCheck:checked').map(c => +c.value);
    try {
      await api('/api/asistencia', { method: 'POST', body: { clase_id: cid, fecha, presentes } });
      toast(`Asistencia guardada: ${presentes.length} presentes ✓`);
    } catch (e) { toast(e.message); }
  });
}

async function verAsistenciaDia() {
  const box = $('#diaResult');
  const fecha = $('#diaFecha').value;
  if (!fecha) { toast('Elegí una fecha'); return; }
  box.innerHTML = '<div class="small" style="color:var(--muted)">Cargando…</div>';
  let d;
  try {
    d = await api('/api/asistencia_por_dia?fecha=' + fecha);
  } catch (e) {
    box.innerHTML = '<div class="empty">' + esc(e.message) + '</div>';
    return;
  }
  box.innerHTML = `
    <div class="small mb">${esc(d.dia)} ${fecha} · <b>${d.clases_dictadas}</b> clase${d.clases_dictadas === 1 ? '' : 's'} ese día</div>
    ${d.clases.length ? d.clases.map(c => `
      <div style="padding:8px 0;border-bottom:1px solid var(--line)">
        <div class="flex space-between">
          <div><b>${esc(c.hora)}</b> · <span class="tag ${slugTipo(c.tipo)}">${esc(c.tipo)}</span> · ${esc(c.nivel)}${c.profesor ? ' · <span class="profe">' + esc(c.profesor) + '</span>' : ''}</div>
          <div class="small">✅ ${c.cantidad}</div>
        </div>
        ${c.presentes.length
          ? `<div class="small" style="margin-top:4px">${c.presentes.map(p => `<span class="tag tag-al-dia">${esc(p.nombre)}</span>`).join(' ')}</div>`
          : '<div class="small" style="color:var(--muted)">Sin asistencias marcadas</div>'}
      </div>`).join('') : '<div class="empty">No hay clases cargadas para este día.</div>'}
  `;
}

/* =====================================================================
   DEUDORES (admin / profesor)
   ===================================================================== */
async function renderDeudores(el) {
  const d = await api('/api/deudores');
  el.innerHTML = `
    ${secHeader('Alumnos con deuda')}
    <div class="card">
      <div class="flex space-between mb">
        <span class="small">Alumnos sin pago del mes actual o por vencer. Los que están en pausa temporal no aparecen acá.</span>
        <button class="btn warn" onclick="notificarTodas()">🔔 Notificar a todos</button>
      </div>
      <div style="overflow:auto"><table>
        <tr><th>Alumno</th><th>Cuota</th><th>Estado</th><th>Días sin pago</th><th>Acción</th></tr>
        ${d.deudores.length ? d.deudores.map(x => `
          <tr>
            <td><div class="flex" style="gap:8px">${avatarHTML(x.foto, x.nombre, 'sm')}<b>${esc(x.nombre)}</b></div> ${beltHTML(x.cinturon)}</td>
            <td>$${num(x.cuota_mensual)}</td>
            <td><span class="tag ${x.estado === 'deuda' ? 'tag-deuda' : 'tag-por-vencer'}">${x.estado === 'deuda' ? 'Debe' : 'Por vencer'}</span></td>
            <td>${x.dias_deuda}</td>
            <td><button class="btn warn small" onclick="notificarDeuda(${x.id})">🔔 Recordar</button></td>
          </tr>`).join('') : '<tr><td colspan="5" class="empty">🎉 No hay deudores. Todos al día.</td></tr>'}
      </table></div>
    </div>`;
}
async function notificarTodas() {
  try {
    const d = await api('/api/notify_deuda', { method: 'POST', body: {} });
    toast(`Recordatorio enviado a ${d.avisados} alumnos 🔔`);
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   ESTADISTICAS DE ASISTENCIA (staff)
   ===================================================================== */
async function renderEstadisticas(el) {
  const d = await api('/api/estadisticas_asistencia');
  el.innerHTML = `
    ${secHeader('Estadísticas de asistencia', 'Comparecencia por alumno: % de clases a las que asistió sobre las dictadas, en los últimos 6 meses')}
    <div class="card mb">
      <div class="small mb">📅 Días que se dictaron clases por mes</div>
      <div style="display:flex;gap:6px">${d.meses.map((m, i) => `
        <div style="flex:1;text-align:center;padding:6px;background:var(--bg2);border-radius:8px">
          <div class="small">${esc(m)}</div><b>${d.dias_con_clases[i]}</b>
        </div>`).join('')}</div>
    </div>
    <div class="card">
      <div style="overflow:auto"><table>
        <tr><th>Alumno</th>${d.meses.map(m => `<th>${esc(m)}</th>`).join('')}<th>Total</th></tr>
        ${d.alumnos.length ? d.alumnos.map(a => `
          <tr>
            <td><div class="flex" style="gap:8px">${avatarHTML(a.foto, a.nombre, 'sm')}<b>${esc(a.nombre)}</b></div> ${beltHTML(a.cinturon)}${a.en_pausa ? ' <span class="tag tag-por-vencer">⏸ en pausa</span>' : ''}</td>
            ${a.serie.map(s => `
              <td style="min-width:56px;text-align:center">
                ${s.pct == null
                  ? '<span class="small" style="color:var(--muted)">—</span>'
                  : `<div style="height:34px;width:16px;background:var(--bg2);border-radius:4px;overflow:hidden;display:inline-block;vertical-align:bottom">
                       <div style="height:${Math.max(8, s.pct)}%;width:100%;background:var(--accent2)" title="${s.pct}% (${s.asist}/${s.dias})"></div>
                     </div>${s.pct}%`}
              </td>`).join('')}
            <td><b>${a.total_asist}</b></td>
          </tr>`).join('') : '<tr><td colspan="' + (d.meses.length + 2) + '" class="empty">Todavía no hay alumnos activos.</td></tr>'}
      </table></div>
    </div>`;
}

/* =====================================================================
   PLANES DEL PROFE
   ===================================================================== */
function fmtFechaISO(f) {
  if (!f) return '';
  const p = String(f).split('-');
  return p.length === 3 ? p[2] + '/' + p[1] + '/' + p[0] : f;
}

async function renderPlanes(el) {
  const R = USER.role;
  const esStaff = esAdmin() || R === 'profesor';
  const semanaSel = $('#planSemana') ? $('#planSemana').value : '';
  const d = await api('/api/planes' + (esStaff && semanaSel ? '?semana=' + semanaSel : ''));
  el.innerHTML = `
    ${secHeader('Planes del profe', esStaff ? 'Objetivo o plan de entrenamiento por semana, asignado a categoría y cinturón.' : 'El plan del profe para esta semana, para tu cinturón.')}
    <div class="card mb">
      <div class="flex space-between" style="align-items:center">
        <span class="small">Semana del <b>${fmtFechaISO(d.semana_inicio)}</b> al <b>${fmtFechaISO(d.semana_fin)}</b></span>
        ${esStaff ? `<div class="flex" style="gap:8px">
          <input type="date" id="planSemana" class="small" value="${d.semana_inicio}" onchange="renderPlanes($('#sec-planes'))" aria-label="Elegir otra semana">
          <button class="btn primary" onclick="formPlan()">＋ Crear plan</button>
        </div>` : ''}
      </div>
    </div>
    <div class="feed">
      ${d.planes.length ? d.planes.map(p => `
        <div class="feed-card">
          <div class="flex space-between" style="align-items:flex-start">
            <div>
              <b>${esc(p.titulo)}</b>
              ${esStaff ? ` <button class="btn ghost small" onclick="formPlan(${p.id})">✏️</button> <button class="btn bad small" onclick="borrarPlan(${p.id})">🗑</button>` : ''}
              <div class="small" style="margin-top:4px">
                <span class="tag ${p.categoria === 'todos' ? 'alumno' : p.categoria}">${p.categoria === 'todos' ? 'Todos' : esc(p.categoria)}</span>
                <span class="tag ${p.cinturon === 'todos' ? 'alumno' : p.cinturon}">${p.cinturon === 'todos' ? 'Todos los cinturones' : esc(p.cinturon)}</span>
                ${p.autor ? '<span style="color:var(--muted)">· ' + esc(p.autor) + '</span>' : ''}
              </div>
            </div>
            ${R === 'alumno' ? `<button class="btn ${p.hecho ? 'good' : 'ghost'} small" onclick="togglePlanHecho(${p.id})">${p.hecho ? '✓ Lo hice' : 'Marca que lo hiciste'}</button>` : ''}
          </div>
          ${p.descripcion ? `<p class="small" style="white-space:pre-wrap;margin:8px 0 0">${esc(p.descripcion)}</p>` : ''}
        </div>`).join('') : '<div class="empty">Todavía no hay planes para esta semana.</div>'}
    </div>`;
}

function formPlan(id) {
  const esNuevo = !id;
  const catSel = CATEGORIAS.map(c => `<option value="${c}">${catLabel(c)}</option>`).join('');
  const cintSel = BELTS_ADULT.concat(BELTS_KIDS).map(b => `<option>${esc(b)}</option>`).join('');
  const lunesISO = (() => {
    const dia = new Date();
    const diff = (dia.getDay() === 0 ? 6 : dia.getDay() - 1);
    return fechaLocalDe(new Date(dia.getTime() - diff * 86400000));
  })();
  openModal(`
    <h3>${esNuevo ? '➕ Crear plan del profe' : '✏️ Editar plan'}</h3>
    <form id="planForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>Título (objetivo del plan)</label><input id="plTitulo" required placeholder="Ej: Semana de claves de muñeca"></div>
      <div class="field" style="grid-column:1/-1"><label>Descripción / qué practicar</label><textarea id="plDesc" rows="4" placeholder="Detallá el plan: técnica, series, lo que se espera lograr..."></textarea></div>
      <div class="field"><label>Categoría</label><select id="plCat"><option value="todos">Todos</option>${catSel}</select></div>
      <div class="field"><label>Cinturón</label><select id="plCintur"><option value="todos">Todos</option>${cintSel}</select></div>
      <div class="field" style="grid-column:1/-1"><label>Semana (comienza el lunes de esa semana)</label><input type="date" id="plFecha" value="${lunesISO}"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Guardar plan</button></div>
    </form>`);
  if (!esNuevo) {
    const semana = $('#planSemana') ? $('#planSemana').value : '';
    api('/api/planes' + (semana ? '?semana=' + semana : '')).then(dd => {
      const p = dd.planes.find(x => x.id === id);
      if (!p) return;
      $('#plTitulo').value = p.titulo || '';
      $('#plDesc').value = p.descripcion || '';
      $('#plCat').value = p.categoria || 'todos';
      $('#plCintur').value = p.cinturon || 'todos';
      $('#plFecha').value = p.fecha || lunesISO;
    }).catch(() => {});
  }
  $('#planForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { titulo: $('#plTitulo').value.trim(), descripcion: $('#plDesc').value,
      categoria: $('#plCat').value, cinturon: $('#plCintur').value, fecha: $('#plFecha').value };
    try {
      if (esNuevo) { await api('/api/planes', { method: 'POST', body }); toast('Plan creado ✓'); }
      else { await api('/api/planes/' + id, { method: 'PUT', body }); toast('Plan actualizado ✓'); }
      closeModal(); renderPlanes($('#sec-planes'));
    } catch (err) { toast(err.message); }
  });
}

async function togglePlanHecho(id) {
  try {
    await api('/api/planes/' + id + '/hecho', { method: 'POST', body: {} });
    renderPlanes($('#sec-planes'));
  } catch (err) { toast(err.message); }
}

async function borrarPlan(id) {
  if (!confirm('¿Eliminar este plan?')) return;
  try {
    await api('/api/planes/' + id, { method: 'DELETE' });
    toast('Plan eliminado');
    renderPlanes($('#sec-planes'));
  } catch (err) { toast(err.message); }
}

/* =====================================================================
   PROFESORES (admin)
   ===================================================================== */
async function renderProfesores(el) {
  const d = await api('/api/profesores');
  const alum = esAdmin() ? (await api('/api/alumnos').catch(() => ({ alumnos: [] }))).alumnos : [];
  el.innerHTML = `
    ${secHeader('Profesores')}
    ${esAdmin() ? `
    <div class="card">
      <p class="small">⚡ Convertí un alumno existente en profesor <b>sin crearle otra cuenta</b> (conserva usuario, contraseña y datos):</p>
      <div class="flex" style="gap:8px;flex-wrap:wrap">
        ${(() => { const soloAlumnos = alum.filter(a => a.role !== 'profesor'); return `
        <select id="promoSelect" class="search" style="max-width:none;flex:1;min-width:220px">
          ${soloAlumnos.map(a => `<option value="${a.id}">${esc(a.nombre)} — @${esc(a.username)}</option>`).join('') || '<option value="" disabled>No hay alumnos activos</option>'}
        </select>
        <button class="btn primary" onclick="promoverProfesor()" ${soloAlumnos.length ? '' : 'disabled'}>Convertir en profesor</button>`; })()}
      </div>
    </div>` : ''}
    <div class="card">
      <p class="small">Los profesores se crean su propia cuenta con el <b>código de la academia</b> (lo encontrás en Configuración), o los podés crear vos acá.</p>
      <button class="btn primary" onclick="formProfesor()">+ Crear profesor</button>
    </div>
    <div class="card"><div style="overflow:auto"><table>
      <tr><th>Profesor</th><th>Faixa</th><th>Edad</th><th>Peso</th><th>Usuario</th><th>Clases</th><th>Cuota</th><th></th></tr>
      ${d.profesores.length ? d.profesores.map(p => `
        <tr>
          <td><div class="flex" style="gap:8px">${avatarHTML(p.foto, p.nombre, 'sm')}<b>${esc(p.nombre)}</b></div></td><td>${beltHTML(p.cinturon)}</td>
          <td>${p.edad != null ? p.edad : '—'}</td><td>${p.peso ? p.peso + 'kg' : '—'}</td>
          <td>@${esc(p.username)}</td><td>${p.clases}</td>
          <td>${p.cuota_mensual ? '$' + num(p.cuota_mensual) : '<span style="color:var(--warn)">sin cargar</span>'}</td>
          <td><div class="flex" style="gap:6px;flex-wrap:wrap">
            ${esAdmin() ? `<button class="btn ghost small" onclick="cambiarCuotaProfe(${p.id},'${escJs(p.nombre)}',${p.cuota_mensual || 0})">💲</button>` : ''}
            <button class="btn bad small" onclick="eliminarProfesor(${p.id},'${escJs(p.nombre)}')">🗑 Eliminar</button></div></td>
        </tr>`).join('') : '<tr><td colspan="8" class="empty">Todavía no hay profesores.</td></tr>'}
    </table></div></div>`;
}

function promoverProfesor() {
  const sel = $('#promoSelect');
  const opt = sel.options[sel.selectedIndex];
  if (!opt || !opt.value) { toast('No hay alumnos para convertir'); return; }
  const id = parseInt(opt.value, 10);
  const nombre = opt.textContent.replace(/ — @.*$/, '');
  hacerProfesor(id, nombre);
}

async function formProfesor() {
  openModal(`
    <h3>Crear profesor</h3>
    <form id="profeForm" class="grid2">
      <div class="field" style="grid-column:1/-1"><label>Nombre y apellido</label><input id="prNombre" required></div>
      <div class="field"><label>Usuario</label><input id="prUser" placeholder="si lo dejas vacío se genera"></div>
      <div class="field"><label>Contraseña</label><input id="prPass" placeholder="si lo dejas vacío: profe123"></div>
      <div class="field"><label>Edad</label><input type="number" id="prEdad"></div>
      <div class="field"><label>Peso (kg)</label><input type="number" step="0.1" id="prPeso"></div>
      <div class="field"><label>Faixa</label><select id="prCinturon">${BELTS_ADULT.map(b => `<option>${b}</option>`).join('')}</select></div>
      <div class="field" style="grid-column:1/-1"><label>Cuota mensual ($) — los profesores también pagan</label>
        <input type="number" step="0.01" id="prCuota" placeholder="si lo dejás vacío se usa la cuota por defecto"></div>
      <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Crear</button></div>
    </form>`);
  $('#profeForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const body = { nombre: $('#prNombre').value.trim(), edad: $('#prEdad').value, peso: $('#prPeso').value,
      cinturon: $('#prCinturon').value };
    if ($('#prCuota').value) body.cuota_mensual = +$('#prCuota').value;
    if ($('#prUser').value) body.username = $('#prUser').value.trim();
    if ($('#prPass').value) body.password = $('#prPass').value;
    try {
      const d = await api('/api/profesores', { method: 'POST', body });
      closeModal(); toast(`Profesor creado · usuario: ${d.username} · contraseña: ${d.password}`);
      renderProfesores($('#sec-profesores'));
    } catch (err) { toast(err.message); }
  });
}

async function eliminarProfesor(id, nombre) {
  if (!confirm(`¿Eliminar al profesor ${nombre}? Sus clases quedan sin asignar y sus pagos se conservan.`)) return;
  await api('/api/profesores/' + id, { method: 'DELETE' }).catch(e => toast(e.message));
  toast('Profesor eliminado');
  renderProfesores($('#sec-profesores'));
}

/* =====================================================================
   CONFIG (admin)
   ===================================================================== */
async function renderConfig(el) {
  const s = await api('/api/settings');
  el.innerHTML = `
    ${secHeader('Configuración')}
    <div class="card">
      <h3>Academia</h3>
      <form id="cfgForm" class="grid2">
        <div class="field"><label>Nombre de la academia</label><input id="cNombre" value="${esc(s.academy_name)}"></div>
        <div class="field"><label>Color principal</label><input type="color" id="cColor" value="${esc(s.academy_color || '#9b5de5')}" style="padding:4px;height:42px"></div>
        <div class="field"><label>Código de la academia (para que los profes se registren)</label><input id="cCodigo" value="${esc(s.academy_code)}"></div>
        <div class="field"><label>Cuota mensual por defecto ($)</label><input id="cCuota" value="${esc(s.default_cuota)}"></div>
        <div class="field"><label>Precio 1 actividad / 1 profe ($)</label><input type="number" id="cPrecio1" value="${esc(s.precio_act_1 ?? '45000')}" placeholder="45000"></div>
        <div class="field"><label>Precio 2 actividades / 2 profes ($)</label><input type="number" id="cPrecio2" value="${esc(s.precio_act_2 ?? '60000')}" placeholder="60000"></div>
        <div class="field"><label>Precio 3 o más actividades / 3+ profes ($)</label><input type="number" id="cPrecio3" value="${esc(s.precio_act_3 ?? '80000')}" placeholder="80000"><small class="hint">La cuota se calcula sola según cuántas actividades entrena el alumno.</small></div>
        <div class="field"><label>Día de vencimiento (día del mes)</label><input type="number" id="cDue" value="${esc(s.due_day)}"></div>
        <div class="field"><label>Recargo por pago con demora (%)</label><input type="number" id="cDemora" value="${esc(s.cargo_demora_pct ?? '10')}" placeholder="10"></div>
        <div class="field"><label>Descuento familiar: 2 integrantes (%)</label><input type="number" id="cDescFam2" value="${esc(s.desc_familiar2 ?? s.desc_familiar ?? '10')}" placeholder="10"></div>
        <div class="field"><label>Descuento familiar: 3 integrantes (%)</label><input type="number" id="cDescFam3" value="${esc(s.desc_familiar3 ?? '15')}" placeholder="15"></div>
        <div class="field"><label>Descuento familiar: 4 o más integrantes (%)</label><input type="number" id="cDescFam4" value="${esc(s.desc_familiar4 ?? '20')}" placeholder="20"><small class="hint">Con 2 o más integrantes, TODOS pagan con descuento. Cada cantidad de integrantes puede tener un % distinto y autónomamente puede quedar en 0 para no descontar.</small></div>
        <div class="field" style="grid-column:1/-1"><label>Link de pago en línea (ej: link de MercadoPago)</label><input id="cLink" value="${esc(s.pago_link || '')}" placeholder="https://link.mercadopago.com.ar/... (dejalo vacío para ocultar el botón de pago)"></div>
        <div class="field" style="grid-column:1/-1"><label>Alias o CVU para transferencia</label><input id="cAlias" value="${esc(s.pago_alias || '')}" placeholder="ej: academia.nexo.madryn (dejalo vacío para ocultarlo)"></div>
        <div class="field" style="grid-column:1/-1"><label>Desplazamiento desde UTC (zona horaria de la academia)</label><input type="number" step="0.5" id="cTz" value="${esc(s.tz_offset ?? '-3')}" placeholder="-3"><small class="hint">Argentina: -3. Sirve para que el "hoy" no se cambie a la madrugada del día siguiente por la diferencia con UTC.</small></div>
        <div class="field" style="grid-column:1/-1"><label>Access Token de MercadoPago (APP_USR-...) para el botón de pago en línea</label><input id="cMpTk" value="${esc(s.mp_access_token || '')}" placeholder="APP_USR-... (dejalo vacío para ocultar el botón de pago online)"></div>
        <div class="field"><label>Número WhatsApp de la academia (con código país)</label><input id="cWp" value="${esc(s.wp_numero || '')}" placeholder="549299..."></div>
        <div class="field"><label>Logro de asistencias (cada cuántas avisar)</label><input type="number" id="cLogroAsist" value="${esc(s.logro_asist ?? '50')}"></div>
        <div class="field"><label>Logro de videos vistos (cada cuántos avisar)</label><input type="number" id="cLogroVids" value="${esc(s.logro_videos ?? '25')}"></div>
        <div class="field" style="grid-column:1/-1"><label>Asistencias para sugerir examen de cinturón</label><input type="number" id="cMinExamen" value="${esc(s.asis_min_examen ?? '30')}"><small class="hint">Cuando un alumno llega a esa cantidad, se avisa al staff (no al alumno) que está listo para el próximo examen. Se reinicia el aviso cuando se registra una promoción.</small></div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Guardar configuración</button></div>
      </form>
    </div>
    <div class="card">
      <h3>📣 Mensajes automáticos</h3>
      <p class="small">Cada alumno recibe el mensaje <b>una vez por día</b> si supera los días sin entrenar o sin pagar (el que pase primero).</p>
      <form id="autoForm" class="grid2">
        <div class="field"><label>Mensaje que reciben</label><input id="aMsg" value="${esc(s.auto_mensaje || '')}" style="grid-column:1/-1"></div>
        <div class="field"><label>Días sin entrenar para avisar</label><input type="number" id="aInact" value="${esc(s.auto_inact_dias ?? '15')}"></div>
        <div class="field"><label>Días sin pagar para avisar</label><input type="number" id="aDeuda" value="${esc(s.auto_deuda_dias ?? '30')}"></div>
        <div class="field" style="grid-column:1/-1"><label><input type="checkbox" id="aActivo" ${s.auto_mensaje_activo === '1' ? 'checked' : ''}> Activar mensajes automáticos</label></div>
        <div class="field" style="grid-column:1/-1">
          <button class="btn primary btn-block" type="submit">💾 Guardar mensaje automático</button>
          <button class="btn ghost btn-block" type="button" onclick="enviarAutoAhora()">🚀 Enviar ahora a quienes corresponda</button>
        </div>
      </form>
    </div>
    <div class="card">
      <h3>📲 Notificaciones push</h3>
      <p class="small">Si no te llegan las notificaciones al celular, activá el permiso y probá una.</p>
      <button class="btn primary btn-block" onclick="activarPush()">🔔 Activar notificaciones</button>
      <button class="btn ghost btn-block mt" onclick="testPush()">🧪 Probar notificación</button>
      <p class="small mt" id="pushDiag" style="color:var(--muted)"></p>
    </div>
    <div class="card">
      <h3>Actualizar cuota masiva</h3>
      <p class="small">Aplica el valor de "Cuota mensual por defecto" a <b>todos los alumnos activos</b> y les manda una notificación a cada uno.</p>
      <button class="btn warn btn-block" onclick="aplicarCuotaTodos()">🔄 Aplicar cuota a todos los alumnos</button>
    </div>`;
  $('#cfgForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/settings', { method: 'PUT', body: {
        academy_name: $('#cNombre').value, academy_color: $('#cColor').value,
        academy_code: $('#cCodigo').value, default_cuota: $('#cCuota').value,
        precio_act_1: $('#cPrecio1').value, precio_act_2: $('#cPrecio2').value, precio_act_3: $('#cPrecio3').value,
        due_day: $('#cDue').value, cargo_demora_pct: $('#cDemora').value, desc_familiar2: $('#cDescFam2').value, desc_familiar3: $('#cDescFam3').value, desc_familiar4: $('#cDescFam4').value, pago_link: $('#cLink').value, pago_alias: $('#cAlias').value, tz_offset: $('#cTz').value,
        mp_access_token: $('#cMpTk').value, wp_numero: $('#cWp').value, logro_asist: $('#cLogroAsist').value, logro_videos: $('#cLogroVids').value, asis_min_examen: $('#cMinExamen').value } });
      toast('Configuración guardada ✓');
      if (location.reload) { /* color aplicado al recargar */ }
      window.ACADEMY_NAME = $('#cNombre').value;
      $('#academyName').textContent = $('#cNombre').value;
      $('#academyTitle') && ($('#academyTitle').textContent = $('#cNombre').value);
    } catch (err) { toast(err.message); }
  });
  $('#autoForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/settings', { method: 'PUT', body: {
        auto_mensaje: $('#aMsg').value, auto_inact_dias: $('#aInact').value,
        auto_deuda_dias: $('#aDeuda').value, auto_mensaje_activo: $('#aActivo').checked ? '1' : '0' } });
      toast('Mensaje automático guardado ✓');
    } catch (err) { toast(err.message); }
  });
}
async function testPush() {
  try {
    const resp = await fetch('/api/test_push', { method: 'POST', headers: { 'Content-Type': 'application/json' } });
    let raw = '(sin body)';
    try { raw = await resp.text(); } catch (e) {}
    const el = pushDiagEl();
    if (el) el.textContent = 'Respuesta del servidor: ' + raw;
    toast('Ver la respuesta abajo');
  } catch (e) {
    toast(e.message);
    const el = pushDiagEl();
    if (el) el.textContent = '✗ ' + e.message;
  }
}
async function activarPush() {
  try {
    if (!('Notification' in window)) { toast('Este navegador no soporta notificaciones'); return; }
    if (Notification.permission === 'denied') { toast('Permiso denegado en el navegador. Entrá a Ajustes del sitio y permití las notificaciones.'); return; }
    const ok = await setupPush(true);
    if (ok) toast('Notificaciones activadas ✓ Probá con el botón de abajo.');
  } catch (e) { toast('No se pudo activar: ' + e.message); }
}
async function perfilActivarPush() {
  try {
    if (!('Notification' in window)) { toast('Este navegador no soporta notificaciones'); return; }
    if (Notification.permission === 'denied') { toast('Permiso denegado en el navegador. Entrá a Ajustes del sitio y permití las notificaciones.'); return; }
    const ok = await setupPush(true);
    if (ok) toast('Notificaciones activadas ✓ Probá con el botón de abajo.');
  } catch (e) { toast('No se pudo activar: ' + e.message); }
}
async function aplicarCuotaTodos() {
  if (!confirm('¿Actualizar la cuota de TODOS los alumnos activos al valor de "Cuota mensual por defecto"? Se les notifica a cada uno.')) return;
  try {
    const r = await api('/api/settings/aplicar_cuota', { method: 'POST' });
    toast(`Cuota actualizada en ${r.alumnos} alumnos ✓`);
  } catch (e) { toast(e.message); }
}
async function enviarAutoAhora() {
  try {
    const r = await api('/api/mensajes/auto', { method: 'POST' });
    toast(r.enviados.length ? `Mensaje enviado a ${r.enviados.length} alumnos (${r.enviados.map(x => x.nombre).join(', ')})` : 'Ningún alumno necesita aviso hoy.');
  } catch (e) { toast(e.message); }
}

/* fetch academy name para el título */
try { fetch('/api/settings').then(r => r.json()).then(s => {
  if (s.academy_name) window.ACADEMY_NAME = s.academy_name;
}).catch(() => {}); } catch (e) {}

/* =====================================================================
   CHAT + GRUPOS POR CATEGORÍA
   ===================================================================== */

async function renderChat(el) {
  const d = await api('/api/chats').catch(() => ({ chats: [] }));
  el.innerHTML = `
    ${secHeader('💬 Chat')}
    <div class="card">
      <h3>Grupos por categoría</h3>
      <p class="small">Podés abrir un chat grupal automático para cada categoría.</p>
      <div class="chips">
        <button class="chip" onclick="abrirGrupo('kids')">🧒 Grupo Kids</button>
        <button class="chip" onclick="abrirGrupo('juveniles')">👦 Grupo Juveniles</button>
        <button class="chip" onclick="abrirGrupo('adulto')">🧑 Grupo Adultos</button>
      </div>
    </div>
    <div class="card">
      <h3>Chats directos</h3>
      <button class="btn primary btn-block" onclick="nuevoChatDirecto()">➕ Nuevo chat</button>
    </div>
    <div class="card">
      <h3>Tus conversaciones</h3>
      <div id="chatLista">${d.chats.length ? d.chats.map(c =>
        `<div class="flex space-between" style="padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer" onclick="abrirChatId(${c.id})">
          <span><b>${esc(c.nombre)}</b> <span class="small" style="color:var(--muted)">${c.tipo === 'grupo' ? '· ' + c.miembros + ' miembros' : ''}</span></span>
          <span class="small" style="color:var(--accent2)">Abrir →</span>
        </div>`).join('') : '<div class="empty">Todavía no tenés conversaciones.</div>'}</div>
    </div>`;
}

async function abrirGrupo(cat) {
  try {
    const r = await api('/api/chats', { method: 'POST', body: { categoria: cat } });
    abrirChatId(r.id);
  } catch (e) { toast(e.message); }
}

async function nuevoChatDirecto() {
  const d = await api('/api/contactos').catch(() => ({ contactos: [] }));
  openModal(`
    <h3>Nuevo chat</h3>
    <div>
      ${d.contactos.length ? d.contactos.map(c =>
        `<div class="flex space-between" style="padding:10px 0;border-bottom:1px solid var(--line);cursor:pointer" onclick="crearDirecto(${c.id})">
          <span>${avatarHTML(c.foto, c.nombre, 'sm')} <b>${esc(c.nombre)}</b> ${c.cinturon ? beltHTML(c.cinturon) : ''}</span>
          <span class="small" style="color:var(--accent2)">Abrir →</span>
        </div>`).join('') : '<div class="empty">No hay contactos disponibles</div>'}
    </div>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
}

async function crearDirecto(uid) {
  try {
    const r = await api('/api/chats', { method: 'POST', body: { user_id: uid } });
    closeModal();
    abrirChatId(r.id);
  } catch (e) { toast(e.message); }
}

async function abrirChatId(cid) {
  CHAT_ACTIVO = cid;
  const d = await api('/api/chats/' + cid + '/mensajes').catch((err) => ({ __err: err.message }));
  if (!d || d.__err) {
    const msg = 'No pudimos abrir el chat: ' + (d ? d.__err : 'error desconocido');
    if (window.toastGlobal) console.error(msg);
    toast(msg);
    CHAT_ACTIVO = null;
    return;
  }
  const mensajes = (d.mensajes || []).map(m => chatMsgHTML(m)).join('');
  // Activar la sección de chat sin volver a llamar a renderChat (que reemplazaría
  // esta conversación por la lista de chats al completar su fetch).
  $$('.sec').forEach(s => s.classList.remove('active'));
  let secEl = $('#sec-chat');
  if (!secEl) {
    secEl = document.createElement('div');
    secEl.className = 'sec';
    secEl.id = 'sec-chat';
    $('#content').appendChild(secEl);
  }
  secEl.classList.add('active');
  $$('#bottombar .bb-item').forEach(x => x.classList.toggle('active', x.dataset.sec === 'chat'));
  const el = secEl;
  el.innerHTML = `
    ${secHeader('💬 ' + esc(d.chat.nombre || 'Chat'))}
    <button class="btn ghost small mb" onclick="renderChat($('#sec-chat'))">← Volver a la lista</button>
    <div class="card">
      <div id="chatMsgs" style="max-height:55vh;overflow:auto;display:flex;flex-direction:column">${mensajes || '<div class="empty">Decí hola 👋</div>'}</div>
      <div style="display:flex;gap:8px;margin-top:12px;align-items:center">
        <input type="file" id="chatFoto" accept="image/*,video/*" style="display:none">
        <button class="btn ghost" onclick="$('#chatFoto').click()" title="Adjuntar foto o video">📎</button>
        <input id="chatInput" placeholder="Escribí un mensaje..." style="flex:1">
        <button class="btn primary" onclick="enviarChat()">Enviar</button>
      </div>
      <div id="chatAdjVista" class="mt small" style="display:none;color:var(--muted)"></div>
    </div>`;
  const inp = $('#chatInput');
  const box = $('#chatMsgs'); box.scrollTop = box.scrollHeight;
  const lastM = (d.mensajes && d.mensajes.length) ? d.mensajes[d.mensajes.length - 1].id : 0;
  CHAT_LAST_MSG = lastM;
  inp.addEventListener('keydown', (e) => { if (e.key === 'Enter') enviarChat(); });
  const fInp = $('#chatFoto');
  if (fInp) fInp.addEventListener('change', () => {
    const f = fInp.files && fInp.files[0];
    if (!f) return;
    if (!/^image\/(png|jpe?g|webp|gif)|^video\/(mp4|webm|quicktime)/.test(f.type)) { toast('Elegí una foto o un video (mp4/webm)'); fInp.value = ''; return; }
    if (f.size > 25 * 1024 * 1024) { toast('El archivo es muy grande (máx 25MB)'); fInp.value = ''; return; }
    const r = new FileReader();
    r.onload = () => {
      CHAT_ADJ = { data: r.result, tipo: f.type.indexOf('image/') === 0 ? 'imagen' : 'video', nombre: f.name };
      const v = $('#chatAdjVista');
      if (v) { v.style.display = ''; v.textContent = '📎 Adjuntado: ' + f.name + (CHAT_ADJ.tipo === 'imagen' ? ' (foto)' : ' (video)'); }
    };
    r.readAsDataURL(f);
  });
  if (CHAT_TIMER) clearInterval(CHAT_TIMER);
  CHAT_TIMER = setInterval(() => { if (CHAT_ACTIVO && $('#sec-chat') && !document.hidden) actualizarChatMsgs(); }, 5000);
}

async function actualizarChatMsgs() {
  // El servidor devuelve solo los mensajes posteriores a CHAT_LAST_MSG: bajar
  // los 200 cada 5 segundos significaba releer todos los adjuntos (hasta 25 MB
  // c/u) de la base aunque no hubiera nada nuevo.
  const incremental = CHAT_LAST_MSG > 0;
  const url = '/api/chats/' + CHAT_ACTIVO + '/mensajes'
    + (incremental ? '?desde=' + CHAT_LAST_MSG : '');
  const d = await api(url).catch(() => null);
  if (!d || !$('#chatMsgs')) return;
  const box = $('#chatMsgs');
  const msgs = d.mensajes || [];
  if (incremental) {
    if (!msgs.length) return; // sin cambios: no re-crear (evita cortar videos)
    const eraAbajo = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
    CHAT_LAST_MSG = msgs[msgs.length - 1].id;
    box.insertAdjacentHTML('beforeend', msgs.map(m => chatMsgHTML(m)).join(''));
    if (eraAbajo) box.scrollTop = box.scrollHeight;
    return;
  }
  CHAT_LAST_MSG = msgs.length ? msgs[msgs.length - 1].id : 0;
  const eraAbajo = box.scrollTop + box.clientHeight >= box.scrollHeight - 40;
  box.innerHTML = msgs.map(m => chatMsgHTML(m)).join('') || '<div class="empty">Decí hola 👋</div>';
  if (eraAbajo) box.scrollTop = box.scrollHeight;
}

function chatMsgHTML(m) {
  const adj = m.adjunto ? (m.adjunto_tipo === 'video'
    ? `<video controls playsinline preload="metadata" style="max-width:100%;max-height:260px;border-radius:10px;margin-top:6px;display:block"><source src="${esc(m.adjunto)}"></video>`
    : `<img loading="lazy" decoding="async" src="${esc(m.adjunto)}" style="max-width:100%;max-height:260px;border-radius:10px;margin-top:6px;display:block;cursor:pointer" onclick="abrirAdjunto(this.src, 'imagen')">`) : '';
  const texto = m.mensaje ? `<div class="chat-bubble">${esc(m.mensaje)}</div>` : '';
  return `<div class="chat-msg ${m.user_id === USER.id ? 'own' : ''}">
      ${texto}
      ${adj}
      <div class="chat-meta">${esc(m.nombre)} · ${esc(m.fecha)}</div>
    </div>`;
}

function abrirAdjunto(src, tipo) {
  const esImagen = tipo === 'imagen' || src.indexOf('data:image/') === 0;
  openModal(esImagen
    ? `<img src="${esc(src)}" style="width:100%;border-radius:10px">`
    : `<video src="${esc(src)}" controls autoplay style="width:100%;border-radius:10px"></video>`);
}

async function enviarChat() {
  const inp = $('#chatInput');
  const msg = inp.value.trim();
  const adj = CHAT_ADJ;
  if (!CHAT_ACTIVO || (!msg && !adj)) return;
  inp.value = '';
  CHAT_ADJ = null;
  const v = $('#chatAdjVista');
  if (v) { v.style.display = 'none'; v.textContent = ''; }
  try {
    await api('/api/chats/' + CHAT_ACTIVO + '/mensajes', { method: 'POST', body: { mensaje: msg, adjunto: adj ? adj.data : null, adjunto_tipo: adj ? adj.tipo : null } });
    actualizarChatMsgs();
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   MURO
   ===================================================================== */
async function renderMuro(el) {
  const d = await api('/api/muro').catch(() => ({ muro: [] }));
  el.innerHTML = `
    ${secHeader('📢 Muro de la academia')}
    <div class="card">
      <textarea id="muroTexto" placeholder="¿Qué está pasando? Si subís una lucha, contá de quién es: nombres, categoría, premios..." style="width:100%;min-height:70px"></textarea>
      <div class="small" style="color:var(--muted);margin:6px 0">🎥 ¿Subís una lucha? Pegá el link de YouTube o elegí un archivo, y escribí de quién es la lucha arriba.</div>
      <input type="text" id="muroLink" placeholder="Link de YouTube de la lucha (ej: https://youtube.com/watch?v=...)" style="width:100%;margin-bottom:6px">
      <input type="file" id="muroVideo" accept="video/mp4,video/webm,video/ogg,video/quicktime" style="margin-bottom:6px">
      <label class="small" style="display:block;color:var(--muted);margin:6px 0 2px">📷 Subir fotos (hasta 5)</label>
      <input type="file" id="muroFoto" accept="image/*" style="margin-bottom:6px">
      <button class="btn primary btn-block mt" onclick="publicarMuro()">Publicar</button>
    </div>
    <div id="muroFeed">
      ${d.muro.length ? d.muro.map(p => `
        <div class="post-card">
          <div class="post-head">
            ${avatarHTML(p.foto, p.nombre, 'sm')} <b>${esc(p.nombre)}</b> ${p.cinturon ? beltHTML(p.cinturon) : ''}
            <span class="small" style="color:var(--muted)">· ${esc(p.fecha)}</span>
            ${p.user_id === USER.id ? `<button class="btn ghost small" style="margin-left:auto" onclick="borrarMuro(${p.id})">🗑</button>` : ''}
          </div>
          ${p.texto ? `<p style="margin:8px 0">${esc(p.texto)}</p>` : ''}
          ${p.video ? muroVideoHTML(p.video) : ''}
          ${(p.fotos || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:10px">${p.fotos.slice(0,4).map(f => `<img loading="lazy" decoding="async" src="${esc(f)}" style="max-width:150px;max-height:150px;border-radius:8px;object-fit:cover;cursor:pointer" onclick="verFoto(this.src)">`).join('')}</div>` : ''}
        </div>`).join('') : '<div class="empty">Todavía no hay publicaciones.</div>'}
    </div>`;
}

function muroVideoHTML(v) {
  if (v.tipo === 'link') {
    const yid = youtubeId(v.url);
    if (yid) return `<div class="post-media"><iframe src="https://www.youtube.com/embed/${yid}" frameborder="0" allow="accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture" allowfullscreen></iframe></div>`;
    return `<div class="post-media post-media-link"><a href="${esc(v.url)}" target="_blank" rel="noopener">🎬 ${esc(v.url)}</a></div>`;
  }
  return `<div class="post-media"><video controls preload="none" playsinline><source src="${esc(v.url)}"></video></div>`;
}

async function publicarMuro() {
  const texto = $('#muroTexto').value.trim();
  const file = $('#muroFoto').files && $('#muroFoto').files[0];
  let foto = null;
  if (file) {
    try { foto = file.size > 400 * 1024 ? await comprimirImagen(file, 900) : await leerArchivoBase64(file); } catch (e) {}
  }
  const link = $('#muroLink').value.trim();
  const vfile = $('#muroVideo').files && $('#muroVideo').files[0];
  let video = null;
  if (link && vfile) { toast('Elegí una sola lucha: link de YouTube O archivo'); return; }
  if (link) video = { link };
  else if (vfile) {
    try {
      const dataUrl = await leerArchivoBase64(vfile);
      if (!String(dataUrl).startsWith('data:video/')) { toast('El archivo debe ser un video'); return; }
      video = { archivo: dataUrl };
    } catch (e) { toast('No se pudo leer el video'); return; }
  }
  if (!texto && !foto && !video) { toast('Escribí algo, subí una foto o un video'); return; }
  try {
    await api('/api/muro', { method: 'POST', body: { texto, fotos: foto ? [foto] : [], video } });
    toast('Publicado ✓');
    renderMuro($('#sec-muro'));
  } catch (e) { toast(e.message); }
}
function leerArchivoBase64(file) {
  return new Promise((res, rej) => {
    const r = new FileReader();
    r.onload = () => res(r.result);
    r.onerror = rej;
    r.readAsDataURL(file);
  });
}
function comprimirImagen(file, maxW = 900) {
  return new Promise((res, rej) => {
    const img = new Image();
    const url = URL.createObjectURL(file);
    img.onload = () => {
      URL.revokeObjectURL(url);
      try {
        const esc = Math.min(1, maxW / img.width);
        const w = Math.max(1, Math.round(img.width * esc));
        const h = Math.max(1, Math.round(img.height * esc));
        const cv = document.createElement('canvas');
        cv.width = w; cv.height = h;
        cv.getContext('2d').drawImage(img, 0, 0, w, h);
        res(cv.toDataURL('image/jpeg', 0.72));
      } catch (e) { res(leerArchivoBase64(file)); }
    };
    img.onerror = () => { URL.revokeObjectURL(url); rej(new Error('imagen inválida')); };
    img.src = url;
  });
}
function verFoto(src) {
  openModal(`
    <div style="text-align:center"><img src="${esc(src)}" style="max-width:100%;max-height:82vh;border-radius:10px"></div>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
}
async function borrarMuro(id) {
  if (!confirm('¿Eliminar esta publicación?')) return;
  try { await api('/api/muro/' + id, { method: 'DELETE' }); renderMuro($('#sec-muro')); }
  catch (e) { toast(e.message); }
}

/* =====================================================================
   GALERÍA DE FOTOS
   ===================================================================== */
async function renderGaleria(el) {
  const d = await api('/api/muro').catch(() => ({ muro: [] }));
  const fotos = [];
  d.muro.forEach(p => (p.fotos || []).forEach(f => fotos.push({ f, n: p.nombre })));
  el.innerHTML = `
    ${secHeader('🖼️ Galería de fotos')}
    <div class="card">${fotos.length
      ? `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:12px">${fotos.map(x => `<div style="position:relative"><img loading="lazy" decoding="async" src="${esc(x.f)}" style="width:100%;height:120px;object-fit:cover;border-radius:10px;cursor:pointer" onclick="verFoto(this.src)"><span class="small" style="position:absolute;bottom:4px;left:6px;color:#fff;text-shadow:0 1px 2px #000">${esc(x.n)}</span></div>`).join('')}</div>`
      : '<div class="empty">Aún no hay fotos. Publicá una en el Muro 🖼️</div>'}</div>`;
}

/* =====================================================================
   RANKING
   ===================================================================== */
async function renderRanking(el) {
  const d = await api('/api/ranking').catch(() => ({ ranking: [] }));
  const medallas = ['🥇', '🥈', '🥉'];
  el.innerHTML = `
    ${secHeader('🏆 Ranking de la academia')}
    <div class="card">
      <p class="small">Puntos: <b>+2</b> por asistencia · <b>+5</b> por video completado · <b>+1</b> por video visto.</p>
      ${d.ranking.length ? d.ranking.map((r, i) => `
        <div class="flex space-between" style="padding:10px 0;border-bottom:1px solid var(--line)">
          <span>${medallas[i] || (i + 1) + 'º'} ${avatarHTML(r.foto, r.nombre, 'sm')} <b>${esc(r.nombre)}</b> ${r.cinturon ? beltHTML(r.cinturon) : ''}</span>
          <span class="small">${r.asistencias} asist · ${r.completados} vid · <b style="color:var(--accent2)">${r.puntos} pts</b></span>
        </div>`).join('') : '<div class="empty">Todavía no hay datos para ranking.</div>'}
    </div>`;
}

/* =====================================================================
   METAS DE ENTRENAMIENTO
   ===================================================================== */
async function renderMetas(el) {
  const d = await api('/api/metas').catch(() => ({ metas: [] }));
  el.innerHTML = `
    ${secHeader('🎯 Mis metas de entrenamiento')}
    <div class="card">
      <form id="metaForm" class="grid2">
        <div class="field" style="grid-column:1/-1"><label>Meta</label><input id="mTitulo" placeholder="ej: Entrenar 3 veces por semana"></div>
        <div class="field"><label>Tipo</label><select id="mTipo"><option value="semanas">Por semana</option><option value="mes">Por mes</option><option value="objetivo">Objetivo puntual</option></select></div>
        <div class="field"><label>Objetivo (veces/valor)</label><input type="number" id="mObj" value="3"></div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">＋ Agregar meta</button></div>
      </form>
    </div>
    <div class="card">
      <h3>Mis metas</h3>
      ${d.metas.length ? d.metas.map(m => `
        <div class="flex space-between" style="padding:10px 0;border-bottom:1px solid var(--line)">
          <span>${m.cumplida ? '✅' : '⭕'} <b>${esc(m.titulo)}</b> <span class="small" style="color:var(--muted)">(${esc(m.tipo)} · ${m.objetivo})</span></span>
          <span>
            ${m.cumplida ? `<button class="btn ghost small" onclick="metaCumplida(${m.id},0)">Desmarcar</button>` : `<button class="btn primary small" onclick="metaCumplida(${m.id},1)">Cumplida ✓</button>`}
            <button class="btn ghost small" onclick="borrarMeta(${m.id})">🗑</button>
          </span>
        </div>`).join('') : '<div class="empty">No tenés metas todavía. Agrega la primera 🎯</div>'}
    </div>`;
  $('#metaForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/metas', { method: 'POST', body: { titulo: $('#mTitulo').value, tipo: $('#mTipo').value, objetivo: +$('#mObj').value } });
      renderMetas($('#sec-metas'));
    } catch (err) { toast(err.message); }
  });
}
async function metaCumplida(id, val) {
  try { await api('/api/metas/' + id, { method: 'POST', body: { cumplida: val } }); renderMetas($('#sec-metas')); }
  catch (e) { toast(e.message); }
}
async function borrarMeta(id) {
  try { await api('/api/metas/' + id, { method: 'DELETE' }); renderMetas($('#sec-metas')); }
  catch (e) { toast(e.message); }
}

/* =====================================================================
   ENCUESTAS
   ===================================================================== */
async function renderEncuestas(el) {
  const d = await api('/api/encuestas').catch(() => ({ encuestas: [] }));
  const esStaff = USER.role !== 'alumno';
  el.innerHTML = `
    ${secHeader('📊 Encuestas')}
    ${esStaff ? `<div class="card">
      <h3>Crear encuesta</h3>
      <form id="encForm" class="grid2">
        <div class="field" style="grid-column:1/-1"><label>Pregunta</label><input id="eTitulo"></div>
        <div class="field" style="grid-column:1/-1"><label>Opciones (una por línea)</label><textarea id="eOpc" style="width:100%;min-height:70px" placeholder="Opción 1&#10;Opción 2&#10;Opción 3"></textarea></div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Publicar encuesta</button></div>
      </form>
    </div>` : ''}
    <div class="card"><h3>Encuestas</h3>
      ${d.encuestas.length ? d.encuestas.map(e => {
        const total = e.conteo.reduce((a, b) => a + b, 0);
        return `<div style="padding:10px 0;border-bottom:1px solid var(--line)">
          <b>${esc(e.titulo)}</b> <span class="small" style="color:var(--muted)">· ${total} voto${total === 1 ? '' : 's'}</span>
          ${e.opciones.map((o, i) => {
            const pct = total ? Math.round(e.conteo[i] * 100 / total) : 0;
            const esMi = e.mi_voto === i;
            return `<div style="margin-top:6px">
              <div class="flex space-between"><span class="small">${esMi ? '✓ ' : ''}${esc(o)}</span><span class="small">${e.conteo[i]} · ${pct}%</span></div>
              <button class="btn ghost small" style="width:100%;margin-top:2px" onclick="votarEncuesta(${e.id},${i})">${esMi ? 'Cambiar voto' : 'Votar'}</button>
              <div style="height:6px;background:var(--bg);border-radius:4px;margin-top:2px"><div style="height:100%;width:${pct}%;background:var(--red);border-radius:4px"></div></div>
            </div>`;
          }).join('')}
        </div>`;
      }).join('') : '<div class="empty">No hay encuestas todavía.</div>'}
    </div>`;
  if (esStaff) $('#encForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const opciones = $('#eOpc').value.split('\n').map(s => s.trim()).filter(Boolean);
    try {
      await api('/api/encuestas', { method: 'POST', body: { titulo: $('#eTitulo').value, opciones } });
      renderEncuestas($('#sec-encuestas'));
    } catch (err) { toast(err.message); }
  });
}
async function votarEncuesta(id, op) {
  try { await api('/api/encuestas/' + id + '/votar', { method: 'POST', body: { opcion: op } }); renderEncuestas($('#sec-encuestas')); }
  catch (e) { toast(e.message); }
}

/* =====================================================================
   EVENTOS Y ACTIVIDADES
   ===================================================================== */
let EVENTOS_CACHE = {};
async function renderEventos(el) {
  const d = await api('/api/eventos').catch(() => ({ eventos: [] }));
  const esStaff = USER.role !== 'alumno';
  EVENTOS_CACHE = {};
  el.innerHTML = `
    ${secHeader('🗓️ Eventos y actividades')}
    ${esStaff ? `<div class="card">
      <h3>Crear evento</h3>
      <form id="evForm" class="grid2">
        <div class="field"><label>Título</label><input id="vTitulo"></div>
        <div class="field"><label>Fecha del evento</label><input type="date" id="vFecha"></div>
        <div class="field"><label>Hora</label><input type="time" id="vHora"></div>
        <div class="field"><label>Lugar</label><input id="vLugar"></div>
        <div class="field" style="grid-column:1/-1"><label>Descripción</label><textarea id="vDesc" style="width:100%;min-height:60px"></textarea></div>
        <div class="field" style="grid-column:1/-1"><label>Foto del evento / flyer (hasta 5)</label>
          <input type="file" id="vFotos" accept="image/*" multiple>
          <div class="flex wrap mt" id="vFotosPre" style="gap:10px"></div>
          <small class="hint">Subí el cartel o flyer del evento. Queda visible para todos.</small>
        </div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">Publicar evento</button></div>
      </form>
    </div>` : ''}
    <div class="card"><h3>Próximos eventos</h3>
      ${d.eventos.length ? d.eventos.map(ev => `
        <div class="flex space-between" style="padding:10px 0;border-bottom:1px solid var(--line)">
          <div>
            <b>${esc(ev.titulo)}</b>
            <div class="small" style="color:var(--muted)">${esc(ev.fecha_evento)}${ev.hora ? ' · ' + esc(ev.hora) : ''}${ev.lugar ? ' · ' + esc(ev.lugar) : ''}</div>
            ${ev.descripcion ? `<div class="small">${esc(ev.descripcion)}</div>` : ''}
            ${ev.fotos && ev.fotos.length ? `<div class="flex wrap mt" style="gap:10px">${ev.fotos.map((f, i) => `<img src="${esc(f)}" data-foto-ev="${ev.id}" data-foto-i="${i}" onclick="abrirFotoEvento(this)" style="width:64px;height:64px;object-fit:cover;border-radius:8px;border:1px solid var(--line);cursor:pointer" title="Ver foto">`).join('')}</div>` : ''}
            <div class="small" style="color:var(--muted)">👥 ${ev.asisten_conf} confirmaron</div>
          </div>
          <span>
            <button class="btn ${ev.voy ? 'ghost' : 'primary'} small" onclick="asistirEvento(${ev.id}, ${ev.voy ? 1 : 0})">${ev.voy ? 'No asistiré' : 'Voy a ir ✓'}</button>
            ${esAdmin() ? `<button class="btn ghost small" onclick="borrarEvento(${ev.id})">🗑</button>` : ''}
          </span>
        </div>`).join('') : '<div class="empty">No hay eventos próximos.</div>'}
      </div>`;
  d.eventos.forEach(ev => { EVENTOS_CACHE[ev.id] = ev; });
  if (esStaff) {
    let fotos = [];
    $('#vFotos').addEventListener('change', async (e) => {
      fotos = [];
      $('#vFotosPre').innerHTML = '';
      const files = Array.from(e.target.files || []).slice(0, 5);
      for (const f of files) {
        try {
          const dataUrl = await comprimirImagen(f, 1200);
          fotos.push(dataUrl);
          $('#vFotosPre').insertAdjacentHTML('beforeend',
            `<img src="${esc(dataUrl)}" style="width:64px;height:64px;object-fit:cover;border-radius:8px;border:1px solid var(--line)">`);
        } catch (err) { toast('No se pudo leer ' + f.name); }
      }
    });
    $('#evForm').addEventListener('submit', async (e) => {
      e.preventDefault();
      try {
        await api('/api/eventos', { method: 'POST', body: {
          titulo: $('#vTitulo').value, fecha_evento: $('#vFecha').value, hora: $('#vHora').value,
          lugar: $('#vLugar').value, descripcion: $('#vDesc').value, fotos: fotos } });
        fotos = [];
        toast('Evento publicado');
        renderEventos($('#sec-eventos'));
      } catch (err) { toast(err.message); }
    });
  }
}
// El src se lee del DOM y se resuelve via el cache, en vez de interpolarse dentro
// del atributo onclick: asi la data-URL nunca queda en el HTML.
function abrirFotoEvento(el) {
  const ev = (EVENTOS_CACHE || {})[el.getAttribute('data-foto-ev')];
  if (!ev || !ev.fotos || !ev.fotos.length) { toast('No hay fotos para este evento'); return; }
  verFoto(ev.fotos[Number(el.getAttribute('data-foto-i'))] || ev.fotos[0]);
}
async function asistirEvento(id, voy) {
  try { await api('/api/eventos/' + id + '/asistir', { method: 'POST', body: { quitar: voy ? 1 : 0 } }); renderEventos($('#sec-eventos')); }
  catch (e) { toast(e.message); }
}
async function borrarEvento(id) {
  if (!confirm('¿Eliminar este evento?')) return;
  try { await api('/api/eventos/' + id, { method: 'DELETE' }); renderEventos($('#sec-eventos')); }
  catch (e) { toast(e.message); }
}

/* =====================================================================
   HISTORIAL CON GRÁFICOS
   ===================================================================== */
async function renderHistorial(el) {
  const d = await api('/api/historial').catch(() => ({ pagos: [], asistencia: [] }));
  const pagos = d.pagos || [];
  const asis = d.asistencia || [];
  const MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
  const maxPagos = Math.max(1, ...pagos.map(p => p.monto));
  const maxAsis = Math.max(1, ...asis.map(a => a.alumnos));
  el.innerHTML = `
    ${secHeader('📈 Historial financiero y asistencia')}
    <div class="card">
      <h3>💵 Ingresos por mes</h3>
      ${pagos.length ? `<div style="display:flex;align-items:flex-end;gap:8px;height:180px;padding-top:10px">
        ${pagos.map(p => `<div style="flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-end">
          <span class="small" style="color:var(--accent2)">$${num(p.monto)}</span>
          <div style="width:100%;background:var(--red);border-radius:6px 6px 0 0;height:${Math.max(4, Math.round(p.monto * 160 / maxPagos))}px"></div>
          <span class="small" style="color:var(--muted)">${MESES[p.mes - 1]}</span>
        </div>`).join('')}
      </div>` : '<div class="empty">Sin pagos registrados.</div>'}
    </div>
    <div class="card">
      <h3>🥋 Asistencia diaria (este mes)</h3>
      ${asis.length ? `<div style="display:flex;align-items:flex-end;gap:4px;height:160px;padding-top:10px;overflow-x:auto">
        ${asis.map(a => `<div style="flex:1;min-width:20px;display:flex;flex-direction:column;align-items:center;justify-content:flex-end">
          <div style="width:100%;background:var(--blue);border-radius:4px 4px 0 0;height:${Math.max(4, Math.round(a.alumnos * 140 / maxAsis))}px"></div>
          <span class="small" style="color:var(--muted)">${a.fecha.slice(8)}</span>
        </div>`).join('')}
      </div>` : '<div class="empty">Sin asistencias este mes.</div>'}
    </div>
    <div class="card">
      <h3>Exportar</h3>
      <button class="btn primary" onclick="exportarExcel()">⬇️ Exportar historial a Excel</button>
    </div>`;
}

/* =====================================================================
   EXPORTAR A EXCEL
   ===================================================================== */
async function exportarExcel() {
  try {
    const hist = await api('/api/historial').catch(() => ({ pagos: [], asistencia: [] }));
    const MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'];
    let csv = '\uFEFFMes,Año,Ingresos,Cantidad\n';
    (hist.pagos || []).forEach(p => csv += MESES[p.mes - 1] + ',' + p.anio + ',$' + p.monto + ',' + p.n + '\n');
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8;' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = 'historial_nexo.csv';
    a.click();
    toast('Historial exportado ✓');
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   RECUPERAR CONTRASEÑA OLVIDADA
   ===================================================================== */
async function guardarSeguridad() {
  const q = ($('#pSecQ')?.value || '').trim();
  const a = ($('#pSecA')?.value || '').trim();
  const np = ($('#pSecPass')?.value || '');
  if (!q || !a) { toast('Completá la pregunta y la respuesta de seguridad'); return; }
  try {
    await api('/api/perfil/seguridad', { method: 'PUT', body: { pregunta: q, respuesta: a, nueva_password: np } });
    toast('Seguridad guardada ✓');
  } catch (e) { toast(e.message); }
}

async function reiniciarPassword(id, nombre) {
  openModal(`
    <h3>🔑 Reiniciar contraseña de ${esc(nombre)}</h3>
    <p class="small">Poné una contraseña nueva para ${esc(nombre)}. Se le notificará que su contraseña fue reiniciada.</p>
    <div class="field"><label>Contraseña nueva (mín. 4 caracteres)</label><input type="password" id="nuevaPass"></div>
    <button class="btn primary btn-block" onclick="hacerReinicio(${id})">Guardar y notificar</button>
    <button class="btn ghost btn-block" onclick="closeModal()">Cancelar</button>
  `);
}

async function hacerReinicio(id) {
  const np = $('#nuevaPass')?.value || '';
  if (np.length < 4) { toast('La contraseña debe tener al menos 4 caracteres'); return; }
  try {
    await api('/api/usuarios/' + id + '/password', { method: 'POST', body: { password: np } });
    closeModal();
    toast('Contraseña reiniciada ✓ Se notificó al alumno.');
  } catch (e) { toast(e.message); }
}
function overlayRecup(markup) {
  const old = document.getElementById('recupOverlay');
  if (old) old.remove();
  const ov = document.createElement('div');
  ov.id = 'recupOverlay';
  ov.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.62);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
  ov.innerHTML = `<div style="width:100%;max-width:400px;background:var(--card,#24182f);border:1px solid var(--line,#3c2a4f);border-radius:16px;padding:22px">${markup}</div>`;
  ov.addEventListener('click', (e) => { if (e.target === ov) ov.remove(); });
  document.body.appendChild(ov);
  return ov;
}

function overlayRecupCerrar() {
  const old = document.getElementById('recupOverlay');
  if (old) old.remove();
}

function abrirModalRecuperar() {
  overlayRecup(`
    <h3 style="color:#fff;margin:0 0 14px">Recuperar contraseña</h3>
    <p class="small" style="color:var(--muted,#b59cc9)">Escribí tu usuario. Si configuraste la pregunta de seguridad, la respondes para cambiar la clave. Si no, pedile al profe/admin que la reinicie.</p>
    <div class="field"><label>Tu usuario</label><input id="recUser" placeholder="Tu usuario"></div>
    <div id="recPasso"></div>
    <button type="button" class="btn primary btn-block" onclick="recPaso1()">Continuar</button>
    <button type="button" class="link-btn" style="width:100%;text-align:center;margin-top:10px" onclick="overlayRecupCerrar()">Cerrar</button>
  `);
}

async function recPaso1() {
  const user = ($('#recUser')?.value || '').trim();
  if (!user) { toast('Escribí tu usuario'); return; }
  try {
    const d = await api('/api/recuperar', { method: 'POST', body: { username: user } });
    const passo = document.getElementById('recPasso');
    if (d.ok) {
      if (passo) passo.innerHTML = `
        <div class="field" style="margin-top:10px"><label>Pregunta de seguridad</label>
          <input value="${esc(d.pregunta)}" disabled style="opacity:.7"></div>
        <div class="field"><label>Tu respuesta</label><input id="recResp" placeholder="Respuesta"></div>
        <div class="field"><label>Contraseña nueva (mín. 4 caracteres)</label><input type="password" id="recNueva"></div>
        <button type="button" class="btn primary btn-block" onclick="recPaso2('${escJs(user)}')">Cambiar mi contraseña</button>`;
    } else {
      if (passo) passo.innerHTML = `
        <p style="color:var(--warn,#f1c40f)">${esc(d.error || 'No tiene pregunta de seguridad configurada.')}</p>
        <p class="small" style="color:var(--muted,#b59cc9)">Pedile al profe/admin que reinicie tu contraseña desde la pantalla de Alumnos.</p>`;
    }
  } catch (e) {
    const passo = document.getElementById('recPasso');
    if (passo) passo.innerHTML = `<p style="color:var(--bad,#e74c3c)">${esc(e.message)}</p>`;
  }
}

async function recPaso2(user) {
  const resp = ($('#recResp')?.value || '').trim();
  const nueva = $('#recNueva')?.value || '';
  if (!resp) { toast('Respondé la pregunta de seguridad'); return; }
  if (nueva.length < 4) { toast('La contraseña debe tener al menos 4 caracteres'); return; }
  try {
    const d = await api('/api/recuperar/verificar', { method: 'POST', body: { username: user, respuesta: resp, nueva_password: nueva } });
    if (d.ok) {
      overlayRecupCerrar();
      toast('Contraseña cambiada ✓ Ingresá con tu clave nueva.');
    }
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   DIARIO DE LA ACADEMIA
   ===================================================================== */
async function renderDiario(el) {
  const d = await api('/api/diario').catch(() => ({ diario: [] }));
  const esStaff = esAdmin() || USER.role === 'profesor';
  const hoy = d.hoy || fechaHoyLocal();
  const yaHoy = d.diario[0] && d.diario[0].fecha === hoy;
  const entradas = d.diario.map(e => `
    <div class="post-card">
      <div class="post-head">
        ${avatarHTML(e.autor_foto, e.autor_nombre, 'sm')} <b>${esc(e.autor_nombre)}</b>
        <span class="small" style="color:var(--muted)">· ${esc(e.fecha)}</span>
        ${esStaff ? `<button class="btn ghost small" style="margin-left:auto" onclick="borrarDiario(${e.id})">🗑</button>` : ''}
      </div>
      ${e.titulo ? `<h4 style="margin:8px 0 4px">${esc(e.titulo)}</h4>` : ''}
      ${e.texto ? `<p class="small" style="white-space:pre-wrap;margin:6px 0 0">${esc(e.texto)}</p>` : ''}
      ${e.foto ? `<img src="${esc(e.foto)}" style="max-width:100%;max-height:260px;border-radius:10px;margin-top:10px;object-fit:cover">` : ''}
    </div>`).join('');
  el.innerHTML = `
    ${secHeader('📓 Diario de la academia', esStaff ? 'Escribí la crónica del día, lo que se trabajó y quiénes vinieron.' : 'La crónica diaria de lo que pasa en el dojo')}
    ${esStaff ? `
    <div class="card">
      <div class="small mb">${yaHoy ? '✏️ Ya escribiste la crónica de hoy. Podés editarla:' : '📝 Crónica de hoy:'}</div>
      <input id="diarioTitulo" placeholder="Título (ej: Trabajo de guardias)" value="${yaHoy ? esc(d.diario[0].titulo || '') : ''}" style="width:100%;margin-bottom:8px">
      <textarea id="diarioTexto" rows="4" placeholder="Contá qué se trabajó hoy, quiénes vinieron, anécdotas..." style="width:100%">${yaHoy ? esc(d.diario[0].texto || '') : ''}</textarea>
      <button class="btn primary btn-block mt" onclick="guardarDiario()">${yaHoy ? 'Actualizar crónica' : 'Guardar crónica de hoy'}</button>
    </div>
    ` : ''}
    <div class="feed">
      ${entradas || '<div class="empty">Todavía no hay entradas en el diario.</div>'}
    </div>`;
}

async function guardarDiario() {
  try {
    await api('/api/diario', { method: 'POST', body: { titulo: $('#diarioTitulo').value, texto: $('#diarioTexto').value } });
    toast('Crónica guardada ✓');
    renderDiario($('#sec-diario'));
  } catch (e) { toast(e.message); }
}

async function borrarDiario(id) {
  if (!confirm('¿Eliminar esta entrada del diario?')) return;
  try {
    await api('/api/diario/' + id, { method: 'DELETE' });
    toast('Entrada eliminada');
    renderDiario($('#sec-diario'));
  } catch (e) { toast(e.message); }
}

/* =====================================================================
   FAMILIAS (grupos familiares)
   ===================================================================== */
async function renderFamilias(el) {
  const d = await api('/api/familias').catch(() => ({ familias: [] }));
  const alumnos = (await api('/api/alumnos').catch(() => ({ alumnos: [] }))).alumnos;
  const usadas = new Set();
  d.familias.forEach(f => (f.miembros || []).forEach(m => usadas.add(m.id)));
  const libres = alumnos.filter(a => !usadas.has(a.id));
  el.innerHTML = `
    ${secHeader('👨‍👩‍👧 Grupos familiares', 'Agrupá familiares para cobrar la cuota con descuento a todos los integrantes')}
    <div class="card">
      <div class="flex space-between" style="align-items:center;gap:8px;margin-bottom:8px">
        <div class="field" style="flex:1;margin:0"><label>Nombre del grupo</label><input id="famNombre" placeholder="Ej: Familia García"></div>
        <div class="field" style="flex:1;margin:0"><label>Titular (primero)</label><select id="famTitular">
          <option value="">— elegir —</option>
          ${libres.map(a => `<option value="${a.id}">${esc(a.nombre)}</option>`).join('')}
        </select></div>
      </div>
      <button class="btn primary btn-block" onclick="crearFamilia()">👨‍👩‍👧 Crear grupo familiar</button>
    </div>
    <div class="feed">
      ${d.familias.length ? d.familias.map(f => `
        <div class="post-card">
          <div class="flex space-between" style="align-items:center">
            <div><b>${esc(f.nombre)}</b> <span class="small" style="color:var(--muted)">· total <b>$${num(f.total)}</b>/mes</span></div>
            <div>
              <button class="btn ghost small" onclick="editarNombreFamilia(${f.id},'${escJs(f.nombre)}')">✏️</button>
              <button class="btn ghost small" onclick="verFamiliaModal(${f.id},'${escJs(f.nombre)}')">➕</button>
              <button class="btn bad small" onclick="borrarFamilia(${f.id},'${escJs(f.nombre)}')">🗑</button>
            </div>
          </div>
          ${(f.miembros || []).map(m => `
            <div class="flex space-between" style="align-items:center;padding:8px 0;border-bottom:1px dashed var(--line)">
              <span>${avatarHTML(m.foto, m.nombre, 'sm')} <b>${esc(m.nombre)}</b> ${m.es_titular ? '<span class="tag tag-al-dia">Titular</span>' : ''}
                <span class="small" style="color:var(--muted)">· ${esc(m.relacion)}</span></span>
              <span class="small">$${num(m.cuota_final)}<br>${m.descuento ? '<span style="color:var(--good)">-' + num(m.descuento) + '</span>' : ''}</span>
              <button class="btn ghost small" onclick="quitarMiembroFamilia(${f.id},${m.id},'${escJs(m.nombre)}')">✕</button>
            </div>`).join('') || '<div class="empty">Sin miembros</div>'}
        </div>`).join('') : '<div class="empty">Todavía no hay grupos familiares. Creá el primero arriba.</div>'}
    </div>`;
}

async function editarNombreFamilia(id, nombre) {
  openModal(`
    <h3>✏️ Renombrar grupo</h3>
    <div class="field"><label>Nombre del grupo</label><input id="efNombre" value="${esc(nombre)}"></div>
    <button class="btn primary btn-block" onclick="guardarNombreFamilia(${id})">Guardar</button>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
  $('#efNombre').focus();
}
async function guardarNombreFamilia(id) {
  try {
    await api('/api/familias/' + id, { method: 'PUT', body: { nombre: $('#efNombre').value } });
    toast('Grupo renombrado ✓');
    closeModal();
    renderFamilias($('#sec-familias'));
  } catch (e) { toast(e.message); }
}
async function borrarFamilia(id, nombre) {
  if (!confirm('¿Eliminar el grupo familiar "' + nombre + '"?')) return;
  try {
    await api('/api/familias/' + id, { method: 'DELETE' });
    toast('Grupo eliminado');
    renderFamilias($('#sec-familias'));
  } catch (e) { toast(e.message); }
}
async function quitarMiembroFamilia(fid, uid, nombre) {
  if (!confirm('¿Sacar a ' + nombre + ' del grupo?')) return;
  try {
    await api('/api/familias/' + fid + '/miembros/' + uid, { method: 'DELETE' });
    toast(nombre + ' fue sacado del grupo');
    renderFamilias($('#sec-familias'));
  } catch (e) { toast(e.message); }
}
async function crearFamilia() {
  const nombre = $('#famNombre').value.trim();
  if (!nombre) { toast('Poné un nombre al grupo'); return; }
  try {
    await api('/api/familias', { method: 'POST', body: { nombre, titular_id: $('#famTitular').value } });
    toast('Grupo familiar creado ✓');
    renderFamilias($('#sec-familias'));
  } catch (e) { toast(e.message); }
}
async function verFamiliaModal(fid, nombre) {
  const d = await api('/api/familias');
  const f = d.familias.find(x => x.id === fid);
  const libres = [];
  try {
    const a = (await api('/api/alumnos')).alumnos;
    const usadas = new Set();
    d.familias.forEach(x => (x.miembros || []).forEach(m => usadas.add(m.id)));
    a.filter(x => !usadas.has(x.id)).forEach(x => libres.push(x));
  } catch (e) {}
  openModal(`
    <h3>👨‍👩‍👧 ${esc(nombre)}</h3>
    ${(f.miembros || []).map(m => `
      <div class="flex space-between" style="align-items:center;padding:8px 0;border-bottom:1px dashed var(--line)">
        <span>${avatarHTML(m.foto, m.nombre, 'sm')} <b>${esc(m.nombre)}</b> <span class="small" style="color:var(--muted)">· ${esc(m.relacion)}</span> ${m.es_titular ? '<span class="tag tag-al-dia">Titular</span>' : ''}</span>
        <button class="btn ghost small" onclick="quitarMiembroFamilia(${fid},${m.id},'${escJs(m.nombre)}')">✕</button>
      </div>`).join('')}
    <div class="field mt"><label>Agregar miembro</label><select id="fmUser">
      <option value="">— elegir alumno —</option>
      ${libres.map(a => `<option value="${a.id}">${esc(a.nombre)}</option>`).join('')}
    </select></div>
    <div class="field"><label>Relación</label><select id="fmRel">
      <option>Hijo/a</option><option>Hija</option><option>Pareja</option>
      <option>Mamá</option><option>Papá</option><option>Hermano/a</option><option>Familiar</option>
    </select></div>
    <button class="btn primary btn-block" onclick="agregarMiembroFamilia(${fid})">➕ Agregar al grupo</button>
    <button class="btn ghost btn-block mt" onclick="closeModal()">Cerrar</button>`);
}
async function agregarMiembroFamilia(fid) {
  const uid = $('#fmUser').value;
  if (!uid) { toast('Elegí un alumno'); return; }
  try {
    await api('/api/familias/' + fid + '/miembros', { method: 'POST', body: { user_id: uid, relacion: $('#fmRel').value } });
    toast('Miembro agregado ✓');
    closeModal();
    renderFamilias($('#sec-familias'));
  } catch (e) { toast(e.message); }
}



async function renderMiDinero(el) {
  const soyAdmin = esAdmin();
  el.innerHTML = secHeader(soyAdmin ? 'Reparto de dinero' : 'Mi dinero');
  try {
    const d = await api('/api/mi_dinero');
    const p = d.pct || { profes: 60, tatami: 30, administrativo: 10 };
    const tot = d.total_mes || 0;
    const cobrado = d.cobrado_mes || 0;
    const sub = soyAdmin
      ? `Cada cuota se parte en <b>${p.profes}% para los profes</b>, ${p.tatami}% tatami y academia y ${p.administrativo}% administrativo. El ${p.profes}% se divide en partes iguales entre los profes que dan las actividades del alumno.`
      : `Te corresponde el <b>${p.profes}% de cada cuota</b>, en partes iguales entre los profes que dan las actividades del alumno.`;
    el.innerHTML = secHeader(soyAdmin ? 'Reparto de dinero' : 'Mi dinero')
      + `<div class="small" style="color:var(--muted);padding:0 4px 10px">${sub}</div>`;
    let html = '';
    if (soyAdmin) {
      const dmap = {};
      (d.destinos || []).forEach(x => { dmap[x.destino] = x.monto; });
      const buckets = [
        { lbl: `Profes (${p.profes}%)`, val: tot, color: 'var(--good)' },
        { lbl: `Tatami y academia (${p.tatami}%)`, val: dmap.tatami || 0, color: '#7c6cf0' },
        { lbl: `Administrativo (${p.administrativo}%)`, val: dmap.administrativo || 0, color: '#e0a13a' },
      ];
      html += `<div class="card"><h3>💼 Cómo se dividió lo cobrado</h3>
        <div class="home-grid">
          ${buckets.map(b => `<div class="stat-card"><div class="num" style="color:${b.color}">$${num(b.val)}</div><div class="lbl">${b.lbl}</div></div>`).join('')}
        </div>
        <div class="small" style="color:var(--muted);margin-top:8px">
          Total cobrado ${d.mes}/${d.anio}: $${num(cobrado)} · ${(d.pagos || []).length} partes de profes
        </div>
        ${cobrado > 0 && !dmap.tatami ? `<div class="small" style="color:var(--muted)">Tatami y administrativo aparecen desde que se activó el reparto 60/30/10: los pagos anteriores no se dividen.</div>` : ''}
      </div>`;
    } else {
      html += `<div class="card"><div class="home-grid">
        <div class="stat-card"><div class="num" style="color:var(--good)">$${num(tot)}</div><div class="lbl">${d.mes}/${d.anio}</div></div>
      </div></div>`;
    }
    if (soyAdmin && (d.por_profesor || []).length) {
      const base = cobrado || tot;
      html += `<div class="card"><h3>👥 Cuánto le tocó a cada profesor</h3>
        <div style="overflow:auto"><table>
          <tr><th>Profesor</th><th>Total ${d.mes}/${d.anio}</th><th>% de lo cobrado</th></tr>
          ${d.por_profesor.map(pr => {
            const pct = base > 0 ? Math.round((pr.total / base) * 100) : 0;
            return `<tr><td>${esc(pr.nombre || 'Sin nombre')}</td><td><b>$${num(pr.total)}</b></td>
              <td style="min-width:120px"><div style="background:rgba(255,255,255,.08);border-radius:6px;height:10px;overflow:hidden">
                <div style="background:var(--good);height:100%;width:${pct}%"></div></div>
                <span class="small" style="color:var(--muted)">${pct}%</span></td></tr>`;
          }).join('')}
        </table></div></div>`;
    }
    html += `<div class="card"><h3>🧾 Detalle de pagos</h3>
      <div style="overflow:auto"><table>
        <tr><th>Fecha</th><th>Alumno</th><th>Actividad</th>${soyAdmin ? '<th>Profesor</th>' : ''}<th>Mes</th><th>Método</th><th>${soyAdmin ? 'Parte del profesor' : 'Mi parte'}</th></tr>
        ${(d.pagos || []).length ? d.pagos.map(p => `<tr>
          <td>${esc(p.fecha)}</td>
          <td>${esc(p.alumno || '—')}</td>
          <td>${p.actividad ? `<span class="tag">${esc(p.actividad)}</span>` : '—'}</td>
          ${soyAdmin ? `<td>${esc(p.profesor || '—')}</td>` : ''}
          <td>${p.mes}/${p.anio}</td><td>${esc(p.metodo || '')}</td>
          <td><b style="color:var(--good)">$${num(p.monto)}</b></td></tr>`).join('')
          : '<tr><td colspan="7" class="empty">Todavía no hay pagos repartidos</td></tr>'}
      </table></div></div>`;
    el.innerHTML = el.innerHTML + html;
  } catch (e) { toast(e.message); }
}

const DESTINOS_EXTRA = ['Fondo academia', 'Viaje a competencia', 'Seminario', 'Cuota de un día', 'Equipamiento', 'Otro'];

async function renderIngresosExtra(el) {
  const soyAdmin = esAdmin();
  el.innerHTML = secHeader('Ingresos extra') + `
    <div class="small" style="color:var(--muted);padding:0 4px 10px">
      Cobros puntuales que <b>no son la cuota mensual</b> y <b>no se reparten</b> entre los profesores: van al fondo de la academia.
    </div>
    <div class="card">
      <form id="ieForm" class="grid2">
        <div class="field"><label>Monto ($)</label><input type="number" step="0.01" id="ieMonto" required placeholder="0"></div>
        <div class="field"><label>Concepto</label><input type="text" id="ieConcepto" required placeholder="Ej: Cuota de un día"></div>
        <div class="field"><label>Destino del dinero</label><select id="ieDestino">${DESTINOS_EXTRA.map(x => `<option>${esc(x)}</option>`).join('')}</select></div>
        <div class="field"><label>Alumno (opcional)</label><select id="ieAlumno"><option value="">— ninguno —</option></select></div>
        <div class="field"><label>Método</label><select id="ieMetodo">${METODOS.map(m => `<option>${esc(m)}</option>`).join('')}</select></div>
        <div class="field"><label>Nota (opcional)</label><input type="text" id="ieNota" placeholder="Ej: viaje a Bs.As."></div>
        <div class="field" style="grid-column:1/-1"><button class="btn primary btn-block" type="submit">🎁 Registrar ingreso al fondo</button></div>
      </form>
    </div>
    <div id="ieResumen"></div>`;

  try {
    const d = await api('/api/ingresos_extra');
    const P = window.NEXO_PRECIOS || [0, 0, 0];
    document.getElementById('ieResumen').innerHTML = `
      <div class="card">
        <div class="home-grid">
          <div class="stat-card"><div class="num" style="color:var(--accent2)">$${num(d.total_mes || 0)}</div><div class="lbl">Fondo ${d.mes}/${d.anio}</div></div>
          <div class="stat-card"><div class="num" style="color:var(--accent2)">$${num(d.total_all || 0)}</div><div class="lbl">Fondo acumulado</div></div>
        </div>
      </div>
      ${(d.por_destino || []).length ? `<div class="card"><h3>🎯 Por destino</h3>
        <div style="overflow:auto"><table><tr><th>Destino</th><th>Total</th></tr>
        ${d.por_destino.map(x => `<tr><td>${esc(x.destino)}</td><td><b>$${num(x.total)}</b></td></tr>`).join('')}
        </table></div></div>` : ''}
      <div class="card"><h3>📋 Historial</h3>
        <div style="overflow:auto"><table>
          <tr><th>Fecha</th><th>Concepto</th><th>Destino</th><th>Alumno</th><th>Mes</th><th>Método</th><th>Monto</th>${soyAdmin ? '<th></th>' : ''}</tr>
          ${(d.ingresos || []).length ? d.ingresos.map(x => `<tr>
            <td>${esc(x.fecha)}</td><td>${esc(x.concepto)}</td>
            <td>${esc(x.destino || '—')}</td><td>${esc(x.alumno || '—')}</td>
            <td>${x.mes}/${x.anio}</td><td>${esc(x.metodo || '')}</td>
            <td><b>$${num(x.monto)}</b></td>
            ${soyAdmin ? `<td><button class="btn bad small" onclick="borrarIngresoExtra(${x.id})">🗑</button></td>` : ''}
          </tr>`).join('') : '<tr><td colspan="8" class="empty">Todavía no hay ingresos extra</td></tr>'}
        </table></div></div>`;

    const sel = document.getElementById('ieAlumno');
    if (sel && sel.options.length <= 1) {
      try {
        const a = await api('/api/alumnos');
        sel.innerHTML = '<option value="">— ninguno —</option>' +
          (a.alumnos || []).map(x => `<option value="${x.id}">${esc(x.nombre)}</option>`).join('');
      } catch (e) {}
    }
  } catch (e) { toast(e.message); }

  const f = document.getElementById('ieForm');
  if (f) f.addEventListener('submit', async (ev) => {
    ev.preventDefault();
    try {
      await api('/api/ingresos_extra', { method: 'POST', body: {
        monto: +document.getElementById('ieMonto').value,
        concepto: document.getElementById('ieConcepto').value,
        destino: document.getElementById('ieDestino').value,
        alumno_id: document.getElementById('ieAlumno').value || null,
        metodo: document.getElementById('ieMetodo').value,
        nota: document.getElementById('ieNota').value
      } });
      toast('Ingreso registrado en el fondo ✓');
      renderIngresosExtra(el);
    } catch (e) { toast(e.message); }
  });
}

async function borrarIngresoExtra(id) {
  if (!confirm('¿Borrar este ingreso del fondo?')) return;
  try {
    await api('/api/ingresos_extra/' + id, { method: 'DELETE' });
    toast('Borrado');
    renderIngresosExtra(document.getElementById('sec-ingresos_extra'));
  } catch (e) { toast(e.message); }
}

async function renderDescuentos(el) {
  el.innerHTML = secHeader('Descuentos') + '<div class="small" style="color:var(--muted);padding:0 4px 10px">Cuánto paga cada alumno: precio por cantidad de actividades y descuento del grupo familiar.</div>';
  try {
    const P = window.NEXO_PRECIOS || [0, 0, 0];
    const cfg = await api('/api/settings').catch(() => ({}));
    const d2 = Number((cfg.desc_familiar2 || 0)) || 0;
    const d3 = Number((cfg.desc_familiar3 || 0)) || 0;
    const d4 = Number((cfg.desc_familiar4 || 0)) || 0;

    let html = `
    <div class="card">
      <h3>📐 Precio según actividades</h3>
      <div class="home-grid">
        <div class="stat-card"><div class="num">$${num(P[0])}</div><div class="lbl">1 actividad</div></div>
        <div class="stat-card"><div class="num">$${num(P[1])}</div><div class="lbl">2 actividades</div></div>
        <div class="stat-card"><div class="num">$${num(P[2])}</div><div class="lbl">3 o más</div></div>
      </div>
      <p class="small" style="color:var(--muted)">Se cobra por la cantidad de actividades en las que entrena el alumno, no por la cantidad de profesores.</p>
    </div>
    <div class="card">
      <h3>👨‍👩‍👧 Descuento familiar</h3>
      <p class="small" style="color:var(--muted)">2 integrantes: ${d2}% · 3: ${d3}% · 4 o más: ${d4}% (se cambian en Configuración)</p>
    </div>`;

    let fam = { familias: [] };
    try { fam = await api('/api/familias'); } catch (e) {}
    const fams = (fam.familias || []).filter(f => (f.miembros || []).length > 1);
    if (fams.length) {
      html += `<div class="card"><h3>👨‍👩‍👧 Quién está en cada familia</h3>` + fams.map(f => {
        const n = (f.miembros || []).length;
        const pct = n >= 4 ? d4 : n === 3 ? d3 : n === 2 ? d2 : 0;
        const miembros = (f.miembros || []).map(m => {
          const acts = (m.actividades || '').split(',').map(s => s.trim()).filter(Boolean);
          const precio = acts.length >= 3 ? P[2] : acts.length === 2 ? P[1] : acts.length === 1 ? P[0] : 0;
          const final = pct > 0 ? Math.round(precio * (100 - pct) / 100) : precio;
          return `<div style="padding:10px 0;border-bottom:1px solid var(--line)">
            <div class="flex space-between"><b>${esc(m.nombre)}</b>
              <span class="small">${precio ? 'paga' : 'sin cuota'}${precio ? ` <s style="color:var(--muted)">$${num(precio)}</s> <b style="color:var(--good)">$${num(final)}</b>` : ''}</span></div>
            <div class="small" style="color:var(--muted);margin-top:4px">${acts.length ? acts.map(a => `<span class="tag">${esc(a)}</span>`).join(' ') : '<i>sin actividades cargadas</i>'}</div>
          </div>`;
        }).join('');
        return `<div style="margin-bottom:18px">
          <div class="flex space-between" style="margin-bottom:6px">
            <b>${esc(f.nombre || 'Familia')}</b>
            <span class="small" style="color:var(--accent2)">${n} integrantes${pct ? ` · ${pct}% de descuento` : ' · sin descuento'}</span>
          </div>${miembros}</div>`;
      }).join('') + `</div>`;
    } else {
      html += `<div class="card"><div class="empty">Todavía no hay grupos familiares con más de un integrante</div></div>`;
    }
    el.innerHTML = el.innerHTML + html;
  } catch (e) { toast(e.message); }
}
