#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Cuanto pesan las fotos (y videos) que estan guardados en la base.

Las imagenes del muro, los eventos, las firmas, los avatares y los comprobantes
se guardan como text base64 dentro de la base (Neon/Postgres). Este script suma
el peso real de cada tabla para saber cuanto ocupan y cuanto egress generan al
pedirlos. NO borra nada: solo informa.

    python informe_almacenamiento.py

Usa la misma base que la app (DB_MODE + DATABASE_URL o DATABASE), igual que
purgar_videos.py: conviene correrlo con las mismas variables que el server.
El base64 pesa ~4/3 del archivo original, asi que el "peso real" se estima
como chars*3/4 (bytes) y se muestra en KB/MB.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import app as A  # noqa: E402


def _peso(chars):
    return (chars or 0) * 3 // 4


def _fmt(nbytes):
    for unidad, div in (('GB', 1024 ** 3), ('MB', 1024 ** 2), ('KB', 1024)):
        if nbytes >= div:
            return '%.1f %s' % (nbytes / div, unidad)
    return '%d B' % nbytes


def _suma(db, tabla, col):
    """(cantidad con contenido, chars) de una tabla/columna; 0/0 si no existe."""
    try:
        row = db.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(LENGTH(%s)),0) AS c '
            "FROM %s WHERE %s IS NOT NULL AND %s <> ''" % (col, tabla, col, col)
        ).fetchone()
        return int(row['n'] or 0), int(row['c'] or 0)
    except Exception:
        return 0, 0


def main():
    with A.app.app_context():
        db = A.get_db()
        grupos = [
            ('Fotos del muro', 'muro_fotos', 'data'),
            ('Fotos de eventos', 'evento_fotos', 'data'),
            ('Videos del muro (subidos)', 'muro_videos', 'data'),
            ('Videos de la seccion (subidos)', 'videos', 'data'),
            ('Avatares / fotos de perfil', 'users', 'foto'),
            ('Firma TyC', 'users', 'firma_tyc'),
            ('Firma autorizacion de fotos', 'users', 'firma_foto'),
            ('Comprobantes de pago', 'avisos_pago', 'comprobante'),
        ]

        total = 0
        print('%-34s %8s  %12s  %10s' % ('Que', 'cantidad', 'peso real', 'base64'))
        print('-' * 70)
        for etiqueta, tabla, col in grupos:
            n, chars = _suma(db, tabla, col)
            peso = _peso(chars)
            total += peso
            print('%-34s %8d  %12s  %10s' % (
                etiqueta, n, _fmt(peso), _fmt(chars)))
        print('-' * 70)
        print('%-34s %8s  %12s' % ('TOTAL imagenes/videos en la base', '', _fmt(total)))

        # Videos que viven en Supabase Storage (no pesan en la base).
        try:
            filas = db.execute("SELECT url FROM videos WHERE url IS NOT NULL AND url <> ''").fetchall()
            storage = sum(1 for f in filas if A._is_storage_url(f['url']))
            if storage:
                print('\nAdemas hay %d video(s) en Supabase Storage (no cuentan aca, '
                      'pesan en el bucket).' % storage)
        except Exception:
            pass

        print('\nNota: el base64 engorda ~33%; el peso real ya esta descontado.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
