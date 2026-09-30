# Deploy gratis en Render + Supabase (sin tarjeta)

Este proyecto usa **Supabase** como base de datos (Postgres gratis, no caduca) y
**Render** para correr el backend (gratis, sin tarjeta). El backend ya trae
soporte nativo: `DB_MODE=postgres` + `DATABASE_URL`.

> IMPORTANTE: la app web y la app Android apuntan a una sola URL que Render te
> da, ejemplo `https://ikigai-viedma.onrender.com`.

---

## 1) Cuentas gratis (sin tarjeta)

| Servicio | Sirve para | URL |
|---|---|---|
| GitHub | guardar el código | https://github.com |
| Supabase | base de datos (Postgres) | https://supabase.com |
| Render | correr el backend 24/7 | https://render.com |

Registrate en las tres con el mismo correo. Ninguna pide tarjeta.

---

## 2) Supabase: crear la base de datos

1. Entrá a https://supabase.com → "Start your project" → **New project**.
2. Elegí un nombre (ej. `ikigai`), región cercana (ej. `South America (São Paulo)`)
   y una contraseña de base de datos **fuerte**. Crear el proyecto tarda ~2 min.
3. En el menú de la izquierda: **Project Settings → Database**.
4. Bajá a **Connection string** y copiá la del **Pooler** (Transaction). Tiene
   este formato:

   ```
   postgresql://postgres.<ref>:<CONTRASEÑA>@aws-0-<region>.pooler.supabase.com:6543/postgres
   ```

   IMPORTANTE: usá la de **puerto 6543 (pooler)** porque Render no soporta IPv6
   y el puerto 5432 (conexión directa) solo funciona con IPv6.
5. Agregale al final: `?sslmode=require` quedando algo así:

   ```
   postgresql://postgres.<ref>:<CONTRASEÑA>@aws-0-<region>.pooler.supabase.com:6543/postgres?sslmode=require
   ```

   Guardala, la vas a pegar en Render en el paso 5.

> Nota: un proyecto de Supabase gratis se "pausa" solo si está **7 días sin
> ninguna actividad**. Si la academia usa la app todos los días, no se pausa.

---

## 3) Subir el código a GitHub

1. Creá un repositorio en https://github.com/new → nombre `ikigai-viedma`,
   visibilidad **Public** (el plan gratis de Render no soporta repos privados).
   NO marques la opción de inicializarlo con README.
2. En esta carpeta abrí una terminal y ejecutá (cambiá `TU_USUARIO`):

   ```
   git init
   git add .
   git commit -m "backend con soporte postgres + render"
   git branch -M main
   git remote add origin https://github.com/TU_USUARIO/ikigai-viedma.git
   git push -u origin main
   ```

   Te va a pedir usuario y contraseña/token de GitHub.

---

## 4) Render: crear el servicio

1. Entrá a https://render.com → **Sign Up with GitHub** (o **New Account** con
   el correo).
2. Aceptá que Render pueda ver tu repositorio.
3. En el dashboard: **New + → Blueprint** y seleccioná el repo
   `ikigai-viedma`. Render va a leer el archivo `render.yaml`.
4. Cuando pida configurar el entorno, en **Environment Variables**:
   - `DB_MODE` ya viene en `postgres` (automático).
   - `SECRET_KEY` ya viene generada (automático).
   - `DATABASE_URL`: pegá el texto del paso 2 de Supabase.
5. **Apply / Deploy**. El primer deploy tarda unos minutos.
6. Cuando termine, abrí la URL que muestra Render
   (ej. `https://ikigai-viedma.onrender.com`). Deberías ver la pantalla de
   login. Entrá con `admin` / `admin123`.

> Si el deploy falla, entrá a la pestaña **Logs** del servicio para ver el error
> (suele ser la `DATABASE_URL` mal copiada).

---

## 5) App Android

1. Instalá el `IKIGAI-VIEDMA.apk` en los celulares.
2. En la pantalla de login, en **Servidor**, poné la URL de Render
   (ej. `https://ikigai-viedma.onrender.com`) y logueate normal.

---

## 6) Web (PWA)

- Se abre igual que la app web: `https://ikigai-viedma.onrender.com`.
- Puede instalarse en el celular como app desde el navegador.

---

## Advertencias del plan gratis

- **Arranque en frío**: el servicio de Render gratis duerme a los 15 min sin
  uso; la primera visita después de un rato tarda ~30-60 segundos. Después anda
  rápido.
- **Fotos y videos subidos**: Render no guarda archivos entre redespliegues.
  Las fotos de perfil y videos subidos (no links) pueden desaparecer si
  redesplegás el servicio. **Para videos, preferí pegar links de YouTube.**
- **Notificaciones push**: funcionan normal, las claves VAPID ahora se guardan
  en la base de datos (no se pierden al redeployar).
- La base de datos no se borra nunca (está en Supabase).

## Verificación local (opcional)

Para probar el backend con Postgres en tu PC:

```
py -m pip install -r requirements.txt
set DB_MODE=postgres
set DATABASE_URL=postgresql://TU_USUARIO:TU_CLAVE@127.0.0.1:5440/mibase
python app.py
```

Con `DB_MODE` vacío usa SQLite local (como siempre) y con `DB_MODE=mysql` usa
MySQL/MariaDB.
