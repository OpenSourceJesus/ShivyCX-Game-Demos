/* vbe.c -- Bochs-VBE (DISPI) linear-framebuffer graphics for mbos.
 *
 * QEMU's std VGA (PCI 1234:1111, present with `-vga std` or `-device VGA`)
 * exposes the Bochs display interface on I/O ports 0x1CE/0x1CF, letting a
 * kernel set a linear-framebuffer graphics mode with no BIOS/VBE real-mode
 * calls. We find the device, enable its memory BAR (BAR0 = the framebuffer),
 * program a 32-bpp mode, and hand back a pointer for the console to draw into.
 *
 * This is the mbos "graphics card driver" -- deliberately tiny: one mode, one
 * 32-bpp framebuffer, no acceleration. Higher resolutions need more VRAM than
 * the 16 MiB std default; `-device VGA,vgamem_mb=64` (as noted in the README)
 * provides it.
 */
#include "mbos.h"

static inline void outw(u16 p, u16 v){ __asm__ volatile("outw %0,%1"::"a"(v),"Nd"(p)); }
static inline u16  inw(u16 p){ u16 r; __asm__ volatile("inw %1,%0":"=a"(r):"Nd"(p)); return r; }
static inline void outl(u16 p, u32 v){ __asm__ volatile("outl %0,%1"::"a"(v),"Nd"(p)); }
static inline u32  inl(u16 p){ u32 r; __asm__ volatile("inl %1,%0":"=a"(r):"Nd"(p)); return r; }

static u32 pci_r32(u8 b, u8 d, u8 f, u8 o){
    outl(0xCF8, 0x80000000u | ((u32)b<<16) | ((u32)d<<11) | ((u32)f<<8) | (o & 0xFC));
    return inl(0xCFC);
}
static void pci_w32(u8 b, u8 d, u8 f, u8 o, u32 v){
    outl(0xCF8, 0x80000000u | ((u32)b<<16) | ((u32)d<<11) | ((u32)f<<8) | (o & 0xFC));
    outl(0xCFC, v);
}

/* DISPI registers */
#define VBE_IDX 0x1CE
#define VBE_DAT 0x1CF
#define DISPI_XRES   1
#define DISPI_YRES   2
#define DISPI_BPP    3
#define DISPI_ENABLE 4
#define DISPI_ENABLED   0x01
#define DISPI_LFB       0x40

static void dispi(u16 index, u16 val){ outw(VBE_IDX, index); outw(VBE_DAT, val); }

static volatile u32 *g_fb;
static u32 g_w, g_h;
static int g_up;

int  gfx_up(void)   { return g_up; }
u32  gfx_width(void)  { return g_w; }
u32  gfx_height(void) { return g_h; }

/* Bring up a `w`x`h` 32-bpp linear framebuffer. Returns 0 on success. */
int gfx_init(u32 w, u32 h) {
    int bus, dev, found = 0; u8 fb, fd = 0;
    for (bus = 0; bus < 4 && !found; bus++) {
        for (dev = 0; dev < 32 && !found; dev++) {
            u32 id = pci_r32((u8)bus, (u8)dev, 0, 0x00);
            if ((id & 0xFFFF) == 0x1234 && (id >> 16) == 0x1111) {
                fb = (u8)bus; fd = (u8)dev; found = 1;
            }
        }
    }
    if (!found) { ser_puts("[gfx] no Bochs/std VGA device\n"); return -1; }

    /* enable memory space + bus master, read framebuffer BAR0 */
    u32 cmd = pci_r32(fb, fd, 0, 0x04);
    pci_w32(fb, fd, 0, 0x04, cmd | 0x7);
    u32 bar0 = pci_r32(fb, fd, 0, 0x10) & 0xFFFFFFF0u;
    if (!bar0) { ser_puts("[gfx] no framebuffer BAR\n"); return -1; }

    dispi(DISPI_ENABLE, 0);
    dispi(DISPI_XRES, (u16)w);
    dispi(DISPI_YRES, (u16)h);
    dispi(DISPI_BPP, 32);
    dispi(DISPI_ENABLE, DISPI_ENABLED | DISPI_LFB);

    /* confirm the device accepted the geometry */
    outw(VBE_IDX, DISPI_XRES); g_w = inw(VBE_DAT);
    outw(VBE_IDX, DISPI_YRES); g_h = inw(VBE_DAT);
    if (g_w == 0 || g_h == 0) { ser_puts("[gfx] mode set failed\n"); return -1; }

    g_fb = (volatile u32 *)(unsigned long)bar0;
    g_up = 1;
    ser_puts("[gfx] framebuffer up\n");
    return 0;
}

void gfx_pixel(u32 x, u32 y, u32 rgb) {
    if (x < g_w && y < g_h) g_fb[y * g_w + x] = rgb;
}

void gfx_fill(u32 rgb) {
    u32 n = g_w * g_h, i;
    for (i = 0; i < n; i++) g_fb[i] = rgb;
}

/* Blit one 8x16 glyph (16 row-bytes, MSB=leftmost) at pixel (px,py). */
void gfx_glyph(const u8 *rows, u32 px, u32 py, u32 fg, u32 bg) {
    u32 ry, rx;
    for (ry = 0; ry < 16; ry++) {
        u8 bits = rows[ry];
        for (rx = 0; rx < 8; rx++)
            gfx_pixel(px + rx, py + ry, (bits & (0x80 >> rx)) ? fg : bg);
    }
}

/* Scroll the whole framebuffer up by `dy` pixels, clearing the new bottom. */
void gfx_scroll(u32 dy, u32 bg) {
    if (!g_up || dy == 0 || dy >= g_h) return;
    u32 row_px = g_w;
    u32 move = (g_h - dy) * row_px;
    u32 i;
    for (i = 0; i < move; i++) g_fb[i] = g_fb[i + dy * row_px];
    for (i = move; i < g_h * row_px; i++) g_fb[i] = bg;
}
