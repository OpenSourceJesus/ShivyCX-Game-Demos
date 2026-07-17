/* rt_freestanding.c -- the freestanding libc the generated rpython runtime
 * (shivyc_rt.c) leans on, backed by the mbos kernel.
 *
 * This is the crux of the "run py2c output on bare metal" step. The generated
 * shivyc_rt.c is written against a hosted libc (malloc, strlen, sprintf,
 * setjmp, ...). Rather than patch generated code, we shadow the four hosted
 * headers (stdio/stdlib/string/setjmp/ctype -- see freestanding_inc/) and
 * implement just the symbols the render path actually reaches:
 *
 *   - memory:  malloc/calloc/realloc/free over a static kernel arena (mirrors
 *              the runtime's own aalloc model: bump, no real free)
 *   - strings: strlen/strcmp/strncmp/memcpy/... (thin, correct, freestanding)
 *   - format:  a tiny sprintf/snprintf supporting the runtime's %ld/%g/%s/%p,
 *              enough for str() of ints; printf/fprintf go to serial
 *   - convert: strtol (used by int(str)); strtod stubbed (no float pages yet)
 *   - control: abort/exit halt; setjmp/longjmp minimal (exceptions unreachable)
 *
 * If a page ever exercises an unreached path (floats, exceptions), the stub
 * makes it fail loudly on serial rather than silently mis-render.
 */
#include "mbos.h"
#include <stdio.h>
#include <stdarg.h>

/* ---- arena (sized for a 1 MiB-loaded kernel; the runtime's default 1 GiB
 *      static arena would never fit, so we shadow malloc instead) ---------- */
#define RT_ARENA_BYTES (2 * 1024 * 1024)
static u8  g_rt_arena[RT_ARENA_BYTES] __attribute__((aligned(16)));
static u32 g_rt_used;

void *malloc(size_t n) {
    g_rt_used = (g_rt_used + 15u) & ~15u;
    if (g_rt_used + n > RT_ARENA_BYTES) { ser_puts("[rt] arena exhausted\n"); return 0; }
    void *p = &g_rt_arena[g_rt_used];
    g_rt_used += (u32)n;
    return p;
}
void *calloc(size_t nm, size_t sz) {
    size_t n = nm * sz;
    void *p = malloc(n);
    if (p) mini_memset(p, 0, n);
    return p;
}
void *realloc(void *old, size_t n) {
    /* bump arena: allocate fresh and copy (size unknown, copy is bounded by n) */
    void *p = malloc(n);
    if (p && old) mini_memcpy(p, old, n);
    return p;
}
void free(void *p) { (void)p; }         /* arena frees in one shot */

/* ---- string.h ---------------------------------------------------------- */
void  *memset(void *d, int c, size_t n)              { return mini_memset(d, c, n); }
void  *memcpy(void *d, const void *s, size_t n)      { return mini_memcpy(d, s, n); }
size_t strlen(const char *s)                         { return mini_strlen(s); }
int    strcmp(const char *a, const char *b)          { return mini_strcmp(a, b); }

void *memmove(void *d, const void *s, size_t n) {
    u8 *dp = (u8 *)d; const u8 *sp = (const u8 *)s;
    if (dp < sp) { while (n--) *dp++ = *sp++; }
    else { dp += n; sp += n; while (n--) *--dp = *--sp; }
    return d;
}
int memcmp(const void *a, const void *b, size_t n) {
    const u8 *x = (const u8 *)a, *y = (const u8 *)b;
    while (n--) { if (*x != *y) return (int)*x - (int)*y; x++; y++; }
    return 0;
}
int strncmp(const char *a, const char *b, size_t n) {
    while (n && *a && (*a == *b)) { a++; b++; n--; }
    if (!n) return 0;
    return (int)(u8)*a - (int)(u8)*b;
}
char *strcpy(char *d, const char *s) {
    char *r = d; while ((*d++ = *s++)) { } return r;
}
char *strchr(const char *s, int c) {
    for (; *s; s++) if (*s == (char)c) return (char *)s;
    return (c == 0) ? (char *)s : 0;
}
char *strstr(const char *h, const char *nn) {
    if (!*nn) return (char *)h;
    for (; *h; h++) {
        const char *a = h, *b = nn;
        while (*a && *b && *a == *b) { a++; b++; }
        if (!*b) return (char *)h;
    }
    return 0;
}

/* ---- ctype.h ----------------------------------------------------------- */
int isspace(int c){ return c==' '||c=='\t'||c=='\n'||c=='\r'||c=='\v'||c=='\f'; }
int isdigit(int c){ return c>='0'&&c<='9'; }
int isalpha(int c){ return (c>='a'&&c<='z')||(c>='A'&&c<='Z'); }
int isalnum(int c){ return isalpha(c)||isdigit(c); }
int isupper(int c){ return c>='A'&&c<='Z'; }
int islower(int c){ return c>='a'&&c<='z'; }
int isprint(int c){ return c>=32&&c<127; }
int toupper(int c){ return islower(c)?c-32:c; }
int tolower(int c){ return isupper(c)?c+32:c; }

