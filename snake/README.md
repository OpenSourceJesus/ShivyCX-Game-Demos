# snakeoban -- ~/snake-game, bare metal, in RPython

A faithful remake of the Unity puzzle game at `~/snake-game` (a turn-based
Sokoban-snake: one cell per key press, push chains, pits, portals, ice,
bombs, weightpads, trapdoors, save/load zones, bug swarms, multiple snakes,
full undo/redo/reset) -- rebuilt as a **freestanding x86-64 kernel** whose
game logic is written in **rpython** and translated to C by ShivyC's
`tools/py2c.py`, rendered through the mbos **high-resolution Bochs-VBE
framebuffer** (1920x1080x32) from `~/ShivyC/examples/rpython2c/mbos`.

No OS, no libc, no GPU driver beyond 4 PCI config writes: QEMU loads the ELF
via the Multiboot AOUT kludge, `boot64.S` maps 4 GiB and enters long mode,
and the game loop polls the PS/2 controller and the PIT.

```
make            # py2c-transpile rpy/*.py -> gen/*.c, build build/snake.elf
make run        # play it: QEMU window, 1920x1080
make serial     # headless, game log on stdout
make test       # scripted playthrough of level 1 (sendkey) + screenshots
make soak       # loads and pokes all 43 levels
make levels     # re-extract levels + sprites from ~/snake-game
```

Controls (same as the original): **arrows/WASD** move, **Z/Q** undo,
**X/E** redo, **R** reset, **Shift/Ctrl/Tab/Space** switch snake, **G**
toggle the grid overlay, **Esc** menu.

## Where the game came from

`tools/extract_levels.py` reads the original Unity project directly:

* **Levels 1-43** are parsed out of the scene YAML
  (`Assets/Scenes/N.unity`): every `PrefabInstance` block's
  `m_LocalPosition` overrides give object positions; `other`,
  `doors.Array.data[*]` and `parts.Array.data[*]` object references are
  resolved through the *stripped* transform documents to link portal pairs,
  weightpad doors, and extra snake segments; the per-level camera `viewSize`
  override sizes the board on screen exactly like the original's
  letterboxing. The result is one compact string per level in
  `rpy/levels.py`.
* **The art** is the original art: sprites (walls, box, bomb, pits, apple,
  star, portal, zones, weightpad, door, bugs, title, congrats banner, level
  buttons, background...) are downsampled from `Assets/Art/Textures/*.png`
  into RGBA tables in `sprites.c`, with the original tints (gray bomb, pink
  propel zone, orange load zone, translucent save icon) premultiplied.
* The level-1 **winning key sequence** in `test.py` was found by the BFS
  solver in the extractor (`--solve 1`) against the extracted data.

## How it runs on bare metal

| layer | files | origin |
|---|---|---|
| boot: Multiboot1 AOUT kludge -> long mode, 4 GiB identity map, SSE | `boot64.S`, `linker64.ld` | mbos, unchanged |
| graphics: Bochs-VBE (DISPI 0x1CE/0x1CF) 32-bpp linear framebuffer | `vbe.c` | mbos, + `gfx_present()` bulk blit |
| freestanding libc for the generated runtime | `rpy/rt_freestanding.c`, `rpy/freestanding_inc/` | mbos, unchanged |
| kernel: serial, polled PS/2 keyboard, PIT ms clock, PC speaker | `kernel.c`, `kernel.h` | new |
| FFI shim: back buffer + baked-scene cache, rects/circles/round-rects, sprite blitter (scale + tint + alpha + quarter rotation), 8x16 text, present, input, time | `glue.c` (+ `sprites.c`, generated) | new |
| the game: turn pipeline, undo/redo, menus, rendering | `rpy/game.py` -> `gen/game.c` | new (rpython) |
| FFI bindings + key decoding | `rpy/engine.py` | new (rpython) |
| level data | `rpy/levels.py` | generated |

The rpython -> C step is the same `ctypes.CDLL` lowering the mbos demo uses:
`engine.py` declares `_g = ctypes.CDLL("snake_glue")` with
`restype`/`argtypes`, and py2c turns every `_g.sg_rect(...)` into a direct
`extern` C call resolved at link time. `kernel.c`'s `kmain` calls the
generated `levels_init()/engine_init()/game_init()` (Python import-time) and
then `snake_main()`, which never returns.

Only ints and strings cross the FFI; the game state lives in parallel
`list[int]`s (py2c lowers them to unboxed C int arrays). Undo/redo snapshots
are flat int arrays -- the checkpoint (save-zone) state rides inside every
snapshot exactly like the original `GameState.checkpointState`.

## Mechanics ported from the original (GameManager.MoveWithHistory order)

move (with recursive push chains; a bomb pushed against a wall explodes) ->
portals -> pit falls -> ice slides (active snake first, then objects/other
snakes, re-teleporting and re-falling each step) -> portals -> weightpads ->
save zones (win over load zones in the same turn) -> trapdoor arming -> bug
swarms -> pit falls -> stars -> trapdoor activation (end of the *next* turn)
-> weightpads -> **no-op detection** (a refused move costs no turn and no
undo entry) -> push the pre-move state onto the undo stack.

Details kept: the one-move-per-press rule (holding a key never repeats), the
win only when the head advances *straight* onto the apple, the 0.2 s/cell
crawl animation with instant logic, snakes dying only when *every* part is
over an unfilled pit (shallow leaves a solid corpse with dead eyes, deep
fills the pit, bottomless swallows), boxes filling deep pits and un-filling
when bombed, blast diagonals shielded by two cardinal walls, blasts riding
portals to an unoccupied exit, teleport locks until the exit is vacated,
blocked-teleport indicators, deferred door closes while the doorway is
occupied, the swarm disabling (not killing) a snake, auto-switch when the
active snake dies, the 9-confetti congrats with the +1 tail growth, star
stash on win and the quarter-alpha ghost star on replay, per-level snake
colors and portal/door link colors.

Simplified (documented deviations): the snake body is drawn as a rounded
tube from cell caps + seam bridges instead of the original Bezier spline
extrusion; portal pair lines are dotted; bug swarms draw three wandering
bugs rather than a physical swarm sim; swarms do not split through portals;
propel-zone "trench" carries at pit depth are not modeled; win/star state is
per-session (no PlayerPrefs on bare metal); no audio (the original ships
only ambient wind).

## Testing

`make test` boots headless QEMU, drives the QEMU monitor with `sendkey` --
title -> select -> level 1 -> the 11-move solution -> congrats -> re-enter
-> undo -> reset -> menu -- asserting 13 serial log fragments and saving
`build/{title,select,level1,midgame,win,select2}.png`. `make soak` walks all
43 level buttons, enters each level, moves, undoes, and leaves, asserting
every level starts and the runtime never faults.

Host tooling needs `qemu-system-x86_64`, `gcc`, `python3` + Pillow
(`requirements.txt`); the extractor additionally needs the original project
at `~/snake-game` and ShivyC at `~/ShivyC` (override with `make SHIVYC=...`).
