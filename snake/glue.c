/* glue.c -- the `snake_glue` FFI shim the generated rpython game calls.
 *
 * Same pattern as mbos_glue.c in ShivyC's mbos demo: py2c lowers
 * `ctypes.CDLL("snake_glue")` + `_g.sg_rect(...)` in engine.py into plain
 * `extern`/`sg_rect(...)` C calls, so these functions are the entire surface
 * between the rpython game and the kernel. Everything stateful about the
 * display lives here: a full-resolution back buffer that drawing calls paint
 * into and sg_present() blits to the Bochs-VBE framebuffer in one pass --
 * the game never sees tearing and never touches the LFB.
 *
 * All parameters are plain ints (colors are 0xRRGGBB) and const char*
 * strings, matching the ctypes argtypes declared on the rpython side.
 */
#include "kernel.h"
#include "font8x16.h"

static u32 g_back[MBOS_GFX_W * (u32)MBOS_GFX_H] __attribute__((aligned(16)));

/* A second full frame: the "baked scene". The game renders everything that
 * only changes on a turn (background, grid, cells, crates, ...) once, bakes
 * it here, and per frame just restores it and draws the animated entities on
 * top. Rebaking happens on state changes only, so the per-frame cost drops
 * from ~4M blended pixels to two bulk copies plus a few sprites. */
static u32 g_scene[MBOS_GFX_W * (u32)MBOS_GFX_H] __attribute__((aligned(16)));

static void copy_frame(u32 *dst, const u32 *src) {
    u64 *d = (u64 *)dst;
    const u64 *s = (const u64 *)src;
    u32 n = (gfx_width() * gfx_height()) >> 1, i;
    for (i = 0; i < n; i++) d[i] = s[i];
}

/* ---- geometry ----------------------------------------------------------- */
int sg_width(void)  { return (int)gfx_width(); }
int sg_height(void) { return (int)gfx_height(); }

/* ---- scene cache --------------------------------------------------------- */
void sg_bake(void)    { copy_frame(g_scene, g_back); }
void sg_restore(void) { copy_frame(g_back, g_scene); }

/* ---- painting into the back buffer -------------------------------------- */
void sg_clear(int rgb) {
    u64 *d = (u64 *)g_back;
    u64 v = (u32)rgb | ((u64)(u32)rgb << 32);
    u32 n = (gfx_width() * gfx_height()) >> 1, i;
    for (i = 0; i < n; i++) d[i] = v;
}

void sg_rect(int x, int y, int w, int h, int rgb) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
    int x1 = x + w > W ? W : x + w, y1 = y + h > H ? H : y + h;
    int i, j;
    for (j = y0; j < y1; j++)
        for (i = x0; i < x1; i++)
            g_back[j * W + i] = (u32)rgb;
}

/* Alpha-blended fill; alpha 0..256. */
void sg_rect_a(int x, int y, int w, int h, int rgb, int alpha) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
    int x1 = x + w > W ? W : x + w, y1 = y + h > H ? H : y + h;
    u32 a = (u32)alpha, na = 256u - a;
    u32 sr = (((u32)rgb >> 16) & 0xFF) * a;
    u32 sg = (((u32)rgb >> 8) & 0xFF) * a;
    u32 sb = ((u32)rgb & 0xFF) * a;
    int i, j;
    for (j = y0; j < y1; j++)
        for (i = x0; i < x1; i++) {
            u32 dst = g_back[j * W + i];
            u32 dr = ((dst >> 16) & 0xFF) * na;
            u32 dg = ((dst >> 8) & 0xFF) * na;
            u32 db = (dst & 0xFF) * na;
            g_back[j * W + i] = ((((sr + dr) >> 8) & 0xFF) << 16) |
                                ((((sg + dg) >> 8) & 0xFF) << 8) |
                                (((sb + db) >> 8) & 0xFF);
        }
}

void sg_circle(int cx, int cy, int r, int rgb) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    int x, y, r2 = r * r;
    for (y = -r; y <= r; y++) {
        int py = cy + y;
        if (py < 0 || py >= H) continue;
        for (x = -r; x <= r; x++) {
            int px = cx + x;
            if (px < 0 || px >= W) continue;
            if (x * x + y * y <= r2) g_back[py * W + px] = (u32)rgb;
        }
    }
}

