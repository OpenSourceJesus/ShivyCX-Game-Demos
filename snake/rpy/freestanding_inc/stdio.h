/* Freestanding <stdio.h> shadow for the mbos rpython runtime.
 * Only the few names shivyc_rt.c references; bodies are in rt_freestanding.c. */
#ifndef MBOS_FS_STDIO_H
#define MBOS_FS_STDIO_H
#include <stddef.h>
#include <stdarg.h>
typedef struct _FS_FILE FILE;
extern FILE *stderr;
extern FILE *stdout;
int   fprintf(FILE *stream, const char *fmt, ...);
int   printf(const char *fmt, ...);
int   sprintf(char *buf, const char *fmt, ...);
int   snprintf(char *buf, size_t n, const char *fmt, ...);
int   puts(const char *s);
int   putchar(int c);
#endif
