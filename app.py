import os
import io
import re
import base64
import json
import secrets
import time
import zipfile
import threading
import urllib.request
import urllib.error
from urllib.parse import urlparse
from datetime import datetime, date, timedelta, timezone

from flask import Flask, request, jsonify, session, redirect, url_for, render_template, g, send_from_directory, Response
from werkzeug.security import generate_password_hash, check_password_hash

import dbadapter
from dbadapter import DB_MODE

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['DATABASE'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024
MAX_VIDEO_BYTES = 150 * 1024 * 1024

# ---------------------------------------------------------------------------
# Supabase Storage (híbrido: videos cortos → Storage; largos → base64 en DB)
# Con SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY activos, los videos de hasta
# STORAGE_MAX se suben al bucket público y dejan de vivir en la base (menos
# RAM al servirlos y sin hinchar la base). Si falta la config o el bucket
# falla, se conserva el comportamiento viejo (base64 en la DB).
# ---------------------------------------------------------------------------
SUPABASE_URL = (os.environ.get('SUPABASE_URL') or '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or ''
SUPABASE_BUCKET = (os.environ.get('SUPABASE_BUCKET') or 'ikigai-media').strip().lower()
STORAGE_MAX = 50 * 1024 * 1024
_storage_ready = [False]

EXT_MIME = {'.mp4': 'video/mp4', '.webm': 'video/webm', '.ogg': 'video/ogg', '.mov': 'video/quicktime'}


def _storage_enabled():
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _storage_ext(mime):
    for e, m in EXT_MIME.items():
        if m == mime:
            return e
    return '.mp4'


def _storage_request(method, path, body=None, ctype=None, timeout=120):
    req = urllib.request.Request(SUPABASE_URL + '/storage/v1' + path, data=body, method=method)
    req.add_header('apikey', SUPABASE_KEY)
    req.add_header('Authorization', 'Bearer ' + SUPABASE_KEY)
    if ctype:
        req.add_header('Content-Type', ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _storage_bucket_ok():
    """Crea el bucket público la primera vez; no falla si ya existe."""
    if not _storage_enabled():
        return False
    if _storage_ready[0]:
        return True
    body = json.dumps({'name': SUPABASE_BUCKET, 'public': True,
                       'file_size_limit': STORAGE_MAX}).encode('utf-8')
    status, _ = _storage_request('POST', '/bucket', body=body, ctype='application/json')
    if status in (200, 201, 400, 409, 423):
        _storage_ready[0] = True
        return True
    return False


def _storage_upload(key, raw, ctype):
    """Sube bytes al bucket. Devuelve la URL pública o None si falló."""
    if _storage_bucket_ok():
        status, _ = _storage_request(
            'POST', '/object/%s/%s' % (SUPABASE_BUCKET, key), body=raw, ctype=ctype or 'video/mp4')
        if status in (200, 201):
            return '%s/storage/v1/object/public/%s/%s' % (SUPABASE_URL, SUPABASE_BUCKET, key)
    return None


def _storage_prefix():
    return SUPABASE_URL + '/storage/v1/object/public/' + SUPABASE_BUCKET + '/'


def _is_storage_url(url):
    """True solo si Storage está configurado Y la URL apunta a nuestro bucket."""
    return bool(_storage_enabled() and url and url.startswith(_storage_prefix()))


def _storage_delete(url):
    """Borra el objeto del Storage si la URL apunta a nuestro bucket."""
    if not _storage_enabled() or not url:
        return
    if _is_storage_url(url):
        try:
            _storage_request('DELETE', '/object/%s/%s' % (SUPABASE_BUCKET, url[len(_storage_prefix()):]))
        except Exception:
            pass


def _storage_stream(url, range_hdr, ctype='video/mp4'):
    """Proxy a un objeto del bucket streamando, respetando Range y con caché larga.

    Así el video sale de Supabase UNA vez por navegador (la primera reproducción)
    y las repeticiones se sirven desde la caché del dispositivo, sin gastar egress.
    """
    headers = {'Range': range_hdr} if range_hdr else {}
    req = urllib.request.Request(url, headers=headers, method='GET')
    try:
        r = urllib.request.urlopen(req, timeout=30)
    except urllib.error.HTTPError as e:
        if e.code == 416:
            return Response(status=416, headers={
                'Content-Range': e.headers.get('Content-Range', 'bytes */1'),
                'Cache-Control': 'no-store'})
        return None
    out_headers = {
        'Content-Type': ctype,
        'Accept-Ranges': 'bytes',
        'Cache-Control': 'public, max-age=15552000, immutable',
    }
    for h in ('Content-Length', 'Content-Range'):
        val = r.headers.get(h)
        if val:
            out_headers[h] = val

    def _gen():
        try:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                yield chunk
        finally:
            r.close()

    return Response(_gen(), status=r.getcode(), headers=out_headers)

# Token secreto embebido en el QR físico de asistencia. Solo quien escanea
# el QR del gimnasio (que contiene este token) puede registrar su asistencia.
QR_SECRET = os.environ.get('QR_SECRET', 'ikigai2024-nopuedesmarcardesdecasa')

BELTS_ADULT = ['Blanco', 'Azul', 'Púrpura', 'Marrón', 'Negro']
BELTS_KIDS = ['Gris', 'Amarillo', 'Naranja', 'Verde', 'Blanco']
BELTS_JUV = ['Blanco', 'Gris', 'Amarillo', 'Naranja', 'Verde']
CATEGORIAS = ['adulto', 'juveniles', 'kids']
TIPOS_CLASE = ['Gi', 'NoGi', 'Kids', 'Juveniles', 'Abierto']
METODOS_PAGO = ['Efectivo', 'Transferencia', 'Débito', 'Crédito', 'Otro']
DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
MESES_NOMBRE = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']

# Link de pago online de la academia. Se puede overridear en Ajustes > pago_link.
PAGO_LINK_DEFAULT = 'https://link.mercadopago.com.ar/bjjviedma'

VAPID_PRIVATE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vapid_private.pem')
VAPID_PUBLIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'vapid_public.pem')

# ---------------------------------------------------------------------------
# Base de datos
# ---------------------------------------------------------------------------

def get_db():
    if 'db' not in g:
        if DB_MODE == 'mysql':
            db = dbadapter.connect_mysql()
            g.db = db
            row = db.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name IN ('settings','videos')").fetchone()
            if row['c'] < 2:
                db.close()
                init_db()
                db = dbadapter.connect_mysql()
        elif DB_MODE == 'postgres':
            db = dbadapter.connect_postgres()
            g.db = db
            row = db.execute(
                "SELECT COUNT(*) AS c FROM information_schema.tables "
                "WHERE table_schema = current_schema() AND table_name IN ('settings','videos')").fetchone()
            if row['c'] < 2:
                db.close()
                init_db()
                db = dbadapter.connect_postgres()
        else:
            db = dbadapter.connect_sqlite(app.config['DATABASE'])
            # Asignar antes del SELECT: si la query falla, teardown_appcontext
            # cierra la connection igual en vez de fugarla.
            g.db = db
            row = db.execute(
                "SELECT COUNT(*) AS c FROM sqlite_master WHERE type='table' AND name IN ('settings','videos')").fetchone()
            if row['c'] < 2:
                db.close()
                init_db()
                db = dbadapter.connect_sqlite(app.config['DATABASE'])
        g.db = db
    return g.db


@app.teardown_appcontext
def close_db(exc):
    db = g.pop('db', None)
    if db is not None:
        db.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    k VARCHAR(100) PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','profesor','alumno')),
    nombre TEXT NOT NULL,
    edad INTEGER,
    peso REAL,
    cinturon TEXT,
    categoria TEXT DEFAULT 'adulto',
    gi_pref TEXT DEFAULT 'Ambas',
    cuota_mensual REAL,
    tel TEXT,
    nacimiento TEXT,
    medic_info TEXT,
    emergency_contact TEXT,
    activo INTEGER DEFAULT 1,
    security_q TEXT,
    security_a TEXT,
    tel_tutor TEXT,
    tel_2 TEXT,
    direccion TEXT,
    dni TEXT,
    foto_ok INTEGER DEFAULT 0,
    acepto_tyc TEXT,
    medic_enfermedades TEXT,
    medic_alergias TEXT,
    medic_medicacion TEXT,
    medic_lesiones TEXT,
    ficha_fecha TEXT,
    firma_tyc TEXT,
    firma_foto TEXT,
    firma_fecha TEXT,
    pausa_desde TEXT,
    pausa_hasta TEXT,
    beca INTEGER DEFAULT 0,
    creado TEXT
);

CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    dia INTEGER NOT NULL,
    hora TEXT NOT NULL,
    tipo TEXT NOT NULL DEFAULT 'Gi',
    nivel TEXT DEFAULT 'Todos',
    profesor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    duracion INTEGER DEFAULT 60
);

CREATE TABLE IF NOT EXISTS pagos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profesor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    monto REAL NOT NULL,
    mes INTEGER NOT NULL,
    anio INTEGER NOT NULL,
    metodo TEXT DEFAULT 'Efectivo',
    concepto TEXT DEFAULT 'Cuota mensual',
    nota TEXT,
    fecha TEXT,
    registrado_por INTEGER
);

CREATE TABLE IF NOT EXISTS avisos_pago (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monto REAL,
    mes INTEGER NOT NULL,
    anio INTEGER NOT NULL,
    nota TEXT,
    comprobante TEXT,
    estado TEXT DEFAULT 'pendiente',
    fecha TEXT,
    confirmado_por INTEGER,
    confirmado_fecha TEXT
);

CREATE TABLE IF NOT EXISTS asistencia (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clase_id INTEGER REFERENCES classes(id) ON DELETE CASCADE,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fecha VARCHAR(10) NOT NULL,
    presente INTEGER DEFAULT 1,
    UNIQUE(clase_id, alumno_id, fecha)
);

CREATE TABLE IF NOT EXISTS notificaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    titulo TEXT,
    mensaje TEXT,
    tipo TEXT DEFAULT 'info',
    leida INTEGER DEFAULT 0,
    fecha TEXT,
    link TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    endpoint TEXT UNIQUE,
    p256dh TEXT,
    auth TEXT
);

CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    descripcion TEXT DEFAULT '',
    belt TEXT DEFAULT 'Todos',
    categoria TEXT DEFAULT 'adulto',
    url TEXT NOT NULL,
    tipo TEXT DEFAULT 'upload',
    subido_por INTEGER REFERENCES users(id) ON DELETE SET NULL,
    fecha TEXT,
    data TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS video_views (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fecha TEXT,
    UNIQUE(video_id, user_id)
);

CREATE TABLE IF NOT EXISTS video_progress (
    video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    segundos INTEGER DEFAULT 0,
    duracion INTEGER DEFAULT 0,
    completado INTEGER DEFAULT 0,
    fecha TEXT,
    PRIMARY KEY (video_id, user_id)
);

CREATE TABLE IF NOT EXISTS chats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT,
    tipo TEXT DEFAULT 'grupo',
    creado_por INTEGER REFERENCES users(id) ON DELETE SET NULL,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS chat_members (
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (chat_id, user_id)
);

CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE SET NULL,
    mensaje TEXT,
    adjunto TEXT,
    adjunto_tipo TEXT,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS muro (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    texto TEXT,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS muro_fotos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    muro_id INTEGER REFERENCES muro(id) ON DELETE CASCADE,
    data TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS muro_videos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    muro_id INTEGER REFERENCES muro(id) ON DELETE CASCADE,
    tipo TEXT DEFAULT 'link',
    url TEXT DEFAULT '',
    data TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS metas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL,
    tipo TEXT DEFAULT 'semanas',
    objetivo INTEGER DEFAULT 3,
    cumplida INTEGER DEFAULT 0,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS encuestas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL,
    opciones TEXT,
    activa INTEGER DEFAULT 1,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS encuesta_votos (
    encuesta_id INTEGER NOT NULL REFERENCES encuestas(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    opcion INTEGER,
    PRIMARY KEY (encuesta_id, user_id)
);

CREATE TABLE IF NOT EXISTS eventos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    titulo TEXT NOT NULL,
    descripcion TEXT,
    fecha TEXT,
    hora TEXT,
    lugar TEXT,
    fecha_evento TEXT
);

CREATE TABLE IF NOT EXISTS evento_asistencias (
    evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    PRIMARY KEY (evento_id, user_id)
);

CREATE TABLE IF NOT EXISTS evento_fotos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    evento_id INTEGER NOT NULL REFERENCES eventos(id) ON DELETE CASCADE,
    data TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS grados (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    cinturon TEXT,
    fecha TEXT,
    notas TEXT,
    registrado_por INTEGER
);

CREATE TABLE IF NOT EXISTS familias (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    titular_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    fecha TEXT
);

CREATE TABLE IF NOT EXISTS familia_miembros (
    familia_id INTEGER NOT NULL REFERENCES familias(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    relacion TEXT DEFAULT 'familia',
    PRIMARY KEY (familia_id, user_id)
);

CREATE TABLE IF NOT EXISTS diario (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    titulo TEXT,
    texto TEXT,
    foto TEXT,
    UNIQUE (fecha)
);

CREATE TABLE IF NOT EXISTS clase_valoraciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    clase_id INTEGER NOT NULL REFERENCES classes(id) ON DELETE CASCADE,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fecha TEXT NOT NULL,
    estrellas INTEGER NOT NULL,
    comentario TEXT,
    UNIQUE (clase_id, alumno_id, fecha)
);

CREATE TABLE IF NOT EXISTS planes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    titulo TEXT NOT NULL,
    descripcion TEXT,
    categoria TEXT DEFAULT 'todos',
    cinturon TEXT DEFAULT 'todos',
    fecha TEXT,
    autor_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    activo INTEGER DEFAULT 1,
    creado TEXT
);