/* Rounded rectangle; `corners` is a bit mask of which corners get radius r:
 * 1 = top-left, 2 = top-right, 4 = bottom-left, 8 = bottom-right. The snake
 * uses this to round only the free ends of head/tail segments. */
void sg_round_rect(int x, int y, int w, int h, int r, int corners, int rgb) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    int i, j;
    if (r > w / 2) r = w / 2;
    if (r > h / 2) r = h / 2;
    for (j = 0; j < h; j++) {
        int py = y + j;
        if (py < 0 || py >= H) continue;
        for (i = 0; i < w; i++) {
            int px = x + i, dx = -1, dy = -1;
            if (px < 0 || px >= W) continue;
            if ((corners & 1) && i < r && j < r)             { dx = r - 1 - i; dy = r - 1 - j; }
            else if ((corners & 2) && i >= w - r && j < r)   { dx = i - (w - r); dy = r - 1 - j; }
            else if ((corners & 4) && i < r && j >= h - r)   { dx = r - 1 - i; dy = j - (h - r); }
            else if ((corners & 8) && i >= w - r && j >= h - r) { dx = i - (w - r); dy = j - (h - r); }
            if (dx >= 0 && dx * dx + dy * dy > r * r) continue;
            g_back[py * W + px] = (u32)rgb;
        }
    }
}

/* 8x16 bitmap text (font from mbos), scaled by an integer factor, drawn with
 * a transparent background so it overlays the scene. */
void sg_text(int x, int y, const char *s, int rgb, int scale) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    int cx = x;
    if (scale < 1) scale = 1;
    for (; *s; s++) {
        unsigned uc = (unsigned char)*s;
        const u8 *rows;
        int rx, ry, sx, sy;
        if (uc < FONT_FIRST || uc > FONT_LAST) uc = ' ';
        rows = FONT8X16[uc - FONT_FIRST];
        for (ry = 0; ry < FONT_H; ry++) {
            u8 bits = rows[ry];
            for (rx = 0; rx < FONT_W; rx++) {
                if (!(bits & (0x80 >> rx))) continue;
                for (sy = 0; sy < scale; sy++) {
                    int py = y + ry * scale + sy;
                    if (py < 0 || py >= H) continue;
                    for (sx = 0; sx < scale; sx++) {
                        int px = cx + rx * scale + sx;
                        if (px < 0 || px >= W) continue;
                        g_back[py * W + px] = (u32)rgb;
                    }
                }
            }
        }
        cx += FONT_W * scale;
    }
}

int sg_text_width(const char *s, int scale) {
    if (scale < 1) scale = 1;
    return (int)mini_strlen(s) * FONT_W * scale;
}

/* ---- baked sprites (sprites.c, generated from the original art) --------- */
typedef struct { int w, h; const unsigned int *px; } Sprite;
extern const Sprite SPRITES[];
extern const int SPRITE_COUNT;

/* (x * a) / 255 without a divide, exact for 8-bit operands. */
static inline u32 mul255(u32 x, u32 a) {
    u32 t = x * a + 128;
    return (t + (t >> 8)) >> 8;
}

/* Scaled, alpha-blended, optionally tinted + quarter-rotated sprite blit.
 * tint is 0xRRGGBB multiplied into the texel (0xFFFFFF = untinted); alpha
 * 0..256 scales the texel's own alpha; rot counts 90-degree CCW turns.
 * If the top byte of tint is 0x01, the low 24 bits are a solid RGB and the
 * sprite only contributes alpha (used to wash unloadables toward white).
 *
 * The rot==0 case (everything except portals/eyes) walks the sprite with
 * fixed-point row/column stepping instead of two divides per pixel, and
 * stores opaque texels directly -- that makes the full-screen background
 * blit a plain scaled copy. */
