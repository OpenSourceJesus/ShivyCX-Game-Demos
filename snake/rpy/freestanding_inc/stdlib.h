/* Freestanding <stdlib.h> shadow. Arena-backed malloc/free; abort/exit halt. */
#ifndef MBOS_FS_STDLIB_H
#define MBOS_FS_STDLIB_H
#include <stddef.h>
void  *malloc(size_t n);
void  *calloc(size_t nmemb, size_t sz);
void  *realloc(void *p, size_t n);
void   free(void *p);
void   abort(void);
void   exit(int code);
long   strtol(const char *s, char **end, int base);
double strtod(const char *s, char **end);
void   qsort(void *base, size_t n, size_t sz, int (*cmp)(const void *, const void *));
#endif
