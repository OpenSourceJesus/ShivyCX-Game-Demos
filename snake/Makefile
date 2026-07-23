# snake -- a bare-metal remake of ~/snake-game, written in rpython and
# translated to C by ShivyC's tools/py2c.py, rendered through the mbos
# Bochs-VBE graphics path (default GFX_DIV=3 → 640x360).
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

# Divide the 1920x1080 authoring size by GFX_DIV for the window.
# Example: GFX_DIV=3 → 640x360; GFX_DIV=1 → full 1920x1080.
# Changing GFX_DIV must rebuild C objects (framebuffer arrays embed W×H).
GFX_DIV ?= 2
GFX_W := $(shell expr 1920 / $(GFX_DIV))
GFX_H := $(shell expr 1080 / $(GFX_DIV))
GFX_TAG := $(BUILD)/gfx_$(GFX_DIV)_$(GFX_W)x$(GFX_H).tag

# 64-bit freestanding, exactly the mbos rpython-path flags, with a roomier
# runtime arena (2^26 = 64 MiB) since a game session allocates indefinitely
# (undo history, level reloads) where mbos rendered one page and halted.
CFLAGS := -m64 -std=c11 -ffreestanding -fno-pic -mno-red-zone \
          -fno-stack-protector -fno-builtin -nostdlib -mcmodel=small -w -O2 \
          -DSHIVYC_ARENA_LOG2=26 \
          -DMBOS_GFX_DIV=$(GFX_DIV) \
          -DMBOS_GFX_W=$(GFX_W) -DMBOS_GFX_H=$(GFX_H) \
          -Irpy/freestanding_inc -Igen -I.
LDFLAGS := -n -T linker64.ld -nostdlib

QEMU      := qemu-system-x86_64
QEMU_VGA  := -vga none -device VGA,vgamem_mb=64
# Keep guest pixels 1:1 in the window (no zoom-to-fit upscale).
QEMU_DISPLAY := -display gtk,zoom-to-fit=off
QEMU_FLAGS := -kernel $(ELF) -no-reboot -m 256 -accel kvm -accel tcg \
              $(QEMU_VGA) $(QEMU_DISPLAY)

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

# New GFX_DIV → new tag name → wipe objects compiled for the old size.
$(GFX_TAG): | $(BUILD)
	@rm -f $(BUILD)/gfx_*.tag $(OBJS) $(ELF)
	@touch $@
	@echo "graphics $(GFX_W)x$(GFX_H) (GFX_DIV=$(GFX_DIV))"

$(BUILD)/boot64.o: boot64.S $(GFX_TAG) | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/%.o: %.c kernel.h mbos.h font8x16.h $(GFX_TAG) | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/gen_%.o: gen/%.c $(GEN) $(GFX_TAG) | $(BUILD)
	$(CC) $(CFLAGS) -c $< -o $@

$(BUILD)/rt_freestanding.o: rpy/rt_freestanding.c kernel.h mbos.h $(GFX_TAG) | $(BUILD)
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