void sg_sprite(int id, int x, int y, int w, int h, int tint, int alpha,
               int rot) {
    int W = (int)gfx_width(), H = (int)gfx_height();
    const Sprite *sp;
    int i, j;
    u32 tr, tg, tb;
    int tinted;
    int solid;
    if (id < 0 || id >= SPRITE_COUNT || w <= 0 || h <= 0) return;
    sp = &SPRITES[id];
    solid = (((u32)tint >> 24) & 0xFF) == 1u;
    tr = ((u32)tint >> 16) & 0xFF; tg = ((u32)tint >> 8) & 0xFF; tb = (u32)tint & 0xFF;
    tinted = !solid && (u32)tint != 0xFFFFFFu;

    if ((rot & 3) == 0) {
        int x0 = x < 0 ? 0 : x, y0 = y < 0 ? 0 : y;
        int x1 = x + w > W ? W : x + w, y1 = y + h > H ? H : y + h;
        u32 sxstep = ((u32)sp->w << 16) / (u32)w;
        u32 systep = ((u32)sp->h << 16) / (u32)h;
        u32 syf = (u32)(y0 - y) * systep;
        u32 sx0 = (u32)(x0 - x) * sxstep;
        for (j = y0; j < y1; j++, syf += systep) {
            const u32 *srow = sp->px + (syf >> 16) * (u32)sp->w;
            u32 *drow = g_back + j * W;
            u32 sxf = sx0;
            for (i = x0; i < x1; i++, sxf += sxstep) {
                u32 texel = srow[sxf >> 16];
                u32 a = texel >> 24;
                u32 r, g, b, dst;
                if (alpha != 256) a = (a * (u32)alpha) >> 8;
                if (a == 0) continue;
                if (solid) {
                    r = tr; g = tg; b = tb;
                } else {
                    r = (texel >> 16) & 0xFF; g = (texel >> 8) & 0xFF; b = texel & 0xFF;
                    if (tinted) { r = mul255(r, tr); g = mul255(g, tg); b = mul255(b, tb); }
                }
                if (a == 255) { drow[i] = (r << 16) | (g << 8) | b; continue; }
                dst = drow[i];
                r = mul255(r, a) + mul255((dst >> 16) & 0xFF, 255 - a);
                g = mul255(g, a) + mul255((dst >> 8) & 0xFF, 255 - a);
                b = mul255(b, a) + mul255(dst & 0xFF, 255 - a);
                drow[i] = (r << 16) | (g << 8) | b;
            }
        }
        return;
    }

    for (j = 0; j < h; j++) {
        int py = y + j;
        if (py < 0 || py >= H) continue;
        for (i = 0; i < w; i++) {
            int px = x + i, sx, sy;
            u32 texel, a, r, g, b, dst;
            if (px < 0 || px >= W) continue;
            /* map dest (i,j) into sprite space with rotation */
            switch (rot & 3) {
                default:
                    sx = i * sp->w / w;           sy = j * sp->h / h;
                    break;
                case 1:  /* 90 CCW: sprite top edge -> dest left edge */
                    sx = (h - 1 - j) * sp->w / h; sy = i * sp->h / w;
                    break;
                case 2:
                    sx = (w - 1 - i) * sp->w / w; sy = (h - 1 - j) * sp->h / h;
                    break;
                case 3:  /* 90 CW */
                    sx = j * sp->w / h;           sy = (w - 1 - i) * sp->h / w;
                    break;
            }
            texel = sp->px[sy * sp->w + sx];
            a = (texel >> 24) & 0xFF;
            a = (a * (u32)alpha) >> 8;
            if (a == 0) continue;
            if (solid) {
                r = tr; g = tg; b = tb;
            } else {
                r = mul255((texel >> 16) & 0xFF, tr);
                g = mul255((texel >> 8) & 0xFF, tg);
                b = mul255(texel & 0xFF, tb);
            }
            dst = g_back[py * W + px];
            r = mul255(r, a) + mul255((dst >> 16) & 0xFF, 255 - a);
            g = mul255(g, a) + mul255((dst >> 8) & 0xFF, 255 - a);
            b = mul255(b, a) + mul255(dst & 0xFF, 255 - a);
            g_back[py * W + px] = (r << 16) | (g << 8) | b;
        }
    }
}

int sg_sprite_w(int id) { return (id >= 0 && id < SPRITE_COUNT) ? SPRITES[id].w : 1; }
int sg_sprite_h(int id) { return (id >= 0 && id < SPRITE_COUNT) ? SPRITES[id].h : 1; }

/* ---- present: back buffer -> framebuffer -------------------------------- */
void sg_present(void) {
    gfx_present(g_back);
}

/* ---- input / time / sound / debug --------------------------------------- */
int  sg_key_event(void) { return kb_poll(); }   /* -1, or code|release<<7|ext<<8 */
int  sg_ms(void)        { return (int)time_ms(); }
void sg_beep(int hz)    { spk_on((u32)hz); }
void sg_quiet(void)     { spk_off(); }
void sg_log(const char *s) { ser_puts(s); ser_puts("\n"); }
