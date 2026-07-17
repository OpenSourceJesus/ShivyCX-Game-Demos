/* linker64.ld -- link mbos (64-bit) at 1 MiB, Multiboot header first, and
 * export the load/bss symbols the AOUT kludge in boot64.S points at. `-n`
 * (nmagic) keeps sections tightly packed so the file layout matches the load
 * addresses QEMU uses. */

ENTRY(_start)

SECTIONS
{
    . = 0x00100000;          /* 1 MiB */
    _load_start = .;

    .text : {
        *(.multiboot)        /* header within the first 8 KiB */
        *(.text)
        *(.text.*)
    }
    .rodata : {
        *(.rodata)
        *(.rodata.*)
    }
    .data : {
        *(.data)
        *(.data.*)
    }
    _load_end = .;

    .bss : {
        *(COMMON)
        *(.bss)
        *(.bss.*)
    }
    _bss_end = .;

    /DISCARD/ : {
        *(.comment)
        *(.note.*)
        *(.eh_frame)
    }
}
