#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Borra todos los videos de la seccion Videos para volver a subirlos comprimidos.

Solo LISTE por defecto: pasa --borrar para borrar de verdad.

    python purgar_videos.py
    python purgar_videos.py --borrar

Usa la misma base que la app (DB_MODE + DATABASE_URL o DATABASE), asi que
correrlo con las mismas variables que usa el server. Borra las filas de
`videos` junto con su progreso/visualizaciones y, si el archivo estaba en el
Supabase Storage, tambien el objeto del bucket.

No toca los videos del muro ni los adjuntos del chat: esas son otras tablas.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A  # noqa: E402


def main():
    p = argparse.ArgumentParser(
        description='Borra los videos subidos a la seccion Videos.')
    p.add_argument('--borrar', action='store_true',
                   help='ejecuta el borrado; sin este flag solo se lista')
    args = p.parse_args()

    with A.app.app_context():
        db = A.get_db()
        filas = db.execute(
            'SELECT id, titulo, tipo, url, '
            'CASE WHEN data IS NULL THEN 0 ELSE length(data) END AS chars '
            'FROM videos ORDER BY id').fetchall()
        if not filas:
            print('No hay videos subidos.')
            return 0

        en_storage = 0
        en_base = 0
        chars = 0
        print('%-5s %-45s %-9s %s' % ('id', 'titulo', 'donde', 'base64 (aprox)'))
        print('-' * 80)
        for f in filas:
            chars += f['chars'] or 0
            if A._is_storage_url(f['url']):
                donde = 'storage'
                en_storage += 1
            else:
                donde = 'base'
                en_base += 1
            kb = (f['chars'] or 0) * 3 // 4 // 1024
            print('%-5s %-45s %-9s %s KB' % (
                f['id'], (f['titulo'] or '')[:45], donde,
                '{:,}'.format(kb).replace(',', '.')))
        print('-' * 80)
        print('%d video(s): %d en la base (%s MB de base64), %d en Storage' % (
            len(filas), en_base, '{:,}'.format(chars * 3 // 4 // (1024 * 1024)).replace(',', '.'),
            en_storage))
        remotos = [f for f in filas if (f['url'] or '').startswith('http')]
        if remotos and not A._storage_enabled():
            print('\nAVISO: %d video(s) apuntan a una URL remota pero no hay '
                  'SUPABASE_URL/SUPABASE_KEY en el entorno: esas filas se borran, '
                  'el archivo del bucket queda huerfano. Corre el script con las '
                  'mismas variables que el server.' % len(remotos))

        if not args.borrar:
            print('\nNada borrado. Repeti con --borrar para eliminarlos.')
            return 0

        for f in filas:
            A._storage_delete(f['url'])
        ids = [f['id'] for f in filas]
        marca = ','.join('?' * len(ids))
        borrados_prog = db.execute(
            'DELETE FROM video_progress WHERE video_id IN (%s)' % marca, ids).rowcount
        borrados_vistos = db.execute(
            'DELETE FROM video_views WHERE video_id IN (%s)' % marca, ids).rowcount
        borrados = db.execute('DELETE FROM videos WHERE id IN (%s)' % marca, ids).rowcount
        db.commit()
        print('\nBorrados %d video(s), %d fila(s) de progreso y %d de vistas.' % (
            borrados, borrados_prog, borrados_vistos))
        return 0


if __name__ == '__main__':
    sys.exit(main())
