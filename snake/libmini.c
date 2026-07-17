/* libmini.c -- the few libc primitives a freestanding build needs. */
#include "mbos.h"

void *mini_memset(void *d, int c, size_t n) {
    u8 *p = (u8 *)d;
    while (n--) *p++ = (u8)c;
    return d;
}

void *mini_memcpy(void *d, const void *s, size_t n) {
    u8 *dp = (u8 *)d;
    const u8 *sp = (const u8 *)s;
    while (n--) *dp++ = *sp++;
    return d;
}

size_t mini_strlen(const char *s) {
    size_t n = 0;
    while (s[n]) n++;
    return n;
}

int mini_strcmp(const char *a, const char *b) {
    while (*a && (*a == *b)) { a++; b++; }
    return (int)((u8)*a) - (int)((u8)*b);
}
