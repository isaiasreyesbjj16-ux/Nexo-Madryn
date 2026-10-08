import gzip
from io import BytesIO
import os
import io
import re
import base64
import json
import queue
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

# Solo texto: comprimir imagenes o video es CPU gastada al pedo (ya van comprimidos).
_COMPRESSIBLE = ('text/', 'application/javascript', 'application/json',
                 'application/xml', 'application/manifest+json', 'image/svg+xml')


def _compress_response(rv):
    try:
        # send_file / send_from_directory llegan con direct_passthrough=True y un
        # body en streaming: hay que materializarlo antes de poder comprimirlo.
        if getattr(rv, 'direct_passthrough', False):
            rv.direct_passthrough = False
        ctype = (rv.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if not ctype.startswith(_COMPRESSIBLE):
            return rv
        if 'gzip' not in (request.headers.get('Accept-Encoding') or '').lower():
            return rv
        data = rv.get_data()
        if len(data) < 700:
            return rv
        buf = BytesIO()
        with gzip.GzipFile(fileobj=buf, mode='wb', compresslevel=6) as f:
            f.write(data)
        rv.set_data(buf.getvalue())
        rv.headers['Content-Encoding'] = 'gzip'
        rv.headers['Content-Length'] = str(len(buf.getvalue()))
        rv.headers['Vary'] = 'Accept-Encoding'
        rv.headers.pop('ETag', None)
    except Exception:
        pass
    return rv

app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', secrets.token_hex(32))
app.config['DATABASE'] = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data.db')
app.config['MAX_CONTENT_LENGTH'] = 150 * 1024 * 1024
# Techo de subida. Sin Supabase Storage configurado el video se guarda como
# base64 en Postgres (~1.33x) y _video_range_response lo carga entero en RAM
# para servir los rangos: con 150MB eso son ~200MB por request y el proceso
# muere en una instancia de 512MB. 50MB es el limite de la rama de Storage,
# asi que todo lo que entra queda en la base sin tocar Supabase.
MAX_VIDEO_BYTES = 50 * 1024 * 1024

# ---------------------------------------------------------------------------
# Supabase Storage (híbrido: videos cortos → Storage; largos → base64 en DB)
# Con SUPABASE_URL + SUPABASE_SERVICE_ROLE_KEY activos, los videos de hasta
# STORAGE_MAX se suben al bucket público y dejan de vivir en la base (menos
# RAM al servirlos y sin hinchar la base). Si falta la config o el bucket
# falla, se conserva el comportamiento viejo (base64 en la DB).
# ---------------------------------------------------------------------------
SUPABASE_URL = (os.environ.get('SUPABASE_URL') or '').rstrip('/')
SUPABASE_KEY = os.environ.get('SUPABASE_SERVICE_ROLE_KEY') or ''
SUPABASE_BUCKET = (os.environ.get('SUPABASE_BUCKET') or 'nexo-madryn-media').strip().lower()
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
QR_SECRET = os.environ.get('QR_SECRET', 'nexo2026-nopuedesmarcardesdecasa')

BELTS_ADULT = ['Blanco', 'Azul', 'Púrpura', 'Marrón', 'Negro']
BELTS_KIDS = ['Gris', 'Amarillo', 'Naranja', 'Verde', 'Blanco']
BELTS_JUV = ['Blanco', 'Gris', 'Amarillo', 'Naranja', 'Verde']
CATEGORIAS = ['adulto', 'juveniles', 'kids']
CINTURONES = set(BELTS_ADULT) | set(BELTS_KIDS) | set(BELTS_JUV)

# ---------------------------------------------------------------------------
# Categorias de competicion BJJ (IBJJF). NO reemplazan a CATEGORIAS (esa sigue
# siendo adulto/juveniles/kids y maneja filtros de video, chat y seguridad de
# menores). La edad de categoria se calcula como anio del torneo - anio de
# nacimiento, sin importar mes ni dia.
# ---------------------------------------------------------------------------
GENEROS = ['M', 'F']

BJJ_DIVISIONES = [
    ('Mighty Mite I', 4), ('Mighty Mite II', 5), ('Mighty Mite III', 6),
    ('Pee Wee I', 7), ('Pee Wee II', 8), ('Pee Wee III', 9),
    ('Junior I', 10), ('Junior II', 11), ('Junior III', 12),
    ('Teen I', 13), ('Teen II', 14), ('Teen III', 15),
    ('Juvenil', 16), ('Adulto', 18),
    ('Master 1', 30), ('Master 2', 36), ('Master 3', 41), ('Master 4', 46),
    ('Master 5', 51), ('Master 6', 56), ('Master 7', 61),
]

BJJ_PESOS = {
    'adulto': {
        'M': {'gi': [('Galo', 57.50), ('Pluma', 64.00), ('Pena', 70.00), ('Leve', 76.00),
                     ('Medio', 82.30), ('Meio-Pesado', 88.30), ('Pesado', 94.30),
                     ('Super Pesado', 100.50), ('Pesadíssimo', None)],
              'nogi': [('Galo', 55.50), ('Pluma', 61.50), ('Pena', 67.50), ('Leve', 73.50),
                       ('Medio', 79.50), ('Meio-Pesado', 85.50), ('Pesado', 91.50),
                       ('Super Pesado', 97.50), ('Pesadíssimo', None)]},
        'F': {'gi': [('Galo', 48.50), ('Pluma', 53.50), ('Pena', 58.50), ('Leve', 64.00),
                     ('Medio', 69.00), ('Meio-Pesado', 74.00), ('Pesado', 79.30),
                     ('Super Pesado', None)],
              'nogi': [('Galo', 46.50), ('Pluma', 51.50), ('Pena', 56.50), ('Leve', 61.50),
                       ('Medio', 66.50), ('Meio-Pesado', 71.50), ('Pesado', 76.50),
                       ('Super Pesado', None)]},
    },
    'juvenil': {
        'M': {'gi': [('Galo', 53.50), ('Pluma', 58.50), ('Pena', 64.00), ('Leve', 69.00),
                     ('Medio', 74.00), ('Meio-Pesado', 79.30), ('Pesado', 84.30),
                     ('Super Pesado', 89.30), ('Pesadíssimo', None)],
              'nogi': [('Galo', 51.50), ('Pluma', 56.50), ('Pena', 61.50), ('Leve', 66.50),
                       ('Medio', 71.50), ('Meio-Pesado', 76.50), ('Pesado', 81.50),
                       ('Super Pesado', 86.50), ('Pesadíssimo', None)]},
        'F': {'gi': [('Galo', 44.30), ('Pluma', 48.30), ('Pena', 52.50), ('Leve', 56.50),
                     ('Medio', 60.50), ('Meio-Pesado', 65.00), ('Pesado', 69.00),
                     ('Super Pesado', None)],
              'nogi': [('Galo', 42.50), ('Pluma', 46.50), ('Pena', 50.50), ('Leve', 54.50),
                       ('Medio', 58.50), ('Meio-Pesado', 62.50), ('Pesado', 66.50),
                       ('Super Pesado', None)]},
    },
}


def bjj_edad_categoria(nacimiento, anio=None):
    """Edad de categoria IBJJF: anio del torneo - anio de nacimiento."""
    if not nacimiento:
        return None, None
    try:
        an_nac = int(str(nacimiento)[:4])
    except (ValueError, TypeError):
        return None, None
    edad = (anio or date.today().year) - an_nac
    division = None
    for nombre, desde in BJJ_DIVISIONES:
        if edad >= desde:
            division = nombre
    return edad, division


def bjj_categoria(nacimiento, peso, genero, gi=True, anio=None):
    """Devuelve la categoria de competicion (edad + peso) para un alumno.

    Devuelve dict con claves ok, edad, division, division_peso, limite,
    genero, gi, motivo (por que no pudo calcular).
    """
    edad, division = bjj_edad_categoria(nacimiento, anio)
    base = {'ok': False, 'edad': edad, 'division': division, 'division_peso': None,
            'limite': None, 'genero': genero or '', 'gi': bool(gi)}
    if not division:
        base['motivo'] = 'Falta la fecha de nacimiento'
        return base
    if genero not in ('M', 'F'):
        base['motivo'] = 'Falta el genero'
        return base
    peso = to_float(peso)
    if not peso or peso <= 0:
        base['motivo'] = 'Falta el peso'
        return base
    grupo = 'juvenil' if division == 'Juvenil' else 'adulto'
    tabla = BJJ_PESOS[grupo][genero]['gi' if gi else 'nogi']
    for nombre, limite in tabla:
        if limite is None or peso <= limite:
            base.update({'ok': True, 'division_peso': nombre, 'limite': limite})
            return base
    return base


TIPOS_CLASE = ['Gi', 'NoGi', 'Kids', 'Juveniles', 'Abierto',
               'Muay Thai', 'MMA', 'Sipalki']
ACTIVIDADES = ['Gi', 'NoGi', 'JJ Kids', 'MMA', 'Muay Thai', 'Sipalki', 'Clase personalizada']
METODOS_PAGO = ['Efectivo', 'Transferencia', 'Débito', 'Crédito', 'Otro']
DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
MESES_NOMBRE = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']

# Reparto de cada cuota: 60% para los profes (en partes iguales entre los que
# dan las actividades del alumno), 30% tatami y academia, 10% administrativo.
# Fijo en el codigo: cambiar aca cambia solo los pagos nuevos.
PCT_PROFES, PCT_TATAMI, PCT_ADMIN = 60, 30, 10
# Nombre visible de cada destino no-profesional en reportes y notificaciones.
DESTINOS_PAGO = {'tatami': 'Tatami y academia', 'administrativo': 'Administrativo'}

# Link de pago online de la academia. Se puede overridear en Ajustes > pago_link.
PAGO_LINK_DEFAULT = ''

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
    if db is None:
        return
    # Conexiones reutilizables (Postgres por hilo) NO se cierran: cerrarlas era
    # justamente lo que costaba ~2.5s por request. Si la conexion quedo colgada
    # por un corte, se descarta para que el proximo request abra una nueva.
    if getattr(db, 'reusable', False):
        try:
            if not db._raw.closed:
                db._raw.rollback()
        except Exception:
            try:
                db._raw.close()
            except Exception:
                pass
            dbadapter._pg_threads.__dict__.pop('conn', None)
        return
    db.close()


SCHEMA = """
-- Marca de version del esquema: si la huella guardada coincide con la del
-- codigo actual, init_db() se salta los ~62 CREATE ... IF NOT EXISTS.
CREATE TABLE IF NOT EXISTS schema_meta (
    clave VARCHAR(64) PRIMARY KEY,
    valor VARCHAR(64)
);

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
    actividades TEXT,
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
    -- Permisos de administrador SIN cambiar el rol principal. Un profesor con
    -- es_admin=1 sigue siendo profesor para todo lo demas (lista de profes,
    -- reparto de cuotas, cuota propia, asistencia) y ademas entra al panel de
    -- administracion. No se usa role='admin' para eso: el CHECK de arriba solo
    -- admite un valor y cambiarlo lo sacaria de todas las listas de profesores.
    es_admin INTEGER DEFAULT 0,
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
    confirmado_fecha TEXT,
    -- Profesor(es) elegidos por el alumno al mandar el comprobante (CSV de IDs).
    -- Cuando hay varios, el 60% de profesores se reparte en partes iguales SOLO
    -- entre los elegidos. NULL = repartir entre quienes dan sus actividades (fallback).
    profesor_ids TEXT,
    -- Profesor elegido por el alumno al mandar el comprobante (NULL = repartir
    -- entre los que dan sus actividades). Va al final para que el ALTER de las
    -- bases viejas agregue la misma columna en la misma posicion.
    profesor_id INTEGER
);

-- Reparto 60/30/10: por cada pago de un alumno, una fila por profesor con el
-- 60% que le toca (partido entre los que dan sus actividades) y dos filas en
-- pago_destino (30% tatami/academia, 10% administrativo). PermiteLiquidar a
-- cada profe lo que le corresponde.
CREATE TABLE IF NOT EXISTS pago_reparto (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pago_id INTEGER NOT NULL REFERENCES pagos(id) ON DELETE CASCADE,
    profesor_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    monto REAL NOT NULL,
    actividad TEXT,
    nota TEXT,
    fecha TEXT
);

-- Destino del dinero que NO se lleva un profesor: 30% tatami/academia y 10%
-- administrativo. Se genera junto con pago_reparto y solo en los pagos que se
-- reparten (la cuota de un profesor, los becados y los ingresos extra no).
CREATE TABLE IF NOT EXISTS pago_destino (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    pago_id INTEGER NOT NULL REFERENCES pagos(id) ON DELETE CASCADE,
    destino TEXT NOT NULL,
    monto REAL NOT NULL,
    fecha TEXT
);

-- Ingresos extra: cobros puntuales para el fondo de la academia (cuota de un
-- dia, ayuda a alumno que compite, seminarios, etc). NO son cuota mensual.
CREATE TABLE IF NOT EXISTS ingresos_extra (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alumno_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    monto REAL NOT NULL,
    concepto TEXT DEFAULT 'Ingreso extra',
    destino TEXT,
    mes INTEGER NOT NULL,
    anio INTEGER NOT NULL,
    metodo TEXT DEFAULT 'Efectivo',
    nota TEXT,
    fecha TEXT,
    registrado_por INTEGER
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
    data TEXT DEFAULT '',
    actividad TEXT
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

-- Torneos: se cargan a mano (no hay integracion con ningun calendario externo).
CREATE TABLE IF NOT EXISTS torneos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    nombre TEXT NOT NULL,
    fecha TEXT,
    ciudad TEXT,
    lugar TEXT,
    tipo TEXT DEFAULT 'IBJJF',
    estado TEXT DEFAULT 'programado',
    descripcion TEXT,
    url TEXT,
    creado_por INTEGER REFERENCES users(id) ON DELETE SET NULL,
    creado TEXT
);

-- Una fila por alumno y torneo: en que categoria compito y que medalla sacó.
CREATE TABLE IF NOT EXISTS torneo_inscripciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    torneo_id INTEGER NOT NULL REFERENCES torneos(id) ON DELETE CASCADE,
    alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    categoria TEXT DEFAULT '',
    medalla TEXT DEFAULT '',
    nota TEXT DEFAULT '',
    creado TEXT,
    UNIQUE (torneo_id, alumno_id)
);

CREATE INDEX IF NOT EXISTS idx_torneos_fecha ON torneos(fecha);
CREATE INDEX IF NOT EXISTS idx_torneo_insc_alumno ON torneo_inscripciones(alumno_id);
CREATE INDEX IF NOT EXISTS idx_torneo_insc_torneo ON torneo_inscripciones(torneo_id);

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


def _partes_schema():
    """(tablas, indices) separados del SCHEMA.

    El orden entre las dos partes importa: los CREATE INDEX se corren despues de
    los ALTER TABLE que agregan las columnas que indexan. Al reves, en una base
    que ya tenia la tabla, CREATE TABLE IF NOT EXISTS no agrega la columna, el
    indice revienta, y como Postgres corre la migracion entera en una sola
    transaccion se pierde tambien todo lo creado antes.
    """
    tablas, indices = [], []
    for stmt in dbadapter._split(SCHEMA):
        (indices if stmt.strip().upper().startswith('CREATE INDEX') else tablas).append(stmt)
    return tablas, indices


def _tablas_sql():
    return '\n'.join(_partes_schema()[0])


def _indices_sql():
    return '\n'.join(_partes_schema()[1])


def _schema_version():
    """Huella del esquema + de las migraciones.

    Sale del source de _init_db_body y de SCHEMA, asi que cualquier cambio en una
    migracion futura invalida la version solo: no hay que acordarse de bumpear un
    numero a mano. Si no se puede leer el source (deploy empaquetado, zip, etc)
    devuelve None y se corre todo, que es el comportamiento de siempre.
    """
    import hashlib
    import inspect
    try:
        src = inspect.getsource(_init_db_body) + SCHEMA
    except Exception:
        return None
    return hashlib.sha256(src.encode('utf-8')).hexdigest()[:16]


def _schema_version_stored(db):
    """Version guardada, o None si todavia no se migro.

    Cuidado con Postgres: si el SELECT falla porque schema_meta no existe, la
    transaccion queda abortada y TODO lo que se corra despues en la misma
    conexion revienta con InFailedSqlTransaction. Por eso se corta la
    transaccion aca: si no, la migracion no puede ni crear la tabla que este
    mismo probe esta buscando.
    """
    try:
        row = db.execute("SELECT valor FROM schema_meta WHERE clave='version'").fetchone()
    except Exception as e:
        _log.warning('schema_meta no se pudo leer (%s): se migra de cero', type(e).__name__)
        try:
            db.rollback()
        except Exception:
            pass
        return None
    if not row:
        return None
    try:
        return row['valor']
    except Exception:
        return row[0]


import logging as _logging

# Un solo logger para toda la app. Con print() el error de migracion salia a
# stdout y Render lo bufferbeaba: la app levantaba igual y el motivo del fallo
# no aparecia en ningun lado.
_log = _logging.getLogger('nexo')
if not _logging.getLogger().handlers:
    _logging.basicConfig(
        level=_logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s')


def init_db():
    # Si una migracion agrega o quita columnas, el cache de columnas de users
    # quedo desactualizado; se recalcula en el proximo request.
    _cols_de_users.clear()
    if DB_MODE == 'postgres':
        db = dbadapter.connect_postgres()
    elif DB_MODE == 'mysql':
        db = dbadapter.connect_mysql()
    else:
        db = dbadapter.connect_sqlite(app.config['DATABASE'])
    # try/finally: antes la conexion solo se cerraba al final del camino feliz y
    # cualquier error en una migracion la dejaba abierta (fuga + pool agotado).
    try:
        ver = _schema_version()
        # Contra Postgres/MySQL los ~62 CREATE ... IF NOT EXISTS viajan uno por
        # uno (executescript no batchea), o sea ~62 round-trips en cada arranque.
        # Con la version ya aplicada nos salteamos todo eso.
        if ver and ver == _schema_version_stored(db):
            return
        _init_db_body(db)
        if ver:
            try:
                db.execute('INSERT OR REPLACE INTO schema_meta(clave, valor) VALUES(?,?)',
                           ('version', ver))
                db.commit()
            except Exception as e:
                # NO se traga en silencio: sin la version guardada la migracion
                # entera se re-corre en cada arranque.
                _log.error('no se pudo guardar la version del esquema %s: %s', ver, e)
    finally:
        try:
            db.close()
        except Exception:
            pass


def _columnas_de(c, tabla):
    """Nombres de columnas de una tabla, en el dialecto que toque."""
    if DB_MODE == 'postgres':
        return [r[0] for r in c.execute(
            "SELECT column_name AS name FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name=?", (tabla,)).fetchall()]
    if DB_MODE == 'mysql':
        return [r[0] for r in c.execute('SHOW COLUMNS FROM ' + tabla).fetchall()]
    return [r[1] for r in c.execute('PRAGMA table_info(%s)' % tabla).fetchall()]


_TIPOS_COL = ('TEXT', 'INTEGER', 'REAL', 'VARCHAR', 'NUMERIC', 'DECIMAL', 'BLOB', 'DATE', 'BOOLEAN')
_NOMBRES_IGNORAR = ('PRIMARY', 'FOREIGN', 'UNIQUE', 'CHECK', 'CONSTRAINT', 'KEY', 'INDEX')


def _columnas_declaradas():
    """{tabla: {columna: tipo}} segun lo que el propio SCHEMA declara.

    Se usa para alinear las tablas viejas con el SCHEMA actual. Si no, cualquier
    columna que se haya agregado al CREATE TABLE despues (videos.belt, por
    ejemplo) volaba: el CREATE TABLE IF NOT EXISTS no hace nada en una tabla que
    ya existe y el indice que la referencia revienta con UndefinedColumn.
    """
    out = {}
    for tabla, cuerpo in re.findall(
            r'CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\)', SCHEMA, re.S):
        cols = {}
        for linea in cuerpo.split('\n'):
            linea = linea.strip().rstrip(',')
            if not linea or linea.startswith('--'):
                continue
            palabras = linea.split()
            nombre = palabras[0]
            if nombre.upper() in _NOMBRES_IGNORAR or '(' in nombre:
                continue
            resto = ' '.join(palabras[1:])
            tipo = 'TEXT'
            for p in palabras[1:]:
                p = p.strip('()').upper()
                if p in _TIPOS_COL:
                    tipo = 'VARCHAR(255)' if p == 'VARCHAR' else p
                    break
                if p in _NOMBRES_IGNORAR:
                    tipo = None
                    break
            if tipo is None:
                continue
            cols[nombre] = tipo
        if cols:
            out[tabla] = cols
    return out


def _alinear_columnas(c):
    """Agrega a cada tabla las columnas que el SCHEMA ya declara y faltan."""
    for tabla, cols in _columnas_declaradas().items():
        try:
            existentes = set(_columnas_de(c, tabla))
        except Exception:
            continue
        for nombre, tipo in cols.items():
            if nombre in existentes:
                continue
            try:
                c.execute('ALTER TABLE %s ADD COLUMN %s %s' % (tabla, nombre, tipo))
            except Exception as e:
                _log.warning('no se pudo agregar %s.%s (%s): %s', tabla, nombre, tipo, e)


def _migrar_columnas(c):
    """Agrega las columnas que se fueron sumando despues del CREATE TABLE.

    Corre ANTES que los CREATE INDEX del SCHEMA: si un indice referenciara una
    de estas columnas en una base ya vieja, reventaria con UndefinedColumn y,
    como Postgres corre toda la migracion en una sola transaccion, se perderian
    tambien las tablas que se acababan de crear. Asi fue comoGI 'torneos' nunca
    llego a existir en produccion.
    """
    cols = _columnas_de(c, 'users')
    if 'foto' not in cols:
        c.execute('ALTER TABLE users ADD COLUMN foto TEXT')
    for col, ddl in [('tel', 'TEXT'), ('nacimiento', 'TEXT'), ('medic_info', 'TEXT'), ('emergency_contact', 'TEXT'),
                     ('security_q', 'TEXT'), ('security_a', 'TEXT'), ('tel_tutor', 'TEXT'), ('tel_2', 'TEXT'),
                     ('direccion', 'TEXT'), ('dni', 'TEXT'), ('foto_ok', 'INTEGER'),
                     ('acepto_tyc', 'TEXT'), ('proximo_examen', 'TEXT'), ('notas_internas', 'TEXT'),
                     ('medic_enfermedades', 'TEXT'), ('medic_alergias', 'TEXT'), ('medic_medicacion', 'TEXT'),
                     ('medic_lesiones', 'TEXT'), ('ficha_fecha', 'TEXT'),
                     ('firma_tyc', 'TEXT'), ('firma_foto', 'TEXT'), ('firma_fecha', 'TEXT'),
                     ('pausa_desde', 'TEXT'), ('pausa_hasta', 'TEXT'), ('beca', 'INTEGER DEFAULT 0'),
                      ('actividades', 'TEXT'), ('genero', "TEXT DEFAULT ''"),
                      ('bjj_categoria', "TEXT DEFAULT ''"),
                      ('es_admin', 'INTEGER DEFAULT 0')]:
        if col not in cols:
            c.execute('ALTER TABLE users ADD COLUMN %s %s' % (col, ddl))
    if 'recordado' not in _columnas_de(c, 'eventos'):
        c.execute('ALTER TABLE eventos ADD COLUMN recordado INTEGER DEFAULT 0')
    if 'comprobante' not in _columnas_de(c, 'avisos_pago'):
        c.execute('ALTER TABLE avisos_pago ADD COLUMN comprobante TEXT')
    if 'profesor_id' not in _columnas_de(c, 'avisos_pago'):
        c.execute('ALTER TABLE avisos_pago ADD COLUMN profesor_id INTEGER')
    if 'profesor_ids' not in _columnas_de(c, 'avisos_pago'):
        c.execute('ALTER TABLE avisos_pago ADD COLUMN profesor_ids TEXT')
    v_cols = _columnas_de(c, 'videos')
    if 'data' not in v_cols:
        c.execute('ALTER TABLE videos ADD COLUMN data TEXT')
    if 'categoria' not in v_cols:
        c.execute('ALTER TABLE videos ADD COLUMN categoria TEXT DEFAULT \'adulto\'')
    if 'actividad' not in v_cols:
        c.execute('ALTER TABLE videos ADD COLUMN actividad TEXT')
    cm_cols = _columnas_de(c, 'chat_messages')
    if 'adjunto' not in cm_cols:
        c.execute('ALTER TABLE chat_messages ADD COLUMN adjunto TEXT')
    if 'adjunto_tipo' not in cm_cols:
        c.execute('ALTER TABLE chat_messages ADD COLUMN adjunto_tipo TEXT')
    if 'link' not in _columnas_de(c, 'notificaciones'):
        c.execute('ALTER TABLE notificaciones ADD COLUMN link TEXT DEFAULT \'\'')


def _init_db_body(db):
    # Orden: CREATE TABLE -> ALTER TABLE (columnas nuevas) -> CREATE INDEX.
    db.executescript(_tablas_sql())
    c = db.cursor()
    _migrar_columnas(c)
    _alinear_columnas(c)
    try:
        c.execute('UPDATE users SET beca=0 WHERE beca IS NULL')
    except Exception as e:
        # En Postgres un UPDATE fallido ABORTA la transaccion: todo lo que se
        # corra despues revienta con InFailedSqlTransaction y el error real queda
        # enmascarado. Por eso se loguea y no se traga en silencio.
        _log.exception('no se pudo normalizar users.beca: %s', e)
        try:
            db.rollback()
        except Exception:
            pass
    db.executescript(_indices_sql())
    defaults = {
        'academy_name': 'NEXO MADRYN JIU JITSU',
        'academy_code': 'NEXO2026',
        'default_cuota': '15000',
        'precio_act_1': '45000',
        'precio_act_2': '60000',
        'precio_act_3': '80000',
        'due_day': '10',
        'cargo_demora_pct': '10',
        'academy_color': '#9b5de5',
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
        # La contrasena del admin NUNCA va hardcodeada: sale de la variable de
        # entorno ADMIN_PASSWORD y, si no esta, se genera una al azar. El repo es
        # publico, asi que una clave fija en el codigo seria un regalo para cualquiera
        # que lo lea.
        clave = os.environ.get('ADMIN_PASSWORD') or secrets.token_urlsafe(12)
        c.execute(
            "INSERT INTO users(username, password_hash, role, nombre) VALUES(?,?,?,?)",
            ('admin', generate_password_hash(clave), 'admin', 'Administrador'))
        _log.critical('ADMIN CREADO usuario=admin contrasena=%s (se muestra una sola vez, guardala)', clave)
    # Migracion de datos, una sola vez: Sebastian Torres queda con los dos roles.
    # El rol principal sigue siendo 'profesor' (lista de profesores, reparto,
    # cuota y asistencia no cambian); es_admin solo suma los permisos de admin.
    # Va DESPUES de crear el admin para que en una base nueva tambien quede con
    # es_admin=1.
    promo = c.execute("SELECT value FROM settings WHERE k='_doble_rol_sebastian'").fetchone()
    if not (promo and promo['value']):
        c.execute("UPDATE users SET es_admin=1 WHERE nombre='Sebastian Torres'")
        c.execute("UPDATE users SET es_admin=1 WHERE role='admin'")
        c.execute(
            "INSERT OR IGNORE INTO settings(k, value) VALUES('_doble_rol_sebastian','1')")
    db.commit()


_SETTING_MISS = object()


def get_setting(key, default=None):
    cache = getattr(g, '_settings_cache', None)
    if cache is None:
        cache = {}
        g._settings_cache = cache
    if key not in cache:
        row = get_db().execute('SELECT value FROM settings WHERE k=?', (key,)).fetchone()
        cache[key] = row['value'] if row else _SETTING_MISS
    val = cache[key]
    return default if val is _SETTING_MISS else val


def set_setting(key, value):
    get_db().execute(
        'INSERT OR REPLACE INTO settings(k, value) VALUES(?,?)',
        (key, str(value)))
    get_db().commit()
    cache = getattr(g, '_settings_cache', None)
    if cache is not None:
        cache.pop(key, None)


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


_VAPID_KEY_CACHE = {}


def ensure_vapid():
    """Claves VAPID persistentes en la BD (Render pierde archivos al redeployar).

    Devuelve la clave publica como applicationServerKey. Todo se cachea en
    memoria: antes escribia las DOS claves en settings (2 commits) y reescribia
    los archivos locales en cada llamada, y /api/vapid_public_key es el health
    check de Render, asi que pasaba a hacer 2 commits cada ~30 segundos. Eso
    mantenia el compute de Neon activo 24/7 y agotaba los 100 CU-hours del plan
    free en ~17 dias. Si el proceso acaba de arrancar (o cambio el formato),
    ahi si se persiste.
    """
    cached = _VAPID_KEY_CACHE.get('key')
    if cached:
        return cached
    primera = not _VAPID_KEY_CACHE
    priv = get_setting('vapid_private')
    pub = get_setting('vapid_public')
    escribir = False
    if not _vapid_valida(priv, pub):
        priv, pub = _generar_vapid()
        escribir = True
    else:
        # normalizar el formato por si quedo raro en una version anterior
        try:
            npriv, npub = _vapid_reconstruida(priv, pub)
            if (npriv, npub) != (priv, pub):
                priv, pub = npriv, npub
                escribir = True
        except Exception:
            pass
    if escribir:
        set_setting('vapid_private', priv)
        set_setting('vapid_public', pub)
    # archivos locales para pywebpush: una sola vez por arranque (Render usa
    # filesystem efimero, asi que reescribirlos a cada rato no sirve de nada).
    if escribir or primera:
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
        key = base64.urlsafe_b64encode(raw_point).rstrip(b'=').decode()
    except Exception:
        pem = pub.replace('-----BEGIN PUBLIC KEY-----', '').replace('-----END PUBLIC KEY-----', '').strip()
        try:
            key = base64.urlsafe_b64encode(base64.b64decode(pem)).rstrip(b'=').decode()
        except Exception:
            key = pem
    _VAPID_KEY_CACHE['key'] = key
    return key


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


_PUSH_COLA = {'q': None, 'hilo': None}
_PUSH_LOCK = threading.Lock()


def _push_worker(q):
    while True:
        item = q.get()
        if item is None:
            return
        user_id, titulo, mensaje, extra = item
        try:
            with app.app_context():
                send_push(user_id, titulo, mensaje, extra=extra)
        except Exception as e:
            _log.debug('push diferido fallo: %s', e)


def _push_async(user_id, titulo, mensaje, extra):
    with _PUSH_LOCK:
        if _PUSH_COLA['q'] is None:
            _PUSH_COLA['q'] = queue.Queue()
        hilo = _PUSH_COLA['hilo']
        if hilo is None or not hilo.is_alive():
            hilo = threading.Thread(target=_push_worker, args=(_PUSH_COLA['q'],), daemon=True)
            hilo.start()
            _PUSH_COLA['hilo'] = hilo
        _PUSH_COLA['q'].put((user_id, titulo, mensaje, extra))


def notify(user_id, titulo, mensaje, tipo='info', push=True, link=None):
    now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    if link is None:
        link = _NOTIF_LINK_POR_TIPO.get(tipo, '')
    get_db().execute(
        'INSERT INTO notificaciones(user_id, titulo, mensaje, tipo, fecha, link) VALUES(?,?,?,?,?,?)',
        (user_id, titulo, mensaje, tipo, now, link))
    get_db().commit()
    if push:
        _push_async(user_id, titulo, mensaje,
                    {'url': '/app?sec=' + link if link else '/app'})


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
            ("SELECT %s FROM users u WHERE u.role IN ('alumno','profesor') AND u.activo=1"
             " AND u.cuota_mensual IS NOT NULL AND (u.beca IS NULL OR u.beca=0)"
             " AND NOT EXISTS (SELECT 1 FROM pagos p WHERE p.alumno_id=u.id AND p.mes=? AND p.anio=?)"
             % columnas_de_users('u.')),
            (hoy.month, hoy.year)).fetchall()
        enviados = 0
        for d in deudores:
            # La pausa se chequea en el loop porque necesita las columnas del
            # usuario. Y el deudor se confirma con cuota_status, no solo por "no
            # tiene pago": asi se respeta el mismo criterio que la lista de
            # deudores y el panel no contradicen al aviso que se acabo de mandar.
            if en_pausa(d):
                continue
            if cuota_status(d)['estado'] not in ('deuda', 'por_vencer'):
                continue
            try:
                notify(d['id'], '💸 Recordatorio de cuota',
                       'Tu cuota de %d/%d está pendiente. Pagala cuando puedas.' % (hoy.month, hoy.year),
                       'cuota', push=True)
                enviados += 1
            except Exception as e:
                _log.warning('no se pudo avisar la cuota a %s: %s', d['id'], e)
        set_setting('aviso_cuota_%d_%d' % (hoy.year, hoy.month), '1')
        return enviados
    except Exception as e:
        _log.exception('AVISO_CUOTA_ERROR: %s', e)
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
        _log.exception('AVISO_EVENTOS_ERROR: %s', e)
        return 0


def aviso_renovacion():
    """Si un alumno o profesor llega al minimo de asistencias configurado
    (asis_min_examen), avisa SOLO al staff (admin/profesores) que puede sugerir
    examen. Flag por alumno evita repetir; se reset cuando el staff entrega el
    nuevo grado. Los profesores entrenan y pagan cuota, asi que tambien rinden."""
    try:
        min_asist = to_int(get_setting('asis_min_examen', '30')) or 30
        if min_asist <= 0:
            return 0
        db = get_db()
        alumnos = db.execute(
            "SELECT u.id, u.nombre, u.cinturon, COUNT(a.id) AS n "
            "FROM users u LEFT JOIN asistencia a ON a.alumno_id=u.id "
            "WHERE u.role IN ('alumno','profesor') AND u.activo=1 "
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
        _log.exception('AVISO_RENOVACION_ERROR: %s', e)
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

# Columnas binarias de users: foto de perfil y firmas de TyC/autorizacion, que
# se guardan como data-URL (el canvas se sube entero como PNG). Son decenas de
# KB por fila y SELECT * las traia en cada request autenticado y en cada
# listado: ese era el otro foco del agotamiento del egress de Neon.
_BLOBS_DE_USERS = ('foto', 'firma_tyc', 'firma_foto')
_cols_de_users = []


def columnas_de_users(pref=''):
    """Columnas de users sin los blobs, con prefijo opcional ('u.' o '')."""
    if not _cols_de_users:
        desc = get_db().execute('SELECT * FROM users LIMIT 0').description
        _cols_de_users.append([_nombre_columna(c) for c in desc])
    return ','.join(pref + c for c in _cols_de_users[0] if c not in _BLOBS_DE_USERS)


def _nombre_columna(c):
    # sqlite3 devuelve tuplas, psycopg3 Column con .name.
    n = getattr(c, 'name', None)
    return n if isinstance(n, str) else c[0]


def current_user():
    if 'user_id' not in session:
        return None
    return get_db().execute(
        'SELECT %s FROM users WHERE id=?' % columnas_de_users(),
        (session['user_id'],)).fetchone()


def login_required(f):
    from functools import wraps
    @wraps(f)
    def wrapper(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'No autorizado'}), 401
        return f(*args, **kwargs)
    return wrapper


def es_admin(u):
    """True si el usuario es administrador (rol principal o permiso extra).

    El rol principal NO cambia: un profesor con es_admin=1 sigue siendo
    'profesor' para todo lo demas (lista de profesores, reparto de cuotas,
    cuota propia, asistencia y listado de alumnos) y ademas entra al panel de
    administracion. Asi no se lo saca de ninguna lista que usa role='profesor'.
    """
    if not u:
        return False
    try:
        if u['role'] == 'admin':
            return True
    except Exception:
        return False
    try:
        return bool(u['es_admin'] if 'es_admin' in u.keys() else 0)
    except Exception:
        return False


def _cumple_rol(u, roles):
    if u['role'] in roles:
        return True
    return 'admin' in roles and es_admin(u)


def role_required(*roles):
    def deco(f):
        from functools import wraps
        @wraps(f)
        def wrapper(*args, **kwargs):
            if 'user_id' not in session:
                return jsonify({'error': 'No autorizado'}), 401
            u = current_user()
            if not _cumple_rol(u, roles):
                return jsonify({'error': 'Sin permisos'}), 403
            return f(*args, **kwargs)
        return wrapper
    return deco


def parse_json():
    """Devuelve el body como dict, o {} si no lo es.

    Antes tiraba el body entero sin avisar cuando no era un objeto, y por eso
    un cliente que manda el JSON ya serializado (o un string) se comia un 400
    de "falta X" sin decir que el problema era el formato del cuerpo entero.
    """
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return data
    if data is not None:
        _log.warning('%s %s: body JSON que no es un objeto (%s), se trata como vacio',
                     request.method, request.path, type(data).__name__)
    return {}


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


def col(r, name, default=None):
    """Lee una columna de una fila sin reventar si la columna no existe o es NULL.

    Hace falta porque una fila es un objeto distinto segun el driver: sqlite3.Row
    tira IndexError si la columna no esta (y no KeyError), el Row de dbadapter KeyError.
    Con una base creada por una version vieja del app la columna puede directamente
    no existir, y `r['tipo']` tumbaba el endpoint entero con un 500.
    """
    try:
        v = r[name]
    except (KeyError, IndexError, TypeError):
        return default
    return default if v is None else v


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


def _genero_de(data, u):
    """Genero ('M', 'F' o '') normalizado, tolerando que falte la columna."""
    g = (data.get('genero') or (u['genero'] if 'genero' in u.keys() else '') or '').strip().upper()[:1]
    return g if g in ('M', 'F') else ''


def user_public(u):
    return {
        'id': u['id'],
        'username': u['username'],
        'role': u['role'],
        'es_admin': bool(u['es_admin'] if 'es_admin' in u.keys() else 0),
        'nombre': u['nombre'],
        'edad': u['edad'],
        'peso': u['peso'],
        'cinturon': u['cinturon'],
        'categoria': u['categoria'],
        'genero': (u['genero'] if 'genero' in u.keys() else '') or '',
        'bjj': bjj_categoria(
            u['nacimiento'] if 'nacimiento' in u.keys() else None,
            u['peso'] if 'peso' in u.keys() else None,
            (u['genero'] if 'genero' in u.keys() else '') or ''),
        'gi_pref': u['gi_pref'],
        'bjj_categoria': (u['bjj_categoria'] or '') if 'bjj_categoria' in u.keys() else '',
        'actividades': (u['actividades'] or '') if 'actividades' in u.keys() else '',
        'cuota_mensual': u['cuota_mensual'],
        'foto': (u['foto'] if ('foto' in u.keys() and u['foto'])
                 else '/api/avatar/%s' % u['id']),
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


def _recuperar_conexion():
    """Deja sana la transaccion de la conexion del request.

    En Postgres, un error dentro de una transaccion la ABORTA: todo lo que se
    corra despues en esa conexion falla con InFailedSqlTransaction hasta que se
    haga ROLLBACK. Estos tres ROLLBACK a mano eran el parche; con DB.rollback()
    queda en un solo lugar y no hay que acordarse de repetirlo.
    """
    if DB_MODE == 'sqlite':
        return
    try:
        get_db().rollback()
    except Exception as e:
        _log.debug('rollback de recuperación: %s', e)


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
            _recuperar_conexion()
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


def _pagos_del_mes(mes, anio):
    """Mapa alumno_id -> ultimo pago del mes, resuelto en una sola query.

    Reemplaza la query por alumno que hacian cuota_status y dias_deuda cuando
    se listan todos: con N alumnos eran 2N queries.
    """
    out = {}
    for p in get_db().execute('SELECT * FROM pagos WHERE mes=? AND anio=? ORDER BY id',
                              (mes, anio)).fetchall():
        out[p['alumno_id']] = p
    return out


def cuota_status(alumno, pagos_mes=None):
    """Estado de la cuota del alumno en el mes actual."""
    hoy = _hoy_academy()
    if pagos_mes is None:
        pago = get_db().execute(
            'SELECT * FROM pagos WHERE alumno_id=? AND mes=? AND anio=? ORDER BY id DESC LIMIT 1',
            (alumno['id'], hoy.month, hoy.year)).fetchone()
    else:
        pago = pagos_mes.get(alumno['id'])
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


def _vencimiento(mes, anio):
    """Día de vencimiento de la cuota de un mes (due_day, con fallback)."""
    due_day = to_int(get_setting('due_day', '10')) or 10
    try:
        return date(anio, mes, due_day)
    except ValueError:
        # si due_day no existe (ej 31 en feb) usar último día del mes
        import calendar
        last = calendar.monthrange(anio, mes)[1]
        return date(anio, mes, last)


def _vencido(mes, anio, fecha=None):
    """True si la cuota de ese mes ya pasó su vencimiento."""
    return (fecha or _hoy_academy()) > _vencimiento(mes, anio)


def calcular_demora(monto, mes=None, anio=None, fecha=None):
    """Aplica el cargo por pago con demora (después del día de vencimiento).

    Devuelve (monto_base, cargo, monto_final). Sin cargo si no corresponde.
    """
    base = monto or 0
    if base <= 0:
        return base, 0, base
    hoy = fecha or _hoy_academy()
    pct = to_float(get_setting('cargo_demora_pct', '10')) or 0
    # Mes objetivo del pago (por defecto el mes actual)
    tmes = mes or hoy.month
    tanio = anio or hoy.year
    venc = _vencimiento(tmes, tanio)
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


def dias_deuda(alumno, pagos_mes=None):
    """Días de atraso de la cuota: 0 si está al día, y 0 también si está en pausa.

    Antes usaba la fecha del último pago, así que un alumno que pagó bien en
    febrero figuraba "al día" en abril y nunca llegaba al umbral de deuda. Y si
    nunca había pagado devolvía los días desde que se creó la cuenta, que no
    tienen nada que ver con el vencimiento de la cuota.
    """
    hoy = _hoy_academy()
    try:
        if int(alumno['beca'] or 0):
            return 0
    except (KeyError, IndexError, TypeError):
        pass
    if en_pausa(alumno):
        return 0
    due_day = to_int(get_setting('due_day', '10')) or 10

    # Solo importa el mes en curso, igual que en cuota_status: si esta pagado,
    # esta al dia. Asi el numero de dias que muestra el panel no contradice el
    # estado, y un alumno que pago el mes vigente no queda marcado con la deuda
    # de un mes anterior que ya se regularizo o se condono.
    if pagos_mes is not None:
        pago = pagos_mes.get(alumno['id'])
    else:
        pago = get_db().execute(
            'SELECT id FROM pagos WHERE alumno_id=? AND mes=? AND anio=? LIMIT 1',
            (alumno['id'], hoy.month, hoy.year)).fetchone()
    if pago:
        return 0
    try:
        venc = date(hoy.year, hoy.month, due_day)
    except ValueError:
        import calendar
        venc = date(hoy.year, hoy.month, calendar.monthrange(hoy.year, hoy.month)[1])
    return max(0, (hoy - venc).days)


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
        "SELECT %s FROM users WHERE role IN ('alumno','profesor') AND activo=1"
        % columnas_de_users()).fetchall()
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
    return render_template('login.html', academy_name=get_setting('academy_name'),
                           actividades=ACTIVIDADES,
                           precio_1=to_float(get_setting('precio_act_1')) or 45000,
                           precio_2=to_float(get_setting('precio_act_2')) or 60000,
                           precio_3=to_float(get_setting('precio_act_3')) or 80000)


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
                           dias=DIAS, academy_name=get_setting('academy_name'),
                           actividades=ACTIVIDADES,
                           precio_1=_precio_actividad(1), precio_2=_precio_actividad(2),
                           precio_3=_precio_actividad(3))


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
# Link de Instagram (aparece arriba, abajo y en contacto).
INSTAGRAM_URL = 'https://www.instagram.com/madrynjiujitsu/'
INSTAGRAM_USUARIO = '@madrynjiujitsu'
#  Direccion de la academia.
DIRECCION = 'San Martín 1310, Puerto Madryn, Chubut, Argentina'
#  WhatsApp para consultas: solo numeros con prefijo internacional, sin + ni espacios.
#  Ej: '54292123456789'. Si queda vacio, el boton de WhatsApp NO se muestra.
WHATSAPP_NUMERO = ''
#  ###############################################################################


def _actividades_csv(data):
    """Convierte actividades (lista JSON o 'a,b,c' o None) en CSV sin duplicados."""
    raw = data.get('actividades')
    items = []
    if isinstance(raw, list):
        items = raw
    elif isinstance(raw, str) and raw.strip():
        items = [x.strip() for x in raw.split(',')]
    seen = []
    for x in items:
        x = x.strip()
        if x and x not in seen:
            seen.append(x)
    return ','.join(seen)


def _cuota_por_actividades(csv_act):
    """Cuota segun cuantas actividades entrena el alumno (1 profe=45, 2=60, 3+=80).

    Usa los precios configurables precio_act_1/2/3 (en pesos) con el precio por
    defecto como respaldo. Si hay 3 o mas actividades aplica el precio_act_3.
    """
    n = len([a for a in (csv_act or '').split(',') if a.strip()])
    p1 = to_float(get_setting('precio_act_1')) or 0
    p2 = to_float(get_setting('precio_act_2')) or 0
    p3 = to_float(get_setting('precio_act_3')) or 0
    if n <= 0:
        return None
    if n == 1 and p1:
        return p1
    if n == 2 and p2:
        return p2
    if n >= 3 and p3:
        return p3
    return None


def _precio_actividad(n):
    """Precio de la cuota segun la cantidad de actividades (1, 2 o 3+).

    Mismo criterio que _cuota_por_actividades pero devolviendo siempre un numero
    (sin None), para poder mostrarlo en pantallas aunque el alumno no tenga
    actividades cargadas.
    """
    p1 = to_float(get_setting('precio_act_1')) or 45000
    p2 = to_float(get_setting('precio_act_2')) or 60000
    p3 = to_float(get_setting('precio_act_3')) or 80000
    if n >= 3:
        return p3
    if n == 2:
        return p2
    return p1


def _ids_desde(raw):
    """Normaliza lo que llego del body en una lista de IDs de profesores.

    Acepta [1, 2], '1,2', '1; 2', 1 o '1'. Devuelve [] si no hay nada
    util, y los IDs repetidos se deduplican (misma seleccion dos veces es
    una sola seleccion).
    """
    if raw is None or raw is False:
        return []
    if isinstance(raw, bool):
        return []
    if isinstance(raw, (list, tuple, set)):
        items = list(raw)
    elif isinstance(raw, (int, float)):
        items = [raw]
    else:
        s = str(raw).strip()
        if not s:
            return []
        items = [p.strip() for p in s.replace(';', ',').split(',')]
    out, seen = [], set()
    for it in items:
        if isinstance(it, bool):
            continue
        n = to_int(it)
        if n is not None and n > 0 and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _leer_profesores(data):
    """(ids, error) de la seleccion de profesores hecha en el body.

    Lee 'profesor_ids' (lista) o 'profesor_id' (legacy: un solo profesor).
    ids=[] significa que no eligio a nadie; error solo si mando basura.
    Los sentinels del staff ("0"/-1 = reparto automatico) cuentan como
    "no eligio" y no son error.
    """
    raw = data.get('profesor_ids')
    if raw is None:
        raw = data.get('profesor_id')
    if raw is None:
        return [], None
    if raw in (0, '0', -1, '-1', False):
        return [], None
    if isinstance(raw, (str, bytes)) and not str(raw).strip():
        return [], None
    if isinstance(raw, (list, tuple, set)) and len(raw) == 0:
        return [], None
    ids = _ids_desde(raw)
    if not ids:
        return [], 'Profesor no válido'
    return ids, None


def _nombres_profes(db, ids):
    """{id: nombre} de los IDs que son profesores. Los que no lo son (o no
    existen) quedan afuera: el llamador los filtra con eso."""
    ids = _ids_desde(ids)
    if not ids:
        return {}
    marks = ','.join('?' for _ in ids)
    rows = db.execute(
        "SELECT id, nombre FROM users WHERE id IN (%s) AND role='profesor'" % marks,
        list(ids)).fetchall()
    return {r['id']: r['nombre'] for r in rows}


def _partes_iguales(ids, monto, nombres):
    """(pid, monto, actividad, nombre) repartiendo el 60% en partes iguales
    SOLO entre los IDs elegidos. El ultimo absorbe el redondeo para que sume
    exacto.
    """
    ids = [i for i in _ids_desde(ids) if i in nombres]
    n = len(ids)
    if n == 0:
        return []
    base = round(monto / n, 2)
    partes = []
    for i, pid in enumerate(ids):
        parte = round(monto - base * (n - 1), 2) if i == n - 1 else base
        partes.append((pid, parte, '', nombres[pid]))
    return partes


def _reparto_por_actividades(alumno_id, monto, profesor_manual_id=None):
    """Divide monto entre los profes que dan las actividades del alumno.

    Recibe YA la parte que le toca a los profes (el 60% de la cuota, ver
    _registrar_reparto).

    profesor_manual_id acepta un ID (legacy) o la lista completa de IDs que
    eligio el alumno/staff. Si hay eleccion, el 60% se divide en partes
    iguales SOLO entre los elegidos (uno elegido = 60% entero), sin mirar
    las actividades. Recién sin eleccion se reparte entre los que dan sus
    actividades: Gi+NoGi con dos prof distintos => 50/50 de esos 60%. Si no
    hay profe que den alguna actividad y tampoco hay eleccion, queda sin
    reparto. Devuelve [(profesor_id, montoParte, actividad_csv, nombre)].
    """
    db = get_db()
    elegidos = _ids_desde(profesor_manual_id)
    if elegidos:
        nombres = _nombres_profes(db, elegidos)
        partes = _partes_iguales(elegidos, monto, nombres)
        if partes:
            return partes
    alumno = db.execute('SELECT actividades FROM users WHERE id=?', (alumno_id,)).fetchone()

    acts_alumno = {a.strip() for a in ((alumno['actividades'] or '') if alumno else '').split(',') if a.strip()}
    if acts_alumno:
        profs = db.execute(
            "SELECT id, nombre, actividades FROM users WHERE role='profesor' AND activo=1").fetchall()
        # profes que dan al menos una actividad que entrena el alumno
        match = []
        for p in profs:
            pa = {a.strip() for a in (p['actividades'] or '').split(',') if a.strip()}
            comunes = sorted(acts_alumno & pa)
            if comunes:
                match.append((p['id'], p['nombre'], ','.join(comunes)))
        if match:
            n = len(match)
            base = round(monto / n, 2)
            partes = []
            for i, (pid, nombre, act) in enumerate(match):
                # el ultimo-profes-absorbe-el-redondeo para que sume exacto
                parte = round(monto - base * (n - 1), 2) if i == n - 1 else base
                partes.append((pid, parte, act, nombre))
            return partes
    return []


def _get_alumno(uid):
    """Busca a un alumno o profesor por id.

    /api/alumnos los lista a los dos (los profesores entrenan y pagan cuota), pero
    casi todas las acciones exigian role='alumno' y devolvian "Alumno no
    encontrado": la pantalla ofrecia a la persona y al tocarla rebotaba. Ahora el
    filtro de rol vive aca y es el mismo en las dos puntas.

    No se filtra por activo a proposito: el listado tampoco lo hace, y un alumno
    dado de baja igual tiene que poder recibir las acciones del staff.
    """
    return get_db().execute(
        "SELECT * FROM users WHERE id=? AND role IN ('alumno','profesor')", (uid,)).fetchone()


def _rol_de(db, user_id):
    try:
        row = db.execute('SELECT role FROM users WHERE id=?', (user_id,)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    try:
        return row['role']
    except Exception:
        return row[0]


def _destinos_del_reparto(monto):
    """Parte del pago que no se lleva un profesor.

    Devuelve (monto_para_profes, [(destino, monto), ...]). El 30% va al tatami
    y academia y el 10% al administrativo; el monto para profes absorbe el
    redondeo para que las tres sumen exacto la cuota.
    """
    m_tatami = round(monto * PCT_TATAMI / 100, 2)
    m_admin = round(monto * PCT_ADMIN / 100, 2)
    m_profes = round(monto - m_tatami - m_admin, 2)
    return m_profes, [('tatami', m_tatami), ('administrativo', m_admin)]


def _registrar_reparto(db, pago_id, alumno_id, monto, profesor_manual_id=None,
                       sin_reparto=False):
    """Guarda el desglose del pago en pago_reparto + pago_destino y devuelve
    las partes de los profes.

    Reparto 60/30/10: 60% para los profes (en partes iguales entre los que dan
    las actividades del alumno), 30% tatami y academia, 10% administrativo.

    La cuota de un PROFESOR, los becados (sin_reparto) y los pagos de un alumno
    sin actividades no se dividen: esa plata entra a la academia entera. El
    chequeo del rol va aca adentro y no en los endpoints a proposito: si
    quedara en el llamador, cualquier camino nuevo (el cobro familiar, otro
    profe que registre el pago, el webhook) volveria a pagarle al profesor su
    propia cuota desde la propia academia.
    """
    if sin_reparto:
        return []
    if _rol_de(db, alumno_id) == 'profesor':
        return []
    m_profes, destinos = _destinos_del_reparto(monto)
    partes = _reparto_por_actividades(alumno_id, m_profes, profesor_manual_id)
    if not partes:
        return []
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    for pid, parte, act, _nombre in partes:
        db.execute("""INSERT INTO pago_reparto(pago_id, profesor_id, monto, actividad, fecha)
                      VALUES(?,?,?,?,?)""", (pago_id, pid, parte, act, ahora))
    for destino, m in destinos:
        db.execute("""INSERT INTO pago_destino(pago_id, destino, monto, fecha)
                      VALUES(?,?,?,?)""", (pago_id, destino, m, ahora))
    return partes


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
        'WEB_TITULO': 'NEXO MADRYN - Jiu Jitsu, MMA y Muay Thai',
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
        titulo='NEXO MADRYN - JIU JITSU, MMA Y MUAY THAI',
        subtitulo='Jiu Jitsu · MMA · Muay Thai',
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
                           color=get_setting('academy_color') or '#9b5de5')


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
    u = db.execute('SELECT nombre, role FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    if not es_admin(current_user()) and u['role'] != 'alumno':
        return jsonify({'error': 'Solo un administrador puede reiniciar la clave de ese usuario'}), 403
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
    genero = (data.get('genero') or '').strip().upper()[:1]
    if genero not in ('M', 'F'):
        genero = ''
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
    cuota_reg = to_float(data.get('cuota_mensual')) if role in ('alumno', 'profesor') else None
    csv_act = _actividades_csv(data)
    if role == 'alumno' and not cuota_reg:
        cuota_reg = _cuota_por_actividades(csv_act)

    try:
        get_db().execute(
            """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, actividades, cuota_mensual, tel, nacimiento, medic_info, emergency_contact, tel_tutor, tel_2, direccion, dni, foto_ok, acepto_tyc, firma_tyc, firma_foto, firma_fecha, genero, creado)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, generate_password_hash(password), role, nombre,
             to_int(data.get('edad')), to_float(data.get('peso')),
             data.get('cinturon'), categoria,
             data.get('gi_pref') or 'Ambas',
             csv_act,
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
             firma_fecha, genero,
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
    if u['role'] in ('alumno', 'profesor'):
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
    avisos = []
    for r in rows:
        # Una sola clase con el dia fuera de 0-6 (dato viejo o cargado por fuera
        # de la app) hacia DIAS[r['dia']] -> IndexError -> 500 -> la seccion entera
        # de Horarios quedaba en "Cargando" sin explicacion. Ahora se muestra con un
        # nombre de dia corregido y se avisa, en vez de matar el calendario.
        dia = col(r, 'dia', 0)
        try:
            dia_nombre = DIAS[int(dia)]
            # DIAS[-1] en Python es "Domingo" y no tira, pero en el front el filtro
            # es h.dia === i con i de 0 a 6, asi que la clase se perdia sin aviso.
            if int(dia) < 0:
                raise IndexError(dia)
        except (IndexError, TypeError, ValueError):
            dia_nombre = 'Día %s' % (dia,)
            avisos.append('La clase #%s tiene un día inválido (%s). Corregilo editando la clase.'
                          % (col(r, 'id'), dia))
            print('HORARIOS: clase id=%s con dia=%r fuera de rango' % (col(r, 'id'), dia))
        item = {
            'id': col(r, 'id'), 'dia': dia, 'dia_nombre': dia_nombre,
            'hora': col(r, 'hora', ''), 'tipo': col(r, 'tipo', 'Gi') or 'Gi',
            'nivel': col(r, 'nivel', ''),
            'duracion': col(r, 'duracion', 60) or 60,
            'profesor_id': col(r, 'profesor_id'),
            'profesor_nombre': col(r, 'profesor_nombre')}
        if ratings:
            item['rating'] = ratings.get(col(r, 'id'), {'n': 0, 'promedio': 0})
        horarios.append(item)
    out = {'horarios': horarios}
    if avisos:
        out['avisos'] = avisos
    return jsonify(out)


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
# Torneos
# ---------------------------------------------------------------------------
# El calendario se carga a mano: no hay integracion con ningun calendario
# externo, asi que un torneo es una fila en 'torneos' y sus participantes son
# filas en 'torneo_inscripciones' (una por alumno y torneo, con su medalla).
#
# El ranking se arma con esas mismas inscripciones, asi que no se mantiene
# ningun contador aparte que se pueda desincronizar.

