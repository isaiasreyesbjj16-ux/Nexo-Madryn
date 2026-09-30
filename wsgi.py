import traceback as _tb

try:
    from app import app
except Exception as _e:
    print('IMPORT_APP_ERROR:', _e)
    _tb.print_exc()
    raise