#!/usr/bin/env python3
"""soak_test.py -- load every level in sequence and poke each one.

Boots the game headless, walks the level-select grid to each of the 43
levels, enters it, makes a couple of moves (plus an undo), leaves, and
moves on. Asserts every level logged its start line and that the runtime
never hit an arena/exception halt. A screenshot of a late level is saved
as a rendering artifact.

Run:  python3 tools/soak_test.py
"""
import os
import re
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ELF = os.environ.get("SNAKE_ELF", os.path.join(HERE, "build", "snake.elf"))
MON = "/tmp/snake_soak.sock"
NLEVELS = 43


def main():
    if os.path.exists(MON):
        os.remove(MON)
    proc = subprocess.Popen(
        ["qemu-system-x86_64", "-kernel", ELF, "-no-reboot", "-m", "256",
         "-accel", "kvm", "-accel", "tcg",
         "-vga", "none", "-device", "VGA,vgamem_mb=64",
         "-display", "none", "-serial", "stdio",
         "-monitor", "unix:%s,server,nowait" % MON],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    time.sleep(4)
    sock = socket.socket(socket.AF_UNIX)
    sock.connect(MON)
    time.sleep(0.3)

    def key(name, wait=0.18):
        sock.sendall(("sendkey %s\n" % name).encode())
        time.sleep(wait)

    key("ret", 0.6)                      # title -> select (cursor at 0)
    cursor = 0
    try:
        for lvl in range(NLEVELS):
            # walk the 8-wide grid from `cursor` to `lvl` (down clamps to
            # the last button when the target row is partial, like the game)
            while cursor // 8 < lvl // 8:
                key("down")
                cursor = cursor + 8 if cursor + 8 < NLEVELS else NLEVELS - 1
            while cursor // 8 > lvl // 8:
                key("up"); cursor -= 8
            while cursor % 8 < lvl % 8:
                key("right"); cursor += 1
            while cursor % 8 > lvl % 8:
                key("left"); cursor -= 1
            key("ret", 0.5)              # enter the level
            for m in ("right", "up", "left"):
                key(m, 0.3)
            key("z", 0.25)               # undo once
            if lvl == 36:                # level 37: trapdoor maze
                sock.sendall(b"screendump /tmp/snake_soak.ppm\n")
                time.sleep(1.0)
            key("esc", 0.5)              # back to select
        sock.sendall(b"quit\n")
        time.sleep(0.3)
    except BrokenPipeError:
        print("QEMU died mid-script (crash?)")
    proc.terminate()
    out = proc.stdout.read().decode("utf-8", "replace")
    proc.wait()

    started = sorted(set(int(m) for m in
                         re.findall(r"\[game\] level (\d+) start", out)))
    missing = [n for n in range(1, NLEVELS + 1) if n not in started]
    bad = [l for l in out.splitlines()
           if "[rt]" in l or "arena exhausted" in l or "longjmp" in l]

    try:
        from PIL import Image
        png = os.path.join(HERE, "build", "soak_level37.png")
        Image.open("/tmp/snake_soak.ppm").save(png)
        print("screenshot:", png)
    except Exception as e:
        print("screenshot skipped:", e)

    print("levels started:", len(started))
    if missing:
        print("FAIL -- levels never started:", missing)
        sys.exit(1)
    if bad:
        print("FAIL -- runtime faults:")
        for l in bad:
            print("  ", l)
        sys.exit(1)
    print("PASS -- all %d levels loaded, poked, and left cleanly." % NLEVELS)


if __name__ == "__main__":
    main()