/* ---- stdlib.h: conversions + control ----------------------------------- */
long strtol(const char *s, char **end, int base) {
    long v = 0; int neg = 0;
    while (isspace((int)*s)) s++;
    if (*s == '-') { neg = 1; s++; } else if (*s == '+') s++;
    if (base == 0) { base = 10; if (*s=='0'&&(s[1]=='x'||s[1]=='X')){base=16;s+=2;} }
    else if (base == 16 && s[0]=='0'&&(s[1]=='x'||s[1]=='X')) s += 2;
    for (;;) {
        int c = (int)(u8)*s, d;
        if (c>='0'&&c<='9') d = c-'0';
        else if (c>='a'&&c<='z') d = c-'a'+10;
        else if (c>='A'&&c<='Z') d = c-'A'+10;
        else break;
        if (d >= base) break;
        v = v*base + d; s++;
    }
    if (end) *end = (char *)s;
    return neg ? -v : v;
}
double strtod(const char *s, char **end) {
    /* No floating-point pages yet; report loudly if a page reaches this. */
    ser_puts("[rt] strtod unsupported on bare metal\n");
    if (end) *end = (char *)s;
    return 0.0;
}
void qsort(void *base, size_t n, size_t sz,
           int (*cmp)(const void *, const void *)) {
    /* insertion sort: fine for the tiny lists a page produces, and freestanding */
    u8 *a = (u8 *)base; u8 tmp[64];
    size_t i, j;
    if (sz > sizeof tmp) return;
    for (i = 1; i < n; i++) {
        mini_memcpy(tmp, a + i*sz, sz);
        j = i;
        while (j > 0 && cmp(a + (j-1)*sz, tmp) > 0) {
            mini_memcpy(a + j*sz, a + (j-1)*sz, sz);
            j--;
        }
        mini_memcpy(a + j*sz, tmp, sz);
    }
}
void abort(void) { ser_puts("[rt] abort\n"); for (;;) __asm__ volatile ("hlt"); }
void exit(int code) { (void)code; for (;;) __asm__ volatile ("hlt"); }

/* ---- setjmp.h: exceptions are unreachable on the render path ------------ */
int  setjmp(long env[8]) { (void)env; return 0; }
void longjmp(long env[8], int val) {
    (void)env; (void)val;
    ser_puts("[rt] longjmp (uncaught exception) -- halting\n");
    for (;;) __asm__ volatile ("hlt");
}

/* ---- stdio.h: printf family -------------------------------------------- */
struct _FS_FILE { int dummy; };
static struct _FS_FILE g_stdout, g_stderr;
FILE *stdout = &g_stdout;
FILE *stderr = &g_stderr;

static void emit_str(char *dst, int *pos, int cap, const char *s) {
    while (*s) { if (dst) { if (*pos < cap-1) dst[*pos] = *s; } else con_putc(*s); (*pos)++; s++; }
}
static void emit_long(char *dst, int *pos, int cap, long v, int base, int isu) {
    char tmp[24]; int t = 0; unsigned long uv;
    int neg = 0;
    if (!isu && v < 0) { neg = 1; uv = (unsigned long)(-v); } else uv = (unsigned long)v;
    if (uv == 0) tmp[t++] = '0';
    while (uv) { int d = (int)(uv % (unsigned)base); tmp[t++] = (char)(d<10?'0'+d:'a'+d-10); uv /= (unsigned)base; }
    if (neg) { if (dst){ if(*pos<cap-1) dst[*pos]='-'; } else con_putc('-'); (*pos)++; }
    while (t--) { if (dst){ if(*pos<cap-1) dst[*pos]=tmp[t]; } else con_putc(tmp[t]); (*pos)++; }
}

/* Supports the runtime's needs: %s %ld %d %u %x %p %c %g(as 0). Not general. */
static int vformat(char *dst, int cap, const char *fmt, va_list ap) {
    int pos = 0;
    for (; *fmt; fmt++) {
        if (*fmt != '%') { if (dst){ if(pos<cap-1) dst[pos]=*fmt; } else con_putc(*fmt); pos++; continue; }
        fmt++;
        int islong = 0;
        while (*fmt == 'l') { islong = 1; fmt++; }
        switch (*fmt) {
            case 's': emit_str(dst, &pos, cap, va_arg(ap, const char *)); break;
            case 'd': case 'i': emit_long(dst,&pos,cap, islong?va_arg(ap,long):(long)va_arg(ap,int),10,0); break;
            case 'u': emit_long(dst,&pos,cap, islong?(long)va_arg(ap,unsigned long):(long)va_arg(ap,unsigned),10,1); break;
            case 'x': emit_long(dst,&pos,cap, islong?(long)va_arg(ap,unsigned long):(long)va_arg(ap,unsigned),16,1); break;
            case 'p': { emit_str(dst,&pos,cap,"0x"); emit_long(dst,&pos,cap,(long)(unsigned long)va_arg(ap,void*),16,1); break; }
            case 'c': { char b[2]; b[0]=(char)va_arg(ap,int); b[1]=0; emit_str(dst,&pos,cap,b); break; }
            case 'g': case 'f': (void)va_arg(ap,double); emit_str(dst,&pos,cap,"0"); break;
            case '%': if (dst){ if(pos<cap-1) dst[pos]='%'; } else con_putc('%'); pos++; break;
            default:  if (dst){ if(pos<cap-1) dst[pos]=*fmt; } else con_putc(*fmt); pos++; break;
        }
    }
    if (dst && cap > 0) dst[pos < cap ? pos : cap-1] = 0;
    return pos;
}

int sprintf(char *buf, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vformat(buf, 1<<30, fmt, ap);
    va_end(ap); return n;
}
int snprintf(char *buf, size_t cap, const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vformat(buf, (int)cap, fmt, ap);
    va_end(ap); return n;
}
int printf(const char *fmt, ...) {
    va_list ap; va_start(ap, fmt);
    int n = vformat(0, 0, fmt, ap);
    va_end(ap); return n;
}
int fprintf(FILE *stream, const char *fmt, ...) {
    (void)stream;
    va_list ap; va_start(ap, fmt);
    int n = vformat(0, 0, fmt, ap);
    va_end(ap); return n;
}
int puts(const char *s) { con_puts(s); con_putc('\n'); return 0; }
int putchar(int c) { con_putc((char)c); return c; }
