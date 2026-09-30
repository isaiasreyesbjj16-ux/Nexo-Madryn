import os
import sqlite3

try:
    import pymysql
    HAVE_MYSQL = True
except ImportError:
    HAVE_MYSQL = False

try:
    import psycopg
    from psycopg.rows import dict_row
    HAVE_PG = True
except ImportError:
    HAVE_PG = False

DB_MODE = os.environ.get('DB_MODE', 'sqlite').strip().lower()


class IntegrityError(Exception):
    pass


class Row:
    """Fila tipo dict con acceso por nombre o indice (como sqlite3.Row)."""
    def __init__(self, data, keys):
        self._d = data
        self._keys = keys

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._d[self._keys[key]]
        return self._d[key]

    def keys(self):
        return self._keys

    def __iter__(self):
        return iter(self._keys)

    def __contains__(self, key):
        return key in self._d

    def get(self, key, default=None):
        return self._d.get(key, default)


def _mysql_translate(sql):
    import re
    sql = sql.replace('INSERT OR IGNORE', 'INSERT IGNORE')
    sql = sql.replace('INSERT OR REPLACE INTO', 'REPLACE INTO')
    sql = sql.replace('last_insert_rowid()', 'LAST_INSERT_ID()')
    # INSERT ... ON CONFLICT (...) DO UPDATE SET a=excluded.a  ->  ON DUPLICATE KEY UPDATE a=VALUES(a)
    m = re.search(r'(?is)\bON\s+CONFLICT\s*\(([^)]*)\)\s*DO\s*UPDATE\s*SET\s+(.+)$', sql)
    if m:
        assigns = m.group(2).strip().rstrip(';').strip()
        assigns = re.sub(r'\bEXCLUDED\.(\w+)', r'VALUES(\1)', assigns, flags=re.I)
        sql = sql[:m.start()].rstrip() + ' ON DUPLICATE KEY UPDATE ' + assigns
    sql = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
                 'INT NOT NULL AUTO_INCREMENT PRIMARY KEY', sql, flags=re.I)
    sql = sql.replace('username TEXT UNIQUE', 'username VARCHAR(100) UNIQUE')
    sql = sql.replace('endpoint TEXT UNIQUE', 'endpoint VARCHAR(767) UNIQUE')
    if 'PRAGMA table_info' in sql.lower():
        m = re.search(r"PRAGMA table_info\(\s*(\w+)\s*\)", sql, re.I)
        if m:
            return ("SELECT COLUMN_NAME AS name FROM information_schema.columns "
                    "WHERE table_schema = DATABASE() AND table_name = '%s'" % m.group(1))
    return sql.replace('?', '%s')


def _pg_translate(sql):
    import re
    # comillas dobles como literal (valido en sqlite/mysql pero en PG es identificador)
    for role in ('alumno', 'profesor', 'admin'):
        sql = sql.replace('role="%s"' % role, "role='%s'" % role)
    # INSERT OR REPLACE -> upsert
    m = re.match(r'(?is)^INSERT\s+OR\s+REPLACE\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*VALUES\s*(.*)$', sql)
    if m:
        table, cols_s, values = m.group(1), m.group(2).strip(), m.group(3).strip()
        cols = [c.strip() for c in cols_s.split(',')]
        sets = ', '.join('%s = EXCLUDED.%s' % (c, c) for c in cols[1:])
        sql = 'INSERT INTO %s (%s) VALUES %s ON CONFLICT (%s) DO UPDATE SET %s' % (
            table, cols_s, values, cols[0], sets)
    else:
        # INSERT OR IGNORE -> ON CONFLICT DO NOTHING
        m = re.match(r'(?is)^INSERT\s+OR\s+IGNORE\s+INTO\s+(.+)$', sql)
        if m:
            sql = 'INSERT INTO ' + m.group(1).strip().rstrip(';').strip() + ' ON CONFLICT DO NOTHING'
    sql = sql.replace('last_insert_rowid()', 'lastval()')
    sql = re.sub(r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
                 'SERIAL PRIMARY KEY', sql, flags=re.I)
    if 'PRAGMA table_info' in sql.lower():
        m = re.search(r"PRAGMA table_info\(\s*(\w+)\s*\)", sql, re.I)
        if m:
            return ("SELECT column_name AS name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = '%s'" % m.group(1))
    if 'SHOW COLUMNS' in sql.upper():
        m = re.search(r"SHOW\s+COLUMNS\s+FROM\s+(\w+)", sql, re.I)
        if m:
            return ("SELECT column_name AS name FROM information_schema.columns "
                    "WHERE table_schema = current_schema() AND table_name = '%s'" % m.group(1))
    return sql.replace('?', '%s')


def _translate(sql):
    if DB_MODE == 'mysql':
        return _mysql_translate(sql)
    if DB_MODE == 'postgres':
        return _pg_translate(sql)
    return sql