MEDALLAS = ('oro', 'plata', 'bronce')

# Mismo DDL que el de SCHEMA. Se vuelve a intentar en cada arranque de la app y
# ademas en el primer uso, porque si el deploy caiu en medio de la migracion
# (o la base se clonó) el endpoint finds "relation does not exist" y la seccion
# entera queda en error sin explicar nada. CREATE TABLE IF NOT EXISTS es
# idempotente, asi que repetirlo no molesta.
TORNEOS_DDL = (
    """CREATE TABLE IF NOT EXISTS torneos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        fecha TEXT,
        ciudad TEXT,
        lugar TEXT,
        tipo TEXT DEFAULT 'IBJJF',
        estado TEXT DEFAULT 'programado',
        descripcion TEXT,
        url TEXT,
        creado_por INTEGER REFERENCES users(id) ON DELETE SET NULL,
        creado TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS torneo_inscripciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        torneo_id INTEGER NOT NULL REFERENCES torneos(id) ON DELETE CASCADE,
        alumno_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        categoria TEXT DEFAULT '',
        medalla TEXT DEFAULT '',
        nota TEXT DEFAULT '',
        creado TEXT,
        UNIQUE (torneo_id, alumno_id)
    )""",
)
_torneos_ready = False


def asegurar_torneos():
    """Crea las tablas de torneos si faltan. Idempotente.

    El flag solo se levanta si el DDL fue bien: si falla una vez (deploy a medio
    camino, base clonada) se reintenta en el proximo pedido en vez de dejar la
    seccion muerta para siempre.
    """
    global _torneos_ready
    if _torneos_ready:
        return
    db = get_db()
    for sql in TORNEOS_DDL:
        db.execute(sql)
    db.commit()
    _torneos_ready = True


