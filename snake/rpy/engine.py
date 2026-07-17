"""engine -- the rpython side of the snake_glue FFI, plus input decoding.

Exactly the mbos render.py pattern: a ctypes.CDLL handle whose calls py2c
lowers to direct C externs (glue.c), guarded so the same file imports under
CPython. Everything crossing the boundary is an int or a string.
"""
import sys
if sys.implementation.name == 'shivyc':
    import rpy_ctypes as ctypes
else:
    import ctypes

_g = ctypes.CDLL("snake_glue")
_g.sg_width.restype = ctypes.c_int
_g.sg_width.argtypes = []
_g.sg_height.restype = ctypes.c_int
_g.sg_height.argtypes = []
_g.sg_clear.restype = None
_g.sg_clear.argtypes = [ctypes.c_int]
_g.sg_rect.restype = None
_g.sg_rect.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                       ctypes.c_int, ctypes.c_int]
_g.sg_rect_a.restype = None
_g.sg_rect_a.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int]
_g.sg_circle.restype = None
_g.sg_circle.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int]
_g.sg_round_rect.restype = None
_g.sg_round_rect.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int]
_g.sg_text.restype = None
_g.sg_text.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
                       ctypes.c_int, ctypes.c_int]
_g.sg_text_width.restype = ctypes.c_int
_g.sg_text_width.argtypes = [ctypes.c_char_p, ctypes.c_int]
_g.sg_sprite.restype = None
_g.sg_sprite.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                         ctypes.c_int, ctypes.c_int]
_g.sg_sprite_w.restype = ctypes.c_int
_g.sg_sprite_w.argtypes = [ctypes.c_int]
_g.sg_sprite_h.restype = ctypes.c_int
_g.sg_sprite_h.argtypes = [ctypes.c_int]
_g.sg_present.restype = None
_g.sg_present.argtypes = []
_g.sg_bake.restype = None
_g.sg_bake.argtypes = []
_g.sg_restore.restype = None
_g.sg_restore.argtypes = []
_g.sg_key_event.restype = ctypes.c_int
_g.sg_key_event.argtypes = []
_g.sg_ms.restype = ctypes.c_int
_g.sg_ms.argtypes = []
_g.sg_log.restype = None
_g.sg_log.argtypes = [ctypes.c_char_p]

# sprite ids, matching the SPRITES table order in sprites.c
SPR_WALL = 0
SPR_WALL2 = 1
SPR_WALL3 = 2
SPR_WALL4 = 3
SPR_WEAKWALL = 4
SPR_BOX = 5
SPR_BOMB = 6
SPR_PIT = 7
SPR_SHALLOW = 8
SPR_BOTTOMLESS = 9
SPR_TRAPDOOR = 10
SPR_APPLE = 11
SPR_STAR = 12
SPR_PORTAL = 13
SPR_SAVE = 14
SPR_LOAD = 15
SPR_PROPEL = 16
SPR_PAD = 17
SPR_PADDOWN = 18
SPR_DOOR = 19
SPR_BUG = 20
SPR_EYES = 21
SPR_EYEIN = 22
SPR_DEADEYES = 23
SPR_TITLE = 24
SPR_CONGRATS = 25
SPR_LVLBTN = 26
SPR_LVLWON = 27
SPR_STARON = 28
SPR_STAROFF = 29
SPR_BLOCKED = 30
SPR_BACKGROUND = 31

NO_TINT = 16777215          # 0xFFFFFF
OPAQUE = 256


def width() -> int:
    return _g.sg_width()


def height() -> int:
    return _g.sg_height()


def clear(rgb: int) -> None:
    _g.sg_clear(rgb)


def rect(x: int, y: int, w: int, h: int, rgb: int) -> None:
    _g.sg_rect(x, y, w, h, rgb)


def rect_a(x: int, y: int, w: int, h: int, rgb: int, alpha: int) -> None:
    _g.sg_rect_a(x, y, w, h, rgb, alpha)


def circle(cx: int, cy: int, r: int, rgb: int) -> None:
    _g.sg_circle(cx, cy, r, rgb)