def _split(script):
    stmts, buf = [], []
    for line in script.splitlines():
        if line.strip().startswith('--'):
            continue
        buf.append(line)
        if ';' in line:
            stmts.append('\n'.join(buf))
            buf = []
    if ''.join(buf).strip():
        stmts.append('\n'.join(buf))
    return [s for s in stmts if s.strip()]


class Cursor:
    def __init__(self, cur, sqlite_rows):
        self._cur = cur
        self._sqlite_rows = sqlite_rows
        self._lastrowid = None

    def __getattr__(self, name):
        return getattr(self._cur, name)

    def execute(self, sql, params=()):
        try:
            if self._sqlite_rows:
                self._cur.execute(sql, params if params is not None else ())
            else:
                t = _translate(sql)
                if DB_MODE == 'postgres' and t.lstrip()[:6].upper() == 'INSERT':
                    t = t.rstrip()
                    if t.endswith(';'):
                        t = t[:-1]
                    target = t.split('INTO', 1)[1].lstrip().split(None, 1)[0].split('(', 1)[0].strip('"')
                    if target != 'settings':
                        t2 = t + ' RETURNING id'
                        try:
                            self._cur.execute('SAVEPOINT sp_returning')
                            self._cur.execute(t2, params if params else None)
                            row = self._cur.fetchone()
                            self._lastrowid = row['id'] if row else None
                            self._cur.execute('RELEASE SAVEPOINT sp_returning')
                        except Exception:
                            # tabla sin columna 'id' (ej: chat_members): reintentar sin RETURNING
                            try:
                                self._cur.execute('ROLLBACK TO SAVEPOINT sp_returning')
                            except Exception:
                                pass
                            self._cur.execute(t, params if params else None)
                            self._lastrowid = None
                    else:
                        self._cur.execute(t, params if params else None)
                        self._lastrowid = None
                else:
                    self._cur.execute(t, params if params else None)
        except sqlite3.IntegrityError:
            raise IntegrityError()
        except Exception as e:
            if HAVE_MYSQL and isinstance(e, pymysql.err.IntegrityError):
                raise IntegrityError()
            if HAVE_PG and isinstance(e, psycopg.errors.IntegrityError):
                raise IntegrityError()
            raise
        return self

    def fetchone(self):
        r = self._cur.fetchone()
        if r is None or self._sqlite_rows:
            return r
        return Row(dict(r), list(r.keys()))

    def fetchall(self):
        rs = self._cur.fetchall()
        if self._sqlite_rows:
            return rs
        return [Row(dict(r), list(r.keys())) for r in rs]

    @property
    def lastrowid(self):
        if self._lastrowid is not None:
            return self._lastrowid
        return getattr(self._cur, 'lastrowid', None)


class DB:
    def __init__(self, raw, sqlite_rows):
        self._raw = raw
        self._sqlite_rows = sqlite_rows

    def cursor(self):
        return Cursor(self._raw.cursor(), self._sqlite_rows)

    def execute(self, sql, params=()):
        return self.cursor().execute(sql, params)

    def executescript(self, script):
        if self._sqlite_rows:
            self._raw.executescript(script)
            return
        for stmt in _split(script):
            stmt = stmt.strip().rstrip(';')
            if not stmt:
                continue
            stmt = _translate(stmt)
            if DB_MODE == 'mysql' and stmt.upper().startswith('CREATE TABLE'):
                stmt += ' ENGINE=InnoDB DEFAULT CHARSET=utf8mb4'
            self._raw.cursor().execute(stmt)

    def commit(self):
        self._raw.commit()

    def close(self):
        self._raw.close()


def connect_sqlite(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys = ON')
    return DB(db, sqlite_rows=True)


def connect_mysql(host=None, user=None, password=None, database=None, port=None):
    if not HAVE_MYSQL:
        raise RuntimeError('pymysql no esta instalado (DB_MODE=mysql)')
    conn = pymysql.connect(
        host=host or os.environ.get('MYSQL_HOST', 'localhost'),
        port=int(port or os.environ.get('MYSQL_PORT', 3306)),
        user=user or os.environ.get('MYSQL_USER', 'root'),
        password=password if password is not None else os.environ.get('MYSQL_PASSWORD', ''),
        database=database or os.environ.get('MYSQL_DB', ''),
        charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor)
    return DB(conn, sqlite_rows=False)


def connect_postgres(database_url=None):
    if not HAVE_PG:
        raise RuntimeError('psycopg no esta instalado (DB_MODE=postgres)')
    url = database_url or os.environ.get('DATABASE_URL', '')
    if not url:
        raise RuntimeError('DATABASE_URL no configurada (DB_MODE=postgres)')
    conn = psycopg.connect(url, row_factory=dict_row)
    return DB(conn, sqlite_rows=False)


def connect():
    if DB_MODE == 'mysql':
        return connect_mysql()
    if DB_MODE == 'postgres':
        return connect_postgres()
    return connect_sqlite(os.environ.get('SQLITE_PATH', 'data.db'))
