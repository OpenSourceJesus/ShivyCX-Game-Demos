/* Freestanding <setjmp.h> shadow. Exceptions are unreachable on the rendering
 * path, so a minimal jmp_buf plus no-op setjmp / trapping longjmp suffice. */
#ifndef MBOS_FS_SETJMP_H
#define MBOS_FS_SETJMP_H
typedef long jmp_buf[8];
int  setjmp(jmp_buf env);
void longjmp(jmp_buf env, int val);
#endif