def torneo_error(e):
    """Vuelve el error real en vez de un 500 sin cuerpo.

    api() del front muestra data.error, asi que con esto el problema se lee en
    pantalla en vez de ser el generico "Error de servidor".
    """
    print('TORNEOS ERROR: %r' % (e,))
    return jsonify({'error': 'No se pudieron leer los torneos: %s' % e,
                    'tipo': type(e).__name__}), 500


def _torneo_row(r):
    return {
        'id': r['id'], 'nombre': r['nombre'], 'fecha': r['fecha'] or '',
        'ciudad': r['ciudad'] or '', 'lugar': r['lugar'] or '',
        'tipo': r['tipo'] or 'IBJJF', 'estado': r['estado'] or 'programado',
        'descripcion': r['descripcion'] or '', 'url': r['url'] or '',
        'inscripciones': [],
    }


@app.route('/api/torneos', methods=['GET'])
@login_required
def api_torneos():
    try:
        asegurar_torneos()
    except Exception as e:
        return torneo_error(e)
    try:
        db = get_db()
        trs = [_torneo_row(r) for r in db.execute(
            'SELECT id, nombre, fecha, ciudad, lugar, tipo, estado, descripcion, url'
            ' FROM torneos ORDER BY COALESCE(fecha,\'\') DESC, id DESC').fetchall()]
        por_id = {t['id']: t for t in trs}
        for i in db.execute(
                'SELECT i.id, i.torneo_id, i.alumno_id, i.categoria, i.medalla, i.nota,'
                '       u.nombre, u.cinturon'
                ' FROM torneo_inscripciones i JOIN users u ON u.id = i.alumno_id'
                ' ORDER BY i.id').fetchall():
            if i['torneo_id'] not in por_id:
                continue  # inscripcion huerfana de un torneo borrado
            por_id[i['torneo_id']]['inscripciones'].append({
                'id': i['id'], 'alumno_id': i['alumno_id'],
                'nombre': i['nombre'] or '',
                'cinturon': i['cinturon'] or '',
                'foto': '/api/avatar/%d' % i['alumno_id'],
                'categoria': i['categoria'] or '',
                'medalla': i['medalla'] or '', 'nota': i['nota'] or '',
            })
        return jsonify({'torneos': trs})
    except Exception as e:
        return torneo_error(e)


def _torneo_campos(data, tid=None):
    nombre = txt_str(data.get('nombre'))
    if not nombre:
        return None, ('Falta el nombre del torneo', 400)
    fecha = txt_str(data.get('fecha'))
    if fecha:
        # El regex solo chequeaba la forma: 2026-13-45 pasaba y despues reventaba
        # al ordenar o al pintar el calendario. Se valida como fecha real.
        try:
            datetime.strptime(fecha, '%Y-%m-%d')
        except ValueError:
            return None, ('Fecha inválida: usá una fecha que exista, tipo 2026-03-15', 400)
    url = url_http_valida(txt_str(data.get('url')))
    return {
        'nombre': nombre,
        'fecha': fecha or None,
        'ciudad': txt_str(data.get('ciudad')),
        'lugar': txt_str(data.get('lugar')),
        'tipo': txt_str(data.get('tipo')) or 'IBJJF',
        'estado': txt_str(data.get('estado')) or 'programado',
        'descripcion': txt_str(data.get('descripcion')),
        # Sin esto la url va directo a un href: un javascript: o data: queda
        # clickeable en la tarjeta del torneo.
        'url': url or '',
    }, None