CREATE TABLE IF NOT EXISTS plan_hecho (
    plan_id INTEGER NOT NULL REFERENCES planes(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    fecha TEXT,
    PRIMARY KEY (plan_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_pagos_alumno ON pagos(alumno_id);
CREATE INDEX IF NOT EXISTS idx_pagos_mes_anio ON pagos(mes, anio);
CREATE INDEX IF NOT EXISTS idx_asistencia_alumno ON asistencia(alumno_id);
CREATE INDEX IF NOT EXISTS idx_asistencia_fecha ON asistencia(fecha);
CREATE INDEX IF NOT EXISTS idx_notif_user ON notificaciones(user_id, leida);
CREATE INDEX IF NOT EXISTS idx_videos_categoria ON videos(categoria);
CREATE INDEX IF NOT EXISTS idx_videos_belt ON videos(belt);
CREATE INDEX IF NOT EXISTS idx_views_user ON video_views(user_id);
CREATE INDEX IF NOT EXISTS idx_progress_user ON video_progress(user_id);
CREATE INDEX IF NOT EXISTS idx_chat_messages_chat ON chat_messages(chat_id, id);
CREATE INDEX IF NOT EXISTS idx_muro_fotos_muro ON muro_fotos(muro_id);
CREATE INDEX IF NOT EXISTS idx_muro_videos_muro ON muro_videos(muro_id);
CREATE INDEX IF NOT EXISTS idx_grados_alumno ON grados(alumno_id);
"""


def init_db():
    if DB_MODE == 'postgres':
        db = dbadapter.connect_postgres()
    elif DB_MODE == 'mysql':
        db = dbadapter.connect_mysql()
    else:
        db = dbadapter.connect_sqlite(app.config['DATABASE'])
    # try/finally: antes la conexion solo se cerraba al final del camino feliz y
    # cualquier error en una migracion la dejaba abierta (fuga + pool agotado).
    try:
        _init_db_body(db)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _init_db_body(db):
    db.executescript(SCHEMA)
    c = db.cursor()
    # migracion: agregar columnas nuevas si faltan
    if DB_MODE == 'postgres':
        cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='users'").fetchall()]
    elif DB_MODE == 'mysql':
        cols = [r[0] for r in c.execute('SHOW COLUMNS FROM users').fetchall()]
    else:
        cols = [r[1] for r in c.execute('PRAGMA table_info(users)').fetchall()]
    if 'foto' not in cols:
        c.execute('ALTER TABLE users ADD COLUMN foto TEXT')
    for col, ddl in [('tel', 'TEXT'), ('nacimiento', 'TEXT'), ('medic_info', 'TEXT'), ('emergency_contact', 'TEXT'),
                     ('security_q', 'TEXT'), ('security_a', 'TEXT'), ('tel_tutor', 'TEXT'), ('tel_2', 'TEXT'),
                     ('direccion', 'TEXT'), ('dni', 'TEXT'), ('foto_ok', 'INTEGER'),
                     ('acepto_tyc', 'TEXT'), ('proximo_examen', 'TEXT'), ('notas_internas', 'TEXT'),
                     ('medic_enfermedades', 'TEXT'), ('medic_alergias', 'TEXT'), ('medic_medicacion', 'TEXT'),
                     ('medic_lesiones', 'TEXT'), ('ficha_fecha', 'TEXT'),
                     ('firma_tyc', 'TEXT'), ('firma_foto', 'TEXT'), ('firma_fecha', 'TEXT'),
                     ('pausa_desde', 'TEXT'), ('pausa_hasta', 'TEXT'), ('beca', 'INTEGER DEFAULT 0')]:
        if col not in cols:
            c.execute('ALTER TABLE users ADD COLUMN %s %s' % (col, ddl))
    try:
        c.execute('UPDATE users SET beca=0 WHERE beca IS NULL')
    except Exception:
        pass
    if DB_MODE == 'postgres':
        ev_cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='eventos'").fetchall()]
    elif DB_MODE == 'mysql':
        ev_cols = [r[0] for r in c.execute('SHOW COLUMNS FROM eventos').fetchall()]
    else:
        ev_cols = [r[1] for r in c.execute('PRAGMA table_info(eventos)').fetchall()]
    if 'recordado' not in ev_cols:
        c.execute('ALTER TABLE eventos ADD COLUMN recordado INTEGER DEFAULT 0')
    if DB_MODE == 'postgres':
        ap_cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='avisos_pago'").fetchall()]
    elif DB_MODE == 'mysql':
        ap_cols = [r[0] for r in c.execute('SHOW COLUMNS FROM avisos_pago').fetchall()]
    else:
        ap_cols = [r[1] for r in c.execute('PRAGMA table_info(avisos_pago)').fetchall()]
    if 'comprobante' not in ap_cols:
        c.execute('ALTER TABLE avisos_pago ADD COLUMN comprobante TEXT')
    if DB_MODE == 'postgres':
        v_cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='videos'").fetchall()]
    elif DB_MODE == 'mysql':
        v_cols = [r[0] for r in c.execute('SHOW COLUMNS FROM videos').fetchall()]
    else:
        v_cols = [r[1] for r in c.execute('PRAGMA table_info(videos)').fetchall()]
    if 'data' not in v_cols:
        c.execute('ALTER TABLE videos ADD COLUMN data TEXT')
    if 'categoria' not in v_cols:
        c.execute('ALTER TABLE videos ADD COLUMN categoria TEXT DEFAULT \'adulto\'')
    if DB_MODE == 'postgres':
        cm_cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='chat_messages'").fetchall()]
    elif DB_MODE == 'mysql':
        cm_cols = [r[0] for r in c.execute('SHOW COLUMNS FROM chat_messages').fetchall()]
    else:
        cm_cols = [r[1] for r in c.execute('PRAGMA table_info(chat_messages)').fetchall()]
    if 'adjunto' not in cm_cols:
        c.execute('ALTER TABLE chat_messages ADD COLUMN adjunto TEXT')
    if 'adjunto_tipo' not in cm_cols:
        c.execute('ALTER TABLE chat_messages ADD COLUMN adjunto_tipo TEXT')
    if DB_MODE == 'postgres':
        n_cols = [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='notificaciones'").fetchall()]
    elif DB_MODE == 'mysql':
        n_cols = [r[0] for r in c.execute('SHOW COLUMNS FROM notificaciones').fetchall()]
    else:
        n_cols = [r[1] for r in c.execute('PRAGMA table_info(notificaciones)').fetchall()]
    if 'link' not in n_cols:
        c.execute('ALTER TABLE notificaciones ADD COLUMN link TEXT DEFAULT \'\'')
    defaults = {
        'academy_name': 'IKIGAI VIEDMA',
        'academy_code': 'BJJ2026',
        'default_cuota': '15000',
        'due_day': '10',
        'cargo_demora_pct': '10',
        'academy_color': '#e05d13',
        'auto_mensaje': '',
        'auto_inact_dias': '15',
        'auto_deuda_dias': '30',
        'auto_mensaje_activo': '0',
        'logro_asist': '50',
        'logro_videos': '25',
        'asis_min_examen': '30',
        'mp_access_token': '',
        'wp_numero': '',
        'public_url': '',
        'pago_link': PAGO_LINK_DEFAULT,
        'pago_alias': '',
    }
    for k, v in defaults.items():
        c.execute('INSERT OR IGNORE INTO settings(k, value) VALUES(?,?)', (k, v))
    # admin por defecto
    row = c.execute("SELECT id FROM users WHERE role='admin' LIMIT 1").fetchone()
    if not row:
        c.execute(
            "INSERT INTO users(username, password_hash, role, nombre) VALUES(?,?,?,?)",
            ('admin', generate_password_hash('admin123'), 'admin', 'Administrador'))
        print('=' * 60)
        print('  ADMIN CREADO ->  usuario: admin   contrasena: admin123')
        print('  CAMBIA LA CONTRASENA EN MI PERFIL cuando puedas.')
        print('=' * 60)
    db.commit()


def get_setting(key, default=None):
    row = get_db().execute('SELECT value FROM settings WHERE k=?', (key,)).fetchone()
    return row['value'] if row else default


def set_setting(key, value):
    get_db().execute(
        'INSERT OR REPLACE INTO settings(k, value) VALUES(?,?)',
        (key, str(value)))
    get_db().commit()


def _hoy_academy():
    """Fecha de 'hoy' en la zona horaria de la academia (setting tz_offset en horas,
    default -3 = Argentina). Así 'hoy' no cambia a las 21:00 por usar UTC."""
    off = to_float(get_setting('tz_offset', '-3')) or 0
    return (datetime.now(timezone.utc) + timedelta(hours=off)).date()


# ---------------------------------------------------------------------------
# VAPID keys para push
# ---------------------------------------------------------------------------

def _generar_vapid():
    """Genera un par de claves VAPID con cryptography: privada PKCS8 y publica SPKI."""
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives import serialization
    sk = ec.generate_private_key(ec.SECP256R1())
    priv = sk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub = sk.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv, pub


def _vapid_valida(priv, pub):
    """Devuelve True si la clave privada/publica se pueden cargar con cryptography."""
    if not priv or not pub:
        return False
    try:
        from cryptography.hazmat.primitives import serialization
        serialization.load_pem_private_key(priv.encode(), password=None)
        serialization.load_pem_public_key(pub.encode())
        return True
    except Exception:
        return False


def _vapid_reconstruida(priv, pub):
    """Re-serializa un PEM valido normalizado (PKCS8/SPKI) desde la clave cargada."""
    from cryptography.hazmat.primitives import serialization
    sk = serialization.load_pem_private_key(priv.encode(), password=None)
    priv2 = sk.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()).decode()
    pub2 = sk.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo).decode()
    return priv2, pub2


def ensure_vapid():
    """Claves VAPID persistentes en la BD (Render pierde archivos al redeployar)."""
    priv = get_setting('vapid_private')
    pub = get_setting('vapid_public')
    if not _vapid_valida(priv, pub):
        priv, pub = _generar_vapid()
        set_setting('vapid_private', priv)
        set_setting('vapid_public', pub)
    else:
        # normalizar el formato por si quedo raro en una version anterior
        try:
            priv, pub = _vapid_reconstruida(priv, pub)
            set_setting('vapid_private', priv)
            set_setting('vapid_public', pub)
        except Exception:
            pass
    # escribe archivos locales para pywebpush y compatibilidad
    for path, pem in ((VAPID_PRIVATE, priv), (VAPID_PUBLIC, pub)):
        try:
            with open(path, 'w') as f:
                f.write(pem)
        except OSError:
            pass
    # Para que el navegador pueda suscribirse, applicationServerKey debe ser el
    # "raw point" P-256 descomprimido (65 bytes, 04||X||Y), NO el DER/SPKI.
    try:
        from cryptography.hazmat.primitives import serialization
        pubkey = serialization.load_pem_public_key(pub.encode())
        raw_point = pubkey.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint)
        return base64.urlsafe_b64encode(raw_point).rstrip(b'=').decode()
    except Exception:
        pem = pub.replace('-----BEGIN PUBLIC KEY-----', '').replace('-----END PUBLIC KEY-----', '').strip()
        try:
            return base64.urlsafe_b64encode(base64.b64decode(pem)).rstrip(b'=').decode()
        except Exception:
            return pem


def send_push(user_id, titulo, mensaje, extra=None, _diag=None):
    """Envia notificacion push a todas las suscripciones del usuario.

    Devuelve la cantidad de notificaciones enviadas. Si se pasa _diag (dict),
    guarda en el el primer error encontrado para poder diagnosticar sin loguear.
    """
    try:
        ensure_vapid()
        # usamos el PEM privado directamente de la BD, no del archivo en disco
        # (Render usa filesystem efimero y el archivo puede faltar/perderse).
        priv_pem = get_setting('vapid_private')
        if not priv_pem:
            if _diag is not None:
                _diag['error'] = 'Falta la clave privada VAPID en la base'
            return 0
        # Normalizar la clave con cryptography y escribirla a un archivo temporal
        # limpio; pywebpush lee la ruta de forma confiable (sin error de formato).
        from cryptography.hazmat.primitives import serialization
        sk = serialization.load_pem_private_key(priv_pem.encode(), password=None)
        priv_pem = sk.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption()).decode()
        import tempfile, os
        tmpf = tempfile.NamedTemporaryFile('w', suffix='.pem', delete=False)
        tmpf.write(priv_pem)
        tmpf.close()
        try:
            from pywebpush import webpush, WebPushException
            subs = get_db().execute('SELECT id, endpoint, p256dh, auth FROM push_subs WHERE user_id=?',
                                    (user_id,)).fetchall()
            payload = json.dumps({'title': titulo, 'body': mensaje, **(extra or {})})
            enviados = 0
            for s in subs:
                try:
                    ep = s['endpoint']
                    # Apple: la Web Push de Safari/iOS usa web.push.apple.com (PWA instalada).
                    # Los endpoints /3/device/ (APNs nativo) son raros en un sitio web: igual los cubrimos.
                    vclaims = {'sub': 'mailto:admin@academia.local'}
                    extra_headers = {}
                    if 'web.push.apple.com' in ep:
                        vclaims['aud'] = 'https://web.push.apple.com'
                    elif '/3/device/' in ep and ('api.push.apple.com' in ep or 'api.sandbox.push.apple.com' in ep):
                        vclaims['aud'] = ep.split('/3/device/')[0]
                        extra_headers = {'apns-push-type': 'alert', 'apns-priority': '10'}
                    webpush(
                        subscription_info={
                            'endpoint': ep,
                            'keys': {'p256dh': s['p256dh'], 'auth': s['auth']}},
                        data=payload,
                        vapid_private_key=tmpf.name,
                        vapid_claims=vclaims,
                        headers=extra_headers or None)
                    enviados += 1
                except WebPushException as wp:
                    # Suscripciones vencidas/invalidas (410, 404, 403): borrarlas
                    status = getattr(wp, 'response', None)
                    code = status.status_code if status is not None else None
                    if _diag is not None and not _diag.get('error'):
                        host = '?'
                        try:
                            host = urlparse(ep).netloc
                        except Exception:
                            pass
                        _diag['error'] = 'WebPush HTTP %s a %s: %s' % (
                            code, host, getattr(wp, 'message', '') or wp)
                    if code in (404, 410, 403):
                        try:
                            get_db().execute('DELETE FROM push_subs WHERE id=?', (s['id'],))
                            get_db().commit()
                        except Exception:
                            pass
                except Exception as e:
                    if _diag is not None and not _diag.get('error'):
                        _diag['error'] = 'WebPush: %s' % e
            return enviados
        finally:
            try:
                os.remove(tmpf.name)
            except Exception:
                pass
    except Exception as e:
        if _diag is not None and not _diag.get('error'):
            _diag['error'] = 'send_push: %s' % e
        return 0


_NOTIF_LINK_POR_TIPO = {
    'cuota': 'mispagos',
    'pago': 'mispagos',
    'logro': 'perfil',
    'evento': 'eventos',
    'chat': 'chat',
    'auto': 'chat',
    'familia': 'perfil',
    'examen': 'perfil',
    'video': 'videos',
    'pausa': 'perfil',
    'plan': 'planes',
}


def notify(user_id, titulo, mensaje, tipo='info', push=True, link=None):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if link is None:
        link = _NOTIF_LINK_POR_TIPO.get(tipo, '')
    get_db().execute(
        'INSERT INTO notificaciones(user_id, titulo, mensaje, tipo, fecha, link) VALUES(?,?,?,?,?,?)',
        (user_id, titulo, mensaje, tipo, now, link))
    get_db().commit()
    if push:
        send_push(user_id, titulo, mensaje, extra={'url': '/app?sec=' + link if link else '/app'})


def aviso_cuotas_automatico():
    """Si pasó el día de vencimiento y no se avisó este mes, manda push a los deudores.
    Se llama en cada request (no hay cron en Render free); controla repetir con un flag."""
    try:
        hoy = _hoy_academy()
        flag = get_setting('aviso_cuota_%d_%d' % (hoy.year, hoy.month), '0')
        if flag == '1':
            return 0
        due_day = to_int(get_setting('due_day', '10')) or 10
        if hoy.day < due_day:
            return 0
        deudores = get_db().execute(
            """SELECT u.id, u.nombre FROM users u WHERE u.role='alumno' AND u.activo=1
               AND NOT EXISTS (SELECT 1 FROM pagos p WHERE p.alumno_id=u.id AND p.mes=? AND p.anio=?)""",
            (hoy.month, hoy.year)).fetchall()
        enviados = 0
        for d in deudores:
            if en_pausa(d):
                continue
            try:
                notify(d['id'], '💸 Recordatorio de cuota',
                       'Tu cuota de %d/%d está pendiente. Pagala cuando puedas.' % (hoy.month, hoy.year),
                       'cuota', push=True)
                enviados += 1
            except Exception:
                pass
        set_setting('aviso_cuota_%d_%d' % (hoy.year, hoy.month), '1')
        return enviados
    except Exception as e:
        import traceback as _tb
        print('AVISO_CUOTA_ERROR:', e)
        _tb.print_exc()
        return 0


def aviso_eventos_hoy():
    """Manda push recordando eventos que son mañana (a los que confirmaron asistencia).
    Se llama en cada request; evita repetir con la columna recordado."""
    try:
        manana = (_hoy_academy() + timedelta(days=1)).strftime('%Y-%m-%d')
        db = get_db()
        evs = db.execute('SELECT * FROM eventos WHERE fecha_evento=? AND (recordado IS NULL OR recordado=0)',
                         (manana,)).fetchall()
        enviados = 0
        for e in evs:
            asistentes = db.execute(
                'SELECT user_id FROM evento_asistencias WHERE evento_id=?', (e['id'],)).fetchall()
            if not asistentes:
                asistentes = db.execute(
                    'SELECT id AS user_id FROM users WHERE role IN (\'admin\',\'profesor\')').fetchall()
            for a in asistentes:
                try:
                    notify(a['user_id'], '📅 Recordatorio: %s' % (e['titulo'] or 'Evento'),
                           'Mañana %s%s — no te lo pierdas.' % (
                               e['fecha_evento'], ' a las ' + e['hora'] if e['hora'] else ''),
                           'evento', push=True)
                    enviados += 1
                except Exception:
                    pass
            db.execute('UPDATE eventos SET recordado=1 WHERE id=?', (e['id'],))
            db.commit()
        return enviados
    except Exception as e:
        import traceback as _tb
        print('AVISO_EVENTOS_ERROR:', e)
        _tb.print_exc()
        return 0


def aviso_renovacion():
    """Si un alumno llega al minimo de asistencias configurado (asis_min_examen),
    avisa SOLO al staff (admin/profesores) que puede sugerir examen. Flag por alumno
    evita repetir; se reset cuando el staff entrega el nuevo grado."""
    try:
        min_asist = to_int(get_setting('asis_min_examen', '30')) or 30
        if min_asist <= 0:
            return 0
        db = get_db()
        alumnos = db.execute(
            "SELECT u.id, u.nombre, u.cinturon, COUNT(a.id) AS n "
            "FROM users u LEFT JOIN asistencia a ON a.alumno_id=u.id "
            "WHERE u.role='alumno' AND u.activo=1 "
            # COUNT(*) contaba tambien la fila vacia del LEFT JOIN, por eso el
            # aviso salia con una asistencia menos (off-by-one). COUNT(a.id)
            # cuenta solo asistencias reales.
            "GROUP BY u.id HAVING COUNT(a.id) >= ?", (min_asist,)).fetchall()
        staff = db.execute("SELECT id FROM users WHERE role IN ('admin','profesor')").fetchall()
        if not staff:
            return 0
        enviados = 0
        for al in alumnos:
            key = 'avisado_examen_%d' % al['id']
            if get_setting(key, '0') == '1':
                continue
            for s in staff:
                try:
                    notify(s['id'], '🥋 Renovación de cinturón',
                           '%s ya tiene %d asistencias (cinturón %s). Está listo para rendir el próximo examen.' % (
                               al['nombre'], al['n'], al['cinturon'] or 'blanco'),
                           'logro', push=True, link='alumnos')
                    enviados += 1
                except Exception:
                    pass
            set_setting(key, '1')
        return enviados
    except Exception as e:
        import traceback as _tb
        print('AVISO_RENOVACION_ERROR:', e)
        _tb.print_exc()
        return 0


def chequear_logros(user_id):
    """Notifica cada vez que el alumno cruza un múltiplo del umbral configurable."""
    th = to_int(get_setting('logro_asist', '50')) or 50
    thv = to_int(get_setting('logro_videos', '25')) or 25
    db = get_db()
    asis = db.execute('SELECT COUNT(*) AS n FROM asistencia WHERE alumno_id=?',
                      (user_id,)).fetchone()['n']
    vids = db.execute('SELECT COUNT(*) AS n FROM video_views WHERE user_id=?',
                      (user_id,)).fetchone()['n']
    avisos = []
    if th > 0 and asis > 0 and asis % th == 0 and not _ya_logro(user_id, 'asis', asis):
        notify(user_id, '🎉 Logro alcanzado',
               '¡Llegaste a %d asistencias! Seguí así 🥋' % asis, 'logro', push=True)
        avisos.append('asistencias')
    if thv > 0 and vids > 0 and vids % thv == 0 and not _ya_logro(user_id, 'vids', vids):
        notify(user_id, '🎉 Logro alcanzado',
               '¡Viste %d videos! Buen progreso 🎥' % vids, 'logro', push=True)
        avisos.append('videos')
    return avisos


def _ya_logro(user_id, tipo, valor):
    key = 'logro_done_%d_%s_%d' % (user_id, tipo, valor)
    if get_setting(key, '0') == '1':
        return True
    set_setting(key, '1')
    return False


# ---------------------------------------------------------------------------
# Utilidades de sesion / auth
# ---------------------------------------------------------------------------

def current_user():
    if 'user_id' not in session:
        return None
    return get_db().execute('SELECT * FROM users WHERE id=?', (session['user_id'],)).fetchone()


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'No autorizado'}), 401
        return f(*args, **kwargs)
    return wrapper


def role_required(*roles):
    def deco(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({'error': 'No autorizado'}), 401
            u = current_user()
            if u['role'] not in roles:
                return jsonify({'error': 'Sin permisos'}), 403
            return f(*args, **kwargs)
        return wrapper
    return deco


def parse_json():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def as_bool(v, default=True):
    """Convierte a booleano tolerando el string "false" que llega de algunos
    clientes (HTML forms / fetch). bool('false') es True en Python y aplicaba
    recargos que el usuario creia desactivados."""
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(v)
    s = str(v).strip().lower()
    if s in ('false', '0', 'no', 'off', ''):
        return False
    return True


def validar_mes_anio(mes, anio):
    """Normaliza (mes, anio). Devuelve (mes, anio, error) para no romper pagos
    con meses imposibles (que antes reventaban con 500 al construir la fecha)."""
    hoy = _hoy_academy()
    try:
        m = int(mes)
        a = int(anio)
    except (TypeError, ValueError):
        m, a = hoy.month, hoy.year
    if not 1 <= m <= 12:
        return m, a, 'Mes inválido (debe ir de 1 a 12).'
    if not 2000 <= a <= hoy.year + 1:
        return m, a, 'Año inválido.'
    return m, a, None


def txt_str(v):
    """Equivale a (v or '').strip() pero sin reventar si v no es string.
    Antes, un cliente que mandaba un numero o un booleano en un campo de texto
    (por ejemplo firma_tyc: true)iba a .strip() sobre un int/bool -> HTTP 500."""
    if not v:
        return ''
    return str(v).strip()


def fecha_iso(valor, hoy=None):
    if valor is None or valor == '':
        return ((hoy or _hoy_academy()).strftime('%Y-%m-%d')), None
    if not isinstance(valor, str):
        return None, 'Fecha inválida'
    v = valor.strip()
    try:
        return datetime.strptime(v, '%Y-%m-%d').strftime('%Y-%m-%d'), None
    except ValueError:
        return None, 'Fecha inválida'


DATA_IMG_RE = re.compile(r'^data:image/(png|jpe?g|webp|gif);base64,[A-Za-z0-9+/=\s]+$', re.I)
DATA_PDF_RE = re.compile(r'^data:application/pdf;base64,[A-Za-z0-9+/=\s]+$', re.I)


def comprobante_valido(comp, max_bytes=12 * 1024 * 1024):
    """Valida que el comprobante sea una data-URL de imagen o PDF real.
    Antes solo se chequeaba el prefijo 'data:image/', lo que dejaba pasar SVG y
    payloads que al interpolarse en el HTML ejecutaban JS (XSS almacenado)."""
    if not comp or len(comp) > max_bytes:
        return False
    return bool(DATA_IMG_RE.match(comp) or DATA_PDF_RE.match(comp))


def imagen_valida(img, max_bytes=12 * 1024 * 1024):
    """Solo imagenes raster (png/jpg/webp/gif). Rechaza SVG, HTML y payloads XSS.
    Para galerias (Muro, Eventos) el PDF no tiene sentido y solo abria el riesgo."""
    if not img or not isinstance(img, str) or len(img) > max_bytes:
        return False
    return bool(DATA_IMG_RE.match(img))


def url_http_valida(u, max_len=600):
    """Solo URLs http/https absolutas. El link de pago se interpola en un href,
    asi que 'javascript:' o 'data:' ejecutarian codigo en la app del alumno."""
    if not u or not isinstance(u, str):
        return ''
    u = u.strip()
    if not u or len(u) > max_len:
        return ''
    try:
        p = urlparse(u)
    except ValueError:
        return ''
    if p.scheme not in ('http', 'https') or not p.netloc:
        return ''
    return u


def user_public(u):
    return {
        'id': u['id'],
        'username': u['username'],
        'role': u['role'],
        'nombre': u['nombre'],
        'edad': u['edad'],
        'peso': u['peso'],
        'cinturon': u['cinturon'],
        'categoria': u['categoria'],
        'gi_pref': u['gi_pref'],
        'cuota_mensual': u['cuota_mensual'],
        'foto': u['foto'] if 'foto' in u.keys() else None,
        'tel': u['tel'] if 'tel' in u.keys() else None,
        'nacimiento': u['nacimiento'] if 'nacimiento' in u.keys() else None,
        'medic_info': u['medic_info'] if 'medic_info' in u.keys() else None,
        'emergency_contact': u['emergency_contact'] if 'emergency_contact' in u.keys() else None,
        'medic_enfermedades': u['medic_enfermedades'] if 'medic_enfermedades' in u.keys() else None,
        'medic_alergias': u['medic_alergias'] if 'medic_alergias' in u.keys() else None,
        'medic_medicacion': u['medic_medicacion'] if 'medic_medicacion' in u.keys() else None,
        'medic_lesiones': u['medic_lesiones'] if 'medic_lesiones' in u.keys() else None,
        'ficha_fecha': u['ficha_fecha'] if 'ficha_fecha' in u.keys() else None,
        'firma_tyc': u['firma_tyc'] if 'firma_tyc' in u.keys() else None,
        'firma_foto': u['firma_foto'] if 'firma_foto' in u.keys() else None,
        'firma_fecha': u['firma_fecha'] if 'firma_fecha' in u.keys() else None,
        'security_q': u['security_q'] if 'security_q' in u.keys() else None,
        'tel_tutor': u['tel_tutor'] if 'tel_tutor' in u.keys() else None,
        'tel_2': u['tel_2'] if 'tel_2' in u.keys() else None,
        'direccion': u['direccion'] if 'direccion' in u.keys() else None,
        'dni': u['dni'] if 'dni' in u.keys() else None,
        'foto_ok': u['foto_ok'] if 'foto_ok' in u.keys() else None,
        'acepto_tyc': u['acepto_tyc'] if 'acepto_tyc' in u.keys() else None,
        'proximo_examen': u['proximo_examen'] if 'proximo_examen' in u.keys() else None,
        'activo': u['activo'],
        'creado': u['creado'],
        'pausa_desde': u['pausa_desde'] if 'pausa_desde' in u.keys() else None,
        'pausa_hasta': u['pausa_hasta'] if 'pausa_hasta' in u.keys() else None,
        'en_pausa': en_pausa(u) if 'pausa_desde' in u.keys() else False,
        'beca': u['beca'] if 'beca' in u.keys() else 0,
    }


STALE_SECONDS = int(os.environ.get('AVISOS_STALE', '600'))
_ultimo_aviso = [0]


def _correr_avisos_periodicos():
    # Corre fuera del request para que el usuario no espere mientras se
    # mandan los push (HTTP sincronico a Google/Apple por suscripcion).
    try:
        with app.app_context():
            aviso_cuotas_automatico()
            aviso_eventos_hoy()
            aviso_renovacion()
            # Si algun aviso fallo a mitad, su transaccion quedo abortada
            # (Postgres). Dejar la conexion sana para el resto del proceso.
            try:
                get_db().execute('ROLLBACK')
            except Exception:
                pass
    except Exception:
        pass


@app.before_request
def _avisos_periodicos():
    # Sin cron en Render free: cada ~10 min el primer request dispara los avisos
    # programados (recordatorio de cuota y eventos de mañana). Sin repetir gracias
    # a flags por mes/dia en settings y a la columna recordado de eventos.
    if request.path.startswith('/api/') and not request.path.startswith('/api/cron'):
        try:
            if _ultimo_aviso[0] == 0:
                _ultimo_aviso[0] = time.time() - STALE_SECONDS
            if time.time() - _ultimo_aviso[0] >= STALE_SECONDS:
                _ultimo_aviso[0] = time.time()
                threading.Thread(target=_correr_avisos_periodicos, daemon=True).start()
        except Exception:
            pass


def cuota_status(alumno):
    """Estado de la cuota del alumno en el mes actual."""
    hoy = _hoy_academy()
    pago = get_db().execute(
        'SELECT * FROM pagos WHERE alumno_id=? AND mes=? AND anio=? ORDER BY id DESC LIMIT 1',
        (alumno['id'], hoy.month, hoy.year)).fetchone()
    due_day = to_int(get_setting('due_day', '10')) or 10
    try:
        beca = int(alumno['beca'] or 0)
    except (KeyError, IndexError, TypeError):
        beca = 0
    if beca:
        return {
            'mes': hoy.month,
            'anio': hoy.year,
            'estado': 'becado',
            'pago': None,
            'cuota': 0,
            'due_day': due_day,
            'cargo_demora_pct': 0,
        }
    if alumno['role'] == 'profesor':
        return {
            'mes': hoy.month,
            'anio': hoy.year,
            'estado': 'profesor',
            'pago': None,
            'cuota': 0,
            'due_day': due_day,
            'cargo_demora_pct': 0,
        }
    estado = 'al_dia' if pago else 'deuda'
    if not pago and hoy.day <= due_day:
        estado = 'por_vencer'
    cuota = alumno['cuota_mensual']
    return {
        'mes': hoy.month,
        'anio': hoy.year,
        'estado': estado,
        'pago': dict(pago) if pago else None,
        'cuota': cuota,
        'due_day': due_day,
        'cargo_demora_pct': to_float(get_setting('cargo_demora_pct', '10')) or 0,
    }


def calcular_demora(monto, mes=None, anio=None, fecha=None):
    """Aplica el cargo por pago con demora (después del día de vencimiento).

    Devuelve (monto_base, cargo, monto_final). Sin cargo si no corresponde.
    """
    base = monto or 0
    if base <= 0:
        return base, 0, base
    hoy = fecha or _hoy_academy()
    due_day = to_int(get_setting('due_day', '10')) or 10
    pct = to_float(get_setting('cargo_demora_pct', '10')) or 0
    # Mes objetivo del pago (por defecto el mes actual)
    tmes = mes or hoy.month
    tanio = anio or hoy.year
    # Determinar el vencimiento: el día due_day del mes del pago
    try:
        venc = date(tanio, tmes, due_day)
    except ValueError:
        # si due_day no existe (ej 31 en feb) usar último día del mes
        import calendar
        last = calendar.monthrange(tanio, tmes)[1]
        venc = date(tanio, tmes, last)
    if hoy > venc and pct > 0:
        cargo = round(base * pct / 100)
        return base, cargo, base + cargo
    return base, 0, base


def en_pausa(alumno, fecha=None):
    """True si el alumno está dentro del rango de pausa temporal (inclusive)."""
    hoy = fecha or _hoy_academy()
    if isinstance(alumno, dict):
        get = lambda k: alumno.get(k)
    else:
        get = lambda k: alumno[k] if k in alumno.keys() else None
    desde = get('pausa_desde')
    hasta = get('pausa_hasta')
    if not desde or not hasta:
        return False
    try:
        d = datetime.strptime(str(desde)[:10], '%Y-%m-%d').date()
        h = datetime.strptime(str(hasta)[:10], '%Y-%m-%d').date()
    except Exception:
        return False
    return d <= hoy <= h


def dias_deuda(alumno):
    hoy = _hoy_academy()
    try:
        beca = int(alumno['beca'] or 0)
    except (KeyError, IndexError, TypeError):
        beca = 0
    if beca:
        return 0
    pago = get_db().execute(
        'SELECT fecha FROM pagos WHERE alumno_id=? ORDER BY fecha DESC LIMIT 1',
        (alumno['id'],)).fetchone()
    if pago and pago['fecha']:
        try:
            last = datetime.strptime(pago['fecha'][:10], '%Y-%m-%d').date()
            return (hoy - last).days
        except Exception:
            pass
    # si nunca pago
    creado = alumno['creado']
    if creado:
        try:
            last = datetime.strptime(creado[:10], '%Y-%m-%d').date()
            return (hoy - last).days
        except Exception:
            pass
    return 0


def dias_sin_entrenar(alumno):
    """Días desde la última asistencia marcada del alumno."""
    hoy = _hoy_academy()
    row = get_db().execute(
        "SELECT fecha FROM asistencia WHERE alumno_id=? AND presente=1 ORDER BY fecha DESC LIMIT 1",
        (alumno['id'],)).fetchone()
    if row and row['fecha']:
        try:
            last = datetime.strptime(row['fecha'], '%Y-%m-%d').date()
            return (hoy - last).days
        except Exception:
            pass
    # si nunca entrenó, usar fecha de creación
    if alumno['creado']:
        try:
            last = datetime.strptime(alumno['creado'][:10], '%Y-%m-%d').date()
            return (hoy - last).days
        except Exception:
            pass
    return 0


def run_auto_mensajes():
    """Dispara mensajes automáticos por inactividad y por deuda (una vez por alumno).

    Controla el envío con notificaciones tipo 'auto' para evitar duplicados.
    Devuelve lista de (alumno_nombre, motivo).
    """
    if not (get_setting('auto_mensaje_activo', '0') == '1'):
        return []
    texto = (get_setting('auto_mensaje', '') or '').strip()
    if not texto:
        return []
    inact_dias = to_int(get_setting('auto_inact_dias', '15')) or 15
    deuda_dias = to_int(get_setting('auto_deuda_dias', '30')) or 30
    alumnos = get_db().execute(
        "SELECT * FROM users WHERE role='alumno' AND activo=1").fetchall()
    enviados = []
    for a in alumnos:
        if en_pausa(a):
            continue
        motivo = None
        if dias_sin_entrenar(a) >= inact_dias:
            motivo = 'inactividad'
        elif dias_deuda(a) >= deuda_dias:
            motivo = 'deuda'
        if not motivo:
            continue
        # verificar que no se le haya enviado ya el mensaje automático (tipo 'auto')
        ya = get_db().execute(
            "SELECT 1 FROM notificaciones WHERE user_id=? AND tipo='auto' AND fecha LIKE ? LIMIT 1",
            (a['id'], _hoy_academy().strftime('%Y-%m-%d') + '%')).fetchone()
        if ya:
            continue
        notify(a['id'], '📣 Mensaje de la academia', texto, tipo='auto', push=True)
        enviados.append((a['nombre'], motivo))
    return enviados


@app.route('/api/mensajes/auto', methods=['POST'])
@role_required('admin', 'profesor')
def api_mensajes_auto():
    enviados = run_auto_mensajes()
    return jsonify({'ok': True, 'enviados': [{'nombre': n, 'motivo': m} for n, m in enviados]})


@app.route('/api/perfil/desactivar', methods=['POST'])
@login_required
def api_perfil_desactivar():
    u = current_user()
    if u['role'] != 'alumno':
        return jsonify({'error': 'Solo los alumnos pueden desactivar su cuenta'}), 403
    get_db().execute('UPDATE users SET activo=0 WHERE id=?', (u['id'],))
    get_db().commit()
    session.clear()
    return jsonify({'ok': True})

# ---------------------------------------------------------------------------
# Paginas
# ---------------------------------------------------------------------------

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('app_page'))
    return render_template('login.html', academy_name=get_setting('academy_name'))


@app.route('/sw.js')
def service_worker():
    return send_from_directory('static', 'sw.js', mimetype='application/javascript')


@app.route('/manifest.json')
def manifest():
    return send_from_directory('static', 'manifest.json', mimetype='application/manifest+json')


@app.route('/app')
def app_page():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    u = current_user()
    return render_template('dashboard.html', user=user_public(u), belts_adult=BELTS_ADULT,
                           belts_kids=BELTS_KIDS, belts_juveniles=BELTS_JUV, categorias=CATEGORIAS,
                           tipos_clase=TIPOS_CLASE, metodos=METODOS_PAGO,
                           dias=DIAS, academy_name=get_setting('academy_name'))


# =============================================================================
#  PAGINA DE PRESENTACION DE LA ACADEMIA  ->  /presentacion
# =============================================================================
#  ###############  DONDE PONER LAS FOTOS DE LOS PROFESORES  ###############
#  1) Copia cada foto dentro de la carpeta:  static/fotos/
#  2) Abajo, en FOTOS_PROFES, escribi el nombre del profe (como figura en la
#     app) y el nombre del archivo de la foto:
#
#         FOTOS_PROFES = {
#             'Juan Perez': 'juan.jpg',
#             'Maria Lopez': 'maria.png',
#         }
#
#  - Se puede escribir con o sin tildes/ñ (no importa).
#  - Si a un profe no lo pones, la pagina usa la foto de perfil que tenga
#    cargada en la app; y si no tiene ninguna, muestra sus iniciales.
#  - Tamaño recomendado de la foto: 600x600 px (cuadrada).
#  ###########################################################################
FOTOS_PROFES = {
    # 'Nombre Apellido': 'archivo.jpg',
}

#  ####################  DATOS DEL SITIO (web + presentacion)  ####################
#  Link de Instagram (aparece arriba, abajo y en contacto).
INSTAGRAM_URL = 'https://www.instagram.com/ikigai_viedma/'
INSTAGRAM_USUARIO = '@ikigai_viedma'
#  Direccion de la academia.
DIRECCION = 'Tucumán 149, Viedma, Río Negro, Argentina'
#  WhatsApp para consultas: solo numeros con prefijo internacional, sin + ni espacios.
#  Ej: '54292123456789'. Si queda vacio, el boton de WhatsApp NO se muestra.
WHATSAPP_NUMERO = ''
#  ###############################################################################


def _norm_txt(s):
    """Compara nombres sin tildes ni mayusculas (para las fotos de los profes)."""
    import unicodedata
    s = (s or '').lower().strip()
    return ''.join(ch for ch in unicodedata.normalize('NFD', s)
                   if unicodedata.category(ch) != 'Mn')


def _datos_web():
    """Datos publicos del sitio: profesores (con foto) y horarios de clases.

    Se leen de la base, asi que si en la app cambias un horario, agregas un
    profe o subis su foto de perfil, el sitio se actualiza solo.
    """
    from urllib.parse import quote
    db = get_db()
    fotos = {_norm_txt(k): v for k, v in FOTOS_PROFES.items() if v}
    profes = db.execute(
        "SELECT id, nombre, cinturon, foto FROM users "
        "WHERE role IN ('admin','profesor') AND activo=1 ORDER BY nombre").fetchall()
    clases = db.execute(
        """SELECT c.dia, c.hora, c.tipo, c.nivel, c.duracion, c.profesor_id,
                  u.nombre AS profesor_nombre
           FROM classes c LEFT JOIN users u ON u.id=c.profesor_id
           ORDER BY c.dia, c.hora""").fetchall()

    def item(c):
        return {'dia': c['dia'], 'dia_nombre': DIAS[c['dia']], 'hora': c['hora'],
                'tipo': c['tipo'], 'nivel': c['nivel'] or 'Todos',
                'duracion': c['duracion'], 'profesor': c['profesor_nombre'] or 'Sin asignar'}

    por_profes, por_dia = {}, {}
    for c in clases:
        por_dia.setdefault(c['dia'], []).append(item(c))
        if c['profesor_id']:
            por_profes.setdefault(c['profesor_id'], []).append(item(c))

    lista = []
    for p in profes:
        manual = (fotos.get(_norm_txt(p['nombre'])) or '').strip()
        lista.append({
            'nombre': p['nombre'],
            'cinturon': p['cinturon'] or '',
            'foto': ('/static/fotos/' + manual) if manual else (p['foto'] or ''),
            'foto_manual': bool(manual),
            'horarios': por_profes.get(p['id'], []),
            'iniciales': ''.join(w[0] for w in (p['nombre'] or '?').split()[:2]).upper(),
        })

    dias_horario = [{'dia': d, 'dia_nombre': DIAS[d], 'clases': por_dia.get(d, [])}
                    for d in range(7) if por_dia.get(d)]
    return {
        'profes': lista,
        'dias_horario': dias_horario,
        'total_clases': len(clases),
        'dias_con_clase': len(dias_horario),
        'tipos': sorted({c['tipo'] for c in clases if c['tipo']}),
        'niveles': sorted({(c['nivel'] or 'Todos') for c in clases}),
    }


@app.context_processor
def _web_global():
    """Datos comunes a todas las paginas del sitio."""
    from urllib.parse import quote
    wa = ''.join(ch for ch in (WHATSAPP_NUMERO or '') if ch.isdigit())
    return {
        'WEB_TITULO': 'IKIGAI - Ciencia y Arte del Conocimiento - Jiu Jitsu Viedma',
        'instagram_url': INSTAGRAM_URL,
        'instagram_usuario': INSTAGRAM_USUARIO,
        'direccion': DIRECCION,
        'maps_url': 'https://www.google.com/maps/search/?api=1&query=' + quote(DIRECCION),
        'whatsapp_url': ('https://wa.me/' + wa) if wa else '',
    }


@app.route('/presentacion')
def presentacion():
    d = _datos_web()
    return render_template(
        'presentacion.html',
        titulo='IKIGAI CIENCIA Y ARTE DEL CONOCIMIENTO - JIU JITSU VIEDMA',
        subtitulo='Ciencia y Arte del Conocimiento',
        profes=d['profes'],
        dias_horario=d['dias_horario'],
        instagram_url=INSTAGRAM_URL,
        instagram_usuario=INSTAGRAM_USUARIO,
    )


# =============================================================================
#  SITIO WEB DE LA ACADEMIA  ->  /web  (inicio, profesores, horarios, contacto)
# =============================================================================
@app.route('/web')
@app.route('/web/')
def web_inicio():
    d = _datos_web()
    return render_template('web_inicio.html', seccion='inicio', profes=d['profes'][:4],
                           dias_horario=d['dias_horario'], total_clases=d['total_clases'],
                           dias_con_clase=d['dias_con_clase'], tipos=d['tipos'])


@app.route('/web/profesores')
def web_profesores():
    d = _datos_web()
    return render_template('web_profesores.html', seccion='profesores', profes=d['profes'])


@app.route('/web/horarios')
def web_horarios():
    d = _datos_web()
    return render_template('web_horarios.html', seccion='horarios', dias_horario=d['dias_horario'],
                           total_clases=d['total_clases'], dias_con_clase=d['dias_con_clase'])


@app.route('/web/contacto')
def web_contacto():
    d = _datos_web()
    return render_template('web_contacto.html', seccion='contacto', dias_horario=d['dias_horario'],
                           total_clases=d['total_clases'])


@app.route('/recibo/<int:pid>')
@login_required
def recibo(pid):
    p = get_db().execute(
        """SELECT p.*, u.nombre AS alumno_nombre, pr.nombre AS profe_nombre
           FROM pagos p JOIN users u ON u.id=p.alumno_id
           LEFT JOIN users pr ON pr.id=p.profesor_id WHERE p.id=?""", (pid,)).fetchone()
    if not p:
        return render_template('error.html', message='Recibo no encontrado'), 404
    u = current_user()
    if u['role'] not in ('admin', 'profesor') and u['id'] != p['alumno_id']:
        return render_template('error.html', message='No tenés permiso para ver este recibo'), 403
    mes = to_int(p['mes'])
    meses = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio',
             'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
    return render_template('recibo.html', p=p,
                           mes_nombre=meses[mes - 1] if mes and 1 <= mes <= 12 else '',
                           academy=get_setting('academy_name'),
                           color=get_setting('academy_color') or '#e05d13')


@app.errorhandler(404)
def not_found(e):
    if request.path.startswith('/api/') or request.path.startswith('/static/'):
        return jsonify({'error': 'No encontrado'}), 404
    return redirect(url_for('index'))


@app.errorhandler(413)
def too_large(e):
    if request.path.startswith('/api/'):
        return jsonify({'error': 'El archivo es demasiado grande (máx %dMB). Para videos largos usá un link de YouTube.' % (MAX_VIDEO_BYTES // (1024 * 1024))}), 413
    return redirect(url_for('index'))


@app.errorhandler(500)
def server_error(e):
    import traceback as _tb
    print('SERVER_ERROR:', request.method, request.path)
    _tb.print_exc()
    if request.path.startswith('/api/'):
        return jsonify({'error': 'Error interno del servidor'}), 500
    return render_template('error.html', message='Error del servidor. Revisá que el archivo data.db no esté bloqueado o roto.'), 500


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route('/api/login', methods=['POST'])
def api_login():
    data = parse_json()
    username = txt_str(data.get('username'))
    password = data.get('password') or ''
    u = get_db().execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u or not check_password_hash(u['password_hash'], password):
        return jsonify({'error': 'Usuario o contrasena incorrectos'}), 401
    if not u['activo']:
        return jsonify({'error': 'Tu cuenta esta desactivada. Contacta al administrador.'}), 403
    session.clear()
    session['user_id'] = u['id']
    return jsonify({'ok': True, 'user': user_public(u)})


# ---------------------------------------------------------------------------
# Recuperacion de contrasena (pregunta de seguridad + reinicio por admin)
# ---------------------------------------------------------------------------

@app.route('/api/recuperar', methods=['POST'])
def api_recuperar():
    """Devuelve la pregunta de seguridad de un usuario (sin exponer la respuesta)."""
    data = parse_json()
    username = txt_str(data.get('username'))
    u = get_db().execute('SELECT id, security_q FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        return jsonify({'error': 'Ese usuario no existe'}), 404
    if not u['security_q']:
        return jsonify({'error': 'Ese usuario no configuró pregunta de seguridad. Pedile al profe/admin que reinicie tu contraseña.'}), 400
    return jsonify({'ok': True, 'pregunta': u['security_q']})


@app.route('/api/recuperar/verificar', methods=['POST'])
def api_recuperar_verificar():
    """Verifica la respuesta de seguridad y cambia la contrasena."""
    data = parse_json()
    username = txt_str(data.get('username'))
    resp = txt_str(data.get('respuesta'))
    nueva = data.get('nueva_password') or ''
    if len(nueva) < 4:
        return jsonify({'error': 'La nueva contrasena debe tener al menos 4 caracteres'}), 400
    u = get_db().execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
    if not u:
        return jsonify({'error': 'Ese usuario no existe'}), 404
    if not u['security_a'] or not u['security_q']:
        return jsonify({'error': 'Ese usuario no configuró pregunta de seguridad.'}), 400
    if resp.lower() != (u['security_a'] or '').strip().lower():
        return jsonify({'error': 'La respuesta no es correcta'}), 401
    get_db().execute('UPDATE users SET password_hash=? WHERE id=?',
                     (generate_password_hash(nueva), u['id']))
    get_db().commit()
    notify(1, 'Contraseña cambiada', 'Se cambio la contraseña de %s con la pregunta de seguridad' % u['nombre'])
    return jsonify({'ok': True})


@app.route('/api/perfil/seguridad', methods=['PUT'])
@login_required
def api_perfil_seguridad():
    u = current_user()
    data = parse_json()
    q = txt_str(data.get('pregunta'))
    a = txt_str(data.get('respuesta'))
    nueva = data.get('nueva_password') or ''
    if not q or not a:
        return jsonify({'error': 'Completa la pregunta y la respuesta'}), 400
    db = get_db()
    if nueva:
        if len(nueva) < 4:
            return jsonify({'error': 'La contrasena debe tener al menos 4 caracteres'}), 400
        db.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(nueva), u['id']))
    db.execute('UPDATE users SET security_q=?, security_a=? WHERE id=?', (q, a, u['id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/terminos/aceptar', methods=['POST'])
@login_required
def api_terminos_aceptar():
    """Registra que el usuario aceptó los Términos y Condiciones (fecha y hora)."""
    u = current_user()
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute('UPDATE users SET acepto_tyc=? WHERE id=?', (now, u['id']))
    db.commit()
    return jsonify({'ok': True, 'acepto_tyc': now})


@app.route('/api/usuarios/<int:uid>/password', methods=['POST'])
@role_required('admin', 'profesor')
def api_usuario_password(uid):
    data = parse_json()
    nueva = data.get('password') or ''
    if len(nueva) < 4:
        return jsonify({'error': 'La contrasena debe tener al menos 4 caracteres'}), 400
    db = get_db()
    u = db.execute('SELECT nombre FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    db.execute('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(nueva), uid))
    db.commit()
    notify(uid, 'Contraseña actualizada', 'Tu contraseña fue reiniciada por la academia. La próxima vez que entres, usá la nueva clave.')
    return jsonify({'ok': True})


@app.route('/api/register', methods=['POST'])
def api_register():
    data = parse_json()
    role = data.get('role')
    if role not in ('alumno', 'profesor'):
        return jsonify({'error': 'Rol invalido'}), 400
    username = txt_str(data.get('username'))
    password = data.get('password') or ''
    nombre = txt_str(data.get('nombre'))
    if not username or not password or not nombre:
        return jsonify({'error': 'Completa usuario, contrasena y nombre'}), 400
    if len(password) < 4:
        return jsonify({'error': 'La contrasena debe tener al menos 4 caracteres'}), 400
    if get_db().execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        return jsonify({'error': 'Ese usuario ya existe'}), 400

    nacimiento = txt_str(data.get('nacimiento'))
    if not nacimiento:
        return jsonify({'error': 'La fecha de nacimiento es obligatoria al crear tu perfil.'}), 400
    try:
        if datetime.strptime(nacimiento, '%Y-%m-%d').date() >= _hoy_academy():
            return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
    except ValueError:
        return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400

    if role == 'profesor':
        codigo = txt_str(data.get('codigo'))
        if codigo != get_setting('academy_code'):
            return jsonify({'error': 'Codigo de academia incorrecto. Pedile el codigo al administrador.'}), 400

    categoria = data.get('categoria') or 'adulto'
    # Sin este check, un categoria arbitraria se guardaba y rompia los filtros de
    # videos/cinturon y los selectores del frontend.
    if categoria not in CATEGORIAS:
        return jsonify({'error': 'Categoría inválida'}), 400
    tel_tutor = txt_str(data.get('tel_tutor'))
    if role == 'alumno' and categoria in ('kids', 'juveniles') and not tel_tutor:
        return jsonify({'error': 'Para menores (Kids/Juveniles) es obligatorio el telefono del padre, madre o tutor responsable.'}), 400
    foto_ok = 1 if data.get('foto_ok') else 0
    if role == 'alumno' and categoria in ('kids', 'juveniles') and not foto_ok:
        return jsonify({'error': 'Para menores (Kids/Juveniles) debe autorizar el mayor, padre, madre o tutor que las fotos del menor puedan exponerse.'}), 400
    tel_2 = txt_str(data.get('tel_2')) or None

    if not data.get('acepto_tyc'):
        return jsonify({'error': 'Debés aceptar los Términos y Condiciones para crear tu cuenta.'}), 400

    # str(): un booleano (firma_tyc: true) reventaba con 500 en .strip()
    firma_tyc = txt_str(data.get('firma_tyc'))
    firma_foto = txt_str(data.get('firma_foto'))
    menor = role == 'alumno' and categoria in ('kids', 'juveniles')
    if menor and not firma_tyc:
        return jsonify({'error': 'Firmá en el recuadro de Términos y Condiciones para crear tu cuenta.'}), 400
    if menor and not firma_foto:
        return jsonify({'error': 'Para menores (Kids/Juveniles) el padre, madre o tutor debe firmar la autorización de fotos.'}), 400
    firma_fecha = datetime.now().strftime('%d/%m/%Y %H:%M') if (firma_tyc or firma_foto) else None
    cuota_reg = to_float(data.get('cuota_mensual')) if role == 'alumno' else None

    try:
        get_db().execute(
            """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, cuota_mensual, tel, nacimiento, medic_info, emergency_contact, tel_tutor, tel_2, direccion, dni, foto_ok, acepto_tyc, firma_tyc, firma_foto, firma_fecha, creado)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, generate_password_hash(password), role, nombre,
             to_int(data.get('edad')), to_float(data.get('peso')),
             data.get('cinturon'), categoria,
             data.get('gi_pref') or 'Ambas',
             cuota_reg,
             txt_str(data.get('tel')) or None,
             txt_str(data.get('nacimiento')) or None,
             txt_str(data.get('medic_info')) or None,
             txt_str(data.get('emergency_contact')) or None,
             tel_tutor or None,
             tel_2,
             txt_str(data.get('direccion')) or None,
             txt_str(data.get('dni')) or None,
             foto_ok,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             firma_tyc or None,
             firma_foto or None,
             firma_fecha,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        get_db().commit()
    except dbadapter.IntegrityError:
        return jsonify({'error': 'Ese usuario ya existe'}), 400

    new_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    if role == 'alumno' and not cuota_reg:
        cuota = to_float(get_setting('default_cuota', '15000')) or 15000
        get_db().execute('UPDATE users SET cuota_mensual=? WHERE id=?', (cuota, new_id))
        get_db().commit()
    notify(1, 'Nuevo registro', f'Se registro un nuevo {role}: {nombre}')
    session.clear()
    session['user_id'] = new_id
    return jsonify({'ok': True, 'user': user_public(
        get_db().execute('SELECT * FROM users WHERE id=?', (new_id,)).fetchone())})


@app.route('/api/logout', methods=['POST'])
def api_logout():
    session.clear()
    return jsonify({'ok': True})


@app.route('/api/me')
@login_required
def api_me():
    u = current_user()
    d = user_public(u)
    if u['role'] == 'alumno':
        d['cuota'] = cuota_status(u)
    d['pago_link'] = url_http_valida(get_setting('pago_link', '')) or PAGO_LINK_DEFAULT
    d['pago_alias'] = get_setting('pago_alias', '')
    d['mp_habilitado'] = bool((get_setting('mp_access_token', '') or '').strip())
    d['wp_numero'] = get_setting('wp_numero', '')
    return jsonify(d)


# ---------------------------------------------------------------------------
# Horarios
# ---------------------------------------------------------------------------

@app.route('/api/horarios')
@login_required
def api_horarios():
    u = current_user()
    db = get_db()
    rows = db.execute(
        """SELECT c.*, u.nombre AS profesor_nombre
           FROM classes c LEFT JOIN users u ON u.id=c.profesor_id
           ORDER BY c.dia, c.hora""").fetchall()
    ratings = {}
    if u['role'] in ('admin', 'profesor'):
        for r in db.execute(
                'SELECT clase_id, COUNT(*) AS n, COALESCE(AVG(estrellas),0) AS prom FROM clase_valoraciones GROUP BY clase_id').fetchall():
            ratings[r['clase_id']] = {'n': r['n'], 'promedio': round(r['prom'] or 0, 1)}
    horarios = []
    for r in rows:
        item = {
            'id': r['id'], 'dia': r['dia'], 'dia_nombre': DIAS[r['dia']],
            'hora': r['hora'], 'tipo': r['tipo'], 'nivel': r['nivel'],
            'duracion': r['duracion'], 'profesor_id': r['profesor_id'],
            'profesor_nombre': r['profesor_nombre']}
        if ratings:
            item['rating'] = ratings.get(r['id'], {'n': 0, 'promedio': 0})
        horarios.append(item)
    return jsonify({'horarios': horarios})


@app.route('/api/horarios', methods=['POST'])
@role_required('admin', 'profesor')
def api_horarios_create():
    data = parse_json()
    dia = to_int(data.get('dia'))
    hora = txt_str(data.get('hora'))
    tipo = data.get('tipo') or 'Gi'
    if dia is None or dia not in range(7) or not hora:
        return jsonify({'error': 'Dia u hora invalidos'}), 400
    prof = to_int(data.get('profesor_id'))
    get_db().execute(
        'INSERT INTO classes(dia, hora, tipo, nivel, profesor_id, duracion) VALUES(?,?,?,?,?,?)',
        (dia, hora, tipo, data.get('nivel') or 'Todos', prof, to_int(data.get('duracion')) or 60))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/horarios/<int:cid>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_horarios_update(cid):
    data = parse_json()
    dia = to_int(data.get('dia'))
    hora = txt_str(data.get('hora'))
    if dia is None or dia not in range(7) or not hora:
        return jsonify({'error': 'Dia u hora invalidos'}), 400
    get_db().execute(
        'UPDATE classes SET dia=?, hora=?, tipo=?, nivel=?, profesor_id=?, duracion=? WHERE id=?',
        (dia, hora, data.get('tipo') or 'Gi', data.get('nivel') or 'Todos',
         to_int(data.get('profesor_id')), to_int(data.get('duracion')) or 60, cid))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/horarios/<int:cid>', methods=['DELETE'])
@role_required('admin')
def api_horarios_delete(cid):
    get_db().execute('DELETE FROM classes WHERE id=?', (cid,))
    get_db().commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Alumnos (admin / profesor)
# ---------------------------------------------------------------------------

@app.route('/api/alumnos')
@role_required('admin', 'profesor')
def api_alumnos():
    db = get_db()
    rows = db.execute(
        """SELECT u.*,
            (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS asistencias,
            (SELECT COUNT(*) FROM pagos p WHERE p.alumno_id=u.id) AS pagos_totales
           FROM users u WHERE u.role IN ('alumno','profesor') ORDER BY u.nombre""").fetchall()
    # mapa alumno -> familia (nombre, titular, relacion) y conteo de miembros
    fam_map = {}
    fam_count = {}
    for fm in db.execute(
        """SELECT fm.user_id, fm.relacion, f.id AS fam_id, f.nombre AS fam_nombre, f.titular_id
           FROM familia_miembros fm JOIN familias f ON f.id=fm.familia_id""").fetchall():
        fam_map[fm['user_id']] = fm
        fam_count[fm['fam_id']] = fam_count.get(fm['fam_id'], 0) + 1
    alumnos = []
    for r in rows:
        d = user_public(r)
        d['asistencias'] = r['asistencias']
        d['pagos_totales'] = r['pagos_totales']
        d['cuota'] = cuota_status(r)
        d['dias_deuda'] = dias_deuda(r)
        if 'notas_internas' in r.keys():
            d['notas_internas'] = r['notas_internas']
        if 'proximo_examen' in r.keys():
            d['proximo_examen'] = r['proximo_examen']
        fm = fam_map.get(r['id'])
        if fm:
            total = fam_count.get(fm['fam_id'], 1)
            base, desc, final = familia_cuota(dict(r, es_titular=(1 if fm['titular_id'] == r['id'] else 0)), total)
            d['familia'] = {'id': fm['fam_id'], 'nombre': fm['fam_nombre'],
                            'relacion': fm['relacion'], 'es_titular': bool(fm['titular_id'] == r['id']),
                            'cuota': base, 'descuento': desc, 'cuota_final': final}
        else:
            d['familia'] = None
        alumnos.append(d)
    return jsonify({'alumnos': alumnos})


@app.route('/api/alumnos', methods=['POST'])
@role_required('admin', 'profesor')
def api_alumnos_create():
    data = parse_json()
    nombre = txt_str(data.get('nombre'))
    if not nombre:
        return jsonify({'error': 'El nombre es obligatorio'}), 400
    username = txt_str(data.get('username')) or f"alumno{secrets.token_hex(3)}"
    if get_db().execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        return jsonify({'error': 'Ese usuario ya existe'}), 400
    password = data.get('password') or 'alumno123'
    cuota = to_float(data.get('cuota_mensual')) or (to_float(get_setting('default_cuota', '15000')) or 15000)
    nacimiento = txt_str(data.get('nacimiento'))
    if not nacimiento:
        return jsonify({'error': 'La fecha de nacimiento es obligatoria al crear el perfil.'}), 400
    try:
        if datetime.strptime(nacimiento, '%Y-%m-%d').date() >= _hoy_academy():
            return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
    except ValueError:
        return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400
    get_db().execute(
        """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, cuota_mensual, nacimiento, creado)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
        (username, generate_password_hash(password), 'alumno', nombre,
         to_int(data.get('edad')), to_float(data.get('peso')),
         data.get('cinturon'), data.get('categoria') or 'adulto',
         data.get('gi_pref') or 'Ambas', cuota, nacimiento,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    new_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    return jsonify({'ok': True, 'id': new_id, 'username': username, 'password': password})


@app.route('/api/alumnos/<int:uid>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumnos_update(uid):
    data = parse_json()
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    nac_upd = txt_str(data.get('nacimiento', u['nacimiento']))
    if nac_upd:
        try:
            if datetime.strptime(nac_upd, '%Y-%m-%d').date() >= _hoy_academy():
                return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
        except ValueError:
            return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400
    get_db().execute(
        """UPDATE users SET nombre=?, edad=?, peso=?, cinturon=?, categoria=?, gi_pref=?, activo=?, tel=?, nacimiento=?, medic_info=?, emergency_contact=?, tel_tutor=?, tel_2=?, direccion=?, dni=?, foto_ok=?, pausa_desde=?, pausa_hasta=? WHERE id=?""",
        ((data.get('nombre') or u['nombre']), to_int(data.get('edad', u['edad'])),
         to_float(data.get('peso', u['peso'])), data.get('cinturon', u['cinturon']),
         data.get('categoria', u['categoria']), data.get('gi_pref', u['gi_pref']),
         1 if data.get('activo', u['activo']) else 0,
         txt_str(data.get('tel', u['tel'])) or None,
         nac_upd or None,
         data.get('medic_info', u['medic_info']),
         data.get('emergency_contact', u['emergency_contact']),
         txt_str(data.get('tel_tutor', u['tel_tutor'])) or None,
         txt_str(data.get('tel_2', u['tel_2'])) or None,
         txt_str(data.get('direccion', u['direccion'])) or None,
         txt_str(data.get('dni', u['dni'])) or None,
         1 if data.get('foto_ok', u['foto_ok']) else 0,
         txt_str(data.get('pausa_desde', u['pausa_desde'])) or None,
         txt_str(data.get('pausa_hasta', u['pausa_hasta'])) or None, uid))
    get_db().commit()
    nuevo_pausa = bool(txt_str(data.get('pausa_desde', u['pausa_desde'])))
    if nuevo_pausa and not en_pausa(u) and en_pausa(get_db().execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()):
        notify(uid, '⏸ Pausa temporal',
               'Registramos tu pausa. Mientras estés de pausa no se te cobra ni contás como deudor.',
               'pausa', push=True, link='perfil')
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_alumnos_delete(uid):
    get_db().execute('DELETE FROM users WHERE id=? AND role="alumno"', (uid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>/cuota', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumnos_cuota(uid):
    data = parse_json()
    cuota = to_float(data.get('cuota_mensual'))
    if not cuota or cuota <= 0:
        return jsonify({'error': 'Monto de cuota invalido'}), 400
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    get_db().execute('UPDATE users SET cuota_mensual=? WHERE id=?', (cuota, uid))
    get_db().commit()
    who = current_user()['nombre']
    notify(uid, 'Tu cuota cambio',
           f'{who} actualizo tu cuota mensual a ${cuota:,.0f}'.replace(',', '.'), 'cuota')
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Profesores
# ---------------------------------------------------------------------------

@app.route('/api/profesores')
@role_required('admin', 'profesor')
def api_profesores():
    rows = get_db().execute(
        """SELECT u.*,
            (SELECT COUNT(*) FROM classes c WHERE c.profesor_id=u.id) AS clases
           FROM users u WHERE u.role='profesor' ORDER BY u.nombre""").fetchall()
    return jsonify({'profesores': [dict(user_public(r), **{'clases': r['clases']}) for r in rows]})


@app.route('/api/profesores', methods=['POST'])
@role_required('admin')
def api_profesores_create():
    data = parse_json()
    nombre = txt_str(data.get('nombre'))
    if not nombre:
        return jsonify({'error': 'El nombre es obligatorio'}), 400
    username = txt_str(data.get('username')) or f"profe{secrets.token_hex(3)}"
    if get_db().execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        return jsonify({'error': 'Ese usuario ya existe'}), 400
    password = data.get('password') or 'profe123'
    get_db().execute(
        """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, creado)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (username, generate_password_hash(password), 'profesor', nombre,
         to_int(data.get('edad')), to_float(data.get('peso')),
         data.get('cinturon'), data.get('categoria') or 'adulto',
         data.get('gi_pref') or 'Ambas',
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    new_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    return jsonify({'ok': True, 'id': new_id, 'username': username, 'password': password})


@app.route('/api/profesores/<int:uid>', methods=['DELETE'])
@role_required('admin')
def api_profesores_delete(uid):
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="profesor"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Profesor no encontrado'}), 404
    admin = get_db().execute('SELECT id FROM users WHERE role="admin" LIMIT 1').fetchone()
    # quita las clases del profesor
    get_db().execute('UPDATE classes SET profesor_id=NULL WHERE profesor_id=?', (uid,))
    # conserva pagos: profesor_id queda con SET NULL
    get_db().execute('DELETE FROM users WHERE id=?', (uid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>/profesor', methods=['POST'])
@role_required('admin')
def api_alumnos_promover(uid):
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    get_db().execute(
        """UPDATE users SET role='profesor', activo=1,
            categoria=COALESCE(NULLIF(TRIM(categoria), ''), 'adulto'),
            gi_pref=COALESCE(NULLIF(TRIM(gi_pref), ''), 'Ambas')
           WHERE id=?""", (uid,))
    get_db().commit()
    return jsonify({'ok': True, 'nombre': u['nombre']})


@app.route('/api/alumnos/<int:uid>/beca', methods=['POST'])
@role_required('admin', 'profesor')
def api_alumnos_beca(uid):
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    nueva = 0 if (int(u['beca']) if 'beca' in u.keys() else 0) else 1
    get_db().execute('UPDATE users SET beca=? WHERE id=?', (nueva, uid))
    get_db().commit()
    return jsonify({'ok': True, 'beca': nueva, 'nombre': u['nombre']})


@app.route('/api/alumnos/<int:uid>/notas', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumno_notas(uid):
    data = parse_json()
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    notas = txt_str(data.get('notas'))
    get_db().execute('UPDATE users SET notas_internas=? WHERE id=?', (notas, uid))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>/ficha', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumno_ficha(uid):
    """El staff puede cargar/editar la ficha medica de un alumno (menores suelen no hacerlo solos)."""
    data = parse_json()
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    db = get_db()
    db.execute(
        'UPDATE users SET medic_info=?, emergency_contact=?, medic_enfermedades=?, medic_alergias=?, medic_medicacion=?, medic_lesiones=?, ficha_fecha=? WHERE id=?',
        (txt_str(data.get('medic_info')) or None,
         txt_str(data.get('emergency_contact')) or None,
         txt_str(data.get('medic_enfermedades')) or None,
         txt_str(data.get('medic_alergias')) or None,
         txt_str(data.get('medic_medicacion')) or None,
         txt_str(data.get('medic_lesiones')) or None,
         data.get('ficha_fecha') or datetime.now().strftime('%d/%m/%Y'),
         uid))
    db.commit()
    try:
        notify(uid, '🩺 Ficha médica', 'El profesor actualizó tu ficha médica. Revisala en tu perfil.', 'info', push=False)
    except Exception:
        pass
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Familias (grupos familiares)
# ---------------------------------------------------------------------------

def _descuento_familiar_pct(total_miembros):
    """Porcentaje de descuento familiar segun la cantidad de integrantes:
    2 -> desc_familiar2, 3 -> desc_familiar3, 4 o mas -> desc_familiar4."""
    d2 = to_float(get_setting('desc_familiar2', '10')) or 0
    d3 = to_float(get_setting('desc_familiar3', '15')) or 0
    d4 = to_float(get_setting('desc_familiar4', '20')) or 0
    if total_miembros >= 4:
        return d4
    if total_miembros == 3:
        return d3
    if total_miembros == 2:
        return d2
    return 0


def _escala_descuento():
    """Escala de descuentos a mostrar en la UI (2, 3, 4 o mas integrantes)."""
    return [
        {'integrantes': 2, 'pct': to_float(get_setting('desc_familiar2', '10')) or 0},
        {'integrantes': 3, 'pct': to_float(get_setting('desc_familiar3', '15')) or 0},
        {'integrantes': 4, 'pct': to_float(get_setting('desc_familiar4', '20')) or 0},
    ]


def familia_cuota(miembro, total_miembros):
    """Cuota de un miembro aplicando el descuento familiar a TODOS los integrantes.
    El porcentaje depende de cuantos integrantes tiene el grupo (2, 3, 4+)."""
    pct = _descuento_familiar_pct(total_miembros)
    base = miembro.get('cuota_mensual') or 0
    desc = 0
    cuota_final = base
    if pct > 0:
        desc = round(base * pct / 100)
        cuota_final = base - desc
    return base, desc, cuota_final


def familia_de(user_id):
    """Devuelve (familia_id, nombre, titular_id) del usuario o (None,None,None)."""
    row = get_db().execute(
        """SELECT f.id, f.nombre, f.titular_id FROM familia_miembros fm
           JOIN familias f ON f.id=fm.familia_id WHERE fm.user_id=?
           LIMIT 1""", (user_id,)).fetchone()
    if not row:
        return None, None, None
    return row['id'], row['nombre'], row['titular_id']


@app.route('/api/familias')
@role_required('admin', 'profesor')
def api_familias():
    db = get_db()
    rows = db.execute('SELECT * FROM familias ORDER BY nombre').fetchall()
    familias = []
    for f in rows:
        miem = db.execute(
            """SELECT u.*, fm.relacion,
                      (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular
               FROM familia_miembros fm
               JOIN users u ON u.id=fm.user_id
               JOIN familias f ON f.id=fm.familia_id
               WHERE fm.familia_id=? ORDER BY es_titular DESC, u.nombre""",
            (f['id'],)).fetchall()
        lista = []
        total = 0
        for m in miem:
            base, desc, final = familia_cuota(dict(m), len(miem))
            total += final
            lista.append({'id': m['id'], 'nombre': m['nombre'], 'cinturon': m['cinturon'],
                          'foto': m['foto'], 'relacion': m['relacion'],
                          'es_titular': bool(m['es_titular']), 'cuota': base,
                          'descuento': desc, 'cuota_final': final})
        familias.append({'id': f['id'], 'nombre': f['nombre'], 'titular_id': f['titular_id'],
                         'fecha': f['fecha'], 'miembros': lista, 'total': round(total, 2)})
    return jsonify({'familias': familias})


@app.route('/api/familias', methods=['POST'])
@role_required('admin', 'profesor')
def api_familia_crear():
    data = parse_json()
    nombre = txt_str(data.get('nombre'))
    if not nombre:
        return jsonify({'error': 'Poné un nombre al grupo familiar'}), 400
    titular_id = to_int(data.get('titular_id'))
    db = get_db()
    cur = db.execute('INSERT INTO familias(nombre, fecha) VALUES(?,?)',
                     (nombre, datetime.now().strftime('%Y-%m-%d %H:%M')))
    fam_id = cur.lastrowid
    if titular_id:
        u = db.execute('SELECT * FROM users WHERE id=? AND role="alumno"', (titular_id,)).fetchone()
        if u:
            db.execute('INSERT INTO familia_miembros(familia_id, user_id, relacion) VALUES(?,?,?)',
                       (fam_id, titular_id, 'Titular'))
            db.execute('UPDATE familias SET titular_id=? WHERE id=?', (titular_id, fam_id))
    db.commit()
    if titular_id:
        try:
            notify(titular_id, '👨‍👩‍👧 Familia', 'Te agregamos al grupo familiar "%s".' % nombre, 'info', push=True)
        except Exception:
            pass
    return jsonify({'ok': True, 'id': fam_id})


@app.route('/api/familias/<int:fam_id>/miembros', methods=['POST'])
@role_required('admin', 'profesor')
def api_familia_agregar(fam_id):
    data = parse_json()
    uid = to_int(data.get('user_id'))
    if not uid:
        return jsonify({'error': 'Falta el alumno'}), 400
    relacion = (data.get('relacion') or 'Familiar').strip() or 'Familiar'
    db = get_db()
    f = db.execute('SELECT * FROM familias WHERE id=?', (fam_id,)).fetchone()
    if not f:
        return jsonify({'error': 'Grupo no encontrado'}), 404
    u = db.execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    # si el alumno ya está en otra familia, se la cambia
    db.execute('DELETE FROM familia_miembros WHERE user_id=?', (uid,))
    db.execute('INSERT INTO familia_miembros(familia_id, user_id, relacion) VALUES(?,?,?)',
               (fam_id, uid, relacion))
    if f['titular_id'] is None:
        db.execute('UPDATE familias SET titular_id=? WHERE id=? AND titular_id IS NULL', (uid, fam_id))
    db.commit()
    try:
        notify(uid, '👨‍👩‍👧 Familia', 'Te incorporamos al grupo familiar "%s".' % f['nombre'], 'info', push=True)
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/familias/<int:fam_id>/miembros/<int:uid>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_familia_quitar(fam_id, uid):
    db = get_db()
    db.execute('DELETE FROM familia_miembros WHERE familia_id=? AND user_id=?', (fam_id, uid))
    # si era titular, pasar a otro miembro o dejar sin titular
    f = db.execute('SELECT * FROM familias WHERE id=?', (fam_id,)).fetchone()
    if f and f['titular_id'] == uid:
        resto = db.execute('SELECT user_id FROM familia_miembros WHERE familia_id=? LIMIT 1', (fam_id,)).fetchone()
        db.execute('UPDATE familias SET titular_id=? WHERE id=?', (resto['user_id'] if resto else None, fam_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/familias/<int:fam_id>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_familia_borrar(fam_id):
    db = get_db()
    db.execute('DELETE FROM familias WHERE id=?', (fam_id,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/familias/<int:fam_id>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_familia_editar(fam_id):
    data = parse_json()
    nombre = txt_str(data.get('nombre'))
    db = get_db()
    if not db.execute('SELECT id FROM familias WHERE id=?', (fam_id,)).fetchone():
        return jsonify({'error': 'Grupo no encontrado'}), 404
    if nombre:
        db.execute('UPDATE familias SET nombre=? WHERE id=?', (nombre, fam_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/mi_familia')
@login_required
def api_mi_familia():
    u = current_user()
    fam_id, nombre, titular_id = familia_de(u['id'])
    db = get_db()
    if not fam_id:
        return jsonify({'familia': None, 'descuento': 0, 'escala': _escala_descuento()})
    miem = db.execute(
        """SELECT u.*, fm.relacion,
                  (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular
           FROM familia_miembros fm
           JOIN users u ON u.id=fm.user_id
           JOIN familias f ON f.id=fm.familia_id
           WHERE fm.familia_id=? ORDER BY es_titular DESC, u.nombre""", (fam_id,)).fetchall()
    lista = []
    for m in miem:
        base, desc, final = familia_cuota(dict(m), len(miem))
        lista.append({'id': m['id'], 'nombre': m['nombre'], 'cinturon': m['cinturon'],
                      'foto': m['foto'], 'relacion': m['relacion'],
                      'es_titular': bool(m['es_titular']), 'cuota': base,
                      'descuento': desc, 'cuota_final': final})
    descto = _descuento_familiar_pct(len(miem))
    return jsonify({'familia': {'id': fam_id, 'nombre': nombre, 'titular_id': titular_id,
                                'miembros': lista},
                    'descuento': descto, 'escala': _escala_descuento()})


# ---------------------------------------------------------------------------
# Control parental self-service: modo Padre / tutora del menor
# ---------------------------------------------------------------------------

def _mi_familia_resumen():
    """Familia propia (solo si soy el titular) con detalle de los hijos/as vinculados."""
    u = current_user()
    fam_id, nombre, titular_id = familia_de(u['id'])
    if not fam_id or titular_id != u['id']:
        return None, []
    db = get_db()
    hijos = []
    for r in db.execute(
            """SELECT ux.*, fm.relacion FROM familia_miembros fm
               JOIN users ux ON ux.id=fm.user_id
               WHERE fm.familia_id=? AND fm.user_id<>? AND fm.relacion='hijo/a'
               ORDER BY ux.nombre""", (fam_id, u['id'])).fetchall():
        d = user_public(dict(r))
        d['relacion'] = r['relacion'] or 'hijo/a'
        if r['role'] == 'alumno':
            d['cuota'] = cuota_status(dict(r))
            d['asistencias'] = db.execute(
                'SELECT COUNT(*) AS n FROM asistencia WHERE alumno_id=? AND presente=1', (r['id'],)).fetchone()['n']
            d['ultima_fecha'] = db.execute(
                'SELECT MAX(fecha) AS f FROM asistencia WHERE alumno_id=? AND presente=1', (r['id'],)).fetchone()['f']
        hijos.append(d)
    return {'id': fam_id, 'nombre': nombre}, hijos


def _crear_grupo_familiar_si_hace_falta():
    """Si el usuario no pertenece a ninguna familia, crea una con él/ella como titular."""
    u = current_user()
    if familia_de(u['id'])[0]:
        return familia_de(u['id'])[0]
    db = get_db()
    cur = db.execute('INSERT INTO familias(nombre, titular_id, fecha) VALUES(?,?,?)',
                     (u['nombre'] + ' y familia', u['id'],
                      datetime.now().strftime('%Y-%m-%d %H:%M')))
    fam_id = cur.lastrowid
    db.execute('INSERT INTO familia_miembros(familia_id, user_id, relacion) VALUES(?,?,?)',
               (fam_id, u['id'], 'titular'))
    db.commit()
    return fam_id


@app.route('/api/mis_hijos')
@role_required('alumno')
def api_mis_hijos():
    fam, hijos = _mi_familia_resumen()
    return jsonify({'familia': fam, 'hijos': hijos})


@app.route('/api/familia', methods=['POST'])
@role_required('alumno')
def api_familia_activar():
    """Activa el 'modo Padre': me vuelvo titular de un grupo familiar."""
    _crear_grupo_familiar_si_hace_falta()
    fam, hijos = _mi_familia_resumen()
    return jsonify({'ok': True, 'familia': fam, 'hijos': hijos})


@app.route('/api/familia/hijos', methods=['POST'])
@role_required('alumno')
def api_familia_hijo_alta():
    """Alta de una cuenta de menor (Kids/Juveniles) creada desde el perfil del padre."""
    u = current_user()
    data = parse_json()
    username = txt_str(data.get('username'))
    password = data.get('password') or ''
    nombre = txt_str(data.get('nombre'))
    if not username or not password or not nombre:
        return jsonify({'error': 'Completa usuario, contrasena, nombre del menor y contrasena'}), 400
    if len(password) < 4:
        return jsonify({'error': 'La contrasena debe tener al menos 4 caracteres'}), 400
    if get_db().execute('SELECT id FROM users WHERE username=?', (username,)).fetchone():
        return jsonify({'error': 'Ese usuario ya existe. Si es la cuenta de tu hijo/a, usa "Vincular cuenta".'}), 400

    nacimiento = txt_str(data.get('nacimiento'))
    if not nacimiento:
        return jsonify({'error': 'La fecha de nacimiento es obligatoria al crear el perfil del menor.'}), 400
    try:
        if datetime.strptime(nacimiento, '%Y-%m-%d').date() >= _hoy_academy():
            return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
    except ValueError:
        return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400

    categoria = data.get('categoria') or 'kids'
    if categoria not in ('kids', 'juveniles'):
        return jsonify({'error': 'Solo se pueden dar de alta menores (Kids/Juveniles) desde el perfil de un padre'}), 400
    tel_tutor = txt_str(data.get('tel_tutor')) or u['tel'] or u['tel_2'] or ''
    if not tel_tutor:
        return jsonify({'error': 'Cargá primero tu telefono en tu perfil para poder ser el tutor responsable.'}), 400
    if not data.get('foto_ok'):
        return jsonify({'error': 'Para menores (Kids/Juveniles) debe autorizar el mayor, padre, madre o tutor que las fotos del menor puedan exponerse.'}), 400
    if not data.get('firma_tyc'):
        return jsonify({'error': 'Firmá en los Términos y Condiciones para crear la cuenta del menor.'}), 400
    if not data.get('firma_foto'):
        return jsonify({'error': 'Para menores, el padre, madre o tutor debe firmar la autorización de fotos.'}), 400
    firma_fecha = datetime.now().strftime('%d/%m/%Y %H:%M')

    fam_id = _crear_grupo_familiar_si_hace_falta()
    cuota_menor = to_float(get_setting('default_cuota', '15000')) or 15000
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, cuota_mensual, tel, nacimiento, medic_info, emergency_contact, tel_tutor, tel_2, direccion, dni, foto_ok, acepto_tyc, firma_tyc, firma_foto, firma_fecha, creado)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, generate_password_hash(password), 'alumno', nombre,
             to_int(data.get('edad')), to_float(data.get('peso')),
             data.get('cinturon') or 'Blanco', categoria,
             data.get('gi_pref') or 'Ambas', cuota_menor,
             None, nacimiento,
             txt_str(data.get('medic_info')) or None,
             txt_str(data.get('emergency_contact')) or None,
             tel_tutor, None, None, txt_str(data.get('dni')) or None,
             1, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             data.get('firma_tyc'), data.get('firma_foto'), firma_fecha,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        db.commit()
        new_id = cur.lastrowid
    except dbadapter.IntegrityError:
        return jsonify({'error': 'Ese usuario ya existe'}), 400
    db.execute('INSERT OR IGNORE INTO familia_miembros(familia_id, user_id, relacion) VALUES(?,?,?)',
               (fam_id, new_id, 'hijo/a'))
    db.commit()
    return jsonify({'ok': True, 'id': new_id})


@app.route('/api/familia/vincular', methods=['POST'])
@role_required('alumno')
def api_familia_vincular():
    """El padre vincula la cuenta YA CREADA de su hijo/a menor, validando el tel_tutor."""
    u = current_user()
    data = parse_json()
    username = txt_str(data.get('username'))
    if not username:
        return jsonify({'error': 'Ingresá el usuario de la cuenta del menor'}), 400
    db = get_db()
    h = db.execute('SELECT * FROM users WHERE username=? AND role=?', (username, 'alumno')).fetchone()
    if not h:
        return jsonify({'error': 'No existe un alumno con ese usuario'}), 404
    if h['id'] == u['id']:
        return jsonify({'error': 'Esa cuenta es tuya'}), 400
    if h['categoria'] not in ('kids', 'juveniles'):
        return jsonify({'error': 'Solo se pueden vincular cuentas de menores (Kids/Juveniles)'}), 400
    mis_tels = {t for t in (u['tel'], u['tel_2']) if t}
    if h['tel_tutor'] and h['tel_tutor'] not in mis_tels:
        return jsonify({'error': 'Esa cuenta está a nombre de otro tutor. Pedile al profe/admin que la vincule.'}), 403
    fam_id, _, titular_id = familia_de(u['id'])
    if fam_id and titular_id != u['id']:
        return jsonify({'error': 'Sos miembro de la familia de ' + ('otra persona') + '. Pedile al titular que agregue a tu hijo.'}), 403
    if not fam_id:
        fam_id = _crear_grupo_familiar_si_hace_falta()
    try:
        db.execute('INSERT INTO familia_miembros(familia_id, user_id, relacion) VALUES(?,?,?)',
                   (fam_id, h['id'], 'hijo/a'))
        db.commit()
    except dbadapter.IntegrityError:
        return jsonify({'error': 'Ese alumno ya está vinculado a tu grupo familiar'}), 400
    return jsonify({'ok': True, 'id': h['id']})


@app.route('/api/familia/hijos/<int:uid>', methods=['DELETE'])
@role_required('alumno')
def api_familia_hijo_quitar(uid):
    u = current_user()
    db = get_db()
    fam_id, _, titular_id = familia_de(u['id'])
    if not fam_id or titular_id != u['id']:
        return jsonify({'error': 'No sos el titular del grupo familiar'}), 403
    if uid == u['id']:
        return jsonify({'error': 'No podés desvincularte solo de tu grupo'}), 400
    ex = db.execute(
        'SELECT 1 FROM familia_miembros WHERE familia_id=? AND user_id=? AND relacion=\'hijo/a\'',
        (fam_id, uid)).fetchone()
    if not ex:
        return jsonify({'error': 'Ese alumno no está vinculado como hijo/a'}), 404
    db.execute('DELETE FROM familia_miembros WHERE familia_id=? AND user_id=?', (fam_id, uid))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Diario de la academia
# ---------------------------------------------------------------------------

@app.route('/api/diario')
@login_required
def api_diario():
    rows = get_db().execute(
        """SELECT d.*, u.nombre AS autor_nombre, u.foto AS autor_foto
           FROM diario d JOIN users u ON u.id=d.user_id
           ORDER BY d.fecha DESC, d.id DESC LIMIT 120""").fetchall()
    return jsonify({'diario': [dict(r) for r in rows],
                    'hoy': _hoy_academy().strftime('%Y-%m-%d')})


@app.route('/api/diario', methods=['POST'])
@role_required('admin', 'profesor')
def api_diario_crear():
    u = current_user()
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    texto = txt_str(data.get('texto'))
    if not titulo and not texto:
        return jsonify({'error': 'Escribí al menos la crónica del día'}), 400
    hoy = _hoy_academy().strftime('%Y-%m-%d')
    db = get_db()
    exist = db.execute('SELECT id FROM diario WHERE fecha=?', (hoy,)).fetchone()
    if exist:
        db.execute('UPDATE diario SET titulo=?, texto=?, user_id=? WHERE id=?',
                   (titulo, texto, u['id'], exist['id']))
        did = exist['id']
    else:
        cur = db.execute('INSERT INTO diario(user_id, fecha, titulo, texto) VALUES(?,?,?,?)',
                         (u['id'], hoy, titulo, texto))
        did = cur.lastrowid
    db.commit()
    return jsonify({'ok': True, 'id': did, 'fecha': hoy})


@app.route('/api/diario/<int:did>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_diario_borrar(did):
    get_db().execute('DELETE FROM diario WHERE id=?', (did,))
    get_db().commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Pagos
# ---------------------------------------------------------------------------

@app.route('/api/pagos', methods=['GET'])
@role_required('admin', 'profesor')
def api_pagos():
    u = current_user()
    q = """SELECT p.*, al.nombre AS alumno_nombre, pr.nombre AS profesor_nombre
           FROM pagos p
           JOIN users al ON al.id=p.alumno_id
           LEFT JOIN users pr ON pr.id=p.profesor_id"""
    params = []
    if u['role'] == 'profesor':
        q += ' WHERE p.profesor_id=?'
        params.append(u['id'])
    q += ' ORDER BY p.id DESC LIMIT 500'
    rows = get_db().execute(q, params).fetchall()
    pagos = []
    for r in rows:
        pagos.append({
            'id': r['id'], 'alumno_id': r['alumno_id'], 'alumno_nombre': r['alumno_nombre'],
            'profesor_id': r['profesor_id'], 'profesor_nombre': r['profesor_nombre'],
            'monto': r['monto'], 'mes': r['mes'], 'anio': r['anio'],
            'metodo': r['metodo'], 'concepto': r['concepto'], 'nota': r['nota'],
            'fecha': r['fecha']})
    return jsonify({'pagos': pagos})


@app.route('/api/pagos', methods=['POST'])
@role_required('admin', 'profesor')
def api_pagos_create():
    data = parse_json()
    alumno_id = to_int(data.get('alumno_id'))
    profesor_id = to_int(data.get('profesor_id'))
    monto = to_float(data.get('monto'))
    mes = to_int(data.get('mes')) or _hoy_academy().month
    anio = to_int(data.get('anio')) or _hoy_academy().year
    if not alumno_id or not monto or monto <= 0:
        return jsonify({'error': 'Alumno y monto son obligatorios'}), 400
    mes, anio, err = validar_mes_anio(mes, anio)
    if err:
        return jsonify({'error': err}), 400
    if profesor_id == -1 or (profesor_id is None and (data.get('profesor_id') == -1)):
        profesor_id = None
    base, cargo, final = calcular_demora(monto, mes, anio)
    if as_bool(data.get('aplicar_cargo')):
        monto = final
    else:
        cargo = 0
    get_db().execute(
        """INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (alumno_id, profesor_id, monto, mes, anio,
         data.get('metodo') or 'Efectivo', data.get('concepto') or 'Cuota mensual',
         data.get('nota'), datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         current_user()['id']))
    get_db().commit()
    alumno = get_db().execute('SELECT * FROM users WHERE id=?', (alumno_id,)).fetchone()
    who = current_user()
    nota_extra = f' (incluye ${cargo:,.0f} de recargo por demora).'.replace(',', '.') if cargo else '.'
    # notificaciones: al alumno
    notify(alumno_id, 'Pago registrado',
           f'Tu pago de ${monto:,.0f} por {mes}/{anio} fue registrado por {who["nombre"]}{nota_extra}'.replace(',', '.'),
           'pago')
    # al profesor que recibio el pago
    if profesor_id and profesor_id != who['id']:
        profe = get_db().execute('SELECT * FROM users WHERE id=?', (profesor_id,)).fetchone()
        if profe:
            notify(profesor_id, 'Recibiste un pago',
                   f'{alumno["nombre"]} te pago ${monto:,.0f} ({data.get("metodo") or "Efectivo"}).'.replace(',', '.'),
                   'pago')
    # a los admins (si no es el que registro)
    admins = get_db().execute('SELECT id FROM users WHERE role="admin"').fetchall()
    for a in admins:
        if a['id'] != who['id']:
            notify(a['id'], 'Nuevo pago registrado',
                   f'{alumno["nombre"]} pago ${monto:,.0f} registrado por {who["nombre"]}.'.replace(',', '.'),
                   'pago')
    return jsonify({'ok': True, 'base': base, 'cargo': cargo, 'monto': monto})


@app.route('/api/pagos/familia', methods=['POST'])
@role_required('admin', 'profesor')
def api_pagos_familia():
    """Registra la cuota (con descuento familiar) de TODOS los integrantes del
    grupo de un titular, en un solo paso. Saltea becados, profesores y quien
    ya tiene pago de ese mes/año."""
    data = parse_json()
    titular_id = to_int(data.get('titular_id'))
    profesor_id = to_int(data.get('profesor_id'))
    mes = to_int(data.get('mes')) or _hoy_academy().month
    anio = to_int(data.get('anio')) or _hoy_academy().year
    metodo = (data.get('metodo') or 'Efectivo').strip() or 'Efectivo'
    nota = txt_str(data.get('nota'))
    if not titular_id:
        return jsonify({'error': 'Elegí el titular de la familia'}), 400
    mes, anio, err = validar_mes_anio(mes, anio)
    if err:
        return jsonify({'error': err}), 400
    db = get_db()
    fam = db.execute('SELECT * FROM familias WHERE titular_id=?', (titular_id,)).fetchone()
    if not fam:
        return jsonify({'error': 'Ese alumno no es titular de ningún grupo familiar'}), 404
    miem = db.execute(
        """SELECT u.*, fm.relacion,
                  (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular
           FROM familia_miembros fm
           JOIN users u ON u.id=fm.user_id
           JOIN familias f ON f.id=fm.familia_id
           WHERE fm.familia_id=?
           ORDER BY es_titular DESC, u.nombre""", (fam['id'],)).fetchall()
    if not miem:
        return jsonify({'error': 'El grupo no tiene integrantes'}), 404
    if profesor_id == -1 or profesor_id is None:
        profesor_id = None
    who = current_user()
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    creados = []
    total = 0
    for m in miem:
        md = dict(m)
        if md.get('beca') or md.get('role') == 'profesor':
            continue
        if db.execute('SELECT COUNT(*) AS n FROM pagos WHERE alumno_id=? AND mes=? AND anio=?',
                      (m['id'], mes, anio)).fetchone()['n']:
            continue
        base, desc, final = familia_cuota(md, len(miem))
        monto = final
        if monto <= 0:
            continue
        _, cargo, monto_final = calcular_demora(monto, mes, anio)
        if not as_bool(data.get('aplicar_cargo')):
            monto_final = monto
        pago_monto = int(round(monto_final))
        db.execute(
            """INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (m['id'], profesor_id, pago_monto, mes, anio, metodo, 'Cuota mensual',
             nota or ('Familia %s' % fam['nombre']), now, who['id']))
        total += pago_monto
        creados.append({'id': m['id'], 'nombre': m['nombre'], 'monto': pago_monto})
    db.commit()
    for cr in creados:
        try:
            notify(cr['id'], 'Pago registrado',
                   'Tu pago de $%d por %d/%d fue registrado por %s (cuota familiar).' % (
                       cr['monto'], mes, anio, who['nombre']),
                   'pago')
        except Exception:
            pass
    if profesor_id and profesor_id != who['id']:
        try:
            profe = db.execute('SELECT nombre FROM users WHERE id=?', (profesor_id,)).fetchone()
            if profe:
                notify(profesor_id, 'Recibiste un pago',
                       'La familia %s te pagó $%d (%s).' % (fam['nombre'], total, metodo),
                       'pago')
        except Exception:
            pass
    return jsonify({'ok': True, 'cantidad': len(creados), 'total': total, 'familia': fam['nombre']})


@app.route('/api/pagos/<int:pid>', methods=['DELETE'])
@role_required('admin')
def api_pagos_delete(pid):
    get_db().execute('DELETE FROM pagos WHERE id=?', (pid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/deudores')
@role_required('admin', 'profesor')
def api_deudores():
    rows = get_db().execute(
        "SELECT * FROM users WHERE role='alumno' AND activo=1 AND cuota_mensual IS NOT NULL ORDER BY nombre").fetchall()
    deudores = []
    for r in rows:
        if en_pausa(r):
            continue
        st = cuota_status(r)
        if st['estado'] in ('deuda', 'por_vencer'):
            deudores.append({
                **user_public(r),
                'estado': st['estado'],
                'cuota': st['cuota'],
                'dias_deuda': dias_deuda(r),
            })
    return jsonify({'deudores': deudores})


@app.route('/api/notify_deuda', methods=['POST'])
@role_required('admin', 'profesor')
def api_notify_deuda():
    # Si algun aviso del before_request fallo en Postgres, su transaccion
    # quedo abortada: healearla antes de tocar la base.
    try:
        get_db().execute('ROLLBACK')
    except Exception:
        pass
    data = parse_json()
    alumno_id = to_int(data.get('alumno_id'))
    if alumno_id:
        ids = [alumno_id]
    else:
        rows = get_db().execute("SELECT * FROM users WHERE role='alumno' AND activo=1 AND cuota_mensual IS NOT NULL").fetchall()
        ids = [r['id'] for r in rows if not en_pausa(r) and cuota_status(r)['estado'] in ('deuda', 'por_vencer')]
    who = current_user()['nombre']
    enviados = 0
    errores = 0
    for aid in ids:
        try:
            alumno = get_db().execute('SELECT * FROM users WHERE id=?', (aid,)).fetchone()
            if not alumno:
                continue
            st = cuota_status(alumno)
            if st.get('estado') in ('becado', 'profesor'):
                continue
            monto_txt = to_int(st.get('cuota') or 0)
            notify(aid, 'Recordatorio de deuda',
                   f'{who} te recuerda que tu cuota de {st["mes"]}/{st["anio"]} ({monto_txt:,.0f} pesos) esta pendiente.'.replace(',', '.'),
                   'deuda')
            enviados += 1
        except Exception:
            import traceback as _tb
            _tb.print_exc()
            errores += 1
    return jsonify({'ok': True, 'avisados': enviados, 'errores': errores})


# ---------------------------------------------------------------------------
# Asistencia
# ---------------------------------------------------------------------------

@app.route('/api/asistencia', methods=['POST'])
@role_required('admin', 'profesor')
def api_asistencia_marcar():
    data = parse_json()
    clase_id = to_int(data.get('clase_id'))
    presentes = data.get('presentes')
    if not clase_id:
        return jsonify({'error': 'Selecciona una clase'}), 400
    fecha, err = fecha_iso(data.get('fecha'))
    if err:
        return jsonify({'error': err}), 400
    # Sin esto, un "presentes": "12" (string) se iteraba caracter por caracter y
    # cada to_int devolvia None -> INSERT con alumno_id NULL.
    if not isinstance(presentes, list):
        presentes = []
    ids = [to_int(p) for p in presentes]
    ids = [i for i in ids if i]
    # borra asistencia existente de ese dia/clase para re-marcar
    get_db().execute('DELETE FROM asistencia WHERE clase_id=? AND fecha=?', (clase_id, fecha))
    for pid in ids:
        get_db().execute(
            'INSERT OR IGNORE INTO asistencia(clase_id, alumno_id, fecha, presente) VALUES(?,?,?,1)',
            (clase_id, pid, fecha))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/asistencia_dia', methods=['GET'])
@role_required('admin', 'profesor')
def api_asistencia_dia():
    clase_id = to_int(request.args.get('clase_id'))
    fecha, err = fecha_iso(request.args.get('fecha'))
    if err:
        return jsonify({'error': err}), 400
    rows = get_db().execute('SELECT alumno_id FROM asistencia WHERE clase_id=? AND fecha=? AND presente=1',
                            (clase_id, fecha)).fetchall()
    return jsonify({'presentes': [r['alumno_id'] for r in rows]})


@app.route('/api/asistencia_por_dia', methods=['GET'])
@role_required('admin', 'profesor')
def api_asistencia_por_dia():
    """Vista general: qué alumnos asistieron cada clase de un día (hoy, ayer, mañana, etc.)."""
    fecha = request.args.get('fecha') or _hoy_academy().strftime('%Y-%m-%d')
    try:
        f = datetime.strptime(fecha, '%Y-%m-%d').date()
    except Exception:
        return jsonify({'error': 'Fecha inválida'}), 400
    db = get_db()
    clases = db.execute(
        """SELECT c.id, c.hora, c.tipo, c.nivel, u.nombre AS profesor_nombre
           FROM classes c LEFT JOIN users u ON u.id=c.profesor_id
           WHERE c.dia=? ORDER BY c.hora""", (f.weekday(),)).fetchall()
    rows = db.execute(
        'SELECT clase_id, alumno_id FROM asistencia WHERE fecha=? AND presente=1', (fecha,)).fetchall()
    por_clase = {}
    for r in rows:
        por_clase.setdefault(r['clase_id'], []).append(r['alumno_id'])
    alumnos = {r['id']: r for r in db.execute(
        "SELECT * FROM users WHERE role='alumno' AND activo=1").fetchall()}
    res = []
    for c in clases:
        ids = por_clase.get(c['id'], [])
        presentes = [user_public(alumnos[i]) for i in ids if i in alumnos]
        res.append({'id': c['id'], 'hora': c['hora'], 'tipo': c['tipo'], 'nivel': c['nivel'],
                    'profesor': c['profesor_nombre'], 'cantidad': len(presentes), 'presentes': presentes})
    return jsonify({'fecha': fecha, 'dia': DIAS[f.weekday()], 'clases_dictadas': len(res), 'clases': res})


@app.route('/api/mi_asistencia')
@role_required('alumno', 'profesor')
def api_mi_asistencia():
    u = current_user()
    db = get_db()
    rows = db.execute(
        """SELECT a.clase_id, a.fecha, c.tipo, c.hora, c.dia, u.nombre AS profesor
           FROM asistencia a JOIN classes c ON c.id=a.clase_id
           LEFT JOIN users u ON u.id=c.profesor_id
           WHERE a.alumno_id=? AND a.presente=1 ORDER BY a.fecha DESC""",
        (u['id'],)).fetchall()
    valoradas = {str(r['clase_id']) + '|' + r['fecha']: 1 for r in db.execute(
        'SELECT clase_id, fecha FROM clase_valoraciones WHERE alumno_id=?', (u['id'],)).fetchall()}
    asis = [{'clase_id': r['clase_id'], 'fecha': r['fecha'], 'tipo': r['tipo'], 'hora': r['hora'],
             'dia': DIAS[r['dia']], 'profesor': r['profesor'],
             'valorada': 1 if (str(r['clase_id']) + '|' + r['fecha']) in valoradas else 0} for r in rows]
    total = db.execute(
        'SELECT COUNT(*) AS n FROM asistencia WHERE alumno_id=? AND presente=1', (u['id'],)).fetchone()['n']
    hoy = _hoy_academy().strftime('%Y-%m-%d')
    hoy_ids = [r['clase_id'] for r in db.execute(
        'SELECT clase_id FROM asistencia WHERE alumno_id=? AND fecha=? AND presente=1',
        (u['id'], hoy)).fetchall()]
    return jsonify({'asistencia': asis, 'total': total, 'hoy': hoy_ids, 'fecha_hoy': hoy})


def _ultimos_meses(n=6):
    """Lista de últimos n meses (anio, mes, label) desde hoy hacia atrás."""
    hoy = _hoy_academy()
    y, m = hoy.year, hoy.month
    meses = []
    for _ in range(n):
        meses.append({'anio': y, 'mes': m, 'label': MESES_NOMBRE[m - 1]})
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    meses.reverse()
    return meses


def _rango_mes(mm):
    primer = f'{mm["anio"]}-{mm["mes"]:02d}-01'
    if mm['mes'] == 12:
        ultimo = f'{mm["anio"] + 1}-01-01'
    else:
        ultimo = f'{mm["anio"]}-{mm["mes"] + 1:02d}-01'
    return primer, ultimo


def _serie_asistencia(alumno_id, meses):
    """Porcentaje de comparecencia (%) por mes: asistencias del alumno / días con clases."""
    db = get_db()
    serie = []
    for mm in meses:
        primer, ultimo = _rango_mes(mm)
        dias = db.execute(
            'SELECT COUNT(DISTINCT fecha) AS n FROM asistencia WHERE presente=1 AND fecha>=? AND fecha<?',
            (primer, ultimo)).fetchone()['n']
        asist = db.execute(
            'SELECT COUNT(*) AS n FROM asistencia WHERE alumno_id=? AND presente=1 AND fecha>=? AND fecha<?',
            (alumno_id, primer, ultimo)).fetchone()['n']
        serie.append({'asist': asist, 'dias': dias, 'pct': round(asist * 100 / dias) if dias else None})
    return serie


@app.route('/api/mi_estadistica_asistencia')
@role_required('alumno', 'profesor')
def api_mi_estadistica_asistencia():
    u = current_user()
    meses = _ultimos_meses()
    serie = _serie_asistencia(u['id'], meses)
    total = get_db().execute(
        'SELECT COUNT(*) AS n FROM asistencia WHERE alumno_id=? AND presente=1', (u['id'],)).fetchone()['n']
    return jsonify({'meses': [mm['label'] for mm in meses],
                    'dias_con_clases': [x['dias'] for x in serie],
                    'serie': serie, 'total': total})


@app.route('/api/estadisticas_asistencia')
@role_required('admin', 'profesor')
def api_estadisticas_asistencia():
    """Comparecencia por alumno (% de clases a las que asistió sobre las dictadas) en los últimos 6 meses."""
    meses = _ultimos_meses()
    db = get_db()
    rows = db.execute(
        """SELECT u.*,
            (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS total_asist
           FROM users u WHERE u.role='alumno' AND u.activo=1
           ORDER BY total_asist DESC LIMIT 40""").fetchall()
    alumnos = []
    for r in rows:
        serie = _serie_asistencia(r['id'], meses)
        d = user_public(r)
        d['total_asist'] = r['total_asist']
        d['en_pausa'] = en_pausa(r)
        d['serie'] = serie
        alumnos.append(d)
    dias_con_clases = []
    for mm in meses:
        primer, ultimo = _rango_mes(mm)
        dias_con_clases.append(db.execute(
            'SELECT COUNT(DISTINCT fecha) AS n FROM asistencia WHERE presente=1 AND fecha>=? AND fecha<?',
            (primer, ultimo)).fetchone()['n'])
    return jsonify({'meses': [mm['label'] for mm in meses],
                    'dias_con_clases': dias_con_clases,
                    'alumnos': alumnos})


@app.route('/api/clase_valorar', methods=['POST'])
@role_required('alumno', 'profesor')
def api_clase_valorar():
    """El alumno valora (1-5 estrellas + comentario) una clase a la que asistió.
    Una sola valoración por clase+fecha (UPDATE si ya existía)."""
    u = current_user()
    data = parse_json()
    clase_id = to_int(data.get('clase_id'))
    fecha = txt_str(data.get('fecha'))
    estrellas = to_int(data.get('estrellas'))
    comentario = txt_str(data.get('comentario'))
    if not clase_id or not fecha:
        return jsonify({'error': 'Faltan datos de la clase'}), 400
    if not estrellas or estrellas < 1 or estrellas > 5:
        return jsonify({'error': 'Elegí entre 1 y 5 estrellas'}), 400
    asistio = get_db().execute(
        'SELECT 1 FROM asistencia WHERE alumno_id=? AND clase_id=? AND fecha=? AND presente=1',
        (u['id'], clase_id, fecha)).fetchone()
    if not asistio:
        return jsonify({'error': 'Solo podés valorar clases a las que asististe'}), 403
    db = get_db()
    db.execute(
        """INSERT INTO clase_valoraciones(clase_id, alumno_id, fecha, estrellas, comentario)
           VALUES(?,?,?,?,?)
           ON CONFLICT(clase_id, alumno_id, fecha) DO UPDATE SET
             estrellas=excluded.estrellas, comentario=excluded.comentario""",
        (clase_id, u['id'], fecha, estrellas, comentario))
    db.commit()
    try:
        prof = db.execute(
            'SELECT u.id FROM classes c LEFT JOIN users u ON u.id=c.profesor_id WHERE c.id=?',
            (clase_id,)).fetchone()
        if prof and prof['id']:
            notify(prof['id'], '⭐ Nueva valoración de clase',
                   '%s valoró tu clase de %s con %d/5 %s' % (
                       u['nombre'], fecha, estrellas,
                       ('— "' + comentario + '"') if comentario else ''),
                   'info', push=False)
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/clase_valoraciones/<int:clase_id>')
@role_required('admin', 'profesor')
def api_clase_valoraciones(clase_id):
    """Staff: puntaje promedio y comentarios de una clase."""
    db = get_db()
    row = db.execute(
        'SELECT COUNT(*) AS n, COALESCE(AVG(estrellas),0) AS prom FROM clase_valoraciones WHERE clase_id=?',
        (clase_id,)).fetchone()
    comentarios = db.execute(
        """SELECT v.estrellas, v.comentario, v.fecha, u.nombre
           FROM clase_valoraciones v JOIN users u ON u.id=v.alumno_id
           WHERE v.clase_id=? ORDER BY v.id DESC LIMIT 20""",
        (clase_id,)).fetchall()
    return jsonify({'clase_id': clase_id, 'n': row['n'], 'promedio': round(row['prom'] or 0, 1),
                    'comentarios': [dict(c) for c in comentarios]})


@app.route('/api/historial_asistencia')
@role_required('admin', 'profesor')
def api_historial_asistencia():
    u = current_user()
    q = """SELECT a.id, a.fecha, a.clase_id, c.tipo, c.hora, al.nombre AS alumno
           FROM asistencia a JOIN classes c ON c.id=a.clase_id
           JOIN users al ON al.id=a.alumno_id"""
    params = []
    if u['role'] == 'profesor':
        q += ' WHERE c.profesor_id=?'
        params.append(u['id'])
    q += ' ORDER BY a.id DESC LIMIT 300'
    rows = get_db().execute(q, params).fetchall()
    return jsonify({'asistencia': [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Perfil / alumno
# ---------------------------------------------------------------------------

@app.route('/api/mis_pagos')
@login_required
def api_mis_pagos():
    u = current_user()
    rows = get_db().execute(
        """SELECT p.*, pr.nombre AS profesor_nombre FROM pagos p
           LEFT JOIN users pr ON pr.id=p.profesor_id
           WHERE p.alumno_id=? ORDER BY p.id DESC LIMIT 100""", (u['id'],)).fetchall()
    aviso = get_db().execute(
        "SELECT * FROM avisos_pago WHERE alumno_id=? AND estado='pendiente' ORDER BY id DESC LIMIT 1",
        (u['id'],)).fetchone()
    return jsonify({'pagos': [dict(r) for r in rows],
                    'aviso_pendiente': dict(aviso) if aviso else None})


@app.route('/api/avisar_pago', methods=['POST'])
@login_required
def api_avisar_pago():
    u = current_user()
    data = parse_json()
    hoy = _hoy_academy()
    mes = to_int(data.get('mes')) or hoy.month
    anio = to_int(data.get('anio')) or hoy.year
    mes, anio, err = validar_mes_anio(mes, anio)
    if err:
        return jsonify({'error': err}), 400
    ex = get_db().execute(
        "SELECT * FROM avisos_pago WHERE alumno_id=? AND mes=? AND anio=? AND estado='pendiente'",
        (u['id'], mes, anio)).fetchone()
    if ex:
        return jsonify({'error': 'Ya enviaste un aviso para este mes. Esperá la confirmación.'}), 400
    monto = to_float(data.get('monto'))
    if not monto:
        monto = to_float(u['cuota_mensual']) or 0
    comp = txt_str(data.get('comprobante'))
    if not comprobante_valido(comp):
        return jsonify({'error': 'Tenés que subir el comprobante de pago (foto, captura o PDF)'}), 400
    if len(comp) > 12 * 1024 * 1024:
        return jsonify({'error': 'El comprobante es muy grande (máx 12MB)'}), 400
    get_db().execute(
        'INSERT INTO avisos_pago(alumno_id, monto, mes, anio, nota, comprobante, estado, fecha) VALUES(?,?,?,?,?,?,?,?)',
        (u['id'], monto, mes, anio, data.get('nota') or 'Cuota mensual', comp, 'pendiente',
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    staff = get_db().execute(
        "SELECT id FROM users WHERE role IN ('admin','profesor') AND activo=1").fetchall()
    for s in staff:
        notify(s['id'], 'Aviso de pago con comprobante',
               f'{u["nombre"]} avisó que pagó la cuota de {mes}/{anio}. Revisá el comprobante y confirmá el pago.',
               'pago')
    return jsonify({'ok': True})


@app.route('/api/avisos_pago')
@role_required('admin', 'profesor')
def api_avisos_pago():
    rows = get_db().execute(
        """SELECT a.*, u.nombre AS alumno_nombre
           FROM avisos_pago a JOIN users u ON u.id=a.alumno_id
           ORDER BY a.estado, a.id DESC LIMIT 300""").fetchall()
    return jsonify({'avisos': [dict(r) for r in rows]})


@app.route('/api/avisos_pago/<int:aid>/confirmar', methods=['POST'])
@role_required('admin', 'profesor')
def api_avisos_confirmar(aid):
    a = get_db().execute('SELECT * FROM avisos_pago WHERE id=?', (aid,)).fetchone()
    if not a:
        return jsonify({'error': 'Aviso no encontrado'}), 404
    if a['estado'] == 'confirmado':
        return jsonify({'error': 'Este aviso ya fue confirmado'}), 400
    who = current_user()
    data = parse_json()
    # El admin puede ajustar el monto real (ej: pagó con el valor de la cuota
    # anterior) y decidir si se suma el aumento/recargo por demora (por defecto
    # se suma como antes; se desactiva si el alumno pagó antes del vencimiento).
    monto_base = to_float(data.get('monto')) or (a['monto'] or 0)
    aplicar_cargo = as_bool(data.get('aplicar_cargo'))
    base, cargo, final = calcular_demora(monto_base, a['mes'], a['anio'])
    if not aplicar_cargo:
        cargo, final = 0, base
    get_db().execute(
        'INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (a['alumno_id'], None, final, a['mes'], a['anio'], 'Aviso', 'Cuota mensual',
         'Confirmado desde aviso de pago' + (f' (recargo por demora ${cargo:,.0f})'.replace(',', '.') if cargo else ''),
         datetime.now().strftime('%Y-%m-%d %H:%M:%S'), who['id']))
    get_db().execute(
        "UPDATE avisos_pago SET estado='confirmado', confirmado_por=?, confirmado_fecha=? WHERE id=?",
        (who['id'], datetime.now().strftime('%Y-%m-%d %H:%M:%S'), aid))
    get_db().commit()
    alumno = get_db().execute('SELECT * FROM users WHERE id=?', (a['alumno_id'],)).fetchone()
    nota = f' (incluye ${cargo:,.0f} de recargo por demora)'.replace(',', '.') if cargo else ''
    notify(a['alumno_id'], 'Pago confirmado',
           f'Tu aviso de pago de la cuota {a["mes"]}/{a["anio"]} por ${final:,.0f} fue confirmado por {who["nombre"]}{nota}.'.replace(',', '.'),
           'pago')
    return jsonify({'ok': True, 'base': base, 'cargo': cargo, 'monto': final})


@app.route('/api/avisos_pago/<int:aid>', methods=['DELETE'])
@role_required('admin')
def api_avisos_delete(aid):
    a = get_db().execute('SELECT * FROM avisos_pago WHERE id=?', (aid,)).fetchone()
    if not a:
        return jsonify({'error': 'Aviso no encontrado'}), 404
    get_db().execute('DELETE FROM avisos_pago WHERE id=?', (aid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/asistencia_yo', methods=['POST'])
@role_required('alumno', 'profesor')
def api_asistencia_yo():
    u = current_user()
    data = parse_json()
    clase_id = to_int(data.get('clase_id'))
    fecha = data.get('fecha') or _hoy_academy().strftime('%Y-%m-%d')
    # Seguridad: solo se puede marcar asistencia presentando el token del QR físico.
    if not data.get('qr_token') or data.get('qr_token') != QR_SECRET:
        return jsonify({'error': 'Debés escanear el QR del gimnasio para registrar tu asistencia.'}), 403
    if not clase_id:
        return jsonify({'error': 'Falta la clase'}), 400
    c = get_db().execute('SELECT * FROM classes WHERE id=?', (clase_id,)).fetchone()
    if not c:
        return jsonify({'error': 'Clase no encontrada'}), 404
    get_db().execute(
        'INSERT OR IGNORE INTO asistencia(clase_id, alumno_id, fecha, presente) VALUES(?,?,?,1)',
        (clase_id, u['id'], fecha))
    get_db().commit()
    chequear_logros(u['id'])
    return jsonify({'ok': True})


@app.route('/api/asistencia_directo', methods=['POST'])
@role_required('alumno', 'profesor')
def api_asistencia_directo():
    # Marcar asistencia sin QR fisico, para accesibilidad (TalkBack).
    u = current_user()
    data = parse_json()
    clase_id = to_int(data.get('clase_id'))
    fecha = data.get('fecha') or _hoy_academy().strftime('%Y-%m-%d')
    if not clase_id:
        return jsonify({'error': 'Falta la clase'}), 400
    c = get_db().execute('SELECT * FROM classes WHERE id=?', (clase_id,)).fetchone()
    if not c:
        return jsonify({'error': 'Clase no encontrada'}), 404
    # La asistencia directa es para marcar HOY (accesibilidad). Antes aceptaba
    # cualquier string y lo guardaba como fecha, ensuciando el historial y
    # dejando que el alumno se auto-asignara dias arbitrarios.
    hoy = _hoy_academy().strftime('%Y-%m-%d')
    if not isinstance(fecha, str) or fecha.strip() != hoy:
        return jsonify({'error': 'Solo se puede marcar la asistencia de hoy'}), 400
    get_db().execute(
        'INSERT OR IGNORE INTO asistencia(clase_id, alumno_id, fecha, presente) VALUES(?,?,?,1)',
        (clase_id, u['id'], fecha))
    get_db().commit()
    chequear_logros(u['id'])
    return jsonify({'ok': True})


@app.route('/api/asistencia_desmarcar', methods=['POST'])
@role_required('alumno', 'profesor')
def api_asistencia_desmarcar():
    """El alumno desmarca su asistencia de HOY (para errores del mismo día).
    No se puede tocar asistencia de otros días."""
    u = current_user()
    data = parse_json()
    clase_id = to_int(data.get('clase_id'))
    if not clase_id:
        return jsonify({'error': 'Falta la clase'}), 400
    hoy = _hoy_academy().strftime('%Y-%m-%d')
    db = get_db()
    ex = db.execute(
        'SELECT 1 FROM asistencia WHERE alumno_id=? AND clase_id=? AND fecha=?',
        (u['id'], clase_id, hoy)).fetchone()
    if not ex:
        return jsonify({'error': 'No tenés marcada esa clase hoy.'}), 404
    db.execute('DELETE FROM asistencia WHERE alumno_id=? AND clase_id=? AND fecha=?',
               (u['id'], clase_id, hoy))
    db.execute('DELETE FROM clase_valoraciones WHERE alumno_id=? AND clase_id=? AND fecha=?',
               (u['id'], clase_id, hoy))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/reporte')
@role_required('admin', 'profesor')
def api_reporte():
    hoy = _hoy_academy()
    mes = to_int(request.args.get('mes')) or hoy.month
    anio = to_int(request.args.get('anio')) or hoy.year
    pagos = get_db().execute(
        """SELECT p.*, u.nombre AS alumno_nombre FROM pagos p
           JOIN users u ON u.id=p.alumno_id
           WHERE p.mes=? AND p.anio=? ORDER BY p.fecha DESC""",
        (mes, anio)).fetchall()
    total = sum((p['monto'] or 0) for p in pagos)
    por_metodo = {}
    for p in pagos:
        k = p['metodo'] or 'Otro'
        por_metodo[k] = por_metodo.get(k, 0) + (p['monto'] or 0)
    deudores = get_db().execute(
        """SELECT u.id, u.nombre, u.cinturon, u.cuota_mensual, u.pausa_desde, u.pausa_hasta FROM users u
           WHERE u.role IN ('alumno','profesor') AND u.activo=1
           AND NOT EXISTS (SELECT 1 FROM pagos p WHERE p.alumno_id=u.id AND p.mes=? AND p.anio=?)""",
        (mes, anio)).fetchall()
    deudores = [d for d in deudores if not en_pausa(d)]
    avisos_pend = get_db().execute(
        "SELECT COUNT(*) AS c FROM avisos_pago WHERE estado='pendiente'").fetchone()['c']
    # Porcentajes de alumnos
    total_alumnos = get_db().execute(
        "SELECT COUNT(*) AS c FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchone()['c']
    pagaron_ids = [p['alumno_id'] for p in pagos]
    cant_pagaron = len(set(pagaron_ids))
    cant_no_pagaron = len(deudores)
    pct_pagaron = round(cant_pagaron * 100 / total_alumnos) if total_alumnos else 0
    pct_no_pagaron = round(cant_no_pagaron * 100 / total_alumnos) if total_alumnos else 0
    # Alumnos que pagaron (detalle)
    alumnos_que_pagaron = get_db().execute(
        """SELECT DISTINCT u.id, u.nombre, u.cinturon, p.monto, p.metodo, p.fecha
           FROM pagos p JOIN users u ON u.id=p.alumno_id
           WHERE p.mes=? AND p.anio=? ORDER BY u.nombre""",
        (mes, anio)).fetchall()
    # Asistencia del mes (primer y último día del mes)
    primer_dia = f'{anio}-{mes:02d}-01'
    if mes == 12:
        ultimo_dia = f'{anio + 1}-01-01'
    else:
        ultimo_dia = f'{anio}-{mes + 1:02d}-01'
    asistieron = get_db().execute(
        """SELECT DISTINCT u.id, u.nombre, u.cinturon, COUNT(*) AS clases
           FROM asistencia a JOIN users u ON u.id=a.alumno_id
           WHERE a.presente=1 AND a.fecha>=? AND a.fecha<?
           GROUP BY u.id ORDER BY u.nombre""",
        (primer_dia, ultimo_dia)).fetchall()
    no_asistieron = get_db().execute(
        """SELECT u.id, u.nombre, u.cinturon, u.pausa_desde, u.pausa_hasta FROM users u
           WHERE u.role IN ('alumno','profesor') AND u.activo=1
           AND NOT EXISTS (SELECT 1 FROM asistencia a WHERE a.alumno_id=u.id
                           AND a.presente=1 AND a.fecha>=? AND a.fecha<?)""",
        (primer_dia, ultimo_dia)).fetchall()
    no_asistieron = [d for d in no_asistieron if not en_pausa(d)]
    cant_asistieron = len(asistieron)
    cant_no_asistieron = len(no_asistieron)
    pct_asistieron = round(cant_asistieron * 100 / total_alumnos) if total_alumnos else 0
    pct_no_asistieron = round(cant_no_asistieron * 100 / total_alumnos) if total_alumnos else 0
    return jsonify({'mes': mes, 'anio': anio, 'total': total, 'cantidad': len(pagos),
                    'por_metodo': por_metodo, 'deudores': [dict(d) for d in deudores],
                    'avisos_pend': int(avisos_pend),
                    'total_alumnos': total_alumnos,
                    'cant_pagaron': cant_pagaron, 'pct_pagaron': pct_pagaron,
                    'cant_no_pagaron': cant_no_pagaron, 'pct_no_pagaron': pct_no_pagaron,
                    'alumnos_que_pagaron': [dict(a) for a in alumnos_que_pagaron],
                    'cant_asistieron': cant_asistieron, 'pct_asistieron': pct_asistieron,
                    'cant_no_asistieron': cant_no_asistieron, 'pct_no_asistieron': pct_no_asistieron,
                    'alumnos_que_asistieron': [{'nombre': a['nombre'], 'cinturon': a['cinturon'], 'clases': a['clases']} for a in asistieron],
                    'alumnos_que_no_asistieron': [{'nombre': a['nombre'], 'cinturon': a['cinturon']} for a in no_asistieron]})


@app.route('/api/cumpleanios')
@login_required
def api_cumpleanios():
    hoy = _hoy_academy()
    rows = get_db().execute(
        "SELECT id, nombre, nacimiento FROM users "
        "WHERE role IN ('alumno','profesor') AND activo=1 AND nacimiento IS NOT NULL AND nacimiento != ''"
    ).fetchall()
    res = []
    for r in rows:
        try:
            nac = r['nacimiento']
            if isinstance(nac, (datetime, date)):
                mes_n, dia, anio_n = nac.month, nac.day, nac.year
            else:
                m = re.search(r'(\d{1,4})[-/.](\d{1,2})[-/.](\d{1,2})', str(nac))
                if not m:
                    continue
                g1, g2, g3 = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if g1 > 1900:
                    anio_n, mes_n, dia = g1, g2, g3
                else:
                    dia, mes_n, anio_n = g1, g2, g3
            if mes_n == hoy.month:
                res.append({'id': r['id'], 'nombre': r['nombre'], 'dia': dia,
                            'edad': (hoy.year - anio_n) if anio_n else None,
                            'hoy': dia == hoy.day})
        except Exception:
            pass
    res.sort(key=lambda x: (0 if x['hoy'] else 1, x['dia'], x['nombre']))
    return jsonify({'cumpleanios': res, 'mes': hoy.month})


@app.route('/api/perfil', methods=['PUT'])
@login_required
def api_perfil_update():
    u = current_user()
    data = parse_json()
    cat = data.get('categoria', u['categoria'])
    # El alumno podia mandarse una categoria cualquiera y romper los filtros de
    # videos, cinturones y los chats por categoria.
    if cat not in CATEGORIAS:
        return jsonify({'error': 'Categoría inválida'}), 400
    tel_tutor = txt_str(data.get('tel_tutor', u['tel_tutor'])) or None
    if u['role'] == 'alumno' and cat in ('kids', 'juveniles') and not tel_tutor:
        return jsonify({'error': 'Para menores (Kids/Juveniles) es obligatorio el telefono del padre, madre o tutor responsable.'}), 400
    foto_ok = data.get('foto_ok', u['foto_ok'])
    if u['role'] == 'alumno' and cat in ('kids', 'juveniles') and not foto_ok:
        return jsonify({'error': 'Para menores (Kids/Juveniles) debe autorizar el mayor, padre, madre o tutor que las fotos del menor puedan exponerse.'}), 400
    nac_upd = txt_str(data.get('nacimiento', u['nacimiento']))
    if nac_upd:
        try:
            if datetime.strptime(nac_upd, '%Y-%m-%d').date() >= _hoy_academy():
                return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
        except ValueError:
            return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400
    get_db().execute(
        'UPDATE users SET nombre=?, edad=?, peso=?, cinturon=?, categoria=?, gi_pref=?, tel=?, nacimiento=?, medic_info=?, emergency_contact=?, tel_tutor=?, tel_2=?, direccion=?, dni=?, foto_ok=?, medic_enfermedades=?, medic_alergias=?, medic_medicacion=?, medic_lesiones=?, ficha_fecha=? WHERE id=?',
        ((data.get('nombre') or u['nombre']), to_int(data.get('edad', u['edad'])),
         to_float(data.get('peso', u['peso'])), data.get('cinturon', u['cinturon']),
         cat, data.get('gi_pref', u['gi_pref']),
         txt_str(data.get('tel', u['tel'])) or None,
         nac_upd or None,
         data.get('medic_info', u['medic_info']),
         data.get('emergency_contact', u['emergency_contact']),
         tel_tutor,
         txt_str(data.get('tel_2', u['tel_2'])) or None,
         txt_str(data.get('direccion', u['direccion'])) or None,
         txt_str(data.get('dni', u['dni'])) or None,
         1 if foto_ok else 0,
         data.get('medic_enfermedades', u['medic_enfermedades']),
         data.get('medic_alergias', u['medic_alergias']),
         data.get('medic_medicacion', u['medic_medicacion']),
         data.get('medic_lesiones', u['medic_lesiones']),
         data.get('ficha_fecha', u['ficha_fecha']),
         u['id']))
    if data.get('password'):
        if len(data['password']) < 4:
            return jsonify({'error': 'La contrasena debe tener al menos 4 caracteres'}), 400
        get_db().execute('UPDATE users SET password_hash=? WHERE id=?',
                         (generate_password_hash(data['password']), u['id']))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/pausa', methods=['POST'])
@role_required('alumno')
def api_pausa_set():
    """El alumno activa una pausa temporal: no se le cobra ni cuenta como deudor."""
    u = current_user()
    data = parse_json()
    desde = txt_str(data.get('desde')) or _hoy_academy().strftime('%Y-%m-%d')
    hasta = txt_str(data.get('hasta'))
    if not hasta:
        return jsonify({'error': 'Indicá hasta qué día estás de pausa'}), 400
    try:
        d = datetime.strptime(desde[:10], '%Y-%m-%d').date()
        h = datetime.strptime(hasta[:10], '%Y-%m-%d').date()
    except Exception:
        return jsonify({'error': 'Formato de fecha inválido'}), 400
    if h < d:
        return jsonify({'error': 'La fecha "hasta" no puede ser anterior a "desde"'}), 400
    get_db().execute('UPDATE users SET pausa_desde=?, pausa_hasta=? WHERE id=?',
                     (desde[:10], hasta[:10], u['id']))
    get_db().commit()
    staff = get_db().execute("SELECT id FROM users WHERE role IN ('admin','profesor')").fetchall()
    for s in staff:
        notify(s['id'], '⏸ Pausa temporal',
               f'{u["nombre"]} está de pausa desde el {desde[:10]} hasta el {hasta[:10]}.',
               'pausa', push=True, link='alumnos')
    return jsonify({'ok': True})


@app.route('/api/pausa', methods=['DELETE'])
@role_required('alumno')
def api_pausa_delete():
    u = current_user()
    get_db().execute('UPDATE users SET pausa_desde=NULL, pausa_hasta=NULL WHERE id=?', (u['id'],))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/foto', methods=['POST'])
@login_required
def api_foto():
    data = parse_json()
    b64 = data.get('foto') or ''
    # Sin este isinstance, un {"foto": 123} reventaba con 500 en `',' not in b64`.
    if not isinstance(b64, str) or ',' not in b64:
        return jsonify({'error': 'No hay imagen'}), 400
    try:
        img_bytes = base64.b64decode(b64.split(',', 1)[1])
    except Exception:
        return jsonify({'error': 'Imagen invalida'}), 400
    if len(img_bytes) > 5 * 1024 * 1024:
        return jsonify({'error': 'Imagen muy grande (max 5MB)'}), 400
    try:
        from PIL import Image
        import io
        img = Image.open(io.BytesIO(img_bytes))
        img.load()
        img = img.convert('RGB')
        img.thumbnail((400, 400))
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=85)
        data_uri = 'data:image/jpeg;base64,' + base64.b64encode(buf.getvalue()).decode('ascii')
    except Exception:
        return jsonify({'error': 'Formato de imagen invalido (usa JPG o PNG)'}), 400
    uid = current_user()['id']
    get_db().execute('UPDATE users SET foto=? WHERE id=?', (data_uri, uid))
    get_db().commit()
    return jsonify({'ok': True, 'foto': data_uri})


# ---------------------------------------------------------------------------
# VIDEOS por cinturón (profesor sube, marca quién los vio)
# ---------------------------------------------------------------------------

def _video_public(v, u):
    d = get_db()
    vistas = d.execute(
        'SELECT COUNT(*) AS n FROM video_views WHERE video_id=?', (v['id'],)).fetchone()['n']
    visto = bool(d.execute(
        'SELECT 1 FROM video_views WHERE video_id=? AND user_id=?',
        (v['id'], u['id'])).fetchone())
    out = {
        'id': v['id'], 'titulo': v['titulo'], 'descripcion': v['descripcion'],
        'belt': v['belt'], 'categoria': v['categoria'], 'url': v['url'], 'tipo': v['tipo'],
        'subido_por': v['subido_por'], 'fecha': v['fecha'],
        'subidor_nombre': v['subidor_nombre'], 'vistas': vistas, 'visto': visto,
    }
    if _is_storage_url(v['url']):
        out['url'] = '/api/video/%d/archivo' % v['id']
    if u['role'] == 'alumno':
        prog = d.execute('SELECT * FROM video_progress WHERE video_id=? AND user_id=?',
                         (v['id'], u['id'])).fetchone()
        completado = bool(prog and prog['completado'])
        pct = 0
        if prog and prog['duracion'] > 0:
            pct = min(99, int(prog['segundos'] * 100 / prog['duracion']))
        out['completado'] = completado
        out['progreso_pct'] = pct
    return out


def _list_videos(u, belt=None, categoria=None):
    db = get_db()
    q = ('SELECT v.id, v.titulo, v.descripcion, v.belt, v.categoria, v.url, v.tipo, v.subido_por, v.fecha, '
         's.nombre AS subidor_nombre FROM videos v '
         'LEFT JOIN users s ON s.id = v.subido_por ')
    args = []
    where = []
    if u['role'] == 'alumno':
        where.append("v.categoria = ?")
        args.append(u['categoria'])
        where.append("(v.belt = 'Todos' OR v.belt = ?)")
        args.append(u['cinturon'])
    else:
        if belt and belt != 'Todos':
            where.append('v.belt = ?')
            args.append(belt)
        if categoria and categoria != 'Todas':
            where.append('v.categoria = ?')
            args.append(categoria)
    if where:
        q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY v.id DESC'
    rows = db.execute(q, args).fetchall()
    return [_video_public(r, u) for r in rows]


@app.route('/api/videos')
@login_required
def api_videos_list():
    u = current_user()
    return jsonify({'videos': _list_videos(u, request.args.get('belt'), request.args.get('categoria'))})


@app.route('/api/videos', methods=['POST'])
@role_required('admin', 'profesor')
def api_videos_create():
    u = current_user()
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    if not titulo:
        return jsonify({'error': 'El título es obligatorio'}), 400
    url = txt_str(data.get('url'))
    if not url:
        return jsonify({'error': 'Falta el link o el video'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db = get_db()
    cur = db.execute(
        'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha) VALUES(?,?,?,?,?,?,?,?)',
        (titulo, txt_str(data.get('descripcion')), (data.get('belt') or 'Todos'),
         (data.get('categoria') or 'adulto'), url, 'link', u['id'], now))
    db.commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/videos/upload', methods=['POST'])
@role_required('admin', 'profesor')
def api_videos_upload():
    u = current_user()
    f = request.files.get('video')
    if not f or not f.filename:
        return jsonify({'error': 'Elegí un archivo de video'}), 400
    ext = os.path.splitext(f.filename)[1].lower()
    if ext not in ('.mp4', '.webm', '.ogg', '.mov'):
        return jsonify({'error': 'Formato no permitido (usa MP4, WebM o MOV)'}), 400
    f.stream.seek(0, 2)
    size = f.stream.tell()
    f.stream.seek(0)
    if size <= 0:
        return jsonify({'error': 'El archivo está vacío'}), 400
    if size > MAX_VIDEO_BYTES:
        return jsonify({'error': 'El video es muy grande (máx %dMB). Para videos largos usá un link de YouTube.' % (MAX_VIDEO_BYTES // (1024 * 1024))}), 400
    titulo = (request.form.get('titulo') or '').strip() or os.path.splitext(f.filename)[0]
    belt = (request.form.get('belt') or 'Todos').strip()
    categoria = (request.form.get('categoria') or 'adulto').strip()
    desc = (request.form.get('descripcion') or '').strip()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db = get_db()
    # Videos cortos (≤ STORAGE_MAX) con Storage configurado → Supabase Storage,
    # que se sirve por CDN sin vivir en la base ni ocupar RAM del servidor.
    if size <= STORAGE_MAX and _storage_enabled():
        raw = f.read()
        pub = _storage_upload('videos/%s%s' % (secrets.token_hex(8), ext),
                              raw, EXT_MIME.get(ext, 'video/mp4'))
        if pub:
            cur = db.execute(
                'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha, data) VALUES(?,?,?,?,?,?,?,?,?)',
                (titulo, desc, belt, categoria, pub, 'upload', u['id'], now, ''))
            db.commit()
            return jsonify({'ok': True, 'id': cur.lastrowid})
        f.stream.seek(0)
    raw = f.read()
    data_b64 = base64.b64encode(raw).decode('ascii')
    cur = db.execute(
        'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha, data) VALUES(?,?,?,?,?,?,?,?,?)',
        (titulo, desc, belt, categoria, '/api/video/0/archivo', 'upload', u['id'], now, data_b64))
    vid = cur.lastrowid
    db.execute('UPDATE videos SET url=? WHERE id=?', ('/api/video/%d/archivo' % vid, vid))
    db.commit()
    return jsonify({'ok': True, 'id': vid})


def _video_range_response(raw, mime):
    """Devuelve el video con soporte real de Range (206/416) para streaming."""
    length = len(raw)
    range_hdr = request.headers.get('Range')
    base_headers = {'Accept-Ranges': 'bytes', 'Content-Type': mime}
    if not range_hdr:
        base_headers['Content-Length'] = str(length)
        return Response(raw, status=200, headers=base_headers)
    m = re.match(r'bytes=(\d*)-(\d*)', range_hdr)
    if not m:
        base_headers['Content-Length'] = str(length)
        return Response(raw, status=200, headers=base_headers)
    s, e = m.group(1), m.group(2)
    start = int(s) if s else None
    end = int(e) if e else None
    if start is None:
        n = end or 0
        start = max(0, length - n)
        end = length - 1
    else:
        if end is None:
            end = length - 1
        else:
            end = min(end, length - 1)
    if start >= length or start > end:
        return Response(status=416, headers={'Content-Range': 'bytes */%d' % length})
    body = raw[start:end + 1]
    headers = dict(base_headers)
    headers['Content-Range'] = 'bytes %d-%d/%d' % (start, end, length)
    headers['Content-Length'] = str(len(body))
    return Response(body, status=206, headers=headers)


@app.route('/api/video/<int:vid>/archivo')
@login_required
def api_video_archivo(vid):
    u = current_user()
    v = get_db().execute('SELECT url, data, belt, categoria FROM videos WHERE id=?', (vid,)).fetchone()
    if not v:
        return jsonify({'error': 'Video no encontrado'}), 404
    # Mismo filtro que el listado: un alumno no puede bajar el archivo de un
    # video de otra categoria ni de otro cinturon.
    if u['role'] == 'alumno':
        if v['categoria'] and v['categoria'] != u['categoria']:
            return jsonify({'error': 'Este video no es de tu categoria'}), 403
        if v['belt'] and v['belt'] != 'Todos' and v['belt'] != u['cinturon']:
            return jsonify({'error': 'Este video no es para tu cinturon'}), 403
    if v['data']:
        ext = os.path.splitext(v['url'])[1].lower()
        mime = EXT_MIME.get(ext, 'video/mp4')
        try:
            raw = base64.b64decode(v['data'])
        except Exception:
            return jsonify({'error': 'Video dañado'}), 500
        return _video_range_response(raw, mime)
    # Video en Supabase Storage: se sirve vía proxy con caché larga para no
    # gastar egress en cada reproducción (Range incluido).
    if _is_storage_url(v['url']):
        ext = os.path.splitext(v['url'])[1].lower()
        return _storage_stream(v['url'], request.headers.get('Range'),
                               EXT_MIME.get(ext, 'video/mp4')) or (jsonify({'error': 'Video no disponible'}), 502)
    return jsonify({'error': 'Video no encontrado'}), 404


@app.route('/api/videos/<int:vid>/view', methods=['POST'])
@login_required
def api_videos_view(vid):
    u = current_user()
    db = get_db()
    v = db.execute('SELECT * FROM videos WHERE id=?', (vid,)).fetchone()
    if not v:
        return jsonify({'error': 'Video no encontrado'}), 404
    if u['role'] == 'alumno' and v['belt'] != 'Todos' and v['belt'] != u['cinturon']:
        return jsonify({'error': 'Este video no es para tu cinturón'}), 403
    if u['role'] == 'alumno' and v['tipo'] == 'upload':
        prog = db.execute('SELECT * FROM video_progress WHERE video_id=? AND user_id=?',
                          (vid, u['id'])).fetchone()
        if not prog or not prog['completado']:
            return jsonify({'error': 'Terminá de ver el video para poder marcarlo como visto'}), 403
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    # Solo avisar la PRIMERA vez: el endpoint es idempotente y se puede llamar
    # varias veces por el mismo video (antes notificaba en cada llamada).
    nuevo = db.execute(
        'INSERT OR IGNORE INTO video_views(video_id, user_id, fecha) VALUES(?,?,?)',
        (vid, u['id'], now)).rowcount > 0
    db.commit()
    if nuevo and v['subido_por']:
        notify(v['subido_por'], 'Nuevo visto',
               '%s vio el video "%s"' % (u['nombre'], v['titulo']), 'info', push=True)
    return jsonify({'ok': True, 'visto': True})


@app.route('/api/videos/<int:vid>/views')
@role_required('admin', 'profesor')
def api_videos_views(vid):
    rows = get_db().execute(
        'SELECT us.nombre, vv.fecha FROM video_views vv JOIN users us ON us.id = vv.user_id '
        'WHERE vv.video_id=? ORDER BY vv.id DESC', (vid,)).fetchall()
    return jsonify({'vistos': [dict(r) for r in rows]})


@app.route('/api/videos/<int:vid>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_videos_delete(vid):
    u = current_user()
    db = get_db()
    v = db.execute('SELECT * FROM videos WHERE id=?', (vid,)).fetchone()
    if not v:
        return jsonify({'error': 'Video no encontrado'}), 404
    if u['role'] != 'admin' and v['subido_por'] != u['id']:
        return jsonify({'error': 'Solo el profesor que lo subió o el admin pueden borrarlo'}), 403
    _storage_delete(v['url'])
    db.execute('DELETE FROM videos WHERE id=?', (vid,))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/estadisticas')
@role_required('admin', 'profesor')
def api_estadisticas():
    u = current_user()
    hoy = _hoy_academy()
    total_alumnos = get_db().execute(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchone()['n']
    ingresos_mes = get_db().execute(
        'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE mes=? AND anio=?',
        (hoy.month, hoy.year)).fetchone()['n']
    clases = get_db().execute('SELECT COUNT(*) AS n FROM classes').fetchone()['n']
    if u['role'] == 'profesor':
        # dinero del profesor
        propio = get_db().execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE profesor_id=? AND mes=? AND anio=?',
            (u['id'], hoy.month, hoy.year)).fetchone()['n']
        total_propio = get_db().execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE profesor_id=?',
            (u['id'],)).fetchone()['n']
        return jsonify({
            'total_alumnos': total_alumnos, 'ingresos_mes': ingresos_mes,
            'clases': clases, 'mi_ingreso_mes': propio, 'mi_ingreso_total': total_propio})
    return jsonify({'total_alumnos': total_alumnos, 'ingresos_mes': ingresos_mes, 'clases': clases})


@app.route('/api/metricas_pagos')
@role_required('admin', 'profesor')
def api_metricas_pagos():
    anio = to_int(request.args.get('anio')) or _hoy_academy().year
    db = get_db()
    total_alumnos = db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchone()['n']
    serie = []
    for mes in range(1, 13):
        ingresos = db.execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchone()['n']
        cantidad = db.execute(
            'SELECT COUNT(*) AS n FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchone()['n']
        cant_pagaron = db.execute(
            'SELECT COUNT(DISTINCT alumno_id) AS n FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchone()['n']
        serie.append({
            'mes': mes,
            'ingresos': ingresos,
            'cantidad': cantidad,
            'cant_pagaron': cant_pagaron,
            'deudores': max(0, total_alumnos - cant_pagaron),
            'pct_pagaron': round(cant_pagaron * 100 / total_alumnos) if total_alumnos else 0,
            'pct_morosidad': round((total_alumnos - cant_pagaron) * 100 / total_alumnos) if total_alumnos else 0,
        })
    total_ingresos = sum(s['ingresos'] for s in serie)
    return jsonify({'anio': anio, 'total_alumnos': total_alumnos,
                    'total_ingresos': total_ingresos, 'serie': serie})


# ---------------------------------------------------------------------------
# Chat + grupos por categoria
# ---------------------------------------------------------------------------

@app.route('/api/chats')
@login_required
def api_chats():
    u = current_user()
    db = get_db()
    rows = db.execute(
        'SELECT c.* FROM chats c JOIN chat_members m ON m.chat_id=c.id '
        'WHERE m.user_id=? ORDER BY c.id DESC', (u['id'],)).fetchall()
    chats = []
    for c in rows:
        nombre = c['nombre']
        if c['tipo'] == 'grupo':
            miembros = db.execute(
                'SELECT COUNT(*) AS n FROM chat_members WHERE chat_id=?', (c['id'],)).fetchone()['n']
            chats.append({'id': c['id'], 'nombre': nombre or 'Grupo', 'tipo': c['tipo'],
                          'miembros': miembros})
        else:
            otro = db.execute(
                'SELECT u.id, u.nombre, u.foto FROM users u JOIN chat_members m ON m.user_id=u.id '
                'WHERE m.chat_id=? AND u.id<>?', (c['id'], u['id'])).fetchone()
            chats.append({'id': c['id'], 'nombre': (otro['nombre'] if otro else 'Chat'),
                          'tipo': c['tipo']})
    return jsonify({'chats': chats})


@app.route('/api/chats', methods=['POST'])
@login_required
def api_chat_crear():
    u = current_user()
    data = parse_json()
    db = get_db()
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    if data.get('categoria') in ('kids', 'juveniles', 'adulto'):
        cat = data['categoria']
        try:
            # reutilizar un chat de grupo existente de esa categoria
            exist = db.execute(
                "SELECT id FROM chats WHERE tipo='grupo' AND nombre=? ORDER BY id ASC LIMIT 1",
                (cat,)).fetchone()
            if exist:
                # El grupo ya existia: hay que agregar al usuario actual como
                # miembro, si no /mensajes le daba 403 y no podia escribir.
                db.execute('INSERT OR IGNORE INTO chat_members(chat_id, user_id) VALUES(?,?)',
                           (exist['id'], u['id']))
                db.commit()
                return jsonify({'ok': True, 'id': exist['id'], 'nombre': cat, 'tipo': 'grupo'})
            cur = db.execute(
                "INSERT INTO chats(nombre, tipo, creado_por, fecha) VALUES(?,?,?,?)",
                (cat, 'grupo', u['id'], now))
            chat_id = cur.lastrowid
            ids = [r['id'] for r in db.execute(
                "SELECT id FROM users WHERE role in ('admin','profesor') OR (role='alumno' AND categoria=?)",
                (cat,)).fetchall()]
            for uid in ids:
                db.execute('INSERT OR IGNORE INTO chat_members(chat_id, user_id) VALUES(?,?)', (chat_id, uid))
            db.commit()
            return jsonify({'ok': True, 'id': chat_id, 'nombre': cat, 'tipo': 'grupo'})
        except Exception as e:
            return jsonify({'error': 'Error al crear el grupo: %s' % str(e)}), 500
    # chat directo
    otro = to_int(data.get('user_id'))
    if not otro or otro == u['id']:
        return jsonify({'error': 'Elegí un contacto válido'}), 400
    # Sin este check, un user_id inexistente reventaba con 500 al insertar en
    # chat_members (FK). Solo se puede chatear con alguien que exista y este activo.
    destino = db.execute('SELECT id FROM users WHERE id=? AND activo=1', (otro,)).fetchone()
    if not destino:
        return jsonify({'error': 'El contacto no existe'}), 404
    exist = db.execute(
        'SELECT c.id FROM chats c JOIN chat_members m1 ON m1.chat_id=c.id JOIN chat_members m2 ON m2.chat_id=c.id '
        'WHERE c.tipo=\'directo\' AND m1.user_id=? AND m2.user_id=? '
        'AND (SELECT COUNT(*) FROM chat_members WHERE chat_id=c.id)=2', (u['id'], otro)).fetchone()
    if exist:
        return jsonify({'ok': True, 'id': exist['id'], 'tipo': 'directo'})
    cur = db.execute("INSERT INTO chats(nombre, tipo, creado_por, fecha) VALUES(?,?,?,?)",
                     (None, 'directo', u['id'], now))
    chat_id = cur.lastrowid
    db.execute('INSERT OR IGNORE INTO chat_members(chat_id, user_id) VALUES(?,?)', (chat_id, u['id']))
    db.execute('INSERT OR IGNORE INTO chat_members(chat_id, user_id) VALUES(?,?)', (chat_id, otro))
    db.commit()
    return jsonify({'ok': True, 'id': chat_id, 'tipo': 'directo'})


@app.route('/api/chats/<int:chat_id>/mensajes')
@login_required
def api_chat_mensajes(chat_id):
    u = current_user()
    db = get_db()
    miembro = db.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?', (chat_id, u['id'])).fetchone()
    chat = db.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
    if not chat or not miembro:
        return jsonify({'error': 'No tenés acceso a este chat'}), 403
    filas = db.execute(
        'SELECT m.id, m.user_id, m.mensaje, m.adjunto, m.adjunto_tipo, m.fecha, us.nombre, us.foto '
        'FROM chat_messages m JOIN users us ON us.id=m.user_id '
        'WHERE m.chat_id=? ORDER BY m.id ASC LIMIT 200', (chat_id,)).fetchall()
    return jsonify({'mensajes': [dict(r) for r in filas], 'chat': dict(chat)})


@app.route('/api/chats/<int:chat_id>/mensajes', methods=['POST'])
@login_required
def api_chat_enviar(chat_id):
    u = current_user()
    data = parse_json()
    msj = txt_str(data.get('mensaje'))
    adjunto = txt_str(data.get('adjunto'))
    adjunto_tipo = txt_str(data.get('adjunto_tipo'))
    if not msj and not adjunto:
        return jsonify({'error': 'Escribí un mensaje o adjuntá una foto/video'}), 400
    # solo permitir imagenes y videos en el adjunto
    if adjunto and not (adjunto.startswith('data:image/') or adjunto.startswith('data:video/')):
        return jsonify({'error': 'El adjunto debe ser una imagen o un video'}), 400
    if len(adjunto) > 25 * 1024 * 1024:
        return jsonify({'error': 'El archivo es muy grande (máx 25MB)'}), 400
    db = get_db()
    chat = db.execute('SELECT * FROM chats WHERE id=?', (chat_id,)).fetchone()
    if not chat or not db.execute('SELECT 1 FROM chat_members WHERE chat_id=? AND user_id=?', (chat_id, u['id'])).fetchone():
        return jsonify({'error': 'No tenés acceso a este chat'}), 403
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db.execute('INSERT INTO chat_messages(chat_id, user_id, mensaje, adjunto, adjunto_tipo, fecha) VALUES(?,?,?,?,?,?)',
               (chat_id, u['id'], msj or None, adjunto or None, adjunto_tipo or None, now))
    db.commit()
    try:
        txt = adjunto_tipo if not msj else msj
        for m in db.execute('SELECT user_id FROM chat_members WHERE chat_id=? AND user_id<>?', (chat_id, u['id'])).fetchall():
            notify(m['user_id'], 'Nuevo mensaje', '%s: %s' % (u['nombre'], txt), 'chat', push=True)
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/contactos')
@login_required
def api_contactos():
    u = current_user()
    if u['role'] == 'alumno':
        rows = get_db().execute(
            "SELECT id, nombre, cinturon, foto FROM users WHERE activo=1 AND id<>? AND role IN ('admin','profesor')",
            (u['id'],)).fetchall()
    else:
        rows = get_db().execute(
            "SELECT id, nombre, cinturon, categoria, foto FROM users WHERE activo=1 AND id<>? AND role='alumno'",
            (u['id'],)).fetchall()
    return jsonify({'contactos': [dict(r) for r in rows]})


# ---------------------------------------------------------------------------
# Video progress (no removible hasta terminar)
# ---------------------------------------------------------------------------

@app.route('/api/videos/<int:vid>/progress', methods=['POST'])
@login_required
def api_video_progress(vid):
    u = current_user()
    data = parse_json()
    seg = max(0, to_int(data.get('segundos')) or 0)
    dur = max(0, to_int(data.get('duracion')) or 0)
    watched = max(0, to_int(data.get('watched')) or 0)
    if dur <= 0 and seg > 0:
        dur = seg
    db = get_db()
    v = db.execute('SELECT * FROM videos WHERE id=?', (vid,)).fetchone()
    if not v:
        return jsonify({'error': 'Video no encontrado'}), 404
    completado = 1 if (dur > 0 and seg >= dur * 0.95 and watched >= dur * 0.8) else 0
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    if completado:
        db.execute('INSERT OR IGNORE INTO video_views(video_id, user_id, fecha) VALUES(?,?,?)',
                   (vid, u['id'], now))
    prev = db.execute(
        'SELECT completado FROM video_progress WHERE video_id=? AND user_id=?',
        (vid, u['id'])).fetchone()
    if prev is not None:
        if not prev['completado']:
            db.execute(
                'UPDATE video_progress SET segundos=?, duracion=?, completado=?, fecha=? WHERE video_id=? AND user_id=?',
                (seg, dur, completado, now, vid, u['id']))
    else:
        db.execute(
            'INSERT INTO video_progress(video_id, user_id, segundos, duracion, completado, fecha) VALUES(?,?,?,?,?,?)',
            (vid, u['id'], seg, dur, completado, now))
    db.commit()
    nuevo_completado = bool(completado) and (prev is None or not prev['completado'])
    if completado and u['role'] == 'alumno':
        chequear_logros(u['id'])
    if nuevo_completado and v['subido_por']:
        notify(v['subido_por'], 'Video completado',
               '%s terminó de ver "%s"' % (u['nombre'], v['titulo']), 'info', push=True)
    return jsonify({'ok': True, 'completado': completado})


@app.route('/api/videos/<int:vid>/progress')
@login_required
def api_video_progress_get(vid):
    u = current_user()
    r = get_db().execute('SELECT * FROM video_progress WHERE video_id=? AND user_id=?',
                         (vid, u['id'])).fetchone()
    return jsonify({'progress': dict(r) if r else {'segundos': 0, 'duracion': 0, 'completado': 0}})


# ---------------------------------------------------------------------------
# Metas de entrenamiento
# ---------------------------------------------------------------------------

@app.route('/api/metas')
@login_required
def api_metas():
    u = current_user()
    filas = get_db().execute('SELECT * FROM metas WHERE user_id=? ORDER BY id DESC', (u['id'],)).fetchall()
    return jsonify({'metas': [dict(r) for r in filas]})


@app.route('/api/metas', methods=['POST'])
@login_required
def api_meta_crear():
    u = current_user()
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    if not titulo:
        return jsonify({'error': 'Poné el título de la meta'}), 400
    obj = to_int(data.get('objetivo')) or 3
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cur = get_db().execute('INSERT INTO metas(user_id, titulo, tipo, objetivo, fecha) VALUES(?,?,?,?,?)',
                           (u['id'], titulo, data.get('tipo') or 'semanas', obj, now))
    get_db().commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/metas/<int:meta_id>', methods=['POST'])
@login_required
def api_meta_actualizar(meta_id):
    u = current_user()
    db = get_db()
    if not db.execute('SELECT 1 FROM metas WHERE id=? AND user_id=?', (meta_id, u['id'])).fetchone():
        return jsonify({'error': 'Meta no encontrada'}), 404
    data = parse_json()
    cumplida = 1 if (data.get('cumplida') or data.get('cumplida') == 'on') else 0
    if cumplida:
        db.execute('UPDATE metas SET cumplida=1 WHERE id=?', (meta_id,))
    else:
        db.execute('UPDATE metas SET titulo=?, tipo=?, objetivo=? WHERE id=?',
                   (txt_str(data.get('titulo')), data.get('tipo') or 'semanas',
                    to_int(data.get('objetivo')) or 3, meta_id))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/metas/<int:meta_id>', methods=['DELETE'])
@login_required
def api_meta_borrar(meta_id):
    u = current_user()
    db = get_db()
    db.execute('DELETE FROM metas WHERE id=? AND user_id=?', (meta_id, u['id']))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Ranking (asistencia + progreso + videos vistos)
# ---------------------------------------------------------------------------

@app.route('/api/ranking')
@role_required('admin', 'profesor')
def api_ranking():
    db = get_db()
    asis = db.execute(
        'SELECT a.alumno_id AS uid, COUNT(*) AS n FROM asistencia a GROUP BY a.alumno_id').fetchall()
    vids = db.execute(
        'SELECT vv.user_id AS uid, COUNT(*) AS n FROM video_views vv GROUP BY vv.user_id').fetchall()
    comp = db.execute(
        'SELECT vp.user_id AS uid, COUNT(*) AS n FROM video_progress vp WHERE vp.completado=1 GROUP BY vp.user_id').fetchall()
    nmap = {a['uid']: a['n'] for a in asis}
    vmap = {v['uid']: v['n'] for v in vids}
    cmap = {c['uid']: c['n'] for c in comp}
    filas = db.execute(
        "SELECT id, nombre, cinturon, categoria, foto FROM users WHERE activo=1 AND role='alumno'").fetchall()
    lista = []
    for r in filas:
        punt = (nmap.get(r['id'], 0) * 2) + (cmap.get(r['id'], 0) * 5) + (vmap.get(r['id'], 0) * 1)
        lista.append({'id': r['id'], 'nombre': r['nombre'], 'cinturon': r['cinturon'],
                      'categoria': r['categoria'], 'asistencias': nmap.get(r['id'], 0),
                      'videos': vmap.get(r['id'], 0), 'completados': cmap.get(r['id'], 0), 'puntos': punt})
    lista.sort(key=lambda x: x['puntos'], reverse=True)
    return jsonify({'ranking': lista})


# ---------------------------------------------------------------------------
# Cinturones / exámenes de grado
# ---------------------------------------------------------------------------

@app.route('/api/mis_grados')
@role_required('alumno')
def api_mis_grados():
    u = current_user()
    db = get_db()
    grados = db.execute(
        'SELECT id, cinturon, fecha, notas FROM grados WHERE alumno_id=? ORDER BY id DESC',
        (u['id'],)).fetchall()
    return jsonify({'cinturon': u['cinturon'],
                    'proximo_examen': u['proximo_examen'] if 'proximo_examen' in u.keys() else None,
                    'grados': [dict(r) for r in grados]})


@app.route('/api/alumnos/<int:uid>/grado', methods=['POST'])
@role_required('admin', 'profesor')
def api_alumno_grado(uid):
    data = parse_json()
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    cinturon = txt_str(data.get('cinturon'))
    if not cinturon:
        return jsonify({'error': 'Elegí el cinturón'}), 400
    fecha = (data.get('fecha') or _hoy_academy().strftime('%Y-%m-%d')).strip()
    notas = txt_str(data.get('notas'))
    me = current_user()
    db = get_db()
    db.execute('INSERT INTO grados(alumno_id, cinturon, fecha, notas, registrado_por) VALUES(?,?,?,?,?)',
               (uid, cinturon, fecha, notas, me['id']))
    db.execute('UPDATE users SET cinturon=? WHERE id=?', (cinturon, uid))
    db.commit()
    set_setting('avisado_examen_%d' % uid, '0')
    try:
        notify(uid, '🥋 Examen aprobado',
               '¡Felicitaciones! Tu nuevo cinturón es %s (fecha: %s).' % (cinturon, fecha),
               'logro', push=True)
    except Exception:
        pass
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>/proximo_examen', methods=['POST'])
@role_required('admin', 'profesor')
def api_alumno_proximo_examen(uid):
    data = parse_json()
    u = get_db().execute('SELECT * FROM users WHERE id=? AND role="alumno"', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    fecha = txt_str(data.get('fecha')) or None
    db = get_db()
    db.execute('UPDATE users SET proximo_examen=? WHERE id=?', (fecha, uid))
    db.commit()
    if fecha:
        try:
            notify(uid, '🥋 Tenés examen de cinturón',
                   'Tu próximo examen está agendado para el %s. ¡A prepararse!' % fecha,
                   'logro', push=True)
        except Exception:
            pass
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Muro + galería de fotos
# ---------------------------------------------------------------------------

@app.route('/api/muro')
@login_required
def api_muro():
    db = get_db()
    filas = db.execute(
        'SELECT m.*, us.nombre, us.cinturon, us.foto '
        'FROM muro m JOIN users us ON us.id=m.user_id ORDER BY m.id DESC LIMIT 100').fetchall()
    out = []
    for r in filas:
        fotos = [f['data'] for f in db.execute('SELECT data FROM muro_fotos WHERE muro_id=?', (r['id'],)).fetchall()]
        v = db.execute('SELECT id, url, tipo FROM muro_videos WHERE muro_id=? ORDER BY id LIMIT 1', (r['id'],)).fetchone()
        vd = dict(v) if v else None
        if vd and _is_storage_url(vd['url']):
            vd['url'] = '/api/muro_video/%d' % vd['id']
        out.append({**dict(r), 'fotos': fotos, 'video': vd})
    return jsonify({'muro': out})


@app.route('/api/muro', methods=['POST'])
@login_required
def api_muro_crear():
    u = current_user()
    data = parse_json()
    texto = txt_str(data.get('texto'))
    fotos = data.get('fotos') or []
    video = data.get('video') or {}
    link = (video.get('link') or '').strip()
    archivo = video.get('archivo') or ''
    if not texto and not fotos and not link and not archivo:
        return jsonify({'error': 'Escribí algo, subí una foto o un video de una lucha'}), 400
    if link and archivo:
        return jsonify({'error': 'Elegí un solo video: link de YouTube O archivo'}), 400
    if isinstance(archivo, str) and archivo and not archivo.startswith('data:video/'):
        return jsonify({'error': 'Formato de video no válido'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db = get_db()
    cur = db.execute('INSERT INTO muro(user_id, texto, fecha) VALUES(?,?,?)', (u['id'], texto, now))
    mid = cur.lastrowid
    for f in fotos[:5]:
        # Antes solo se chequeaba el prefijo 'data:image/' y eso dejaba pasar SVG con JS (XSS).
        if imagen_valida(f, max_bytes=6 * 1024 * 1024):
            db.execute('INSERT INTO muro_fotos(muro_id, data) VALUES(?,?)', (mid, f))
    if link:
        db.execute('INSERT INTO muro_videos(muro_id, tipo, url, data) VALUES(?,?,?,?)', (mid, 'link', link, ''))
    elif archivo:
        m = re.match(r'^data:([^;]+);base64,(.+)$', archivo, re.S)
        if m and _storage_enabled() and m.group(1) in EXT_MIME.values():
            try:
                raw = base64.b64decode(m.group(2))
            except Exception:
                raw = b''
            if len(raw) <= STORAGE_MAX:
                pub = _storage_upload('muro/%s%s' % (secrets.token_hex(8), _storage_ext(m.group(1))),
                                      raw, m.group(1))
                if pub:
                    db.execute('INSERT INTO muro_videos(muro_id, tipo, url, data) VALUES(?,?,?,?)',
                               (mid, 'upload', pub, ''))
                    db.commit()
                    return jsonify({'ok': True, 'id': mid})
        vid = db.execute('INSERT INTO muro_videos(muro_id, tipo, url, data) VALUES(?,?,?,?)',
                         (mid, 'upload', '/api/muro_video/0', archivo)).lastrowid
        db.execute('UPDATE muro_videos SET url=? WHERE id=?', ('/api/muro_video/%d' % vid, vid))
    db.commit()
    return jsonify({'ok': True, 'id': mid})


@app.route('/api/muro/<int:muro_id>', methods=['DELETE'])
@login_required
def api_muro_borrar(muro_id):
    u = current_user()
    db = get_db()
    # Ownership primero: antes se borraban los videos/fotos de cualquier muro
    # (y del Storage) aunque el DELETE del post no afectara al de otro usuario.
    post = db.execute('SELECT id FROM muro WHERE id=? AND user_id=?', (muro_id, u['id'])).fetchone()
    if not post:
        return jsonify({'error': 'Publicación no encontrada'}), 404
    for r in db.execute('SELECT url FROM muro_videos WHERE muro_id=?', (muro_id,)).fetchall():
        _storage_delete(r['url'])
    db.execute('DELETE FROM muro_videos WHERE muro_id=?', (muro_id,))
    db.execute('DELETE FROM muro WHERE id=? AND user_id=?', (muro_id, u['id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/muro_video/<int:muro_vid>')
@login_required
def api_muro_video(muro_vid):
    r = get_db().execute('SELECT url, data FROM muro_videos WHERE id=?', (muro_vid,)).fetchone()
    if not r:
        return jsonify({'error': 'Video no encontrado'}), 404
    if r['data']:
        m = re.match(r'^data:([^;]+);base64,(.+)$', r['data'], re.S)
        if not m:
            return jsonify({'error': 'Video dañado'}), 500
        try:
            raw = base64.b64decode(m.group(2))
        except Exception:
            return jsonify({'error': 'Video dañado'}), 500
        return _video_range_response(raw, m.group(1))
    if _is_storage_url(r['url']):
        ext = os.path.splitext(r['url'])[1].lower()
        return _storage_stream(r['url'], request.headers.get('Range'),
                               EXT_MIME.get(ext, 'video/mp4')) or (jsonify({'error': 'Video no disponible'}), 502)
    return jsonify({'error': 'Video no encontrado'}), 404


# ---------------------------------------------------------------------------
# Encuestas
# ---------------------------------------------------------------------------

@app.route('/api/encuestas')
@login_required
def api_encuestas():
    u = current_user()
    db = get_db()
    filas = db.execute('SELECT * FROM encuestas ORDER BY id DESC').fetchall()
    out = []
    for r in filas:
        opciones = json.loads(r['opciones']) if r['opciones'] else []
        mi_voto = db.execute('SELECT opcion FROM encuesta_votos WHERE encuesta_id=? AND user_id=?',
                             (r['id'], u['id'])).fetchone()
        conteo = []
        for i in range(len(opciones)):
            conteo.append(db.execute('SELECT COUNT(*) AS n FROM encuesta_votos WHERE encuesta_id=? AND opcion=?',
                                     (r['id'], i)).fetchone()['n'])
        out.append({**dict(r), 'opciones': opciones, 'conteo': conteo,
                    'mi_voto': mi_voto['opcion'] if mi_voto else None})
    return jsonify({'encuestas': out})


@app.route('/api/encuestas', methods=['POST'])
@role_required('admin', 'profesor')
def api_encuesta_crear():
    u = current_user()
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    opciones = [str(x).strip() for x in (data.get('opciones') or []) if str(x).strip()]
    if not titulo or len(opciones) < 2:
        return jsonify({'error': 'Necesitás título y al menos 2 opciones'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    cur = get_db().execute('INSERT INTO encuestas(user_id, titulo, opciones, fecha) VALUES(?,?,?,?)',
                           (u['id'], titulo, json.dumps(opciones), now))
    get_db().commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/encuestas/<int:eid>/votar', methods=['POST'])
@login_required
def api_encuesta_votar(eid):
    u = current_user()
    data = parse_json()
    opcion = to_int(data.get('opcion'))
    db = get_db()
    e = db.execute('SELECT * FROM encuestas WHERE id=?', (eid,)).fetchone()
    if not e:
        return jsonify({'error': 'Encuesta no encontrada'}), 404
    n_opts = len(json.loads(e['opciones'])) if e['opciones'] else 0
    if opcion is None or opcion < 0 or opcion >= n_opts:
        return jsonify({'error': 'Opción inválida'}), 400
    db.execute('DELETE FROM encuesta_votos WHERE encuesta_id=? AND user_id=?', (eid, u['id']))
    db.execute('INSERT INTO encuesta_votos(encuesta_id, user_id, opcion) VALUES(?,?,?)', (eid, u['id'], opcion))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Eventos y actividades
# ---------------------------------------------------------------------------

@app.route('/api/eventos')
@login_required
def api_eventos():
    filas = get_db().execute('SELECT * FROM eventos ORDER BY fecha_evento ASC').fetchall()
    db = get_db()
    u = current_user()
    out = []
    for r in filas:
        asisten = db.execute('SELECT COUNT(*) AS n FROM evento_asistencias WHERE evento_id=?', (r['id'],)).fetchone()['n']
        voy = db.execute('SELECT 1 FROM evento_asistencias WHERE evento_id=? AND user_id=?', (r['id'], u['id'])).fetchone()
        fotos = [f['data'] for f in db.execute(
            'SELECT data FROM evento_fotos WHERE evento_id=? ORDER BY id', (r['id'],)).fetchall()]
        out.append({**dict(r), 'asisten_conf': asisten, 'voy': 1 if voy else 0, 'fotos': fotos})
    return jsonify({'eventos': out})


@app.route('/api/eventos', methods=['POST'])
@role_required('admin', 'profesor')
def api_evento_crear():
    u = current_user()
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    if not titulo:
        return jsonify({'error': 'Poné el título del evento'}), 400
    now = datetime.now().strftime('%Y-%m-%d %H:%M')
    db = get_db()
    cur = db.execute(
        'INSERT INTO eventos(user_id, titulo, descripcion, fecha, hora, lugar, fecha_evento) VALUES(?,?,?,?,?,?,?)',
        (u['id'], titulo, txt_str(data.get('descripcion')), now,
         txt_str(data.get('hora')), txt_str(data.get('lugar')),
         txt_str(data.get('fecha_evento')) or now[:10]))
    eid = cur.lastrowid
    # Fotos / flyer del evento (max 5, imagenes raster validas; nada de SVG ni PDF)
    fotos = data.get('fotos')
    fotos = fotos if isinstance(fotos, list) else []
    for f in fotos[:5]:
        if imagen_valida(f, max_bytes=6 * 1024 * 1024):
            db.execute('INSERT INTO evento_fotos(evento_id, data) VALUES(?,?)', (eid, f))
    db.commit()
    return jsonify({'ok': True, 'id': eid})


@app.route('/api/eventos/<int:eid>/asistir', methods=['POST'])
@login_required
def api_evento_asistir(eid):
    u = current_user()
    db = get_db()
    data = parse_json()
    quitar = data.get('quitar')
    # Sin este check, un eid inexistente reventaba con 500 por FK.
    if not db.execute('SELECT 1 FROM eventos WHERE id=?', (eid,)).fetchone():
        return jsonify({'error': 'Evento no encontrado'}), 404
    if quitar:
        db.execute('DELETE FROM evento_asistencias WHERE evento_id=? AND user_id=?', (eid, u['id']))
    else:
        db.execute('INSERT OR IGNORE INTO evento_asistencias(evento_id, user_id) VALUES(?,?)', (eid, u['id']))
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/eventos/<int:eid>', methods=['DELETE'])
@role_required('admin')
def api_evento_borrar(eid):
    db = get_db()
    if not db.execute('SELECT 1 FROM eventos WHERE id=?', (eid,)).fetchone():
        return jsonify({'error': 'Evento no encontrado'}), 404
    db.execute('DELETE FROM evento_fotos WHERE evento_id=?', (eid,))
    db.execute('DELETE FROM evento_asistencias WHERE evento_id=?', (eid,))
    db.execute('DELETE FROM eventos WHERE id=?', (eid,))
    db.commit()
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Historial financiero + asistencias (gráficos)
# ---------------------------------------------------------------------------

@app.route('/api/historial')
@role_required('admin', 'profesor')
def api_historial():
    db = get_db()
    pagos = db.execute(
        'SELECT anio, mes, COALESCE(SUM(monto),0) AS monto, COUNT(*) AS n '
        'FROM pagos GROUP BY anio, mes ORDER BY anio, mes').fetchall()
    asis = db.execute(
        'SELECT substr(fecha,1,10) AS d, COUNT(DISTINCT alumno_id) AS n FROM asistencia '
        'WHERE fecha >= ? GROUP BY substr(fecha,1,10) ORDER BY substr(fecha,1,10)',
        (_hoy_academy().strftime('%Y-%m-01'))).fetchall()
    return jsonify({
        'pagos': [dict(r) for r in pagos],
        'asistencia': [{'fecha': r['d'], 'alumnos': r['n']} for r in asis]
    })


# ---------------------------------------------------------------------------
# MercadoPago (link de checkout)
# ---------------------------------------------------------------------------

@app.route('/api/checkout', methods=['POST'])
@login_required
def api_checkout():
    """Genera un link de pago de MercadoPago para la cuota del alumno."""
    u = current_user()
    token = (get_setting('mp_access_token', '') or '').strip()
    if not token:
        return jsonify({'error': 'MercadoPago aún no está configurado'}), 400
    cuota = u['cuota_mensual'] or to_float(get_setting('default_cuota')) or 0
    if cuota <= 0:
        return jsonify({'error': 'No hay un monto de cuota definido'}), 400
    desc = 'Cuota IKIGAI VIEDMA'
    email = (u.get('username') or '') + '@alumno.local' if '@' not in (u.get('username') or '') else u.get('username')
    import urllib.request
    payload = {
        'items': [{'title': desc, 'quantity': 1, 'unit_price': float(cuota), 'currency_id': 'ARS'}],
        'back_urls': {'success': 'https://ikigai-viedma.onrender.com/app', 'failure': 'https://ikigai-viedma.onrender.com/app'},
        'auto_return': 'approved',
        'notification_url': 'https://ikigai-viedma.onrender.com/api/mp_webhook',
        'external_reference': 'cuota-%s-%d' % (u['id'], int(datetime.now().timestamp())),
    }
    req = urllib.request.Request(
        'https://api.mercadopago.com/checkout/preferences',
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token},
        method='POST')
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode('utf-8'))
        return jsonify({'init_point': data.get('init_point'), 'preference_id': data.get('id')})
    except Exception as e:
        return jsonify({'error': 'No se pudo crear el pago: %s' % e}), 502


@app.route('/api/mp_webhook', methods=['POST'])
def api_mp_webhook():
    """Webhook de MercadoPago: registra el aviso de pago cuando se aprueba."""
    try:
        data = request.get_json(silent=True) or {}
        ext = data.get('external_reference') or ''
        if ext.startswith('cuota-'):
            parts = ext.split('-')
            alumno_id = int(parts[1])
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            hoy = _hoy_academy()
            db = get_db()
            cur = db.execute(
                'INSERT INTO avisos_pago(alumno_id, monto, mes, anio, nota, comprobante, estado, fecha) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (alumno_id, 0, hoy.month, hoy.year, 'Pago por MercadoPago', None, 'pendiente', now))
            db.commit()
            aviso_id = cur.lastrowid
            # notificar a staff
            staff = db.execute("SELECT id FROM users WHERE role IN ('admin','profesor') AND activo=1").fetchall()
            for s in staff:
                notify(s['id'], '🧾 Nuevo pago por MercadoPago',
                       'El alumno pagó por MercadoPago. Revisá y confirmá el aviso #%d.' % aviso_id,
                       'info', push=True)
        return jsonify({'ok': True})
    except Exception as e:
        import traceback as _tb
        print('MP_WEBHOOK_ERROR:', e)
        _tb.print_exc()
        return jsonify({'ok': False, 'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Notificaciones en la app + push
# ---------------------------------------------------------------------------

@app.route('/api/notificaciones')
@login_required
def api_notificaciones():
    u = current_user()
    rows = get_db().execute(
        'SELECT * FROM notificaciones WHERE user_id=? ORDER BY id DESC LIMIT 50',
        (u['id'],)).fetchall()
    no_leidas = get_db().execute(
        'SELECT COUNT(*) AS n FROM notificaciones WHERE user_id=? AND leida=0',
        (u['id'],)).fetchone()['n']
    return jsonify({'notificaciones': [dict(r) for r in rows], 'no_leidas': no_leidas})


@app.route('/api/notificaciones/<int:nid>', methods=['POST'])
@login_required
def api_notificaciones_leer(nid):
    get_db().execute('UPDATE notificaciones SET leida=1 WHERE id=? AND user_id=?',
                     (nid, current_user()['id']))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/notificaciones/leer_todas', methods=['POST'])
@login_required
def api_notificaciones_leer_todas():
    get_db().execute('UPDATE notificaciones SET leida=1 WHERE user_id=?', (current_user()['id'],))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/mensajes/broadcast', methods=['POST'])
@role_required('admin', 'profesor')
def api_mensajes_broadcast():
    data = parse_json()
    texto = txt_str(data.get('texto'))
    if not texto:
        return jsonify({'error': 'Escribe el mensaje para los alumnos'}), 400
    if len(texto) > 500:
        return jsonify({'error': 'El mensaje es muy largo (máx 500 caracteres)'}), 400
    titulo = data.get('titulo') or '📣 Mensaje de la academia'
    quienes = data.get('quienes') or 'alumnos'
    envio = data.get('push', True)
    who = current_user()['nombre']
    alumno_id = to_int(data.get('alumno_id'))
    # Si un aviso/codigo previo dejo la transaccion abortada (Postgres),
    # recuperar la conexion antes de la primera consulta de este request.
    try:
        get_db().execute('ROLLBACK')
    except Exception:
        pass
    if alumno_id:
        rows = get_db().execute("SELECT id, nombre FROM users WHERE id=? AND activo=1", (alumno_id,)).fetchall()
    elif quienes == 'todos':
        rows = get_db().execute("SELECT id, nombre FROM users WHERE activo=1").fetchall()
    else:
        rows = get_db().execute("SELECT id, nombre FROM users WHERE role='alumno' AND activo=1").fetchall()
    enviados = 0
    errores = []
    for r in rows:
        try:
            notify(r['id'], titulo, texto, tipo='mensaje', push=envio)
            enviados += 1
        except Exception as e:
            errores.append({'id': r['id'], 'error': str(e)})
    return jsonify({'ok': True, 'destinatarios': len(rows), 'enviados': enviados, 'errores': errores[:5]})


@app.route('/api/vapid_public_key')
def api_vapid_key():
    return jsonify({'key': ensure_vapid()})


@app.route('/api/push_subscribe', methods=['POST'])
@login_required
def api_push_subscribe():
    data = parse_json()
    sub = data.get('subscription') or {}
    endpoint = sub.get('endpoint')
    keys = sub.get('keys') or {}
    if not endpoint or not keys.get('p256dh') or not keys.get('auth'):
        return jsonify({'error': 'Suscripcion incompleta'}), 400
    uid = current_user()['id']
    try:
        db = get_db()
        existe = db.execute('SELECT id FROM push_subs WHERE endpoint=?', (endpoint,)).fetchone()
        if existe:
            db.execute('UPDATE push_subs SET user_id=?, p256dh=?, auth=? WHERE id=?',
                       (uid, keys['p256dh'], keys['auth'], existe['id']))
        else:
            db.execute('INSERT INTO push_subs(user_id, endpoint, p256dh, auth) VALUES(?,?,?,?)',
                       (uid, endpoint, keys['p256dh'], keys['auth']))
        db.commit()
    except dbadapter.IntegrityError:
        pass
    except Exception as e:
        return jsonify({'error': 'Error al guardar la suscripción: %s' % e}), 500
    return jsonify({'ok': True})


# ---------------------------------------------------------------------------
# Planes del profe
# ---------------------------------------------------------------------------

def _semana_actual():
    hoy = _hoy_academy()
    lunes = hoy - timedelta(days=hoy.weekday())
    return lunes, lunes + timedelta(days=6)


def _planes_semana(semana=None, user_id=None):
    """Planes de la semana (por defecto la actual). Para alumno filtra por su categoría/cinturón."""
    lunes, domingo = _semana_actual()
    if semana:
        try:
            f = datetime.strptime(str(semana), '%Y-%m-%d').date()
            lunes = f - timedelta(days=f.weekday())
            domingo = lunes + timedelta(days=6)
        except Exception:
            lunes, domingo = _semana_actual()
    db = get_db()
    rows = db.execute(
        """SELECT p.*, u.nombre AS autor_nombre FROM planes p
           LEFT JOIN users u ON u.id=p.autor_id
           WHERE p.activo=1 AND p.fecha>=? AND p.fecha<=? ORDER BY p.id DESC""",
        (lunes.strftime('%Y-%m-%d'), domingo.strftime('%Y-%m-%d'))).fetchall()
    hechos = set()
    if user_id:
        hechos = {r['plan_id'] for r in db.execute(
            'SELECT plan_id FROM plan_hecho WHERE user_id=?', (user_id,)).fetchall()}
    planes = []
    perfil = None
    if user_id:
        perfil = get_db().execute('SELECT categoria, cinturon FROM users WHERE id=?', (user_id,)).fetchone()
    for p in rows:
        if user_id and perfil:
            cat_ok = p['categoria'] in ('todos', None, '') or p['categoria'] == perfil['categoria']
            cint_ok = p['cinturon'] in ('todos', None, '') or p['cinturon'] == perfil['cinturon']
            if not (cat_ok and cint_ok):
                continue
        planes.append({
            'id': p['id'], 'titulo': p['titulo'], 'descripcion': p['descripcion'],
            'categoria': p['categoria'], 'cinturon': p['cinturon'], 'fecha': p['fecha'],
            'autor': p['autor_nombre'], 'hecho': p['id'] in hechos,
        })
    return {'semana_inicio': lunes.strftime('%Y-%m-%d'), 'semana_fin': domingo.strftime('%Y-%m-%d'),
            'planes': planes}


@app.route('/api/planes')
@login_required
def api_planes():
    u = current_user()
    if u['role'] == 'alumno':
        return jsonify(_planes_semana(user_id=u['id']))
    semana = request.args.get('semana')
    return jsonify(_planes_semana(semana=semana))


@app.route('/api/planes', methods=['POST'])
@role_required('admin', 'profesor')
def api_planes_crear():
    data = parse_json()
    titulo = txt_str(data.get('titulo'))
    if not titulo:
        return jsonify({'error': 'El título es obligatorio'}), 400
    lunes, _ = _semana_actual()
    try:
        if data.get('fecha'):
            f = datetime.strptime(str(data['fecha']), '%Y-%m-%d').date()
            lunes = f - timedelta(days=f.weekday())
    except Exception:
        pass
    get_db().execute(
        'INSERT INTO planes(titulo, descripcion, categoria, cinturon, fecha, autor_id, creado) VALUES(?,?,?,?,?,?,?)',
        (titulo, txt_str(data.get('descripcion')),
         data.get('categoria') or 'todos', data.get('cinturon') or 'todos',
         lunes.strftime('%Y-%m-%d'), current_user()['id'],
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/planes/<int:pid>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_planes_update(pid):
    data = parse_json()
    p = get_db().execute('SELECT * FROM planes WHERE id=?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': 'Plan no encontrado'}), 404
    get_db().execute(
        'UPDATE planes SET titulo=?, descripcion=?, categoria=?, cinturon=?, fecha=? WHERE id=?',
        ((data.get('titulo') or p['titulo']), data.get('descripcion', p['descripcion']),
         data.get('categoria', p['categoria']), data.get('cinturon', p['cinturon']),
         data.get('fecha', p['fecha']), pid))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/planes/<int:pid>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_planes_delete(pid):
    p = get_db().execute('SELECT * FROM planes WHERE id=?', (pid,)).fetchone()
    if not p:
        return jsonify({'error': 'Plan no encontrado'}), 404
    get_db().execute('DELETE FROM planes WHERE id=?', (pid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/planes/<int:pid>/hecho', methods=['POST'])
@role_required('alumno')
def api_planes_hecho(pid):
    u = current_user()
    db = get_db()
    # Sin este check, un pid inexistente reventaba con 500 por FK.
    if not db.execute('SELECT 1 FROM planes WHERE id=?', (pid,)).fetchone():
        return jsonify({'error': 'Plan no encontrado'}), 404
    existe = db.execute('SELECT 1 FROM plan_hecho WHERE plan_id=? AND user_id=?', (pid, u['id'])).fetchone()
    if existe:
        db.execute('DELETE FROM plan_hecho WHERE plan_id=? AND user_id=?', (pid, u['id']))
        hecho = False
    else:
        db.execute('INSERT INTO plan_hecho(plan_id, user_id, fecha) VALUES(?,?,?)',
                   (pid, u['id'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
        hecho = True
    db.commit()
    return jsonify({'ok': True, 'hecho': hecho})


# ---------------------------------------------------------------------------
# Settings / admin
# ---------------------------------------------------------------------------

@app.route('/api/settings', methods=['GET'])
@login_required
def api_settings_get():
    u = current_user()
    if u['role'] == 'admin':
        keys = ['academy_name', 'default_cuota', 'due_day', 'cargo_demora_pct', 'academy_code', 'pago_link', 'pago_alias',
                'auto_mensaje', 'auto_inact_dias', 'auto_deuda_dias', 'auto_mensaje_activo', 'logro_asist', 'logro_videos',
                'asis_min_examen', 'mp_access_token', 'wp_numero', 'desc_familiar',
                'desc_familiar2', 'desc_familiar3', 'desc_familiar4', 'tz_offset', 'public_url', 'academy_color']
        return jsonify({k: get_setting(k) for k in keys})
    # Alumno/profesor: solo lo publicable. academy_code permite registrarse como
    # profesor y mp_access_token es una credencial de MercadoPago: no se exponen.
    pub = ['academy_name', 'pago_link', 'pago_alias', 'logro_asist', 'logro_videos',
           'desc_familiar', 'desc_familiar2', 'desc_familiar3', 'desc_familiar4']
    return jsonify({k: get_setting(k) for k in pub})


@app.route('/api/settings', methods=['PUT'])
@role_required('admin')
def api_settings_put():
    data = parse_json()
    # Rangos: sin esto cualquier string se guardaba y despues reventaba al
    # calcular la demora o al mostrar el calendario (due_day=abc, cuota=NaN...).
    numericos = {
        'default_cuota': (0, 10_000_000),
        'due_day': (1, 31),
        'cargo_demora_pct': (0, 100),
        'auto_inact_dias': (0, 3650),
        'auto_deuda_dias': (0, 3650),
        'logro_asist': (0, 1000),
        'logro_videos': (0, 1000),
        'asis_min_examen': (0, 1000),
        'tz_offset': (-12, 14),
    }
    for k, (lo, hi) in numericos.items():
        if k not in data or data[k] is None or data[k] == '':
            continue
        n = to_float(data[k])
        if n is None or not (lo <= n <= hi):
            return jsonify({'error': 'Valor inválido para %s (debe ir de %s a %s)' % (k, lo, hi)}), 400
        data[k] = int(n) if k in ('due_day', 'auto_inact_dias', 'auto_deuda_dias',
                                  'logro_asist', 'logro_videos', 'asis_min_examen', 'tz_offset') else n
    if 'pago_link' in data and data['pago_link'] not in (None, ''):
        link = url_http_valida(data['pago_link'])
        if not link:
            return jsonify({'error': 'El link de pago debe ser una URL http/https válida'}), 400
        data['pago_link'] = link
    if 'pago_link' in data:
        data['pago_link'] = url_http_valida(data['pago_link'])
    for k in ['academy_name', 'default_cuota', 'due_day', 'cargo_demora_pct', 'academy_code', 'academy_color', 'pago_link', 'pago_alias',
              'auto_mensaje', 'auto_inact_dias', 'auto_deuda_dias', 'auto_mensaje_activo', 'logro_asist', 'logro_videos',
              'asis_min_examen', 'mp_access_token', 'wp_numero', 'desc_familiar', 'public_url',
              'desc_familiar2', 'desc_familiar3', 'desc_familiar4', 'tz_offset']:
        if k in data and data[k] is not None:
            if k in ('auto_mensaje_activo',):
                data[k] = 1 if as_bool(data[k]) else 0
            elif not isinstance(data[k], (str, int, float)):
                return jsonify({'error': 'Valor inválido para %s' % k}), 400
            set_setting(k, data[k])
    return jsonify({'ok': True})


@app.route('/api/settings/aplicar_cuota', methods=['POST'])
@role_required('admin')
def api_settings_aplicar_cuota():
    cuota = to_float(get_setting('default_cuota'))
    if not cuota or cuota <= 0:
        return jsonify({'error': 'Configura una cuota mensual valida primero'}), 400
    alumnos = get_db().execute(
        "SELECT id, nombre FROM users WHERE role='alumno' AND activo=1").fetchall()
    for a in alumnos:
        get_db().execute('UPDATE users SET cuota_mensual=? WHERE id=?', (cuota, a['id']))
    get_db().commit()
    who = current_user()['nombre']
    for a in alumnos:
        notify(a['id'], 'Tu cuota cambio',
               f'{who} actualizo tu cuota mensual a ${cuota:,.0f}'.replace(',', '.'),
               'cuota')
    return jsonify({'ok': True, 'alumnos': len(alumnos)})


# ---------------------------------------------------------------------------
# Probar push manualmente (para desarrollo)
# ---------------------------------------------------------------------------

@app.route('/api/test_push', methods=['POST'])
@login_required
def api_test_push():
    u = current_user()
    n = get_db().execute('SELECT COUNT(*) AS n FROM push_subs WHERE user_id=?', (u['id'],)).fetchone()['n']
    if not n:
        return jsonify({'ok': False, 'error': 'Tu dispositivo no está suscrito a notificaciones. Abrí la app, andá a Configuración y tocá "Activar notificaciones" (o recargá la app).'}), 400
    enviados_test = {}
    enviados = send_push(u['id'], '✅ Notificación de prueba',
                         '¡Funciona! Si ves esto, las notificaciones push están activas.',
                         _diag=enviados_test)
    resp = {'ok': True, 'suscripciones': n, 'enviados': enviados}
    if enviados_test.get('error'):
        resp['error'] = enviados_test['error']
        resp['ok'] = False
    return jsonify(resp)


# ---------------------------------------------------------------------------
# QR de asistencia imprimible
# ---------------------------------------------------------------------------

@app.route('/qr_print')
@role_required('admin', 'profesor')
def qr_print():
    return render_template('qr_print.html')


def _url_qr_asistencia():
    """URL que se imprime en el cartel QR.

    Antes usaba request.host_url, que sale del header Host: un host manipulado
    metia el QR_SECRET (el mismo del cartel ya impreso) en un dominio ajeno.
    Ahora se prioriza el setting 'public_url' y el Host solo se acepta si es un
    hostname simple. NO se toca QR_SECRET: el cartel físico ya impreso debe
    seguir funcionando.
    """
    base = (get_setting('public_url', '') or '').strip().rstrip('/')
    if base:
        if not re.match(r'^https?://[A-Za-z0-9.-]+(:\d+)?$', base):
            return None
        return base + '/?qr=1&t=' + QR_SECRET
    host = (request.host or '').strip()
    if not re.match(r'^[A-Za-z0-9.-]+(:\d+)?$', host):
        return None
    return request.host_url + '?qr=1&t=' + QR_SECRET


@app.route('/qr_print.png')
@role_required('admin', 'profesor')
def qr_print_png():
    import qrcode
    url = _url_qr_asistencia()
    if not url:
        return jsonify({'error': 'No se pudo determinar la URL pública. Configurá "public_url" en Ajustes.'}), 500
    img = qrcode.make(url)
    buf = io.BytesIO()
    img.save(buf, 'PNG')
    buf.seek(0)
    return Response(buf.getvalue(), mimetype='image/png',
                    headers={'Cache-Control': 'no-cache'})


# ---------------------------------------------------------------------------

@app.route('/api/exportar_alumnos')
@role_required('admin', 'profesor')
def api_exportar_alumnos():
    rows = get_db().execute(
        """SELECT u.*,
            (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS asistencias,
            (SELECT COUNT(*) FROM pagos p WHERE p.alumno_id=u.id) AS pagos_totales
           FROM users u WHERE u.role IN ('alumno','profesor') AND u.activo=1 ORDER BY u.nombre""").fetchall()

    def cel(v):
        if v is None:
            return ''
        if isinstance(v, (int, float)):
            return str(v)
        return str(v)

    headers = ['Nombre', 'DNI', 'Direccion / Domicilio', 'Telefono', 'Celular 2',
               'Telefono tutor/padre', 'Email/Usuario',
               'Fecha de nacimiento', 'Edad', 'Peso (kg)', 'Categoria', 'Cinturon / Faixa',
               'Modalidad', 'Ficha medica', 'Contacto emergencia', 'Autoriza fotos (menores)',
               'Cuota mensual ($)', 'Estado de pago',
               'Asistencias', 'Total pagos registrados', 'Miembro desde']

    groups = {}
    for r in rows:
        cat = (r['categoria'] or '').lower()
        if cat in ('kids', 'juveniles'):
            aut_foto = 'SI' if r['foto_ok'] else 'NO'
        else:
            aut_foto = 'N/A (adulto)'
        try:
            cs = cuota_status(r)
            estado_label = {
                'al_dia': 'Al dia',
                'por_vencer': 'Por vencer (antes del dia %s)' % (cs.get('due_day') or 10),
                'deuda': 'Deuda',
                'becado': 'Beca (no paga)',
                'profesor': 'Profesor (no paga)',
            }.get(cs['estado'], cs['estado'])
        except Exception:
            estado_label = ''
        gcat = (r['categoria'] or '').strip().lower()
        gkey = 'Juveniles' if gcat == 'juveniles' else ('Kids' if gcat == 'kids' else 'Adultos')
        groups.setdefault(gkey, []).append([
            r['nombre'], r['dni'], r['direccion'], r['tel'], r['tel_2'], r['tel_tutor'],
            r['username'], r['nacimiento'],
            r['edad'], r['peso'], r['categoria'], r['cinturon'], r['gi_pref'],
            r['medic_info'], r['emergency_contact'], aut_foto,
            r['cuota_mensual'], estado_label,
            r['asistencias'], r['pagos_totales'],
            (r['creado'] or '')[:10],
        ])

    from xml.sax.saxutils import escape as xesc

    def x(row):
        return '<row>' + ''.join(f'<c t="inlineStr"><is><t>{xesc(str(c))}</t></is></c>' for c in row) + '</row>'
    def sheet_xml(gs):
        return (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            + x(headers)
            + ''.join(x(r) for r in gs)
            + '</sheetData></worksheet>'
        )

    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"></sst>'
    )

    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="1"><xf/></cellXfs>'
        '</styleSheet>'
    )

    def rels():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '</Relationships>')

    SHEETS = ['Adultos', 'Juveniles', 'Kids']

    def content_types():
        overrides = ''.join(
            '<Override PartName="/xl/worksheets/sheet%d.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' % (i + 1)
            for i in range(len(SHEETS)))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                + overrides
                + '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                + '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                + '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                + '</Types>')

    def workbook():
        sheets = ''.join(
            '<sheet name="%s" sheetId="%d" r:id="rId%d"/>' % (n, i + 1, i + 1)
            for i, n in enumerate(SHEETS))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets>' + sheets + '</sheets></workbook>')

    def workbook_rels():
        ws = ''.join(
            '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet%d.xml"/>' % (i + 1, i + 1)
            for i in range(len(SHEETS)))
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                + ws
                + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>' % (len(SHEETS) + 1)
                + '<Relationship Id="rId%d" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>' % (len(SHEETS) + 2)
                + '</Relationships>')

    def core():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<dc:creator>IKIGAI VIEDMA</dc:creator>'
                '<cp:lastModifiedBy>IKIGAI VIEDMA</cp:lastModifiedBy>'
                '<dcterms:created xsi:type="dcterms:W3CDTF">' + datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ') + '</dcterms:created>'
                '</cp:coreProperties>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types())
        z.writestr('_rels/.rels', rels())
        z.writestr('docProps/core.xml', core())
        z.writestr('xl/workbook.xml', workbook())
        z.writestr('xl/_rels/workbook.xml.rels', workbook_rels())
        for i in range(len(SHEETS)):
            z.writestr('xl/worksheets/sheet%d.xml' % (i + 1), sheet_xml(groups.get(SHEETS[i], [])))
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/sharedStrings.xml', shared)
    buf.seek(0)

    from flask import send_file
    return send_file(buf, as_attachment=True, download_name='alumnos_activos.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/api/exportar_pagos')
@role_required('admin', 'profesor')
def api_exportar_pagos():
    anio = to_int(request.args.get('anio')) or _hoy_academy().year
    db = get_db()
    total_alumnos = db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchone()['n']
    resumen = []
    for mes in range(1, 13):
        pagos = db.execute('SELECT monto FROM pagos WHERE mes=? AND anio=?', (mes, anio)).fetchall()
        cant_pagaron = db.execute(
            'SELECT COUNT(DISTINCT alumno_id) AS n FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchone()['n']
        ingresos = sum((p['monto'] or 0) for p in pagos)
        deudores = max(0, total_alumnos - cant_pagaron)
        resumen.append([
            ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
             'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'][mes - 1],
            ingresos, len(pagos),
            cant_pagaron, deudores,
            str(round(cant_pagaron * 100 / total_alumnos) if total_alumnos else 0) + '%',
            str(round(deudores * 100 / total_alumnos) if total_alumnos else 0) + '%',
        ])
    rows_pagos = db.execute(
        """SELECT p.id, p.fecha, u.nombre AS alumno, p.metodo, p.monto, p.mes, p.anio
           FROM pagos p JOIN users u ON u.id=p.alumno_id
           WHERE p.anio=? ORDER BY p.mes, p.fecha""", (anio,)).fetchall()

    def xenc(v):
        from xml.sax.saxutils import escape as xesc
        return xesc(str(v))

    def xrow(row):
        return '<row>' + ''.join(f'<c t="inlineStr"><is><t>{xenc(c)}</t></is></c>' for c in row) + '</row>'

    def sheet_xml(headers, rows):
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                '<sheetData>' + xrow(headers) + ''.join(xrow(r) for r in rows)
                + '</sheetData></worksheet>')

    headers_resumen = ['Mes', 'Total cobrado ($)', 'Cantidad pagos', 'Alumnos que pagaron',
                       'Deudores', '% pagó', '% morosidad']
    headers_detalle = ['ID', 'Fecha', 'Alumno', 'Método', 'Monto ($)', 'Mes', 'Año']
    sh1 = sheet_xml(headers_resumen, resumen)
    sh2 = sheet_xml(headers_detalle,
                    [[p['id'], p['fecha'], p['alumno'], p['metodo'], p['monto'], p['mes'], p['anio']]
                     for p in rows_pagos])

    shared = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="0" uniqueCount="0"></sst>'
    )
    styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="1"><font><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border/></borders>'
        '<cellStyleXfs count="1"><xf/></cellStyleXfs>'
        '<cellXfs count="1"><xf/></cellXfs>'
        '</styleSheet>'
    )

    def rels():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                '</Relationships>')

    def content_types():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
                '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '</Types>')

    def workbook():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Resumen anual" sheetId="1" r:id="rId1"/>'
                '<sheet name="Detalle pagos" sheetId="2" r:id="rId2"/></sheets></workbook>')

    def workbook_rels():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                '</Relationships>')

    def core():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<dc:creator>IKIGAI VIEDMA</dc:creator>'
                '<cp:lastModifiedBy>IKIGAI VIEDMA</cp:lastModifiedBy>'
                '<dcterms:created xsi:type="dcterms:W3CDTF">' + datetime.now().strftime('%Y-%m-%dT%H:%M:%SZ') + '</dcterms:created>'
                '</cp:coreProperties>')

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml', content_types())
        z.writestr('_rels/.rels', rels())
        z.writestr('docProps/core.xml', core())
        z.writestr('xl/workbook.xml', workbook())
        z.writestr('xl/_rels/workbook.xml.rels', workbook_rels())
        z.writestr('xl/worksheets/sheet1.xml', sh1)
        z.writestr('xl/worksheets/sheet2.xml', sh2)
        z.writestr('xl/styles.xml', styles)
        z.writestr('xl/sharedStrings.xml', shared)
    buf.seek(0)

    from flask import send_file
    return send_file(buf, as_attachment=True, download_name=f'pagos_{anio}.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


import traceback as _tb

try:
    init_db()
except Exception as _e:
    # No tumbar el arranque por errores de migracion/BD al importar:
    # se loguea el error y la app sigue (la DB real ya tiene las tablas).
    print('INIT_DB_IMPORT_ERROR:', _e)
    _tb.print_exc()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)),
            debug=os.environ.get('FLASK_DEBUG', '0') == '1')
