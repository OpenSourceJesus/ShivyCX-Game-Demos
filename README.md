# ShivyCX Game Demos

Games that run on bare metal, written in the rpython dialect that ShivyC's
`tools/py2c.py` translates to C, on the boot/graphics stack from ShivyC's
`examples/rpython2c/mbos` demo (Multiboot -> x86-64 long mode, Bochs-VBE
linear framebuffer, QEMU `-kernel`).

| demo | what it is |
|---|---|
| [`snake/`](snake/) | a remake of the `~/snake-game` Unity puzzle game (turn-based Sokoban-snake with pits, portals, ice, bombs, weightpads, undo) at 1920x1080, with the original levels and art extracted straight from the Unity project |

Each demo directory has its own README with build/run/test instructions;
`make run` in the demo directory is all it takes (needs `gcc`,
`qemu-system-x86_64`, `python3`, and ShivyC checked out as a sibling at
`~/ShivyC`).
