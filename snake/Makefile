# snake -- a bare-metal remake of ~/snake-game, written in rpython and
# translated to C by ShivyC's tools/py2c.py, rendered through the mbos
# hi-res Bochs-VBE graphics path (1920x1080x32).
#
#   make            build build/snake.elf
#   make run        boot in a QEMU window
#   make serial     boot headless, game log streamed to the terminal
#   make test       headless self-test: scripted keys + serial asserts + shots
#   make clean
#
# The platform pieces (boot64.S, linker64.ld, vbe.c, libmini.c, font8x16.h,
# rpy/rt_freestanding.c, rpy/freestanding_inc/) are lifted from ShivyC's
# examples/rpython2c/mbos demo (vbe.c gains a bulk gfx_present blit);
# kernel.c/glue.c and the rpy game modules are this demo's own.

SHIVYC ?= ../../ShivyC
PY2C   := $(SHIVYC)/tools/py2c.py

BUILD  := build
ELF    := $(BUILD)/snake.elf

CC := gcc
LD := ld

# The mbos hi-res geometry: 1920x1080 needs more VRAM than std VGA's 16 MiB
# default, hence `-vga none -device VGA,vgamem_mb=64` on every QEMU line.
GFX_W ?= 1920
GFX_H ?= 1080

# 64-bit freestanding, exactly the mbos rpython-path flags, with a roomier
# runtime arena (2^26 = 64 MiB) since a game session allocates indefinitely
# (undo history, level reloads) where mbos rendered one page and halted.
CFLAGS := -m64 -std=c11 -ffreestanding -fno-pic -mno-red-zone \
          -fno-stack-protector -fno-builtin -nostdlib -mcmodel=small -w -O2 \
          -DSHIVYC_ARENA_LOG2=26 \
          -DMBOS_GFX_W=$(GFX_W) -DMBOS_GFX_H=$(GFX_H) \
          -Irpy/freestanding_inc -Igen -I.
LDFLAGS := -n -T linker64.ld -nostdlib

QEMU      := qemu-system-x86_64
QEMU_VGA  := -vga none -device VGA,vgamem_mb=64
QEMU_FLAGS := -kernel $(ELF) -no-reboot -m 256 -accel kvm -accel tcg $(QEMU_VGA)

# ---- rpython -> C --------------------------------------------------------
RPY_SRC := rpy/game.py rpy/engine.py rpy/levels.py
GEN     := gen/game.c gen/engine.c gen/levels.c gen/shivyc_rt.c

.PHONY: all transpile run serial test clean

all: $(ELF)

transpile $(GEN): $(RPY_SRC) $(PY2C)
	python3 $(PY2C) $(RPY_SRC) --out gen

# ---- objects --------------------------------------------------------------
KERNEL_C := kernel.c glue.c sprites.c libmini.c vbe.c
OBJS := $(BUILD)/boot64.o \
        $(KERNEL_C:%.c=$(BUILD)/%.o) \
        $(GEN:gen/%.c=$(BUILD)/gen_%.o) \
        $(BUILD)/rt_freestanding.o

$(BUILD):
	@mkdir -p $(BUILD)

$(BUILD)/boot64.o: boot64.S | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/%.o: %.c kernel.h mbos.h font8x16.h | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/gen_%.o: gen/%.c $(GEN) | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/rt_freestanding.o: rpy/rt_freestanding.c kernel.h mbos.h | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(ELF): $(OBJS) linker64.ld | $(BUILD)
	$(LD) $(LDFLAGS) -o $@ $(OBJS)
	@echo "built $@ ($(GFX_W)x$(GFX_H))"

# ---- run / test ------------------------------------------------------------
run: $(ELF)
	$(QEMU) $(QEMU_FLAGS) -serial stdio

serial: $(ELF)
	$(QEMU) $(QEMU_FLAGS) -display none -serial stdio

test: $(ELF)
	python3 test.py

soak: $(ELF)
	python3 tools/soak_test.py

# regenerate rpy/levels.py + sprites.c from the original Unity project
levels:
	python3 tools/extract_levels.py

clean:
	rm -rf $(BUILD) gen
