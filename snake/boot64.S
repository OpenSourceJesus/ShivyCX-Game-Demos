/* boot64.S -- Multiboot1 -> 64-bit long mode entry for mbos.
 *
 * Two things make this boot with a plain `qemu-system-x86_64 -kernel mbos.elf`
 * and no GRUB:
 *
 *  1. The Multiboot1 header sets the AOUT-kludge flag (bit 16) and carries
 *     load/entry addresses. QEMU's -kernel loader would otherwise reject a
 *     64-bit ELF ("give a 32bit one"); with the kludge it loads the image flat
 *     by those addresses and ignores the ELF class.
 *  2. We enter in 32-bit protected mode (as Multiboot guarantees), build page
 *     tables, and switch to long mode ourselves.
 *
 * Paging: identity-map the first 4 GiB with 2 MiB pages. 1 GiB isn't enough --
 * the graphics framebuffer (Bochs-VBE LFB) lives up at ~0xFD000000, so writing
 * a pixel would fault a 1 GiB map. SSE is enabled too, since -O2 C code emits
 * SSE for struct/mem moves.
 *
 * The 32-bit text-mode path (boot.S) is still available via `make text32`.
 */

.set MB_MAGIC,    0x1BADB002
.set MB_FLAGS,    (1 << 16)                 /* AOUT kludge: load addrs below */
.set MB_CHECKSUM, -(MB_MAGIC + MB_FLAGS)

.section .multiboot
.align 4
mb_header:
    .long MB_MAGIC
    .long MB_FLAGS
    .long MB_CHECKSUM
    .long mb_header        /* header_addr   */
    .long _load_start      /* load_addr     */
    .long _load_end        /* load_end_addr */
    .long _bss_end         /* bss_end_addr  */
    .long _start           /* entry_addr    */

.section .text
.code32
.global _start
_start:
    cli
    cld
    mov $stack_top, %esp

    /* stash Multiboot magic (eax) and info ptr (ebx) for kmain */
    mov %eax, saved_magic
    mov %ebx, saved_mbi

    /* zero PML4 + PDPT + 4 PDs (6 pages = 6144 dwords) */
    mov $pml4, %edi
    xor %eax, %eax
    mov $6144, %ecx
    rep stosl

    /* PML4[0] -> PDPT */
    mov $pdpt, %eax
    or  $3, %eax
    mov %eax, pml4

    /* PDPT[0..3] -> pd0..pd3  (4 GiB) */
    mov $pd0, %eax
    or  $3, %eax
    mov %eax, pdpt
    add $4096, %eax
    mov %eax, pdpt+8
    add $4096, %eax
    mov %eax, pdpt+16
    add $4096, %eax
    mov %eax, pdpt+24

    /* fill 2048 PD entries: entry i maps 2 MiB at (i << 21), present|rw|PS */
    mov $pd0, %edi
    xor %ecx, %ecx
1:  mov %ecx, %eax
    shl $21, %eax
    or  $0x83, %eax
    mov %eax, (%edi)
    mov %ecx, %eax
    shr $11, %eax           /* high dword: (i << 21) >> 32 == i >> 11 */
    mov %eax, 4(%edi)
    add $8, %edi
    inc %ecx
    cmp $2048, %ecx
    jne 1b

    mov $pml4, %eax
    mov %eax, %cr3

    /* CR4.PAE | CR4.OSFXSR | CR4.OSXMMEXCPT  (PAE + SSE) */
    mov %cr4, %eax
    or  $0x220, %eax
    mov %eax, %cr4

    /* EFER.LME */
    mov $0xC0000080, %ecx
    rdmsr
    or  $0x100, %eax
    wrmsr

    /* CR0: set PG|PE, clear EM, set MP (so SSE is usable) */
    mov %cr0, %eax
    or  $0x80000001, %eax   /* PG | PE */
    and $0xFFFFFFFB, %eax   /* clear EM (bit 2) */
    or  $0x2, %eax          /* set MP (bit 1) */
    mov %eax, %cr0

    lgdt gdt64_ptr
    ljmp $0x08, $long_start

.code64
long_start:
    mov $0x10, %ax
    mov %ax, %ds
    mov %ax, %es
    mov %ax, %fs
    mov %ax, %gs
    mov %ax, %ss
    movq $stack_top, %rsp
    xor %rbp, %rbp

    movl saved_magic, %edi
    movl saved_mbi, %esi
    call kmain
hang:
    cli
    hlt
    jmp hang

/* Multiboot puts magic in eax, info ptr in ebx at entry; stash for kmain.
 * (We read them into edi/esi above; grab them here right after entry.) */

.section .rodata
.align 16
gdt64:
    .quad 0
    .quad 0x00209A0000000000     /* code: long, present, exec, read */
    .quad 0x0000920000000000     /* data: present, write */
gdt64_ptr:
    .word gdt64_ptr - gdt64 - 1
    .long gdt64

.section .bss
.align 4096
pml4: .skip 4096
pdpt: .skip 4096
pd0:  .skip 4096
pd1:  .skip 4096
pd2:  .skip 4096
pd3:  .skip 4096
.align 16
stack_bottom: .skip 32768
stack_top:
saved_magic: .skip 4
saved_mbi:   .skip 4
