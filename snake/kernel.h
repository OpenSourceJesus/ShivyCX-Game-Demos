/* kernel.h -- shared declarations for the freestanding snake kernel.
 *
 * Platform layer lifted from ShivyC's examples/rpython2c/mbos demo (mbos.h):
 * same port I/O helpers, mini libc, Bochs-VBE graphics driver interface and
 * geometry defines, so vbe.c / libmini.c / rt_freestanding.c compile here
 * unchanged apart from the header name. On top of that: a polled PS/2
 * keyboard, a PIT millisecond clock, and the PC-speaker, for the game loop.
 */
#ifndef KERNEL_H
#define KERNEL_H

typedef unsigned char  u8;
typedef unsigned short u16;
typedef unsigned int   u32;
typedef unsigned long  u64;
typedef __SIZE_TYPE__  size_t;

/* ---- port I/O ---------------------------------------------------------- */
static inline void outb(u16 port, u8 val) {
    __asm__ volatile ("outb %0, %1" : : "a"(val), "Nd"(port));
}
static inline u8 inb(u16 port) {
    u8 r; __asm__ volatile ("inb %1, %0" : "=a"(r) : "Nd"(port)); return r;
}

/* ---- mini libc (libmini.c) -------------------------------------------- */
void  *mini_memset(void *d, int c, size_t n);
void  *mini_memcpy(void *d, const void *s, size_t n);
size_t mini_strlen(const char *s);
int    mini_strcmp(const char *a, const char *b);

/* ---- serial console (kernel.c) ----------------------------------------- *
 * There is no on-screen text console in the game kernel: con_putc/con_puts
 * exist because the generated runtime's printf/puts reach them (see
 * rt_freestanding.c) and they go straight to COM1, like ser_puts. */
void ser_init(void);
void ser_puts(const char *s);
void con_putc(char c);
void con_puts(const char *s);

/* ---- polled PS/2 keyboard (kernel.c) ------------------------------------ *
 * kb_poll() drains one byte from the i8042 output buffer if present.
 * Returns -1 when no key data is pending, else a scancode event:
 *   bits 0..6  scancode (set 1)
 *   bit  7     release (break) flag
 *   bit  8     0xE0-extended prefix seen (arrows, keypad enter, ...)
 */
int kb_poll(void);

/* ---- PIT millisecond clock (kernel.c) ----------------------------------- *
 * time_ms() latches PIT channel 0 and accumulates deltas into a monotonic
 * millisecond counter. Call it at least every ~50 ms (any game loop does). */
u32 time_ms(void);

/* ---- PC speaker (kernel.c) ---------------------------------------------- */
void spk_on(u32 hz);
void spk_off(void);

/* ---- graphics: Bochs-VBE linear framebuffer (vbe.c, from mbos) ---------- */
int  gfx_init(u32 w, u32 h);     /* 0 on success; sets a 32-bpp LFB mode    */
int  gfx_up(void);
u32  gfx_width(void);
u32  gfx_height(void);
void gfx_pixel(u32 x, u32 y, u32 rgb);
void gfx_fill(u32 rgb);
void gfx_glyph(const u8 *rows, u32 px, u32 py, u32 fg, u32 bg);
void gfx_scroll(u32 dy, u32 bg);
void gfx_present(const u32 *src); /* bulk full-frame copy into the LFB      */

/* Display geometry: authored for 1920x1080, window is that size divided
 * by MBOS_GFX_DIV (Makefile GFX_DIV, default 3 → 640x360). Override
 * DIV and/or W/H via -D. vbe.c reads MBOS_GFX_W/H. */
#ifndef MBOS_GFX_DIV
#define MBOS_GFX_DIV 3
#endif
#ifndef MBOS_GFX_W
#define MBOS_GFX_W (1920 / MBOS_GFX_DIV)
#endif
#ifndef MBOS_GFX_H
#define MBOS_GFX_H (1080 / MBOS_GFX_DIV)
#endif

#endif /* KERNEL_H */