@app.route('/api/torneos', methods=['POST'])
@role_required('admin', 'profesor')
def api_torneos_create():
    campos, err = _torneo_campos(parse_json())
    if err:
        return jsonify({'error': err[0]}), err[1]
    asegurar_torneos()
    cur = get_db().cursor()
    cur.execute(
        'INSERT INTO torneos(nombre, fecha, ciudad, lugar, tipo, estado, descripcion,'
        ' url, creado_por, creado) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (campos['nombre'], campos['fecha'], campos['ciudad'], campos['lugar'],
         campos['tipo'], campos['estado'], campos['descripcion'], campos['url'],
         current_user()['id'], datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    return jsonify({'ok': True, 'id': cur.lastrowid})


@app.route('/api/torneos/<int:tid>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_torneos_update(tid):
    campos, err = _torneo_campos(parse_json())
    if err:
        return jsonify({'error': err[0]}), err[1]
    asegurar_torneos()
    if not get_db().execute('SELECT id FROM torneos WHERE id=?', (tid,)).fetchone():
        return jsonify({'error': 'Torneo no encontrado'}), 404
    get_db().execute(
        'UPDATE torneos SET nombre=?, fecha=?, ciudad=?, lugar=?, tipo=?, estado=?,'
        ' descripcion=?, url=? WHERE id=?',
        (campos['nombre'], campos['fecha'], campos['ciudad'], campos['lugar'],
         campos['tipo'], campos['estado'], campos['descripcion'], campos['url'], tid))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/torneos/<int:tid>', methods=['DELETE'])
@role_required('admin')
def api_torneos_delete(tid):
    get_db().execute('DELETE FROM torneos WHERE id=?', (tid,))
    get_db().commit()
    return jsonify({'ok': True})


def _es_unique(e):
    """True si la excepcion es una violacion de indice unico."""
    txt = str(e).lower()
    return ('unique' in txt or 'duplicate' in txt
            or 'uniqueviolation' in type(e).__name__.lower())


@app.route('/api/torneos/<int:tid>/inscripcion', methods=['POST'])
@role_required('admin', 'profesor')
def api_torneos_inscribir(tid):
    """Inscribe (o actualiza) a un alumno en un torneo. Una fila por alumno."""
    asegurar_torneos()
    data = parse_json()
    if not get_db().execute('SELECT id FROM torneos WHERE id=?', (tid,)).fetchone():
        return jsonify({'error': 'Torneo no encontrado'}), 404
    alumno = to_int(data.get('alumno_id'))
    # Mismo criterio que el resto de las acciones sobre alumnos (ver _get_alumno):
    # cualquiera que compita, sea alumno o profesor. El filtro de activo se
    # dejaba antes y rompia a los dados de baja, que el listado igual muestra.
    if not alumno or not _get_alumno(alumno):
        return jsonify({'error': 'Alumno no encontrado'}), 400
    medalla = txt_str(data.get('medalla')).lower()
    if medalla and medalla not in MEDALLAS:
        return jsonify({'error': 'Medalla inválida'}), 400
    # Solo se pisan los campos que vienen en el payload. Si no, un update parcial
    # (por ejemplo cambiar solo la medalla) borraria la categoria sin avisar.
    cambios, vals = [], []
    for campo, val in (('categoria', txt_str(data.get('categoria'))),
                       ('medalla', medalla),
                       ('nota', txt_str(data.get('nota')))):
        if campo in data:
            cambios.append(campo + '=?')
            vals.append(val)
    db = get_db()
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    valores = {'categoria': txt_str(data.get('categoria')),
               'medalla': medalla,
               'nota': txt_str(data.get('nota'))}
    try:
        # Se intenta el INSERT y, si choca con el UNIQUE, se pasa al UPDATE. Asi
        # la carrera del doble clic la resuelve la base (no dos inserts) sin
        # perder de paso el update parcial: el upsert con COALESCE pisaba con ''
        # los campos ausentes del payload y vaciaba la categoria.
        db.execute('INSERT INTO torneo_inscripciones(torneo_id, alumno_id, categoria,'
                   ' medalla, nota, creado) VALUES(?,?,?,?,?,?)',
                   (tid, alumno,
                    valores['categoria'] if 'categoria' in data else '',
                    valores['medalla'] if 'medalla' in data else '',
                    valores['nota'] if 'nota' in data else '',
                    ahora))
    except Exception as e:
        if not _es_unique(e):
            raise
        if not cambios:
            return jsonify({'ok': True})
        db.execute('UPDATE torneo_inscripciones SET ' + ', '.join(cambios)
                   + ' WHERE torneo_id=? AND alumno_id=?', vals + [tid, alumno])
    db.commit()
    return jsonify({'ok': True})


@app.route('/api/torneos/torneo/<int:iid>', methods=['DELETE'])
@role_required('admin', 'profesor')
def api_torneos_quitar_inscripcion(iid):
    get_db().execute('DELETE FROM torneo_inscripciones WHERE id=?', (iid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/torneos/ranking')
@login_required
def api_torneos_ranking():
    """Ranking de la academia: a quien mas compite y a quien mas medallas trae.

    Se calcula entero en cada pedido desde las inscripciones, sin contadores
    guardados, para que no se pueda desincronizar del historial real.
    Solo cuentan los torneos con fecha: los que estan programados todavia no.
    """
    # El ORDER BY va en una consulta exterior porque PostgreSQL NO acepta un alias
    # de salida dentro de una expresion ("ORDER BY n_oro + n_plata" lo interpreta
    # como columna de las tablas y dice que no existe). SQLite lo resuelve bien y
    # por eso los tests en local no lo detectaban.
    try:
        asegurar_torneos()
        filas = get_db().execute(
            'SELECT * FROM ('
            '  SELECT u.id, u.nombre, u.cinturon,'
            '         COUNT(DISTINCT i.torneo_id) AS n_torneos,'
            "         SUM(CASE WHEN i.medalla='oro' THEN 1 ELSE 0 END) AS n_oro,"
            "         SUM(CASE WHEN i.medalla='plata' THEN 1 ELSE 0 END) AS n_plata,"
            "         SUM(CASE WHEN i.medalla='bronce' THEN 1 ELSE 0 END) AS n_bronce"
            '  FROM torneo_inscripciones i'
            '  JOIN torneos t ON t.id = i.torneo_id'
            '  JOIN users u ON u.id = i.alumno_id'
            "  WHERE u.role IN ('alumno','profesor') AND t.fecha IS NOT NULL AND t.fecha <> ''"
            '  GROUP BY u.id, u.nombre, u.cinturon'
            ') r'
            ' ORDER BY r.n_torneos DESC, (r.n_oro + r.n_plata + r.n_bronce) DESC,'
            '          r.n_oro DESC, r.n_plata DESC, r.nombre ASC').fetchall()
    except Exception as e:
        return torneo_error(e)
    out = []
    for f in filas:
        oro, plata, bronce = f['n_oro'] or 0, f['n_plata'] or 0, f['n_bronce'] or 0
        out.append({
            'alumno_id': f['id'],
            'nombre': f['nombre'] or '',
            'cinturon': f['cinturon'] or '', 'foto': '/api/avatar/%d' % f['id'],
            'torneos': f['n_torneos'] or 0,
            'oro': oro, 'plata': plata, 'bronce': bronce,
            'medallas': oro + plata + bronce,
            # 3/2/1: criterio de desempate legible para la UI
            'puntos': oro * 3 + plata * 2 + bronce})
    return jsonify({'ranking': out})


# ---------------------------------------------------------------------------
# Alumnos (admin / profesor)
# ---------------------------------------------------------------------------

@app.route('/api/alumnos')
@role_required('admin', 'profesor')
def api_alumnos():
    db = get_db()
    rows = db.execute(
        "SELECT %s,"
        "  (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS asistencias,"
        "  (SELECT COUNT(*) FROM pagos p WHERE p.alumno_id=u.id) AS pagos_totales"
        " FROM users u WHERE u.role IN ('alumno','profesor') ORDER BY u.nombre"
        % columnas_de_users('u.')).fetchall()
    # mapa alumno -> familia (nombre, titular, relacion) y conteo de miembros
    fam_map = {}
    fam_count = {}
    for fm in db.execute(
        """SELECT fm.user_id, fm.relacion, f.id AS fam_id, f.nombre AS fam_nombre, f.titular_id
           FROM familia_miembros fm JOIN familias f ON f.id=fm.familia_id""").fetchall():
        fam_map[fm['user_id']] = fm
        fam_count[fm['fam_id']] = fam_count.get(fm['fam_id'], 0) + 1
    hoy = _hoy_academy()
    pagos_mes = _pagos_del_mes(hoy.month, hoy.year)
    alumnos = []
    for r in rows:
        d = user_public(r)
        d['asistencias'] = r['asistencias']
        d['pagos_totales'] = r['pagos_totales']
        d['cuota'] = cuota_status(r, pagos_mes)
        d['dias_deuda'] = dias_deuda(r, pagos_mes)
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
    cuota_calc = data.get('cuota_mensual') or ''
    cuota = to_float(cuota_calc) if cuota_calc not in (None, '', 'auto') else (_cuota_por_actividades(_actividades_csv(data)) or (to_float(get_setting('default_cuota', '15000')) or 15000))
    nacimiento = txt_str(data.get('nacimiento'))
    if not nacimiento:
        return jsonify({'error': 'La fecha de nacimiento es obligatoria al crear el perfil.'}), 400
    try:
        if datetime.strptime(nacimiento, '%Y-%m-%d').date() >= _hoy_academy():
            return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
    except ValueError:
        return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400
    # El INSERT dejaba afuera todo lo que el formulario de alta trae (DNI,
    # direccion, telefonos, ficha medica, pausa), asi que el admin lo cargaba y
    # desaparecia. Se persisten las mismas columnas que usa api_alumnos_update.
    get_db().execute(
        """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref,
                             actividades, cuota_mensual, nacimiento, genero, creado,
                             tel, tel_2, tel_tutor, dni, direccion, medic_info, emergency_contact,
                             foto_ok, pausa_desde, pausa_hasta)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (username, generate_password_hash(password), 'alumno', nombre,
         to_int(data.get('edad')), to_float(data.get('peso')),
         data.get('cinturon'), data.get('categoria') or 'adulto',
         data.get('gi_pref') or 'Ambas', _actividades_csv(data), cuota, nacimiento,
         _genero_de(data, {'genero': ''}),
         datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         txt_str(data.get('tel')) or None,
         txt_str(data.get('tel_2')) or None,
         txt_str(data.get('tel_tutor')) or None,
         txt_str(data.get('dni')) or None,
         txt_str(data.get('direccion')) or None,
         txt_str(data.get('medic_info')) or None,
         txt_str(data.get('emergency_contact')) or None,
         1 if data.get('foto_ok') else 0,
         txt_str(data.get('pausa_desde')) or None,
         txt_str(data.get('pausa_hasta')) or None))
    get_db().commit()
    new_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    return jsonify({'ok': True, 'id': new_id, 'username': username, 'password': password})


@app.route('/api/alumnos/<int:uid>', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumnos_update(uid):
    data = parse_json()
    u = _get_alumno(uid)
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    nac_upd = txt_str(data.get('nacimiento', u['nacimiento']))
    if nac_upd:
        try:
            if datetime.strptime(nac_upd, '%Y-%m-%d').date() >= _hoy_academy():
                return jsonify({'error': 'La fecha de nacimiento no puede ser hoy ni del futuro.'}), 400
        except ValueError:
            return jsonify({'error': 'Fecha de nacimiento inválida (formato AAAA-MM-DD).'}), 400
    # UPDATE parcial: solo las columnas que vienen de verdad en el body. Antes
    # armaba un SET con las 21 columnas y para las que no llegaban usaba valores
    # derivados del body en vez del usuario, asi que editar un solo campo desde
    # el perfil (por ejemplo la ficha medica) vol activities a '' y recalculaba
    # la cuota desde cero. Un update parcial no puede romper lo que no manda.
    campos, vals = [], []

    def poner(col, valor):
        campos.append('%s=?' % col)
        vals.append(valor)

    def si(mapping, calc=None):
        for col, clave in mapping:
            if clave in data:
                poner(col, data[clave] if calc is None else calc(clave))
                break

    si([('nombre', 'nombre')], lambda k: data.get('nombre') or u['nombre'])
    si([('edad', 'edad')], lambda k: to_int(data.get('edad', u['edad'])))
    si([('peso', 'peso')], lambda k: to_float(data.get('peso', u['peso'])))
    si([('cinturon', 'cinturon'), ('cinturon', 'cinturon_actual')])
    si([('categoria', 'categoria')])
    si([('gi_pref', 'gi_pref')])
    si([('genero', 'genero')], lambda k: _genero_de(data, u))
    si([('activo', 'activo')], lambda k: 1 if data.get('activo', u['activo']) else 0)
    si([('tel', 'tel')], lambda k: txt_str(data.get('tel')) or None)
    si([('nacimiento', 'nacimiento')], lambda k: nac_upd or None)
    si([('medic_info', 'medic_info'), ('medic_info', 'medico')])
    si([('emergency_contact', 'emergency_contact'), ('emergency_contact', 'contacto_emergencia')])
    si([('tel_tutor', 'tel_tutor')], lambda k: txt_str(data.get('tel_tutor')) or None)
    si([('tel_2', 'tel_2')], lambda k: txt_str(data.get('tel_2')) or None)
    si([('direccion', 'direccion')], lambda k: txt_str(data.get('direccion')) or None)
    si([('dni', 'dni')], lambda k: txt_str(data.get('dni')) or None)
    si([('foto_ok', 'foto_ok')], lambda k: 1 if data.get('foto_ok', u['foto_ok']) else 0)
    si([('pausa_desde', 'pausa_desde')], lambda k: txt_str(data.get('pausa_desde')) or None)
    si([('pausa_hasta', 'pausa_hasta')], lambda k: txt_str(data.get('pausa_hasta')) or None)

    # actividades: si viene la clave se recalcula la cuota automatica; si no, se
    # respeta la que ya tiene el usuario (no se pisa con un default).
    if 'actividades' in data:
        acts = _actividades_csv(data)
        poner('actividades', acts)
        if data.get('cuota_mensual') in (None, '', 'auto'):
            poner('cuota_mensual', _cuota_por_actividades(acts)
                  or (to_float(get_setting('default_cuota', '15000')) or 15000))
    if 'cuota_mensual' in data:
        cm = data.get('cuota_mensual')
        if cm in (None, '', 'auto') and 'actividades' not in data:
            # 'auto' sin actividades nuevas: no hay de donde calcular un precio,
            # asi que se conserva la cuota actual en vez de mandarle un default.
            cm = u['cuota_mensual']
        poner('cuota_mensual', to_float(cm))
    if not campos:
        return jsonify({'ok': True})
    vals.append(uid)
    get_db().execute('UPDATE users SET %s WHERE id=?' % ', '.join(campos), vals)
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
    # Baja logica, no un DELETE: los pagos, la asistencia y las inscripciones a
    # torneo quedan colgando de la fila. Si se borra, ese historial desaparece y
    # los reportes quedan con pagos de un alumno inexistente.
    u = _get_alumno(uid)
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    get_db().execute('UPDATE users SET activo=0 WHERE id=?', (uid,))
    get_db().commit()
    notify(uid, 'Tu cuenta fue dada de baja',
           'Tu cuenta quedo desactivada. Si creés que es un error, escribinos.', 'cuota')
    return jsonify({'ok': True, 'activo': 0})


@app.route('/api/alumnos/<int:uid>/cuota', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumnos_cuota(uid):
    data = parse_json()
    cuota = to_float(data.get('cuota_mensual'))
    if not cuota or cuota <= 0:
        return jsonify({'error': 'Monto de cuota invalido'}), 400
    u = _get_alumno(uid)
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
        "SELECT %s,"
        "  (SELECT COUNT(*) FROM classes c WHERE c.profesor_id=u.id) AS clases"
        " FROM users u WHERE u.role='profesor' ORDER BY u.nombre"
        % columnas_de_users('u.')).fetchall()
    return jsonify({'profesores': [dict(user_public(r), **{'clases': r['clases']}) for r in rows]})


@app.route('/api/profesores_disponibles')
@login_required
def api_profesores_disponibles():
    """Lista mínima id+nombre para que el alumno elija a quién le está pagando.

    /api/profesores es de staff (devuelve la cuota y los datos del profe), así
    que no se abre a los alumnos: esto alcanza para armar el selector del
    comprobante.
    """
    rows = get_db().execute(
        "SELECT id, nombre FROM users WHERE role='profesor' AND activo=1 ORDER BY nombre"
    ).fetchall()
    return jsonify({'profesores': [dict(r) for r in rows]})


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
    # Los profes pagan cuota. Sin esto queda NULL y el profe no aparece como
    # deudor ni puede pagar por MercadoPago.
    cuota_profe = to_float(data.get('cuota_mensual')) or to_float(get_setting('default_cuota', '0')) or None
    get_db().execute(
        """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, cuota_mensual, creado)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (username, generate_password_hash(password), 'profesor', nombre,
         to_int(data.get('edad')), to_float(data.get('peso')),
         data.get('cinturon'), data.get('categoria') or 'adulto',
         data.get('gi_pref') or 'Ambas', cuota_profe,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
    get_db().commit()
    new_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    return jsonify({'ok': True, 'id': new_id, 'username': username, 'password': password})


@app.route('/api/profesores/<int:uid>/cuota', methods=['PUT'])
@role_required('admin')
def api_profesores_cuota(uid):
    """Carga el monto de cuota de un profesor.

    Los profes pagan cuota, asi que su cuota_mensual deja de ser NULL: sin monto
    /api/checkout no tendria nada que cobrar y no figurarian en /api/deudores
    (que filtra por cuota_mensual IS NOT NULL).
    """
    data = parse_json()
    cuota = to_float(data.get('cuota_mensual'))
    if not cuota or cuota <= 0:
        return jsonify({'error': 'Monto de cuota invalido'}), 400
    u = get_db().execute("SELECT * FROM users WHERE id=? AND role='profesor'", (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Profesor no encontrado'}), 404
    get_db().execute('UPDATE users SET cuota_mensual=? WHERE id=?', (cuota, uid))
    get_db().commit()
    who = current_user()['nombre']
    notify(uid, 'Tu cuota cambio',
           f'{who} actualizo tu cuota mensual a ${cuota:,.0f}'.replace(',', '.'), 'cuota')
    return jsonify({'ok': True, 'cuota_mensual': cuota})


@app.route('/api/profesores/<int:uid>', methods=['DELETE'])
@role_required('admin')
def api_profesores_delete(uid):
    u = get_db().execute("SELECT * FROM users WHERE id=? AND role='profesor'", (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Profesor no encontrado'}), 404
    admin = get_db().execute("SELECT id FROM users WHERE role='admin' OR es_admin=1 LIMIT 1").fetchone()
    # quita las clases del profesor
    get_db().execute('UPDATE classes SET profesor_id=NULL WHERE profesor_id=?', (uid,))
    # conserva pagos: profesor_id queda con SET NULL
    get_db().execute('DELETE FROM users WHERE id=?', (uid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/alumnos/<int:uid>/profesor', methods=['POST'])
@role_required('admin')
def api_alumnos_promover(uid):
    # Aca si se exige role='alumno': es "convertir alumno en profesor". Si se
    # aceptara a un profesor ya dado de baja, el UPDATE le pondria activo=1 y lo
    # reactivaria sin que nadie lo pida.
    u = get_db().execute("SELECT * FROM users WHERE id=? AND role='alumno'", (uid,)).fetchone()
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
    u = _get_alumno(uid)
    if not u:
        return jsonify({'error': 'Alumno no encontrado'}), 404
    nueva = 0 if (int(u['beca']) if 'beca' in u.keys() else 0) else 1
    get_db().execute('UPDATE users SET beca=? WHERE id=?', (nueva, uid))
    get_db().commit()
    return jsonify({'ok': True, 'beca': nueva, 'nombre': u['nombre']})


@app.route('/api/usuarios/<int:uid>/admin', methods=['POST'])
@role_required('admin')
def api_usuario_toggle_admin(uid):
    db = get_db()
    u = db.execute('SELECT id, role, es_admin, nombre FROM users WHERE id=?', (uid,)).fetchone()
    if not u:
        return jsonify({'error': 'Usuario no encontrado'}), 404
    nuevo = 0 if (u['es_admin'] if 'es_admin' in u.keys() else 0) else 1
    # Proteccion: no dejar sin ningun admin activo (rol principal o es_admin)
    if nuevo == 0 and u['role'] != 'admin':
        # Ver cuantos admins efectivos hay (role admin o es_admin)
        count = db.execute(
            "SELECT COUNT(*) AS c FROM users WHERE role='admin' OR es_admin=1").fetchone()['c']
        if count <= 1:
            return jsonify({'error': 'No podes quitar el ultimo administrador'}), 400
    db.execute('UPDATE users SET es_admin=? WHERE id=?', (nuevo, uid))
    db.commit()
    return jsonify({'ok': True, 'es_admin': nuevo})


@app.route('/api/alumnos/<int:uid>/notas', methods=['PUT'])
@role_required('admin', 'profesor')
def api_alumno_notas(uid):
    data = parse_json()
    u = _get_alumno(uid)
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
    u = _get_alumno(uid)
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
            "SELECT %s, fm.relacion,"
            "  (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular"
            " FROM familia_miembros fm"
            " JOIN users u ON u.id=fm.user_id"
            " JOIN familias f ON f.id=fm.familia_id"
            " WHERE fm.familia_id=? ORDER BY es_titular DESC, u.nombre"
            % columnas_de_users('u.'),
            (f['id'],)).fetchall()
        lista = []
        total = 0
        for m in miem:
            base, desc, final = familia_cuota(dict(m), len(miem))
            total += final
            lista.append({'id': m['id'], 'nombre': m['nombre'], 'cinturon': m['cinturon'],
                          'foto': '/api/avatar/%d' % m['id'], 'relacion': m['relacion'],
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
        u = _get_alumno(titular_id)
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
    u = _get_alumno(uid)
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
        "SELECT %s, fm.relacion,"
        "  (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular"
        " FROM familia_miembros fm"
        " JOIN users u ON u.id=fm.user_id"
        " JOIN familias f ON f.id=fm.familia_id"
        " WHERE fm.familia_id=? ORDER BY es_titular DESC, u.nombre"
        % columnas_de_users('u.'), (fam_id,)).fetchall()
    lista = []
    for m in miem:
        base, desc, final = familia_cuota(dict(m), len(miem))
        lista.append({'id': m['id'], 'nombre': m['nombre'], 'cinturon': m['cinturon'],
                      'foto': '/api/avatar/%d' % m['id'], 'relacion': m['relacion'],
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
    genero = (data.get('genero') or '').strip().upper()[:1]
    if genero not in ('M', 'F'):
        genero = ''
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
    cuota_menor = _cuota_por_actividades(_actividades_csv(data)) or (to_float(get_setting('default_cuota', '15000')) or 15000)
    db = get_db()
    try:
        cur = db.execute(
            """INSERT INTO users(username, password_hash, role, nombre, edad, peso, cinturon, categoria, gi_pref, actividades, cuota_mensual, tel, nacimiento, medic_info, emergency_contact, tel_tutor, tel_2, direccion, dni, foto_ok, acepto_tyc, firma_tyc, firma_foto, firma_fecha, genero, creado)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (username, generate_password_hash(password), 'alumno', nombre,
             to_int(data.get('edad')), to_float(data.get('peso')),
             data.get('cinturon') or 'Blanco', categoria,
             data.get('gi_pref') or 'Ambas', _actividades_csv(data), cuota_menor,
             None, nacimiento,
             txt_str(data.get('medic_info')) or None,
             txt_str(data.get('emergency_contact')) or None,
             tel_tutor, None, None, txt_str(data.get('dni')) or None,
             1, datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             data.get('firma_tyc'), data.get('firma_foto'), firma_fecha, genero,
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
        "SELECT d.*, u.nombre AS autor_nombre, u.id AS autor_id"
        " FROM diario d JOIN users u ON u.id=d.user_id"
        " ORDER BY d.fecha DESC, d.id DESC LIMIT 120").fetchall()
    out = []
    for r in rows:
        e = dict(r)
        # El avatar del autor no viaja en cada entrada: son hasta 120 blobs.
        e['autor_foto'] = '/api/avatar/%d' % e['autor_id']
        out.append(e)
    return jsonify({'diario': out,
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
    if u['role'] == 'profesor' and not es_admin(u):
        # Tambien se mira pago_reparto: cuando el alumno elige a varios, el
        # profesor elegido en segundo lugar no figura en pagos.profesor_id pero
        # igual cobra su parte y tenia que poder ver el pago.
        q += (' WHERE (p.profesor_id=? OR p.id IN '
              '(SELECT pago_id FROM pago_reparto WHERE profesor_id=?))')
        params += [u['id'], u['id']]
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
    monto = to_float(data.get('monto'))
    mes = to_int(data.get('mes')) or _hoy_academy().month
    anio = to_int(data.get('anio')) or _hoy_academy().year
    if not alumno_id or not monto or monto <= 0:
        return jsonify({'error': 'Alumno y monto son obligatorios'}), 400
    mes, anio, err = validar_mes_anio(mes, anio)
    if err:
        return jsonify({'error': err}), 400
    who = current_user()
    # Un profesor puede cargar su propia cuota (los profes pagan), pero ese pago
    # NO se reparte: la plata entra a la academia. Antes se bloqueaba entero y un
    # profe no podia darse de alta su cuota sin que lo hiciera un admin.
    propio = (not es_admin(who) and alumno_id == who['id'])
    # El staff puede elegir uno o varios profesores: el 60% se divide en partes
    # iguales SOLO entre los elegidos. Si no elige ninguno (o manda el 0/-1 del
    # select), el reparto es automatico por actividades, como siempre.
    ids, err = _leer_profesores(data)
    if err:
        return jsonify({'error': err}), 400
    nombres = _nombres_profes(get_db(), ids)
    if len(nombres) != len(ids):
        return jsonify({'error': 'Ese profesor no existe'}), 400
    profesor_id = ids[0] if ids else None
    base, cargo, final = calcular_demora(monto, mes, anio)
    # Opt-in: si no viene el campo o no es explicito, NO se cobra recargo.
    if as_bool(data.get('aplicar_cargo'), default=False):
        monto = final
    else:
        cargo = 0
    # Doble clic o reenvio del formulario: mismo alumno, mismo mes, mismo monto
    # final y casi misma hora. Se compara sobre el monto ya ajustado porque es lo
    # que queda guardado. No bloquea pagos legitimos cargados mas tarde.
    dup = get_db().execute(
        "SELECT id FROM pagos WHERE alumno_id=? AND mes=? AND anio=? AND monto=? "
        "AND registrado_por=? AND fecha >= ? LIMIT 1",
        (alumno_id, mes, anio, monto, who['id'],
         (datetime.now() - timedelta(seconds=90)).strftime('%Y-%m-%d %H:%M:%S'))).fetchone()
    if dup:
        return jsonify({'error': 'Ese pago recien fue registrado. Mirá el historial antes de volver a cargar.'}), 409
    get_db().execute(
        """INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (alumno_id, profesor_id, monto, mes, anio,
         data.get('metodo') or 'Efectivo', data.get('concepto') or 'Cuota mensual',
         data.get('nota'), datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
         current_user()['id']))
    get_db().commit()
    pago_id = get_db().execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    # reparto 60% profes / 30% tatami-academia / 10% administrativo
    partes = _registrar_reparto(get_db(), pago_id, alumno_id, monto, ids or None,
                                sin_reparto=propio)
    destinos = _destinos_del_reparto(monto)[1] if partes else []
    get_db().commit()
    alumno = get_db().execute('SELECT * FROM users WHERE id=?', (alumno_id,)).fetchone()
    nota_extra = f' (incluye ${cargo:,.0f} de recargo por demora)'.replace(',', '.') if cargo else '.'
    # notificaciones: al alumno
    notify(alumno_id, 'Pago registrado',
           f'Tu pago de ${monto:,.0f} por {mes}/{anio} fue registrado por {who["nombre"]}{nota_extra}'.replace(',', '.'),
           'pago')
    # a cada profesor que se lleva su parte del reparto
    for pid, parte, act_prof, nombre in partes:
        if pid == who['id']:
            continue
        detalle = f' ({PCT_PROFES}% entre los profes: {act_prof})' if len(partes) > 1 and act_prof else ''
        notify(pid, 'Te toca parte de un pago',
               f'{alumno["nombre"]} pagó ${monto:,.0f}: te corresponden ${parte:,.0f}{detalle}.'.replace(',', '.'),
               'pago', link='dinero')
    # a los admins (si no es el que registro)
    admins = get_db().execute("SELECT id FROM users WHERE role='admin' OR es_admin=1").fetchall()
    for a in admins:
        if a['id'] != who['id']:
            notify(a['id'], 'Nuevo pago registrado',
                   f'{alumno["nombre"]} pagó ${monto:,.0f} registrado por {who["nombre"]}.',
                   'pago')
    return jsonify({'ok': True, 'base': base, 'cargo': cargo, 'monto': monto,
                    'reparto': [{'profesor': n, 'monto': p, 'actividad': a} for _i, p, a, n in partes],
                    'destinos': [{'destino': d, 'monto': m} for d, m in destinos]})


@app.route('/api/pagos/familia', methods=['POST'])
@role_required('admin', 'profesor')
def api_pagos_familia():
    """Registra la cuota (con descuento familiar) de TODOS los integrantes del
    grupo de un titular, en un solo paso. Saltea becados, profesores y quien
    ya tiene pago de ese mes/año."""
    data = parse_json()
    titular_id = to_int(data.get('titular_id'))
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
    ids, err = _leer_profesores(data)
    if err:
        return jsonify({'error': err}), 400
    nombres = _nombres_profes(db, ids)
    if len(nombres) != len(ids):
        return jsonify({'error': 'Ese profesor no existe'}), 400
    profesor_id = ids[0] if ids else None
    fam = db.execute('SELECT * FROM familias WHERE titular_id=?', (titular_id,)).fetchone()
    if not fam:
        return jsonify({'error': 'Ese alumno no es titular de ningún grupo familiar'}), 404
    miem = db.execute(
        "SELECT %s, fm.relacion,"
        "  (CASE WHEN f.titular_id=u.id THEN 1 ELSE 0 END) AS es_titular"
        " FROM familia_miembros fm"
        " JOIN users u ON u.id=fm.user_id"
        " JOIN familias f ON f.id=fm.familia_id"
        " WHERE fm.familia_id=?"
        " ORDER BY es_titular DESC, u.nombre" % columnas_de_users('u.'),
        (fam['id'],)).fetchall()
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
        if md.get('beca'):
            continue
        if db.execute('SELECT COUNT(*) AS n FROM pagos WHERE alumno_id=? AND mes=? AND anio=?',
                      (m['id'], mes, anio)).fetchone()['n']:
            continue
        base, desc, final = familia_cuota(md, len(miem))
        monto = final
        if monto <= 0:
            continue
        _, cargo, monto_final = calcular_demora(monto, mes, anio)
        if not as_bool(data.get('aplicar_cargo'), default=False):
            monto_final = monto
        pago_monto = int(round(monto_final))
        db.execute(
            """INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (m['id'], profesor_id, pago_monto, mes, anio, metodo, 'Cuota mensual',
             nota or ('Familia %s' % fam['nombre']), now, who['id']))
        pid_pago = db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
        _registrar_reparto(db, pid_pago, m['id'], pago_monto, ids or None)
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
    for pid in ids:
        if pid == who['id']:
            continue
        try:
            profe = db.execute('SELECT nombre FROM users WHERE id=?', (pid,)).fetchone()
            if profe:
                notify(pid, 'Recibiste un pago',
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
        "SELECT %s FROM users WHERE role IN ('alumno','profesor') AND activo=1 AND cuota_mensual IS NOT NULL ORDER BY nombre"
        % columnas_de_users()).fetchall()
    hoy = _hoy_academy()
    pagos_mes = _pagos_del_mes(hoy.month, hoy.year)
    deudores = []
    for r in rows:
        if en_pausa(r):
            continue
        st = cuota_status(r, pagos_mes)
        if st['estado'] in ('deuda', 'por_vencer'):
            deudores.append({
                **user_public(r),
                'estado': st['estado'],
                'cuota': st['cuota'],
                'dias_deuda': dias_deuda(r, pagos_mes),
            })
    return jsonify({'deudores': deudores})


@app.route('/api/notify_deuda', methods=['POST'])
@role_required('admin', 'profesor')
def api_notify_deuda():
    # Si algo anterior en el ciclo del request fallo en Postgres, su
    # transaccion quedo abortada: recuperarla antes de tocar la base.
    _recuperar_conexion()
    data = parse_json()
    hoy = _hoy_academy()
    pagos_mes = _pagos_del_mes(hoy.month, hoy.year)
    alumno_id = to_int(data.get('alumno_id'))
    if alumno_id:
        ids = [alumno_id]
    else:
        rows = get_db().execute(
            "SELECT %s FROM users WHERE role IN ('alumno','profesor') AND activo=1 AND cuota_mensual IS NOT NULL"
            % columnas_de_users()).fetchall()
        ids = [r['id'] for r in rows if not en_pausa(r) and cuota_status(r, pagos_mes)['estado'] in ('deuda', 'por_vencer')]
    who = current_user()['nombre']
    enviados = 0
    errores = 0
    for aid in ids:
        try:
            alumno = get_db().execute('SELECT * FROM users WHERE id=?', (aid,)).fetchone()
            if not alumno:
                continue
            st = cuota_status(alumno, pagos_mes)
            if st.get('estado') == 'becado':
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
        "SELECT %s FROM users WHERE role IN ('alumno','profesor') AND activo=1"
        % columnas_de_users()).fetchall()}
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
    # Dos agregados en vez de 2 queries por alumno por mes: con 40 alumnos eran
    # ~490 round-trips a la DB y la seccion se quedaba en blanco varios segundos
    # (no hay estado de carga mientras la promesa tarda).
    primer = '%04d-%02d-01' % (meses[0]['anio'], meses[0]['mes'])
    ultimo = _rango_mes(meses[-1])[1]
    asist = {}
    for r in db.execute(
            "SELECT alumno_id, substr(fecha,1,7) AS ym, COUNT(*) AS n FROM asistencia"
            " WHERE presente=1 AND fecha>=? AND fecha<?"
            " GROUP BY alumno_id, substr(fecha,1,7)",
            (primer, ultimo)).fetchall():
        asist[(r['alumno_id'], r['ym'])] = r['n']
    dias = {r['ym']: r['n'] for r in db.execute(
            "SELECT substr(fecha,1,7) AS ym, COUNT(DISTINCT fecha) AS n FROM asistencia"
            " WHERE presente=1 AND fecha>=? AND fecha<?"
            " GROUP BY substr(fecha,1,7)",
            (primer, ultimo)).fetchall()}
    ym_mes = ['%04d-%02d' % (m['anio'], m['mes']) for m in meses]
    dias_con_clases = [dias.get(k, 0) for k in ym_mes]
    rows = db.execute(
        "SELECT %s,"
        "  (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS total_asist"
        " FROM users u WHERE u.role IN ('alumno','profesor') AND u.activo=1"
        " ORDER BY total_asist DESC LIMIT 40"
        % columnas_de_users('u.')).fetchall()
    alumnos = []
    for r in rows:
        serie = []
        for k in ym_mes:
            n, dias_mes = asist.get((r['id'], k), 0), dias.get(k, 0)
            serie.append({'asist': n, 'dias': dias_mes,
                          'pct': round(n * 100 / dias_mes) if dias_mes else None})
        d = user_public(r)
        d['total_asist'] = r['total_asist']
        d['en_pausa'] = en_pausa(r)
        d['serie'] = serie
        alumnos.append(d)
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
        """SELECT a.id, a.mes, a.anio, a.monto, a.nota, a.estado, a.fecha, a.profesor_id,
                  a.profesor_ids, pr.nombre AS profesor_nombre
           FROM avisos_pago a LEFT JOIN users pr ON pr.id=a.profesor_id
           WHERE a.alumno_id=? AND a.estado='pendiente' ORDER BY a.id DESC LIMIT 1""",
        (u['id'],)).fetchone()
    # Con la acreditación automática el comprobante ya no queda pendiente: sin
    # esto el alumno volvía a ver el botón "mandar comprobante" de un pago que
    # el propio sistema ya le acreditó. Sin la columna comprobante a propósito:
    # es un data-URL de hasta 12MB y la pantalla no lo usa.
    ultimo = get_db().execute(
        """SELECT a.id, a.mes, a.anio, a.monto, a.nota, a.estado, a.fecha,
                  a.confirmado_fecha, a.profesor_id, a.profesor_ids,
                  pr.nombre AS profesor_nombre
           FROM avisos_pago a LEFT JOIN users pr ON pr.id=a.profesor_id
           WHERE a.alumno_id=? ORDER BY a.id DESC LIMIT 1""",
        (u['id'],)).fetchone()

    def armar_aviso(row):
        d = dict(row)
        sel = _ids_desde(d.get('profesor_ids')) or ([d['profesor_id']] if d.get('profesor_id') else [])
        d['profesor_ids'] = sel
        if sel:
            nom = _nombres_profes(get_db(), sel)
            lista = [nom[i] for i in sel if i in nom]
            d['profesor_nombre'] = ', '.join(lista) if lista else d.get('profesor_nombre')
        return d

    pagos = [dict(r) for r in rows]
    # Con varios profes elegidos, pagos.profesor_id guarda solo al primero:
    # los demas cobran igual via pago_reparto, asi que se muestran todos.
    por_pago = {}
    if pagos:
        marks = ','.join('?' for _ in pagos)
        rr = get_db().execute(
            "SELECT pr.pago_id, u.nombre FROM pago_reparto pr "
            "JOIN users u ON u.id=pr.profesor_id WHERE pr.pago_id IN (%s) ORDER BY pr.id" % marks,
            [p['id'] for p in pagos]).fetchall()
        for x in rr:
            por_pago.setdefault(x['pago_id'], []).append(x['nombre'])
    for p in pagos:
        lista = por_pago.get(p['id'])
        if lista:
            p['profesor_nombre'] = ', '.join(lista)
    return jsonify({'pagos': pagos,
                    'aviso_pendiente': armar_aviso(aviso) if aviso else None,
                    'ultimo_aviso': armar_aviso(ultimo) if ultimo else None})


def _aviso_profesores(aviso):
    """Profesor(es) que eligio el alumno al mandar el comprobante ([] si nadie).

    Lee 'profesor_ids' (CSV) y, si ese aviso es viejo y no lo tiene, cae en
    'profesor_id' (un solo profesor). Se lee con try/except a proposito: el
    aviso puede venir de un SELECT * de una base todavia sin la columna (o de
    un dict armado a mano en un test), y eso no tiene que tumbar la
    acreditacion. Devuelve IDs validos, sin repetir.
    """
    ids = []
    try:
        if 'profesor_ids' in aviso.keys():
            ids = _ids_desde(aviso['profesor_ids'])
    except Exception:
        ids = []
    if not ids:
        try:
            uno = to_int(aviso['profesor_id']) or None
        except Exception:
            uno = None
        if uno:
            ids = [uno]
    return ids


def _aviso_profesor(aviso):
    """Profesor principal del aviso (el primero elegido, o None).

    Es el que se guarda en pagos.profesor_id para la columna "Profesor que
    recibio"; con varios elegidos, los demas cobran igual via
    _aviso_profesores -> _reparto_por_actividades.
    """
    ids = _aviso_profesores(aviso)
    return ids[0] if ids else None


# Columnas de avisos_pago que NO son el comprobante. El comprobante es un
# data-URL de hasta 12 MB: leerlo de mas cuesta egress de la base por cada
# confirmacion, borrado o webhook. El archivo se pide solo en
# /api/avisos_pago/<id>/comprobante.
_AVISO_COLS = ('id, alumno_id, monto, mes, anio, nota, estado, fecha, '
               'confirmado_por, confirmado_fecha, profesor_id, profesor_ids')


def _acreditar_aviso(db, aviso, monto_base, quien_id=None, aplicar_cargo=None):
    """Registra el pago de un aviso de pago y lo deja confirmado.

    La crea el alumno al mandar el comprobante (quien_id=None, automatico) o el
    admin/profe al revisarlo. Devuelve (base, cargo, final, pago_id, partes).
    aplicar_cargo=None usa el recargo que corresponde por fecha; False lo anula
    (el admin lo desmarca cuando el alumno pago antes del vencimiento).
    El/los profesores que eligio el alumno al mandar el comprobante quedan
    reflejados en el pago: el primero figura en pagos.profesor_id y todos
    dividen el 60% en partes iguales; sin eleccion el reparto es por
    actividades como siempre.
    """
    base, cargo, final = calcular_demora(monto_base, aviso['mes'], aviso['anio'])
    if aplicar_cargo is False:
        cargo, final = 0, base
    elegidos = _aviso_profesores(aviso)
    profe_id = elegidos[0] if elegidos else None
    concepto = ('Acreditado automaticamente al recibir el comprobante' if quien_id is None
                else 'Confirmado desde aviso de pago')
    if cargo:
        concepto += f' (recargo por demora ${cargo:,.0f})'.replace(',', '.')
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        'INSERT INTO pagos(alumno_id, profesor_id, monto, mes, anio, metodo, concepto, nota, fecha, registrado_por) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (aviso['alumno_id'], profe_id, final, aviso['mes'], aviso['anio'], 'Aviso', 'Cuota mensual',
         concepto, ahora, quien_id))
    pago_id = db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    # Sin esto el pago entra a la academia pero los profes que dirigieron las
    # actividades del alumno nunca lo cobraban: el alumno pagaba y el profe no
    # veía nada en su reparto.
    partes = _registrar_reparto(db, pago_id, aviso['alumno_id'], final,
                                profesor_manual_id=elegidos or None)
    db.execute(
        "UPDATE avisos_pago SET estado='confirmado', confirmado_por=?, confirmado_fecha=? WHERE id=?",
        (quien_id, ahora, aviso['id']))
    return base, cargo, final, pago_id, partes


@app.route('/api/avisar_pago', methods=['POST'])
@login_required
def api_avisar_pago():
    u = current_user()
    data = parse_json()
    db = get_db()
    hoy = _hoy_academy()
    mes = to_int(data.get('mes')) or hoy.month
    anio = to_int(data.get('anio')) or hoy.year
    mes, anio, err = validar_mes_anio(mes, anio)
    if err:
        return jsonify({'error': err}), 400
    # El bloqueo tiene que mirar los avisos de cualquier estado y tambien la
    # tabla pagos. Antes solo frenaba los pendientes: como ahora la acreditacion
    # es automatica, un aviso ya confirmado no impedia el segundo envio y el
    # alumno se cobraba dos veces el mismo mes.
    ex = db.execute(
        'SELECT estado FROM avisos_pago WHERE alumno_id=? AND mes=? AND anio=? ORDER BY id DESC LIMIT 1',
        (u['id'], mes, anio)).fetchone()
    if ex:
        return jsonify({'error': f'Ya mandaste el comprobante de {mes}/{anio}. '
                                 + ('Esperá la confirmación del profe/admin.'
                                    if ex['estado'] == 'pendiente'
                                    else 'Tu cuota de ese mes ya está acreditada.')}), 400
    if db.execute('SELECT id FROM pagos WHERE alumno_id=? AND mes=? AND anio=? LIMIT 1',
                  (u['id'], mes, anio)).fetchone():
        return jsonify({'error': f'La cuota de {mes}/{anio} ya figura como pagada.'}), 400
    monto = to_float(data.get('monto'))
    if not monto:
        monto = to_float(u['cuota_mensual']) or 0
    comp = txt_str(data.get('comprobante'))
    if not comprobante_valido(comp):
        return jsonify({'error': 'Tenés que subir el comprobante de pago (foto, captura o PDF)'}), 400
    if len(comp) > 12 * 1024 * 1024:
        return jsonify({'error': 'El comprobante es muy grande (máx 12MB)'}), 400
    # El alumno elige a qué profesor(es) le está pagando: el 60% de profesores
    # se divide en partes iguales SOLO entre los elegidos (uno elegido = 60%
    # entero). Es obligatorio: si no hay eleccion, el reparto por actividades
    # sigue disponible para los pagos viejos y el webhook, pero el alumno no
    # puede mandar un comprobante sin decir a quién le paga.
    ids, err = _leer_profesores(data)
    if err:
        return jsonify({'error': err}), 400
    if not ids:
        return jsonify({'error': 'Elegí al menos un profesor'}), 400
    nombres = _nombres_profes(db, ids)
    if len(nombres) != len(ids):
        return jsonify({'error': 'Ese profesor no existe'}), 400
    profe_id = ids[0]
    csv_ids = ','.join(str(i) for i in ids)
    ahora = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    db.execute(
        'INSERT INTO avisos_pago(alumno_id, monto, mes, anio, nota, comprobante, estado, fecha, profesor_id, profesor_ids) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (u['id'], monto, mes, anio, data.get('nota') or 'Cuota mensual', comp, 'pendiente', ahora,
         profe_id, csv_ids))
    aviso_id = db.execute('SELECT last_insert_rowid() AS id').fetchone()['id']
    staff = db.execute(
        "SELECT id FROM users WHERE role IN ('admin','profesor') AND activo=1").fetchall()

    # Si la cuota ya vencio no se acredita sola: el recargo por demora es una
    # decision del admin. Queda pendiente y se le avisa para que lo revise.
    if _vencido(mes, anio, hoy):
        db.commit()
        todos = ', '.join(nombres.get(i, '') for i in ids)
        detalle = f' El alumno indicó que le paga a {todos}'
        for s in staff:
            notify(s['id'], '⏰ Comprobante vencido por revisar',
                   f'{u["nombre"]} mandó el comprobante de {mes}/{anio} y esa cuota venció el '
                   f'{_vencimiento(mes, anio).day}. Revisá el comprobante y confirmá el pago '
                   f'(con o sin recargo por demora).' + detalle, 'pago')
        for pid in ids:
            if pid == u['id']:
                continue
            notify(pid, '🧾 Comprobante tuyo para revisar',
                   f'{u["nombre"]} mandó el comprobante de la cuota {mes}/{anio} y te indicó a vos '
                   f'como profesor. La cuota está vencida, así que el admin la confirma antes de '
                   f'que se acredite.', 'pago')
        return jsonify({'ok': True, 'auto': False, 'aviso_id': aviso_id,
                        'msg': 'Comprobante enviado. La cuota ya venció, así que el '
                               'profe/admin tiene que revisarlo antes de acreditarlo.'})

    aviso = db.execute('SELECT %s FROM avisos_pago WHERE id=?' % _AVISO_COLS,
                       (aviso_id,)).fetchone()
    base, cargo, final, pago_id, partes = _acreditar_aviso(db, aviso, monto)
    db.commit()
    notify(u['id'], 'Pago acreditado ✓',
           f'Recibimos tu comprobante de la cuota {mes}/{anio} por '
           f'${final:,.0f} y quedó acreditada al instante.'.replace(',', '.'), 'pago')
    # Si el alumno eligió al profe, ese es el único que cobra: se lo avisamos
    # para que no se entere recién cuando liquide. El staff igual se entera.
    for s in staff:
        notify(s['id'], '🧾 Comprobante acreditado automáticamente',
               f'{u["nombre"]} mandó el comprobante de {mes}/{anio} por '
               f'${final:,.0f} y se acreditó solo. Queda guardado el comprobante por '
               f'si lo querés revisar.'.replace(',', '.'), 'pago')
    for pid in ids:
        if pid == u['id']:
            continue
        toca = next((p[1] for p in partes if p[0] == pid), None)
        notify(pid, '💰 Te pagaron la cuota',
               f'{u["nombre"]} te acreditó la cuota {mes}/{anio}'
               + (f' y te corresponde ${toca:,.0f} de reparto'.replace(',', '.') if toca else '')
               + '.', 'pago')
    return jsonify({'ok': True, 'auto': True, 'aviso_id': aviso_id, 'base': base,
                    'cargo': cargo, 'monto': final, 'pago_id': pago_id,
                    'profesor_id': profe_id, 'profesor_ids': ids})


@app.route('/api/avisos_pago', methods=['GET'])
@role_required('admin', 'profesor')
def api_avisos_pago():
    # Sin la columna comprobante a proposito: es un data-URL de hasta 12MB y
    # mandarlos todos en el listado hacia que la pantalla de pagos cargara
    # megabytes de imagenes. El archivo se pide on-demand con
    # /api/avisos_pago/<id>/comprobante. Los pendientes van primero.
    rows = get_db().execute(
        """SELECT a.id, a.alumno_id, a.monto, a.mes, a.anio, a.nota, a.estado, a.fecha,
                  a.confirmado_por, a.confirmado_fecha, a.profesor_id, a.profesor_ids,
                  pr.nombre AS profesor_nombre,
                  (a.comprobante IS NOT NULL) AS tiene_comprobante,
                  u.nombre AS alumno_nombre
           FROM avisos_pago a
           JOIN users u ON u.id=a.alumno_id
           LEFT JOIN users pr ON pr.id=a.profesor_id
           ORDER BY a.estado='pendiente' DESC, a.id DESC LIMIT 300""").fetchall()
    avisos = []
    todos = set()
    for r in rows:
        d = dict(r)
        # varios profes elegidos -> lista de IDs + el nombre de cada uno
        sel = _ids_desde(d.get('profesor_ids')) or ([d['profesor_id']] if d.get('profesor_id') else [])
        d['profesor_ids'] = sel
        d['profesor_nombre'] = None
        todos.update(sel)
        avisos.append(d)
    nombres = _nombres_profes(get_db(), todos)
    for d in avisos:
        nom = [nombres[i] for i in d['profesor_ids'] if i in nombres]
        d['profesor_nombre'] = ', '.join(nom) if nom else None
    return jsonify({'avisos': avisos})


@app.route('/api/avisos_pago/<int:aid>/comprobante', methods=['GET'])
@login_required
def api_aviso_comprobante(aid):
    """Devuelve el comprobante de un aviso, para el admin o el propio alumno."""
    db = get_db()
    a = db.execute(
        """SELECT a.*, u.nombre AS alumno_nombre, pr.nombre AS profesor_nombre
           FROM avisos_pago a
           JOIN users u ON u.id=a.alumno_id
           LEFT JOIN users pr ON pr.id=a.profesor_id
           WHERE a.id=?""",
        (aid,)).fetchone()
    if not a:
        return jsonify({'error': 'Aviso no encontrado'}), 404
    if a['alumno_id'] != current_user()['id'] and current_user()['role'] == 'alumno':
        return jsonify({'error': 'No es tu comprobante'}), 403
    if not a['comprobante']:
        return jsonify({'error': 'Este aviso no tiene comprobante'}), 404
    sel = _ids_desde(a['profesor_ids'] if 'profesor_ids' in a.keys() else None)
    if not sel and a['profesor_id']:
        sel = [a['profesor_id']]
    nombres = _nombres_profes(db, sel)
    nom = [nombres[i] for i in sel if i in nombres]
    return jsonify({
        'ok': True, 'id': a['id'], 'comprobante': a['comprobante'],
        'alumno_nombre': a['alumno_nombre'], 'mes': a['mes'], 'anio': a['anio'],
        'monto': a['monto'], 'nota': a['nota'], 'estado': a['estado'], 'fecha': a['fecha'],
        'confirmado_fecha': a['confirmado_fecha'],
        'profesor_id': a['profesor_id'], 'profesor_ids': sel,
        'profesor_nombre': ', '.join(nom) if nom else a['profesor_nombre'],
    })


@app.route('/api/avisos_pago/<int:aid>/confirmar', methods=['POST'])
@role_required('admin', 'profesor')
def api_avisos_confirmar(aid):
    db = get_db()
    a = db.execute('SELECT %s FROM avisos_pago WHERE id=?' % _AVISO_COLS, (aid,)).fetchone()
    if not a:
        return jsonify({'error': 'Aviso no encontrado'}), 404
    if a['estado'] == 'confirmado':
        return jsonify({'error': 'Este aviso ya fue confirmado'}), 400
    who = current_user()
    data = parse_json()
    # El admin puede cambiar la eleccion del alumno: es la ultima palabra sobre
    # quienes cobran el 60%. Acepta varios (profesor_ids) o uno solo
    # (profesor_id); mandar null/0 lo limpia y el reparto vuelve a ser por
    # actividades, como siempre.
    if 'profesor_id' in data or 'profesor_ids' in data:
        ids, err = _leer_profesores(data)
        if err:
            return jsonify({'error': err}), 400
        nombres = _nombres_profes(db, ids)
        if len(nombres) != len(ids):
            return jsonify({'error': 'Ese profesor no existe'}), 400
        db.execute('UPDATE avisos_pago SET profesor_id=?, profesor_ids=? WHERE id=?',
                   (ids[0] if ids else None,
                    ','.join(str(i) for i in ids) if ids else None, aid))
        a = db.execute('SELECT %s FROM avisos_pago WHERE id=?' % _AVISO_COLS, (aid,)).fetchone()
    # El admin puede ajustar el monto real (ej: pagó con el valor de la cuota
    # anterior) y decidir si se suma el aumento/recargo por demora (por defecto
    # se suma como antes; se desactiva si el alumno pagó antes del vencimiento).
    monto_base = to_float(data.get('monto')) or (a['monto'] or 0)
    aplicar_cargo = as_bool(data.get('aplicar_cargo'), default=False)
    base, cargo, final, _pid2, partes = _acreditar_aviso(db, a, monto_base, quien_id=who['id'],
                                                        aplicar_cargo=aplicar_cargo)
    db.commit()
    nota = f' (incluye ${cargo:,.0f} de recargo por demora)'.replace(',', '.') if cargo else ''
    notify(a['alumno_id'], 'Pago confirmado',
           f'Tu aviso de pago de la cuota {a["mes"]}/{a["anio"]} por ${final:,.0f} fue confirmado por {who["nombre"]}{nota}.'.replace(',', '.'),
           'pago')
    for pid2, parte, _act, _nombre in partes:
        if pid2 == who['id']:
            continue
        notify(pid2, '💰 Te pagaron la cuota',
               f'{who["nombre"]} confirmó la cuota {a["mes"]}/{a["anio"]} de un alumno y te '
               f'corresponde ${parte:,.0f} de reparto.'.replace(',', '.'), 'pago')
    return jsonify({'ok': True, 'base': base, 'cargo': cargo, 'monto': final,
                    'profesor_id': _aviso_profesor(a), 'profesor_ids': _aviso_profesores(a)})


@app.route('/api/avisos_pago/<int:aid>', methods=['DELETE'])
@role_required('admin')
def api_avisos_delete(aid):
    a = get_db().execute('SELECT %s FROM avisos_pago WHERE id=?' % _AVISO_COLS, (aid,)).fetchone()
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
    # Porcentajes de alumnos. El numerador tiene que salir de la MISMA poblacion
    # que el denominador: antes contaba como "pagaron" a cualquiera con un pago
    # del mes, incluso uno dado de baja, y el reporte llegava a 110%. Y el total
    # se limita a los que tienen cuota cargada, como la lista de deudores, para
    # que las dos cosas cuadren entre si.
    universo = get_db().execute(
        "SELECT id FROM users WHERE role IN ('alumno','profesor') AND activo=1 "
        "AND cuota_mensual IS NOT NULL").fetchall()
    universo_ids = {r['id'] for r in universo}
    total_alumnos = len(universo_ids)
    ids_deudores = {d['id'] for d in deudores}
    ids_pagaron = {p['alumno_id'] for p in pagos} & universo_ids
    cant_pagaron = len(ids_pagaron)
    cant_no_pagaron = len(ids_deudores)

    def pct(n, d):
        return min(100, round(n * 100 / d)) if d else 0

    pct_pagaron = pct(cant_pagaron, total_alumnos)
    pct_no_pagaron = pct(cant_no_pagaron, total_alumnos)
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
    pct_asistieron = pct(cant_asistieron, total_alumnos)
    pct_no_asistieron = pct(cant_no_asistieron, total_alumnos)
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
    genero = (data.get('genero', u['genero']) or '').strip().upper()[:1]
    if genero not in ('M', 'F'):
        genero = ''
    cat = data.get('categoria', u['categoria'])
    # El alumno podia mandarse una categoria cualquiera y romper los filtros de
    # videos, cinturones y los chats por categoria.
    if cat not in CATEGORIAS:
        return jsonify({'error': 'Categoría inválida'}), 400
    cinturon = u['cinturon']
    if u['role'] != 'alumno':
        nuevo_cinturon = txt_str(data.get('cinturon'))
        if nuevo_cinturon:
            if nuevo_cinturon not in CINTURONES:
                return jsonify({'error': 'Cinturón inválido'}), 400
            cinturon = nuevo_cinturon
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
        'UPDATE users SET nombre=?, edad=?, peso=?, cinturon=?, categoria=?, gi_pref=?, actividades=?, genero=?, tel=?, nacimiento=?, medic_info=?, emergency_contact=?, tel_tutor=?, tel_2=?, direccion=?, dni=?, foto_ok=?, medic_enfermedades=?, medic_alergias=?, medic_medicacion=?, medic_lesiones=?, ficha_fecha=? WHERE id=?',
        ((data.get('nombre') or u['nombre']), to_int(data.get('edad', u['edad'])),
         to_float(data.get('peso', u['peso'])), cinturon,
         cat, data.get('gi_pref', u['gi_pref']), _actividades_csv(data), genero,
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
        'actividad': v.get('actividad') if hasattr(v, 'get') else v['actividad'],
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


def _list_videos(u, belt=None, categoria=None, actividad=None):
    db = get_db()
    q = ('SELECT v.id, v.titulo, v.descripcion, v.belt, v.categoria, v.url, v.tipo, v.subido_por, v.fecha, '
         'v.actividad, s.nombre AS subidor_nombre FROM videos v '
         'LEFT JOIN users s ON s.id = v.subido_por ')
    args = []
    where = []
    if u['role'] == 'alumno':
        where.append("v.categoria = ?")
        args.append(u['categoria'])
        where.append("(v.belt = 'Todos' OR v.belt = ?)")
        args.append(u['cinturon'])
        # el alumno solo ve videos de las actividades que entrena (o los generales)
        acts = [a.strip() for a in (u.get('actividades') or '').split(',') if a.strip()]
        if acts:
            marks = ','.join('?' * len(acts))
            where.append("(v.actividad IS NULL OR v.actividad = '' OR v.actividad IN (%s))" % marks)
            args.extend(acts)
    else:
        if belt and belt != 'Todos':
            where.append('v.belt = ?')
            args.append(belt)
        if categoria and categoria != 'Todas':
            where.append('v.categoria = ?')
            args.append(categoria)
        if actividad and actividad != 'Todas':
            where.append('v.actividad = ?')
            args.append(actividad)
    if where:
        q += ' WHERE ' + ' AND '.join(where)
    q += ' ORDER BY v.id DESC'
    rows = db.execute(q, args).fetchall()
    return [_video_public(r, u) for r in rows]


@app.route('/api/videos')
@login_required
def api_videos_list():
    u = current_user()
    return jsonify({'videos': _list_videos(u, request.args.get('belt'), request.args.get('categoria'),
                                           request.args.get('actividad'))})


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
        'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha, actividad) VALUES(?,?,?,?,?,?,?,?,?)',
        (titulo, txt_str(data.get('descripcion')), (data.get('belt') or 'Todos'),
         (data.get('categoria') or 'adulto'), url, 'link', u['id'], now,
         txt_str(data.get('actividad')) or None))
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
    actividad = (request.form.get('actividad') or '').strip() or None
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
                'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha, data, actividad) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (titulo, desc, belt, categoria, pub, 'upload', u['id'], now, '', actividad))
            db.commit()
            return jsonify({'ok': True, 'id': cur.lastrowid})
        f.stream.seek(0)
    raw = f.read()
    data_b64 = base64.b64encode(raw).decode('ascii')
    cur = db.execute(
        'INSERT INTO videos(titulo, descripcion, belt, categoria, url, tipo, subido_por, fecha, data, actividad) VALUES(?,?,?,?,?,?,?,?,?,?)',
        (titulo, desc, belt, categoria, '/api/video/0/archivo', 'upload', u['id'], now, data_b64, actividad))
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
    v = db.execute('SELECT belt, tipo, subido_por, titulo FROM videos WHERE id=?',
                   (vid,)).fetchone()
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
    v = db.execute('SELECT subido_por, url FROM videos WHERE id=?', (vid,)).fetchone()
    if not v:
        return jsonify({'error': 'Video no encontrado'}), 404
    if not es_admin(u) and v['subido_por'] != u['id']:
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
        # dinero del profesor: su parte del reparto (si no hay reparto legacy,
        # cae a los pagos que tiene asignados como profesor_id).
        propio = get_db().execute(
            """SELECT COALESCE(SUM(r.monto),0) AS n FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id
               WHERE r.profesor_id=? AND p.mes=? AND p.anio=?""",
            (u['id'], hoy.month, hoy.year)).fetchone()['n']
        legacy_mes = get_db().execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE profesor_id=? AND mes=? AND anio=?',
            (u['id'], hoy.month, hoy.year)).fetchone()['n']
        if not propio and legacy_mes:
            propio = legacy_mes
        total_propio = get_db().execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pago_reparto WHERE profesor_id=?',
            (u['id'],)).fetchone()['n']
        legacy_total = get_db().execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE profesor_id=?',
            (u['id'],)).fetchone()['n']
        if not total_propio and legacy_total:
            total_propio = legacy_total
        return jsonify({
            'total_alumnos': total_alumnos, 'ingresos_mes': ingresos_mes,
            'clases': clases, 'mi_ingreso_mes': propio, 'mi_ingreso_total': total_propio})
    return jsonify({'total_alumnos': total_alumnos, 'ingresos_mes': ingresos_mes, 'clases': clases})


@app.route('/api/mi_dinero')
@role_required('admin', 'profesor')
def api_mi_dinero():
    """Detalle del dinero del profesor (su parte del 60%) y, para el admin,
    cómo se dividió todo lo cobrado: profes / tatami y academia / administrativo."""
    u = current_user()
    hoy = _hoy_academy()
    mes = to_int(request.args.get('mes')) or hoy.month
    anio = to_int(request.args.get('anio')) or hoy.year
    db = get_db()
    cobrado_mes, destinos = None, []
    if es_admin(u):
        filas = db.execute(
            """SELECT r.monto, r.actividad, r.fecha, p.mes, p.anio, p.metodo,
                      p.concepto, a.nombre AS alumno, pr.nombre AS profesor, r.profesor_id
               FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id
               JOIN users a ON a.id=p.alumno_id
               LEFT JOIN users pr ON pr.id=r.profesor_id
               ORDER BY p.fecha DESC""").fetchall()
        tot_mes = db.execute(
            """SELECT COALESCE(SUM(r.monto),0) AS n FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id WHERE p.mes=? AND p.anio=?""",
            (mes, anio)).fetchone()['n']
        por_profe = db.execute(
            """SELECT pr.nombre, COALESCE(SUM(r.monto),0) AS total
               FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id
               LEFT JOIN users pr ON pr.id=r.profesor_id
               WHERE p.mes=? AND p.anio=? GROUP BY r.profesor_id, pr.nombre
               ORDER BY total DESC""", (mes, anio)).fetchall()
        cobrado_mes = db.execute(
            'SELECT COALESCE(SUM(monto),0) AS n FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchone()['n']
        destinos = db.execute(
            """SELECT d.destino AS destino, COALESCE(SUM(d.monto),0) AS monto
               FROM pago_destino d
               JOIN pagos p ON p.id=d.pago_id
               WHERE p.mes=? AND p.anio=?
               GROUP BY d.destino ORDER BY d.destino""", (mes, anio)).fetchall()
    else:
        filas = db.execute(
            """SELECT r.monto, r.actividad, r.fecha, p.mes, p.anio, p.metodo,
                      p.concepto, a.nombre AS alumno, pr.nombre AS profesor, r.profesor_id
               FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id
               JOIN users a ON a.id=p.alumno_id
               LEFT JOIN users pr ON pr.id=r.profesor_id
               WHERE r.profesor_id=? ORDER BY p.fecha DESC""", (u['id'],)).fetchall()
        tot_mes = db.execute(
            """SELECT COALESCE(SUM(r.monto),0) AS n FROM pago_reparto r
               JOIN pagos p ON p.id=r.pago_id
               WHERE r.profesor_id=? AND p.mes=? AND p.anio=?""",
            (u['id'], mes, anio)).fetchone()['n']
        por_profe = []
    return jsonify({
        'mes': mes, 'anio': anio, 'total_mes': tot_mes,
        'cobrado_mes': cobrado_mes,
        'destinos': [dict(x) for x in destinos],
        'pct': {'profes': PCT_PROFES, 'tatami': PCT_TATAMI, 'administrativo': PCT_ADMIN},
        'pagos': [dict(f) for f in filas],
        'por_profesor': [dict(x) for x in por_profe],
    })


@app.route('/api/ingresos_extra')
@role_required('admin', 'profesor')
def api_ingresos_extra_list():
    """Cobros puntuales para el fondo de la academia (cuota de un dia, ayuda a
    alumno que compite, seminarios). NO son cuota mensual ni se reparten."""
    u = current_user()
    hoy = _hoy_academy()
    mes = to_int(request.args.get('mes')) or hoy.month
    anio = to_int(request.args.get('anio')) or hoy.year
    db = get_db()
    where, args = '', []
    if u['role'] == 'profesor':
        where = 'WHERE ie.registrado_por=?'
        args = [u['id']]
    filas = db.execute(
        """SELECT ie.*, a.nombre AS alumno FROM ingresos_extra ie
           LEFT JOIN users a ON a.id=ie.alumno_id %s
           ORDER BY ie.fecha DESC""" % where, args).fetchall()
    total_mes = db.execute(
        'SELECT COALESCE(SUM(monto),0) AS n FROM ingresos_extra WHERE mes=? AND anio=?',
        (mes, anio)).fetchone()['n']
    total_all = db.execute('SELECT COALESCE(SUM(monto),0) AS n FROM ingresos_extra').fetchone()['n']
    por_destino = db.execute(
        """SELECT COALESCE(destino,'Sin destino') AS destino, COALESCE(SUM(monto),0) AS total
           FROM ingresos_extra GROUP BY destino ORDER BY total DESC""").fetchall()
    return jsonify({'mes': mes, 'anio': anio, 'total_mes': total_mes, 'total_all': total_all,
                    'ingresos': [dict(f) for f in filas],
                    'por_destino': [dict(d) for d in por_destino]})


@app.route('/api/ingresos_extra', methods=['POST'])
@role_required('admin', 'profesor')
def api_ingresos_extra_create():
    data = parse_json()
    monto = to_float(data.get('monto'))
    if not monto or monto <= 0:
        return jsonify({'error': 'El monto es obligatorio'}), 400
    concepto = txt_str(data.get('concepto')) or 'Ingreso extra'
    destino = txt_str(data.get('destino')) or 'Fondo academia'
    hoy = _hoy_academy()
    mes, anio, err = validar_mes_anio(to_int(data.get('mes')) or hoy.month,
                                      to_int(data.get('anio')) or hoy.year)
    if err:
        return jsonify({'error': err}), 400
    get_db().execute(
        """INSERT INTO ingresos_extra(alumno_id, monto, concepto, destino, mes, anio, metodo, nota, fecha, registrado_por)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (to_int(data.get('alumno_id')) or None, monto, concepto, destino, mes, anio,
         data.get('metodo') or 'Efectivo', txt_str(data.get('nota')) or None,
         datetime.now().strftime('%Y-%m-%d %H:%M:%S'), current_user()['id']))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/ingresos_extra/<int:eid>', methods=['DELETE'])
@role_required('admin')
def api_ingresos_extra_delete(eid):
    get_db().execute('DELETE FROM ingresos_extra WHERE id=?', (eid,))
    get_db().commit()
    return jsonify({'ok': True})


@app.route('/api/metricas_pagos')
@role_required('admin', 'profesor')
def api_metricas_pagos():
    anio = to_int(request.args.get('anio')) or _hoy_academy().year
    db = get_db()
    total_alumnos = db.execute(
        "SELECT COUNT(*) AS n FROM users WHERE role IN ('alumno','profesor') "
        "AND activo=1 AND cuota_mensual IS NOT NULL").fetchone()['n']
    # 3 agregados en vez de 3 queries por mes (37 round-trips antes)
    ingresos_mes = {r['mes']: r['n'] for r in db.execute(
        'SELECT mes, COALESCE(SUM(monto),0) AS n FROM pagos WHERE anio=? GROUP BY mes',
        (anio,)).fetchall()}
    cantidad_mes = {r['mes']: r['n'] for r in db.execute(
        'SELECT mes, COUNT(*) AS n FROM pagos WHERE anio=? GROUP BY mes',
        (anio,)).fetchall()}
    # solo cuentan los que hoy siguen activos y con cuota: si no, dar de baja
    # a alguien hace que el mes aparezca con mas del 100% pagado.
    pagaron_mes = {r['mes']: r['n'] for r in db.execute(
        'SELECT p.mes, COUNT(DISTINCT p.alumno_id) AS n FROM pagos p '
        'JOIN users u ON u.id=p.alumno_id '
        "WHERE p.anio=? AND u.role IN ('alumno','profesor') AND u.activo=1 "
        'AND u.cuota_mensual IS NOT NULL GROUP BY p.mes',
        (anio,)).fetchall()}
    serie = []
    for mes in range(1, 13):
        ingresos = ingresos_mes.get(mes, 0)
        cantidad = cantidad_mes.get(mes, 0)
        cant_pagaron = pagaron_mes.get(mes, 0)
        pct_pagaron = min(100, round(cant_pagaron * 100 / total_alumnos)) if total_alumnos else 0
        serie.append({
            'mes': mes,
            'ingresos': ingresos,
            'cantidad': cantidad,
            'cant_pagaron': cant_pagaron,
            'deudores': max(0, total_alumnos - cant_pagaron),
            'pct_pagaron': pct_pagaron,
            'pct_morosidad': min(100, 100 - pct_pagaron),
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
    # 2 agregados en vez de 1-2 queries por chat
    miembros = {m['chat_id']: m['n'] for m in db.execute(
        'SELECT chat_id, COUNT(*) AS n FROM chat_members GROUP BY chat_id').fetchall()}
    otros = {}
    for m in db.execute(
            'SELECT m.chat_id, u.id, u.nombre FROM chat_members m JOIN users u ON u.id=m.user_id '
            'WHERE m.user_id<>? ORDER BY u.id', (u['id'],)).fetchall():
        if m['chat_id'] not in otros:
            otros[m['chat_id']] = m
    for c in rows:
        nombre = c['nombre']
        if c['tipo'] == 'grupo':
            chats.append({'id': c['id'], 'nombre': nombre or 'Grupo', 'tipo': c['tipo'],
                          'miembros': miembros.get(c['id'], 0)})
        else:
            otro = otros.get(c['id'])
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
    # Sin us.foto: se habia pedido y nunca se usaba.
    cols = ('m.id, m.user_id, m.mensaje, (m.adjunto IS NOT NULL) AS tiene_adjunto, '
            'm.adjunto_tipo, m.fecha, us.nombre')
    # El poll de la app corre cada 5 segundos. Con ?desde= solo se devuelven los
    # mensajes nuevos: antes se bajaban los 200 (con sus adjuntos de hasta
    # 25 MB cada uno) en cada tick aunque no hubiera nada nuevo.
    desde = to_int(request.args.get('desde')) or 0
    if desde:
        filas = db.execute(
            'SELECT %s FROM chat_messages m JOIN users us ON us.id=m.user_id '
            'WHERE m.chat_id=? AND m.id>? ORDER BY m.id ASC LIMIT 200' % cols,
            (chat_id, desde)).fetchall()
        return jsonify({'mensajes': [_chat_msg_public(r) for r in filas]})
    filas = db.execute(
        'SELECT %s FROM chat_messages m JOIN users us ON us.id=m.user_id '
        'WHERE m.chat_id=? ORDER BY m.id ASC LIMIT 200' % cols, (chat_id,)).fetchall()
    return jsonify({'mensajes': [_chat_msg_public(r) for r in filas], 'chat': dict(chat)})


def _chat_msg_public(r):
    """El adjunto se pide on-demand: es un data-URL de hasta 25 MB."""
    d = dict(r)
    d['adjunto'] = '/api/chat_adjunto/%d' % d['id'] if d.pop('tiene_adjunto') else None
    return d


@app.route('/api/chat_adjunto/<int:mid>')
@login_required
def api_chat_adjunto(mid):
    u = current_user()
    r = get_db().execute(
        'SELECT m.adjunto FROM chat_messages m '
        'JOIN chat_members cm ON cm.chat_id=m.chat_id AND cm.user_id=? '
        'WHERE m.id=?', (u['id'], mid)).fetchone()
    if not r or not r['adjunto']:
        return jsonify({'error': 'Adjunto no encontrado'}), 404
    return _foto_response(r['adjunto'], default_mime='application/octet-stream')


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
            "SELECT id, nombre, cinturon FROM users WHERE activo=1 AND id<>? AND role IN ('admin','profesor')",
            (u['id'],)).fetchall()
    else:
        rows = get_db().execute(
            "SELECT id, nombre, cinturon, categoria FROM users WHERE activo=1 AND id<>? AND role IN ('alumno','profesor')",
            (u['id'],)).fetchall()
    return jsonify({'contactos': [dict(r, foto='/api/avatar/%d' % r['id']) for r in rows]})


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
    # Nunca SELECT * aca: la columna `data` pesa hasta ~67 MB y este endpoint se
    # llama cada 5 s desde ontimeupdate. Solo hace falta saber quien lo subio.
    v = db.execute('SELECT subido_por, titulo FROM videos WHERE id=?', (vid,)).fetchone()
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
        "SELECT id, nombre, cinturon, categoria FROM users"
        " WHERE activo=1 AND role IN ('alumno','profesor')").fetchall()
    lista = []
    for r in filas:
        punt = (nmap.get(r['id'], 0) * 2) + (cmap.get(r['id'], 0) * 5) + (vmap.get(r['id'], 0) * 1)
        lista.append({'id': r['id'], 'nombre': r['nombre'], 'cinturon': r['cinturon'],
                      'categoria': r['categoria'], 'foto': '/api/avatar/%d' % r['id'],
                      'asistencias': nmap.get(r['id'], 0),
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
    u = _get_alumno(uid)
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
    u = _get_alumno(uid)
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

def _foto_response(data_url, default_mime='image/jpeg'):
    """Sirve un data-URL guardado en la base como imagen normal (bytes + mime).

    El feed referenciaba las fotos como data-URL dentro del JSON, asi que
    /api/muro tenia que bajar TODAS las fotos de TODOS los posts de la base
    aunque el alumno solo mirara los primeros. Ahora el listado manda
    /api/muro_foto/<id> y la base solo se lee para la foto que el navegador
    pide. Igual para los avatares y las fotos de eventos.
    """
    m = re.match(r'^data:([^;]+);base64,(.+)$', data_url or '', re.S)
    if not m:
        return jsonify({'error': 'Imagen dañada'}), 500
    try:
        raw = base64.b64decode(m.group(2))
    except Exception:
        return jsonify({'error': 'Imagen dañada'}), 500
    return Response(raw, mimetype=m.group(1) or default_mime)


@app.route('/api/muro_foto/<int:fid>')
@login_required
def api_muro_foto(fid):
    r = get_db().execute('SELECT data FROM muro_fotos WHERE id=?', (fid,)).fetchone()
    if not r:
        return jsonify({'error': 'Foto no encontrada'}), 404
    return _foto_response(r['data'])


@app.route('/api/evento_foto/<int:fid>')
@login_required
def api_evento_foto(fid):
    r = get_db().execute('SELECT data FROM evento_fotos WHERE id=?', (fid,)).fetchone()
    if not r:
        return jsonify({'error': 'Foto no encontrada'}), 404
    return _foto_response(r['data'])


@app.route('/api/avatar/<int:uid>')
@login_required
def api_avatar(uid):
    r = get_db().execute('SELECT foto FROM users WHERE id=?', (uid,)).fetchone()
    if not r or not r['foto']:
        return jsonify({'error': 'Sin foto de perfil'}), 404
    return _foto_response(r['foto'])


@app.route('/api/muro')
@login_required
def api_muro():
    db = get_db()
    # Sin us.foto: son ~40 KB por fila y aca entran hasta 100 posts. El
    # avatar se pide on-demand igual que las fotos del post.
    filas = db.execute(
        'SELECT m.*, us.nombre, us.cinturon '
        'FROM muro m JOIN users us ON us.id=m.user_id ORDER BY m.id DESC LIMIT 100').fetchall()
    # 2 agregados en vez de 2 queries por post
    fotos_por_muro = {}
    for f in db.execute('SELECT muro_id, id FROM muro_fotos ORDER BY id').fetchall():
        fotos_por_muro.setdefault(f['muro_id'], []).append(f['id'])
    video_por_muro = {}
    for v in db.execute('SELECT muro_id, id, url, tipo FROM muro_videos ORDER BY id').fetchall():
        # "el primero de cada muro", igual que el LIMIT 1 anterior
        if v['muro_id'] not in video_por_muro:
            video_por_muro[v['muro_id']] = v
    out = []
    for r in filas:
        ids = fotos_por_muro.get(r['id'], [])
        v = video_por_muro.get(r['id'])
        vd = {'id': v['id'], 'url': v['url'], 'tipo': v['tipo']} if v else None
        if vd and _is_storage_url(vd['url']):
            vd['url'] = '/api/muro_video/%d' % vd['id']
        out.append({**dict(r), 'foto': '/api/avatar/%d' % r['user_id'],
                    'fotos': ['/api/muro_foto/%d' % i for i in ids], 'video': vd})
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
    # 2 agregados en vez de (1 + una por opcion) por encuesta
    mi_votos = {r['encuesta_id']: r['opcion'] for r in db.execute(
        'SELECT encuesta_id, opcion FROM encuesta_votos WHERE user_id=?', (u['id'],)).fetchall()}
    votos = {}
    for r in db.execute(
            'SELECT encuesta_id, opcion, COUNT(*) AS n FROM encuesta_votos '
            'GROUP BY encuesta_id, opcion').fetchall():
        votos.setdefault(r['encuesta_id'], {})[r['opcion']] = r['n']
    out = []
    for r in filas:
        opciones = json.loads(r['opciones']) if r['opciones'] else []
        c = votos.get(r['id'], {})
        conteo = [c.get(i, 0) for i in range(len(opciones))]
        out.append({**dict(r), 'opciones': opciones, 'conteo': conteo,
                    'mi_voto': mi_votos.get(r['id'])})
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
    # 3 agregados en vez de 3 queries por evento
    asisten = {r['evento_id']: r['n'] for r in db.execute(
        'SELECT evento_id, COUNT(*) AS n FROM evento_asistencias GROUP BY evento_id').fetchall()}
    voy_ids = {r['evento_id'] for r in db.execute(
        'SELECT evento_id FROM evento_asistencias WHERE user_id=?', (u['id'],)).fetchall()}
    # Ids, no data-URLs: el listado de eventos era todavia mas grande que
    # el muro (sin LIMIT de eventos) y aca solo se pide lo que se mira.
    fotos = {}
    for f in db.execute('SELECT evento_id, id FROM evento_fotos ORDER BY id').fetchall():
        fotos.setdefault(f['evento_id'], []).append(f['id'])
    out = []
    for r in filas:
        out.append({**dict(r), 'asisten_conf': asisten.get(r['id'], 0),
                    'voy': 1 if r['id'] in voy_ids else 0,
                    'fotos': ['/api/evento_foto/%d' % i for i in fotos.get(r['id'], [])]})
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
    desc = 'Cuota NEXO MADRYN'
    email = (u.get('username') or '') + '@alumno.local' if '@' not in (u.get('username') or '') else u.get('username')
    base = (get_setting('public_url', '') or '').strip().rstrip('/')
    import urllib.request
    payload = {
        'items': [{'title': desc, 'quantity': 1, 'unit_price': float(cuota), 'currency_id': 'ARS'}],
        'auto_return': 'approved',
        'external_reference': 'cuota-%s-%d' % (u['id'], int(datetime.now().timestamp())),
    }
    if base:
        payload['back_urls'] = {'success': base + '/app', 'failure': base + '/app'}
        payload['notification_url'] = base + '/api/mp_webhook'
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


def _mp_firma_valida(data):
    """Valida la firma que manda MercadoPago (x-signature / x-request-id).

    El endpoint es publico, asi que sin esto cualquiera que conozca la URL puede
    Postear avisos de pago falsos. Si no hay secreto configurado se acepta
    (para no romper una instalacion que todavia no lo tiene), pero queda avisado
    en el log: es una decision consciente, no un olvido.
    """
    import hmac
    import hashlib
    secret = (get_setting('mp_secret', '') or '').strip()
    if not secret:
        _log.warning('mp_secret no esta configurado: el webhook no puede validar la firma')
        return True
    ts = request.headers.get('x-signature', '')
    req_id = request.headers.get('x-request-id', '')
    if not ts or not req_id:
        return False
    # x-signature: ts=...,v1=...
    partes = dict(p.strip().split('=', 1) for p in ts.split(',') if '=' in p)
    ts_val = partes.get('ts', '')
    v1 = partes.get('v1', '')
    if not ts_val or not v1:
        return False
    # La plantilla es fija: ts + el body literal que se recibio.
    crudo = request.get_data() or b''
    if isinstance(crudo, bytes):
        crudo = crudo.decode('utf-8', 'replace')
    objetivo = 'ts:%s;%s' % (ts_val, request.path)
    esperado = hmac.new(secret.encode('utf-8'),
                        ('%s%s' % (objetivo, crudo)).encode('utf-8'),
                        hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, v1)


@app.route('/api/mp_webhook', methods=['POST'])
def api_mp_webhook():
    """Webhook de MercadoPago: registra el aviso de pago cuando se aprueba."""
    try:
        data = request.get_json(silent=True) or {}
        if not _mp_firma_valida(data):
            _log.warning('mp_webhook: firma invalida, se rechaza')
            return jsonify({'ok': False, 'error': 'firma invalida'}), 401
        ext = data.get('external_reference') or ''
        if ext.startswith('cuota-'):
            parts = ext.split('-')
            alumno_id = int(parts[1])
            now = datetime.now().strftime('%Y-%m-%d %H:%M')
            hoy = _hoy_academy()
            db = get_db()
            # Un alumno inexistente reventaba el INSERT por la foreign key y
            # devolvia 500, con lo que MercadoPago reintentaba el POST para
            # siempre. Se responde 200 para cortar los reintentos.
            if not db.execute('SELECT id FROM users WHERE id=?', (alumno_id,)).fetchone():
                _log.warning('mp_webhook: alumno %s no existe, se ignora', alumno_id)
                return jsonify({'ok': True, 'ignorado': 'alumno_inexistente'})
            # Idempotencia: MercadoPago reintenta el POST y cada intento creaba
            # un aviso nuevo. La llave va en la nota, que es lo unico que hay
            # disponible sin cambiar el esquema.
            nota = 'Pago por MercadoPago (ref %s)' % ext
            ya = db.execute(
                'SELECT id FROM avisos_pago WHERE nota=? AND alumno_id=?',
                (nota, alumno_id)).fetchone()
            if ya:
                return jsonify({'ok': True, 'aviso_id': ya['id'], 'duplicado': True})
            # El monto real viene del webhook; antes se guardaba 0 y el admin
            # terminaba adivinando el importe al confirmar.
            monto = to_float((data.get('transaction_amount')
                              or (data.get('data') or {}).get('transaction_amount'))) or 0
            cur = db.execute(
                'INSERT INTO avisos_pago(alumno_id, monto, mes, anio, nota, comprobante, estado, fecha) '
                'VALUES(?,?,?,?,?,?,?,?)',
                (alumno_id, monto, hoy.month, hoy.year, nota, None, 'pendiente', now))
            aviso_id = cur.lastrowid
            aviso = db.execute('SELECT %s FROM avisos_pago WHERE id=?' % _AVISO_COLS,
                               (aviso_id,)).fetchone()
            # La firma de MercadoPago ya valido que la plata entro, asi que el
            # pago se acredita solo. Antes quedaba esperando que alguien lo
            # confirmara a mano y era el paso que mas se atrasaba.
            _b, _c, final, _pid, _partes = _acreditar_aviso(db, aviso, monto)
            db.commit()
            notify(alumno_id, 'Pago acreditado ✓',
                   'Recibimos tu pago por MercadoPago de la cuota %d/%d por $%s y quedó '
                   'acreditado al instante.'
                   % (hoy.month, hoy.year, f'{final:,.0f}'.replace(',', '.')),
                   'pago')
            # notificar a staff
            staff = db.execute("SELECT id FROM users WHERE role IN ('admin','profesor') AND activo=1").fetchall()
            for s in staff:
                notify(s['id'], '🧾 Pago por MercadoPago acreditado',
                       'El alumno pagó por MercadoPago y se acreditó solo (aviso #%d). '
                       'Queda guardado por si lo querés revisar.' % aviso_id,
                       'info', push=True)
        return jsonify({'ok': True})
    except Exception as e:
        _log.exception('MP_WEBHOOK_ERROR: %s', e)
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
    # Si algo previo en este proceso dejo la transaccion abortada (Postgres),
    # recuperar la conexion antes de la primera consulta de este request.
    _recuperar_conexion()
    if alumno_id:
        rows = get_db().execute("SELECT id, nombre FROM users WHERE id=? AND activo=1", (alumno_id,)).fetchall()
    elif quienes == 'todos':
        rows = get_db().execute("SELECT id, nombre FROM users WHERE activo=1").fetchall()
    else:
        rows = get_db().execute("SELECT id, nombre FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchall()
    enviados = 0
    errores = []
    for r in rows:
        try:
            notify(r['id'], titulo, texto, tipo='mensaje', push=envio)
            enviados += 1
        except Exception as e:
            errores.append({'id': r['id'], 'error': str(e)})
    return jsonify({'ok': True, 'destinatarios': len(rows), 'enviados': enviados, 'errores': errores[:5]})


@app.route('/health')
def api_health():
    """Health check de Render: responde 200 sin tocar la base.

    Render lo llama cada ~30 segundos las 24 horas. Apuntaba a
    /api/vapid_public_key, que leia y escribia settings en la BD, asi que
    mantenia el compute de Neon despierto todo el tiempo y agotaba la cuota
    de 100 CU-hours del plan free. Tampoco dispara los avisos periodicos:
    esa ruta no empieza con /api/.
    """
    return jsonify({'ok': True})


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
    if es_admin(u):
        keys = ['academy_name', 'default_cuota', 'due_day', 'cargo_demora_pct', 'academy_code', 'pago_link', 'pago_alias',
                'auto_mensaje', 'auto_inact_dias', 'auto_deuda_dias', 'auto_mensaje_activo', 'logro_asist', 'logro_videos',
                'asis_min_examen', 'mp_access_token', 'wp_numero', 'desc_familiar',
                'desc_familiar2', 'desc_familiar3', 'desc_familiar4', 'tz_offset', 'public_url', 'academy_color',
                'precio_act_1', 'precio_act_2', 'precio_act_3']
        return jsonify({k: get_setting(k) for k in keys})
    # Alumno/profesor: solo lo publicable. academy_code permite registrarse como
    # profesor y mp_access_token es una credencial de MercadoPago: no se exponen.
    pub = ['academy_name', 'pago_link', 'pago_alias', 'logro_asist', 'logro_videos',
           'desc_familiar', 'desc_familiar2', 'desc_familiar3', 'desc_familiar4',
           'precio_act_1', 'precio_act_2', 'precio_act_3']
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
        'precio_act_1': (0, 10_000_000),
        'precio_act_2': (0, 10_000_000),
        'precio_act_3': (0, 10_000_000),
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
              'desc_familiar2', 'desc_familiar3', 'desc_familiar4', 'tz_offset',
              'precio_act_1', 'precio_act_2', 'precio_act_3']:
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
    # Los profesores tambien pagan cuota, asi que aplicar el valor por defecto
    # tiene que alcanzarlos: antes solo tocaba a los alumnos y los profes
    # quedaban con el monto viejo (o en null) sin avisar.
    alumnos = get_db().execute(
        "SELECT id, nombre FROM users WHERE role IN ('alumno','profesor') AND activo=1").fetchall()
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
        "SELECT %s,"
        "  (SELECT COUNT(*) FROM asistencia a WHERE a.alumno_id=u.id AND a.presente=1) AS asistencias,"
        "  (SELECT COUNT(*) FROM pagos p WHERE p.alumno_id=u.id) AS pagos_totales"
        " FROM users u WHERE u.role IN ('alumno','profesor') AND u.activo=1 ORDER BY u.nombre"
        % columnas_de_users('u.')).fetchall()

    # --- datos para las columnas nuevas del export ---
    # Quien pago: el titular de la familia. A quien: el propio alumno.
    # Reparto: como se divide el ultimo pago entre los profesores.
    pagador = {}
    for fm in get_db().execute(
            """SELECT fm.user_id, t.nombre AS titular_nombre
               FROM familia_miembros fm JOIN familias f ON f.id=fm.familia_id
               LEFT JOIN users t ON t.id=f.titular_id""").fetchall():
        pagador[fm['user_id']] = fm['titular_nombre']

    reparto = {}
    for rp in get_db().execute(
            """SELECT p.alumno_id, p.id AS pago_id, p.monto AS monto_pago, p.fecha AS fecha,
                      pr.profesor_id, pr.monto AS monto_prof, pr.actividad,
                      u.nombre AS prof_nombre
               FROM pago_reparto pr
               JOIN pagos p ON p.id=pr.pago_id
               LEFT JOIN users u ON u.id=pr.profesor_id
               ORDER BY p.id, pr.id""").fetchall():
        a_id = rp['alumno_id']
        if a_id not in reparto or rp['pago_id'] != reparto[a_id]['pago_id']:
            reparto[a_id] = {'pago_id': rp['pago_id'], 'monto': rp['monto_pago'],
                             'fecha': rp['fecha'], 'partes': []}
        if rp['pago_id'] == reparto[a_id]['pago_id']:
            reparto[a_id]['partes'].append(rp)

    destinos_por_pago = {}
    for dp in get_db().execute(
            "SELECT pago_id, destino, monto FROM pago_destino ORDER BY pago_id, id").fetchall():
        destinos_por_pago.setdefault(dp['pago_id'], []).append(dp)

    def _m(v):
        if v is None:
            return ''
        return str(int(v)) if float(v) == int(v) else ('%g' % v)

    def _destinos_txt(pago_id):
        return '; '.join('%s $%s' % (
            DESTINOS_PAGO.get(d['destino'], d['destino']), _m(d['monto']))
            for d in (destinos_por_pago.get(pago_id) or []))

    def col_pago(r):
        nombre = r['nombre']
        pag = pagador.get(r['id'])
        quien = pag if (pag and pag != nombre) else nombre
        partes = []
        rep = reparto.get(r['id'])
        if rep and rep['partes']:
            for x in rep['partes']:
                pn = x['prof_nombre'] or ('Profesor #%s' % x['profesor_id'])
                act = (' [%s]' % x['actividad']) if x['actividad'] else ''
                partes.append('%s $%s%s' % (pn, _m(x['monto_prof']), act))
            extra = _destinos_txt(r['id'])
            if extra:
                partes.append(extra)
        out = ['Pago: %s' % quien, 'Imputado a: %s' % nombre]
        out.append(('Reparto: ' + '; '.join(partes)) if partes
                   else 'Reparto: sin reparto registrado')
        return ' | '.join(out)

    def col_competicion(r):
        b = bjj_categoria(r['nacimiento'], r['peso'],
                          (r['genero'] if 'genero' in r.keys() else '') or '')
        if b['ok']:
            return '%s - %s' % (b['division_peso'], b['division'])
        return b.get('motivo') or ''

    def cel(v):
        if v is None:
            return ''
        if isinstance(v, (int, float)):
            return str(v)
        return str(v)

    headers = ['Nombre', 'DNI', 'Direccion / Domicilio', 'Telefono', 'Celular 2',
               'Telefono tutor/padre', 'Email/Usuario',
               'Fecha de nacimiento', 'Edad', 'Peso (kg)', 'Categoria', 'Cinturon / Faixa',
               'Categoria de competicion BJJ',
               'Modalidad', 'Ficha medica', 'Contacto emergencia', 'Autoriza fotos (menores)',
               'Cuota mensual ($)', 'Estado de pago',
               'Asistencias', 'Total pagos registrados',
               'Quien pago / a quien / reparto', 'Miembro desde']

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
                # Los profes pagan cuota: no hay estado 'profesor' para el que
                # exento. Si aparece, es un dato viejo, no una excepcion.
            }.get(cs['estado'], cs['estado'])
        except Exception:
            estado_label = ''
        gcat = (r['categoria'] or '').strip().lower()
        gkey = 'Juveniles' if gcat == 'juveniles' else ('Kids' if gcat == 'kids' else 'Adultos')
        groups.setdefault(gkey, []).append([
            r['nombre'], r['dni'], r['direccion'], r['tel'], r['tel_2'], r['tel_tutor'],
            r['username'], r['nacimiento'],
            r['edad'], r['peso'], r['categoria'], r['cinturon'], col_competicion(r), r['gi_pref'],
            r['medic_info'], r['emergency_contact'], aut_foto,
            r['cuota_mensual'], estado_label,
            r['asistencias'], r['pagos_totales'], col_pago(r),
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
                '<dc:creator>NEXO MADRYN</dc:creator>'
                '<cp:lastModifiedBy>NEXO MADRYN</cp:lastModifiedBy>'
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
    universo_ids = {r['id'] for r in db.execute(
        "SELECT id FROM users WHERE role IN ('alumno','profesor') AND activo=1 "
        "AND cuota_mensual IS NOT NULL").fetchall()}
    total_alumnos = len(universo_ids)
    resumen = []
    for mes in range(1, 13):
        pagos = db.execute('SELECT monto FROM pagos WHERE mes=? AND anio=?', (mes, anio)).fetchall()
        ids_pagaron = {r['alumno_id'] for r in db.execute(
            'SELECT DISTINCT alumno_id FROM pagos WHERE mes=? AND anio=?',
            (mes, anio)).fetchall()}
        cant_pagaron = len(ids_pagaron & universo_ids)
        ingresos = sum((p['monto'] or 0) for p in pagos)
        deudores = max(0, total_alumnos - cant_pagaron)
        pct_p = min(100, round(cant_pagaron * 100 / total_alumnos)) if total_alumnos else 0
        pct_d = min(100, round(deudores * 100 / total_alumnos)) if total_alumnos else 0
        resumen.append([
            ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio',
             'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre'][mes - 1],
            ingresos, len(pagos),
            cant_pagaron, deudores,
            str(pct_p) + '%',
            str(pct_d) + '%',
        ])
    rows_pagos = db.execute(
        """SELECT p.id, p.fecha, u.nombre AS alumno, p.alumno_id, p.metodo, p.monto, p.mes, p.anio,
                  p.concepto, p.nota, reg.nombre AS registro_por
           FROM pagos p JOIN users u ON u.id=p.alumno_id
           LEFT JOIN users reg ON reg.id=p.registrado_por
           WHERE p.anio=? ORDER BY p.mes, p.fecha""", (anio,)).fetchall()

    # Reparto de cada pago entre los profesores, y quien pago en nombre del alumno.
    partes_por_pago = {}
    for rp in db.execute(
            """SELECT pr.pago_id, pr.monto, pr.actividad, pr.profesor_id,
                      u.nombre AS prof_nombre
               FROM pago_reparto pr
               LEFT JOIN users u ON u.id=pr.profesor_id
               ORDER BY pr.pago_id, pr.id""").fetchall():
        partes_por_pago.setdefault(rp['pago_id'], []).append(rp)

    destinos_por_pago = {}
    for dp in db.execute(
            "SELECT pago_id, destino, monto FROM pago_destino ORDER BY pago_id, id").fetchall():
        destinos_por_pago.setdefault(dp['pago_id'], []).append(dp)

    pagador_por_alumno = {}
    for fm in db.execute(
            """SELECT fm.user_id, t.nombre AS titular_nombre
               FROM familia_miembros fm JOIN familias f ON f.id=fm.familia_id
               LEFT JOIN users t ON t.id=f.titular_id""").fetchall():
        pagador_por_alumno[fm['user_id']] = fm['titular_nombre']

    def _m(v):
        if v is None:
            return ''
        return str(int(v)) if float(v) == int(v) else ('%g' % v)

    def col_reparto(p):
        partes = partes_por_pago.get(p['id']) or []
        if not partes:
            return 'sin reparto registrado'
        filas = ['%s $%s%s' % (
            x['prof_nombre'] or ('Profesor #%s' % x['profesor_id']),
            _m(x['monto']), (' [%s]' % x['actividad']) if x['actividad'] else '')
            for x in partes]
        filas.extend('%s $%s' % (DESTINOS_PAGO.get(d['destino'], d['destino']),
                                 _m(d['monto']))
                     for d in (destinos_por_pago.get(p['id']) or []))
        return '; '.join(filas)

    def col_quien_pago(p):
        nombre = p['alumno']
        pag = pagador_por_alumno.get(p['alumno_id'])
        if not pag or pag == nombre:
            return 'Pago propio de %s' % nombre
        return 'Pago de %s (titular) por %s' % (pag, nombre)

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
    headers_detalle = ['ID', 'Fecha', 'Alumno', 'Quién pagó / a quién', 'Método', 'Concepto',
                       'Monto ($)', 'Reparto del pago', 'Registrado por', 'Mes', 'Año']
    sh1 = sheet_xml(headers_resumen, resumen)
    sh2 = sheet_xml(headers_detalle,
                    [[p['id'], p['fecha'], p['alumno'], col_quien_pago(p), p['metodo'],
                      p['concepto'] or '', p['monto'], col_reparto(p),
                      p['registro_por'] or '', p['mes'], p['anio']]
                     for p in rows_pagos])

    # Hoja 3: como se divide cada pago (60% profesores, 30% tatami y academia,
    # 10% administrativo), con el total del ano por destino.
    def col_destino_profesor(nombre):
        return 'Profesor: %s' % nombre

    def _pct(part, total):
        if not total:
            return ''
        return '%d%%' % min(100, round((part or 0) * 100 / total))

    rows_division = []
    totales = {}
    for p in rows_pagos:
        monto_pago = p['monto'] or 0
        partes = partes_por_pago.get(p['id']) or []
        dests = destinos_por_pago.get(p['id']) or []
        if not partes and not dests:
            rows_division.append([p['id'], p['fecha'], p['mes'], p['anio'], p['alumno'],
                                  p['concepto'] or '', _m(monto_pago),
                                  'sin reparto registrado', '', '', ''])
            continue
        for x in partes:
            etiqueta = col_destino_profesor(
                x['prof_nombre'] or ('Profesor #%s' % x['profesor_id']))
            m = x['monto'] or 0
            rows_division.append([p['id'], p['fecha'], p['mes'], p['anio'], p['alumno'],
                                  p['concepto'] or '', _m(monto_pago), etiqueta,
                                  x['actividad'] or '', _m(m), _pct(m, monto_pago)])
            totales[etiqueta] = round(totales.get(etiqueta, 0) + m, 2)
        for d in dests:
            etiqueta = DESTINOS_PAGO.get(d['destino'], d['destino'])
            m = d['monto'] or 0
            rows_division.append([p['id'], p['fecha'], p['mes'], p['anio'], p['alumno'],
                                  p['concepto'] or '', _m(monto_pago), etiqueta,
                                  '', _m(m), _pct(m, monto_pago)])
            totales[etiqueta] = round(totales.get(etiqueta, 0) + m, 2)

    if totales:
        rows_division.append([''] * 11)
        rows_division.append(['TOTAL DEL AÑO', '', '', '', '', '', '',
                              'Destino', '', 'Monto ($)', '% sobre total'])
        total_gral = round(sum(totales.values()), 2)
        for etiqueta in sorted(totales, key=lambda k: -totales[k]):
            m = totales[etiqueta]
            rows_division.append(['', '', '', '', '', '', '', etiqueta, '', _m(m),
                                  _pct(m, total_gral)])
        rows_division.append(['', '', '', '', '', '', '', 'TOTAL', '', _m(total_gral),
                              '100%'])

    headers_division = ['Pago ID', 'Fecha', 'Mes', 'Año', 'Alumno', 'Concepto',
                        'Monto del pago ($)', 'Destino', 'Actividad', 'Monto ($)',
                        '% del pago']
    sh3 = sheet_xml(headers_division, rows_division)

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
                '<Override PartName="/xl/worksheets/sheet3.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
                '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'
                '<Override PartName="/xl/sharedStrings.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sharedStrings+xml"/>'
                '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
                '</Types>')

    def workbook():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
                'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
                '<sheets><sheet name="Resumen anual" sheetId="1" r:id="rId1"/>'
                '<sheet name="Detalle pagos" sheetId="2" r:id="rId2"/>'
                '<sheet name="División de pagos" sheetId="3" r:id="rId3"/></sheets></workbook>')

    def workbook_rels():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
                '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>'
                '<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet3.xml"/>'
                '<Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
                '<Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/sharedStrings" Target="sharedStrings.xml"/>'
                '</Relationships>')

    def core():
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
                'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
                'xmlns:dcmitype="http://purl.org/dc/dcmitype/" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
                '<dc:creator>NEXO MADRYN</dc:creator>'
                '<cp:lastModifiedBy>NEXO MADRYN</cp:lastModifiedBy>'
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
        z.writestr('xl/worksheets/sheet3.xml', sh3)
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

def bjj_claves():
    """Todas las claves validas de categoria: 'grupo|genero|modalidad|peso|edad'.

    Permite que el alumno elija su categoria a mano y que la app la guarde como
    una sola string, sin inventar una tabla nueva ni colgar el dato de tablas
    externas. La clave sale de las mismas estructuras que usa /api/bjj/categorias,
    asi que si esas cambian, la clave cambia con ellas.
    """
    out = set()
    for grupo, genders in BJJ_PESOS.items():
        for g, mods in genders.items():
            for modalidad in ('gi', 'nogi'):
                for nombre, _ in mods.get(modalidad) or []:
                    for div, _ in BJJ_DIVISIONES:
                        out.add('%s|%s|%s|%s|%s' % (grupo, g, modalidad, nombre, div))
    return out


def bjj_clave_valida(clave):
    return (clave or '') in bjj_claves()


def bjj_clave_label(clave):
    """'adulto|M|gi|Medio|Adulto' -> 'Medio · Adulto · Con Gi'."""
    p = (clave or '').split('|')
    if len(p) != 5:
        return ''
    return '%s · %s · %s' % (p[3], p[4], 'No-Gi' if p[2] == 'nogi' else 'Con Gi')


@app.route('/api/bjj/categorias')
@login_required
def api_bjj_categorias():
    """Tablas de referencia de categorias de competicion IBJJF."""
    return jsonify({'divisiones': [n for n, _ in BJJ_DIVISIONES],
                    'desde': dict(BJJ_DIVISIONES), 'pesos': BJJ_PESOS})


@app.route('/api/bjj/calcular', methods=['POST'])
@login_required
def api_bjj_calcular():
    """Categoria de competicion de un alumno.

    Admin/profesor pueden pasar user_id. Sin user_id usa los datos enviados
    (nacimiento, peso, genero) o los del usuario logged.
    """
    data = parse_json()
    u = current_user()
    uid = to_int(data.get('user_id'))
    if uid and u['role'] not in ('admin', 'profesor'):
        return jsonify({'error': 'Sin permisos'}), 403
    if uid:
        row = get_db().execute(
            'SELECT nombre, peso, genero, nacimiento FROM users WHERE id=?', (uid,)).fetchone()
        if not row:
            return jsonify({'error': 'Alumno no encontrado'}), 404
        nacimiento, peso, genero = row['nacimiento'], row['peso'], row['genero']
        nombre = row['nombre']
    else:
        nacimiento = data.get('nacimiento') or (u['nacimiento'] if u else None)
        peso = data.get('peso') if data.get('peso') not in (None, '') else (u['peso'] if u else None)
        genero = data.get('genero') if data.get('genero') not in (None, '') else (u['genero'] if u else '')
        nombre = (u['nombre'] if u else None)
    gi = str(data.get('gi', 'gi')).lower() != 'nogi'
    res = bjj_categoria(nacimiento, peso, genero, gi, to_int(data.get('anio')) or None)
    res['nombre'] = nombre
    res['gi'] = gi
    return jsonify(res)


@app.route('/api/bjj/categoria', methods=['POST'])
@login_required
def api_bjj_categoria():
    """El alumno elige a que categoria compite y queda guardada en su perfil.

    A diferencia de /api/bjj/calcular, que la deduce de edad y peso, esta es la
    eleccion del usuario: cuando la IBJJF agrega una division de edad o cambia
    un limite, el alumno se corrige solo sin que haya que tocar la app.
    """
    data = parse_json()
    clave = str(data.get('clave') or '').strip()
    if clave and not bjj_clave_valida(clave):
        return jsonify({'error': 'Categoría inválida'}), 400
    uid = current_user()['id']
    get_db().execute('UPDATE users SET bjj_categoria=? WHERE id=?', (clave, uid))
    get_db().commit()
    return jsonify({'ok': True, 'clave': clave, 'label': bjj_clave_label(clave)})


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 5000)),
            debug=os.environ.get('FLASK_DEBUG', '0') == '1')


@app.after_request
def _after_req(resp):
    try:
        path = request.path
        if path.startswith('/static/'):
            # cache agresivo para assets versionados (tienen ?v=) o largos TTL
            qs = request.query_string.decode('utf-8', errors='ignore')
            if 'v=' in qs:
                resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
            else:
                resp.headers['Cache-Control'] = 'public, max-age=86400'
        elif path.startswith('/api/video/') or path.startswith('/api/muro_video/') \
                or path.startswith('/api/muro_foto/') or path.startswith('/api/evento_foto/') \
                or path.startswith('/api/avatar/') or path.startswith('/api/chat_adjunto/'):
            # Media: el navegador del alumno puede cachear (repetir una tecnica no
            # debe re-bajar 10MB cada vez), pero 'private' para que ningun proxy
            # compartido guarde videos que no le corresponden a esa categoria/cinturon.
            resp.headers['Cache-Control'] = 'private, max-age=3600'
        else:
            resp.headers.setdefault('Cache-Control', 'no-store, max-age=0')
    except Exception:
        pass
    return _compress_response(resp)