def round_rect(x: int, y: int, w: int, h: int, r: int, corners: int,
               rgb: int) -> None:
    _g.sg_round_rect(x, y, w, h, r, corners, rgb)


def text(x: int, y: int, msg: "char*", rgb: int, scale: int) -> None:
    _g.sg_text(x, y, msg, rgb, scale)


def text_width(msg: "char*", scale: int) -> int:
    return _g.sg_text_width(msg, scale)


def text_centered(cx: int, y: int, msg: "char*", rgb: int,
                  scale: int) -> None:
    _g.sg_text(cx - _g.sg_text_width(msg, scale) // 2, y, msg, rgb, scale)


def sprite(sid: int, x: int, y: int, w: int, h: int) -> None:
    _g.sg_sprite(sid, x, y, w, h, 16777215, 256, 0)


def sprite_ex(sid: int, x: int, y: int, w: int, h: int, tint: int,
              alpha: int, rot: int) -> None:
    _g.sg_sprite(sid, x, y, w, h, tint, alpha, rot)


def sprite_w(sid: int) -> int:
    return _g.sg_sprite_w(sid)


def sprite_h(sid: int) -> int:
    return _g.sg_sprite_h(sid)


def present() -> None:
    _g.sg_present()


def bake() -> None:
    """Snapshot the back buffer into the scene cache."""
    _g.sg_bake()


def restore() -> None:
    """Reset the back buffer from the scene cache."""
    _g.sg_restore()


def ms() -> int:
    return _g.sg_ms()


def log(msg: "char*") -> None:
    _g.sg_log(msg)


# ---- input --------------------------------------------------------------
# kb events: bits 0..6 scancode (set 1), bit 7 break flag, bit 8 E0-extended.
# Logical key numbers the game loop consumes:
K_NONE = 0
K_UP = 1
K_DOWN = 2
K_LEFT = 3
K_RIGHT = 4
K_UNDO = 5
K_REDO = 6
K_RESET = 7
K_SWITCH = 8
K_ENTER = 9
K_ESC = 10
K_GRID = 11


def _decode(code: int, ext: int) -> int:
    if ext == 1:
        if code == 72:          # 0x48 up
            return K_UP
        if code == 80:          # 0x50 down
            return K_DOWN
        if code == 75:          # 0x4B left
            return K_LEFT
        if code == 77:          # 0x4D right
            return K_RIGHT
        if code == 29:          # right ctrl
            return K_SWITCH
        return K_NONE
    if code == 17:              # W
        return K_UP
    if code == 31:              # S
        return K_DOWN
    if code == 30:              # A
        return K_LEFT
    if code == 32:              # D
        return K_RIGHT
    if code == 44 or code == 16:            # Z, Q undo
        return K_UNDO
    if code == 45 or code == 18:            # X, E redo
        return K_REDO
    if code == 19:                           # R reset
        return K_RESET
    if code == 42 or code == 54 or code == 29 or code == 15 or code == 57:
        return K_SWITCH                      # shift/ctrl/tab/space
    if code == 28:                           # enter
        return K_ENTER
    if code == 1:                            # esc
        return K_ESC
    if code == 34:                           # G grid toggle
        return K_GRID
    return K_NONE


# held-state tracking so PS/2 typematic repeats do not auto-move the snake:
# a logical key fires once on make, re-arms on break. (Original rule: a move
# fires when the held direction changes; holding never repeats.)
_held = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]


def poll_key() -> int:
    """Next logical key PRESS (edge), or K_NONE."""
    while 1:
        ev = _g.sg_key_event()
        if ev < 0:
            return K_NONE
        code = ev & 127
        brk = (ev // 128) & 1
        ext = (ev // 256) & 1
        k = _decode(code, ext)
        if k == K_NONE:
            continue
        if brk == 1:
            _held[k] = 0
            continue
        if _held[k] == 1:
            continue                # typematic repeat: ignore
        _held[k] = 1
        return k
    return K_NONE
