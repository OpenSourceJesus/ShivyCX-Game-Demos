#!/usr/bin/env python3
"""test.py -- scripted end-to-end test for the bare-metal snake game.

Boots build/snake.elf headless under QEMU, drives it entirely through the
monitor's `sendkey` (title -> level select -> level 1 -> the winning move
sequence -> congrats -> back to select -> re-enter -> undo/reset), captures
the serial game log, asserts the expected transitions, and saves screenshots
of every stage to build/.

The level-1 winning sequence was found by the BFS solver in
tools/extract_levels.py (--solve 1) against the extracted level data.

Exit code 0 = pass. Needs qemu-system-x86_64; Pillow for PNG screenshots.
"""
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ELF = os.environ.get("SNAKE_ELF", os.path.join(HERE, "build", "snake.elf"))
MON = "/tmp/snake_mon.sock"

SOLUTION = "uuulurrrrrr"          # tools/extract_levels.py --solve 1
KEYMAP = {"u": "up", "d": "down", "l": "left", "r": "right"}

EXPECT = [
    "[snake] boot ok (rpython game path)",
    "[gfx] framebuffer up",
    "[game] boot",
    "[game] title",
    "[game] level select",
    "[game] level 1 start",
    "[game] turn 1",
    "[game] box filled a pit",
    "[game] won level 1",
    "[game] back to level select",
    "[game] undo",
    "[game] reset",
    "[game] back to menu",
]


class Monitor:
    def __init__(self, path):
        self.sock = socket.socket(socket.AF_UNIX)
        for _ in range(50):
            try:
                self.sock.connect(path)
                break
            except OSError:
                time.sleep(0.1)
        time.sleep(0.3)
        self.drain()

    def drain(self):
        self.sock.setblocking(False)
        try:
            while True:
                if not self.sock.recv(65536):
                    break
        except BlockingIOError:
            pass
        self.sock.setblocking(True)

    def cmd(self, line, wait=0.25):
        self.sock.sendall((line + "\n").encode())
        time.sleep(wait)
        self.drain()

    def key(self, name, wait=0.3):
        self.cmd("sendkey %s" % name, wait)

    def shot(self, name):
        ppm = "/tmp/snake_%s.ppm" % name
        self.cmd("screendump %s" % ppm, wait=1.0)
        try:
            from PIL import Image
            img = Image.open(ppm)
            png = os.path.join(HERE, "build", "%s.png" % name)
            img.save(png)
            lo, hi = img.convert("L").getextrema()
            print("screenshot %s (%s, contrast %d..%d)" %
                  (png, img.size, lo, hi))
            return hi - lo
        except Exception as e:
            sys.stderr.write("screenshot %s skipped: %s\n" % (name, e))
            return -1


def main():
    if not os.path.exists(ELF):
        sys.exit("missing %s -- run `make` first" % ELF)
    if os.path.exists(MON):
        os.remove(MON)

    proc = subprocess.Popen(
        ["qemu-system-x86_64", "-kernel", ELF, "-no-reboot", "-m", "256",
         "-accel", "kvm", "-accel", "tcg",
         "-vga", "none", "-device", "VGA,vgamem_mb=64",
         "-display", "none", "-serial", "stdio",
         "-monitor", "unix:%s,server,nowait" % MON],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    contrasts = []
    try:
        time.sleep(4)                      # boot + title
        mon = Monitor(MON)
        contrasts.append(mon.shot("title"))

        mon.key("ret", 0.6)                # -> level select
        contrasts.append(mon.shot("select"))

        mon.key("ret", 0.8)                # -> level 1
        contrasts.append(mon.shot("level1"))

        for i, m in enumerate(SOLUTION):
            # the crawl animation runs at 0.2 s/cell; pace the keys so the
            # push/portal/pit visuals settle like a human playthrough
            mon.key(KEYMAP[m], 0.45)
            if i == 4:
                contrasts.append(mon.shot("midgame"))

        time.sleep(0.8)                    # bite + growth + congrats
        contrasts.append(mon.shot("win"))
        time.sleep(3.5)                    # auto-return to level select

        mon.key("ret", 0.8)                # re-enter level 1
        mon.key("right", 0.5)              # turn 1
        mon.key("z", 0.4)                  # undo
        mon.key("right", 0.5)
        mon.key("r", 0.4)                  # reset
        mon.key("esc", 0.5)                # back to select
        contrasts.append(mon.shot("select2"))

        mon.cmd("quit", 0.2)
    except Exception as e:
        sys.stderr.write("monitor scripting failed: %s\n" % e)
    finally:
        try:
            proc.terminate()
        except OSError:
            pass

    out = proc.stdout.read().decode("utf-8", "replace")
    proc.wait()
    print(out)

    missing = [e for e in EXPECT if e not in out]
    flat = [c for c in contrasts if c == 0]
    if missing:
        print("FAIL -- missing expected serial output:")
        for m in missing:
            print("   %r" % m)
        sys.exit(1)
    if flat:
        print("FAIL -- %d screenshot(s) were blank" % len(flat))
        sys.exit(1)
    print("PASS -- all %d expected fragments seen, %d screenshots taken."
          % (len(EXPECT), len([c for c in contrasts if c >= 0])))
    sys.exit(0)


if __name__ == "__main__":
    main()
