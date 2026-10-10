# coding: utf-8
# Copyright (c) 2026 霜星. All rights reserved.
"""Small ctypes binding to the replaceable LGPL libmpv player."""
import ctypes as C


class Player:
    def __init__(self, library, hwnd):
        self.dll = d = C.CDLL(str(library))
        signatures = {
            'mpv_create': ([], C.c_void_p),
            'mpv_set_option_string': ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int),
            'mpv_initialize': ([C.c_void_p], C.c_int),
            'mpv_command': ([C.c_void_p, C.POINTER(C.c_char_p)], C.c_int),
            'mpv_get_property_string': ([C.c_void_p, C.c_char_p], C.c_void_p),
            'mpv_set_property_string': ([C.c_void_p, C.c_char_p, C.c_char_p], C.c_int),
            'mpv_free': ([C.c_void_p], None),
            'mpv_wait_event': ([C.c_void_p, C.c_double], C.c_void_p),
            'mpv_terminate_destroy': ([C.c_void_p], None),
        }
        for name, (args, result) in signatures.items():
            fn = getattr(d, name)
            fn.argtypes, fn.restype = args, result
        self.handle = d.mpv_create()
        if not self.handle:
            raise RuntimeError('无法创建播放器。')
        try:
            for name, value in {'wid': str(hwnd), 'vo': 'gpu', 'gpu-api': 'd3d11',
                                'hwdec': 'auto-safe', 'config': 'no', 'terminal': 'no',
                                'input-default-bindings': 'no', 'input-vo-keyboard': 'no',
                                'osc': 'no', 'keep-open': 'yes', 'pause': 'yes',
                                'sub-auto': 'no', 'volume': '65'}.items():
                self.check(d.mpv_set_option_string(self.handle, name.encode(), value.encode()))
            self.check(d.mpv_initialize(self.handle))
        except Exception:
            self.close()
            raise

    @staticmethod
    def check(code):
        if code < 0:
            raise RuntimeError(f'播放器操作失败（{code}）。')

    def command(self, *args):
        values = [str(v).encode('utf-8') for v in args] + [None]
        self.check(self.dll.mpv_command(self.handle, (C.c_char_p * len(values))(*values)))

    def get(self, name, default=None):
        ptr = self.dll.mpv_get_property_string(self.handle, name.encode())
        if not ptr:
            return default
        try:
            return C.string_at(ptr).decode('utf-8', errors='replace')
        finally:
            self.dll.mpv_free(ptr)

    def number(self, name, default=0.):
        try:
            return float(self.get(name))
        except (TypeError, ValueError):
            return default

    def set(self, name, value):
        self.check(self.dll.mpv_set_property_string(self.handle, name.encode(), str(value).encode()))

    def drain(self):
        for _ in range(100):
            event = self.dll.mpv_wait_event(self.handle, 0.)
            if not event or C.cast(event, C.POINTER(C.c_int))[0] == 0:
                break

    def close(self):
        if self.handle:
            self.dll.mpv_terminate_destroy(self.handle)
            self.handle = None
