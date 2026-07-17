"""game -- the snake-game remake, in rpython, on the mbos VGA path.

A faithful port of ~/snake-game's mechanics (a turn-based Sokoban-snake:
discrete one-cell moves, push chains, pits, portals, ice, bombs, weightpads,
trapdoors, save/load zones, bug swarms, multiple snakes, full undo/redo/
reset) driven through the snake_glue FFI. Levels and art are extracted from
the original Unity project by tools/extract_levels.py.

Everything is integer math (fixed point where needed) -- no floats reach the
bare-metal runtime. Data lives in parallel list[int]s; the only boxed
containers are the undo/redo stacks of Snap objects.
"""
import engine
import levels

SCR_TITLE = 0
SCR_SELECT = 1
SCR_GAME = 2

MAX_SNAKES = 6                          # 3 level slots + room for clones
CRAWL_MS = 200          # 0.2 s per cell, the original moveSpeed=5
WIN_HOLD_MS = 2000      # congrats hold, the original endAnimDur=2
GROW_MS = 1000          # tail growth, the original growSpeed=1


def _log(msg: "char*") -> None:
    engine.log(msg)


class Snake:
    def __init__(self):
        self.xs: "list[int]" = []
        self.ys: "list[int]" = []
        self.zs: "list[int]" = []          # 0 surface, 1 in pit, 2 gone deep
        self.vx: "list[int]" = []          # visual pos, 1/256 cell
        self.vy: "list[int]" = []
        self.alive = 1
        self.gone = 1                      # slot unused until loaded
        self.swarmed = 0
        self.colr = 0
        self.colg = 255
        self.colb = 0
        self.fdx = 1
        self.fdy = 0

    def npart(self) -> int:
        return len(self.xs)

    def head_x(self) -> int:
        return self.xs[0]

    def head_y(self) -> int:
        return self.ys[0]

    def occupies(self, x: int, y: int) -> int:
        """1 if a surface (solid) part covers the cell."""
        if self.gone == 1:
            return 0
        i = 0
        n = len(self.xs)
        while i < n:
            if self.xs[i] == x:
                if self.ys[i] == y:
                    if self.zs[i] == 0:
                        return 1
            i = i + 1
        return 0

    def on_cell_any(self, x: int, y: int) -> int:
        """1 if any part (any depth) covers the cell."""
        if self.gone == 1:
            return 0
        i = 0
        n = len(self.xs)
        while i < n:
            if self.xs[i] == x:
                if self.ys[i] == y:
                    return 1
            i = i + 1
        return 0

    def translate(self, dx: int, dy: int) -> None:
        i = 0
        n = len(self.xs)
        while i < n:
            self.xs[i] = self.xs[i] + dx
            self.ys[i] = self.ys[i] + dy
            i = i + 1

    def snap_visual(self) -> None:
        i = 0
        n = len(self.xs)
        while i < n:
            if i < len(self.vx):
                self.vx[i] = self.xs[i] * 256
                self.vy[i] = self.ys[i] * 256
            else:
                self.vx.append(self.xs[i] * 256)
                self.vy.append(self.ys[i] * 256)
            i = i + 1
        while len(self.vx) > len(self.xs):
            self.vx.pop()
            self.vy.pop()


# NOTE: whole list values never cross function boundaries in this module --
# py2c represents annotated list params as unboxed typed arrays but class
# fields as boxed lists, so lists live in Game fields and every transfer is
# element-wise. Only scalars (and strings) are passed around.


class Game:
    def __init__(self):
        self.screen = SCR_TITLE
        self.levelidx = 0
        self.nlevels = levels.level_count()
        self.won: "list[int]" = []
        self.stashed: "list[int]" = []
        self.hasstar: "list[int]" = []
        self.cursor = 0
        self.gridon = 1                     # the original defaults Show Grid on

        # board geometry
        self.minx = 0
        self.miny = 0
        self.gw = 1
        self.gh = 1
        self.vw = 11
        self.vh = 11
        self.camx = 0                       # *100
        self.camy = 0
        self.cell = 96
        self.ox = 0                         # screen px of cell (minx,miny)
        self.oy = 0

        # static arrays (size gw*gh)
        self.wall: "list[int]" = []         # 0 none, 1..4 walls, 5 weak
        self.pit: "list[int]" = []          # 0 none, 1 shallow, 2 deep, 3 btm
        self.filled: "list[int]" = []
        self.fillbox: "list[int]" = []      # box index that filled the pit
        self.ice: "list[int]" = []
        self.zone: "list[int]" = []         # 1 save, 2 load
        self.trapst: "list[int]" = []       # -1 none, 0 idle, 1 armed, 2 hot
        self.doorat: "list[int]" = []       # -1 or door index

        self.ax = 0
        self.ay = 0

        self.starx: "list[int]" = []
        self.stary: "list[int]" = []
        self.starcol: "list[int]" = []

        self.boxx: "list[int]" = []
        self.boxy: "list[int]" = []
        self.boxz: "list[int]" = []
        self.boxlive: "list[int]" = []

        self.bombx: "list[int]" = []
        self.bomby: "list[int]" = []
        self.bomblive: "list[int]" = []

        self.porx: "list[int]" = []
        self.pory: "list[int]" = []
        self.porpair: "list[int]" = []
        self.porcol: "list[int]" = []
        self.porbox: "list[int]" = []       # -1 or box index (Connectable)
        self.pairlock: "list[int]" = []
        self.porocc: "list[int]" = []       # occupied at last resolution
        self.entbuf: "list[int]" = []       # scratch: a snake's entry portals

        # movable Connectables (propel / save / load zones). Positions track
        # the box they are connectedTo; ice[] and zone[] are the lookup cache.
        self.connx: "list[int]" = []
        self.conny: "list[int]" = []
        self.conntype: "list[int]" = []     # 1 propel, 2 save, 3 load
        self.connbox: "list[int]" = []      # -1 or box index

        self.padx: "list[int]" = []
        self.pady: "list[int]" = []
        self.padcol: "list[int]" = []
        self.padpressed: "list[int]" = []
        self.padopen: "list[int]" = []      # pad toggle parity (0 at start)
        self.padpend: "list[int]" = []      # deferred toggles
        self.doorx: "list[int]" = []
        self.doory: "list[int]" = []
        self.doorpadidx: "list[int]" = []
        self.dooropen0: "list[int]" = []    # door starts open (scene state)

        self.swarmx: "list[int]" = []
        self.swarmy: "list[int]" = []

        self.s0 = Snake()
        self.s1 = Snake()
        self.s2 = Snake()
        self.s3 = Snake()                   # clone slots (portal duplicates)
        self.s4 = Snake()
        self.s5 = Snake()
        self.nsnakes = 0
        self.active = 0

        self.turn = 0
        self.lastdx = 0
        self.lastdy = 0

        # undo/redo: snapshots concatenated into one flat array + lengths
        # (the checkpoint state is nested inside each snapshot, like the
        # original GameState.checkpointState)
        self.hist: "list[int]" = []
        self.histlen: "list[int]" = []
        self.rhist: "list[int]" = []
        self.rhistlen: "list[int]" = []
        self.chkdata: "list[int]" = []
        self.initdata: "list[int]" = []
        self.snapbuf: "list[int]" = []     # ser() output
        self.applybuf: "list[int]" = []    # apply() input
        self.beforebuf: "list[int]" = []   # pre-move state
        self.beforechk: "list[int]" = []   # pre-move checkpoint

        # transient FX
        self.blockx: "list[int]" = []
        self.blocky: "list[int]" = []
        self.blockms: "list[int]" = []
        self.winning = 0
        self.winms = 0
        self.grew = 0
        self.confx: "list[int]" = []
        self.confy: "list[int]" = []
        self.confc: "list[int]" = []
        self.lastms = 0
        self.fpsframes = 0
        self.fpsms = 0
        # scene-cache dirty flag: when 1, the static board layer is redrawn
        # and baked (engine.bake); frames in between just restore the bake
        # and draw the animated entities on top.
        self.dirty = 1

    # ------------------------------------------------------------- helpers

    def snake(self, i: int) -> "Snake":
        if i == 0:
            return self.s0
        if i == 1:
            return self.s1
        if i == 2:
            return self.s2
        if i == 3:
            return self.s3
        if i == 4:
            return self.s4
        return self.s5

    def idx(self, x: int, y: int) -> int:
        ix = x - self.minx
        iy = y - self.miny
        if ix < 0:
            return -1
        if iy < 0:
            return -1
        if ix >= self.gw:
            return -1
        if iy >= self.gh:
            return -1
        return iy * self.gw + ix

    def door_open(self, dj: int) -> int:
        """1 if door dj is open: its scene-start state XOR the pad's
        toggle parity (Unity flips each door's activeSelf on toggle)."""
        if self.dooropen0[dj] != self.padopen[self.doorpadidx[dj]]:
            return 1
        return 0

    def wall_at(self, x: int, y: int) -> int:
        """1 if a wall or closed door blocks the cell."""
        i = self.idx(x, y)
        if i < 0:
            return 1                        # outside the board: solid
        if self.wall[i] > 0:
            return 1
        dj = self.doorat[i]
        if dj >= 0:
            if self.door_open(dj) == 0:
                return 1
        return 0

    def pit_open_at(self, x: int, y: int) -> int:
        """pit type 1..3 if an unfilled pit is there, else 0."""
        i = self.idx(x, y)
        if i < 0:
            return 0
        if self.pit[i] > 0:
            if self.filled[i] == 0:
                return self.pit[i]
        return 0

    def box_at(self, x: int, y: int) -> int:
        i = 0
        n = len(self.boxx)
        while i < n:
            if self.boxlive[i] == 1:
                if self.boxx[i] == x:
                    if self.boxy[i] == y:
                        if self.boxz[i] == 0:
                            return i
            i = i + 1
        return -1

    def bomb_at(self, x: int, y: int) -> int:
        i = 0
        n = len(self.bombx)
        while i < n:
            if self.bomblive[i] == 1:
                if self.bombx[i] == x:
                    if self.bomby[i] == y:
                        return i
            i = i + 1
        return -1

    def snake_at(self, x: int, y: int) -> int:
        i = 0
        while i < self.nsnakes:
            s = self.snake(i)
            if s.occupies(x, y) == 1:
                return i
            i = i + 1
        return -1

    def apple_at(self, x: int, y: int) -> int:
        if x == self.ax:
            if y == self.ay:
                return 1
        return 0

    def cell_free(self, x: int, y: int, ignore_snake: int) -> int:
        """1 if an entity part may occupy the cell (pits are enterable)."""
        if self.wall_at(x, y) == 1:
            return 0
        if self.apple_at(x, y) == 1:
            return 0
        if self.box_at(x, y) >= 0:
            return 0
        if self.bomb_at(x, y) >= 0:
            return 0
        i = 0
        while i < self.nsnakes:
            if i != ignore_snake:
                s = self.snake(i)
                if s.occupies(x, y) == 1:
                    return 0
            i = i + 1
        return 1

    def solid_on(self, x: int, y: int) -> int:
        """1 if any surface solid stands on the cell (pads/zones/traps)."""
        if self.box_at(x, y) >= 0:
            return 1
        if self.bomb_at(x, y) >= 0:
            return 1
        if self.snake_at(x, y) >= 0:
            return 1
        return 0

    # -------------------------------------------------------- level loading

    def boot_scan(self) -> None:
        i = 0
        while i < self.nlevels:
            self.won.append(0)
            self.stashed.append(0)
            d = levels.level_data(i)
            if d.find("\n* ") >= 0:
                self.hasstar.append(1)
            else:
                self.hasstar.append(0)
            i = i + 1

    def load_level(self, idx: int) -> None:
        self.levelidx = idx
        self.dirty = 1
        d = levels.level_data(idx)
        lines = d.split("\n")
        nlines = len(lines)

        # pass 1: bounds
        minx = 9999
        miny = 9999
        maxx = -9999
        maxy = -9999
        li = 0
        while li < nlines:
            line: "char*" = lines[li]
            toks = line.split(" ")
            if len(toks) >= 3:
                op: "char*" = toks[0]
                if op != "V":
                    k = 1
                    npos = 1
                    if op == "S":
                        k = 4
                        npos = (len(toks) - 4) // 2
                    if op == "D":
                        npos = 1        # door cells handled below
                    j = 0
                    while j < npos:
                        x = int(toks[k + 2 * j])
                        y = int(toks[k + 2 * j + 1])
                        if x < minx:
                            minx = x
                        if x > maxx:
                            maxx = x
                        if y < miny:
                            miny = y
                        if y > maxy:
                            maxy = y
                        j = j + 1
                    if op == "D":
                        nd = int(toks[6])
                        j = 0
                        while j < nd:
                            x = int(toks[7 + 3 * j])
                            y = int(toks[8 + 3 * j])
                            if x < minx:
                                minx = x
                            if x > maxx:
                                maxx = x
                            if y < miny:
                                miny = y
                            if y > maxy:
                                maxy = y
                            j = j + 1
            li = li + 1

        self.minx = minx - 1
        self.miny = miny - 1
        self.gw = maxx - minx + 3
        self.gh = maxy - miny + 3

        n = self.gw * self.gh
        self.wall = []
        self.pit = []
        self.filled = []
        self.fillbox = []
        self.ice = []
        self.zone = []
        self.trapst = []
        self.doorat = []
        i = 0
        while i < n:
            self.wall.append(0)
            self.pit.append(0)
            self.filled.append(0)
            self.fillbox.append(-1)
            self.ice.append(0)
            self.zone.append(0)
            self.trapst.append(-1)
            self.doorat.append(-1)
            i = i + 1

        self.starx = []
        self.stary = []
        self.starcol = []
        self.boxx = []
        self.boxy = []
        self.boxz = []
        self.boxlive = []
        self.bombx = []
        self.bomby = []
        self.bomblive = []
        self.porx = []
        self.pory = []
        self.porpair = []
        self.porcol = []
        self.porbox = []
        self.pairlock = []
        self.porocc = []
        self.connx = []
        self.conny = []
        self.conntype = []
        self.connbox = []
        self.padx = []
        self.pady = []
        self.padcol = []
        self.padpressed = []
        self.padopen = []
        self.padpend = []
        self.doorx = []
        self.doory = []
        self.doorpadidx = []
        self.dooropen0 = []
        self.swarmx = []
        self.swarmy = []
        self.s0 = Snake()
        self.s1 = Snake()
        self.s2 = Snake()
        self.s3 = Snake()
        self.s4 = Snake()
        self.s5 = Snake()
        self.nsnakes = 0
        self.active = 0
        self.turn = 0
        self.lastdx = 0
        self.lastdy = 0
        self.hist = []
        self.histlen = []
        self.rhist = []
        self.rhistlen = []
        self.blockx = []
        self.blocky = []
        self.blockms = []
        self.winning = 0
        self.grew = 0

        npairs = 0
        li = 0
        while li < nlines:
            line2: "char*" = lines[li]
            toks = line2.split(" ")
            li = li + 1
            if len(toks) < 3:
                continue
            op2: "char*" = toks[0]
            if op2 == "V":
                self.vw = int(toks[1])
                self.vh = int(toks[2])
                self.camx = int(toks[3])
                self.camy = int(toks[4])
                continue
            x = int(toks[1])
            y = int(toks[2])
            ci = self.idx(x, y)
            if op2 == "W":
                kind = int(toks[3])
                self.wall[ci] = kind + 1
            elif op2 == "P":
                t = int(toks[3])
                self.pit[ci] = t + 1
            elif op2 == "O":
                self.boxx.append(x)
                self.boxy.append(y)
                self.boxz.append(0)
                self.boxlive.append(1)
            elif op2 == "M":
                self.bombx.append(x)
                self.bomby.append(y)
                self.bomblive.append(1)
            elif op2 == "A":
                self.ax = x
                self.ay = y
            elif op2 == "*":
                self.starx.append(x)
                self.stary.append(y)
                self.starcol.append(0)
            elif op2 == "I":
                self.ice[ci] = 1
                self.connx.append(x)
                self.conny.append(y)
                self.conntype.append(1)
                bi = -1
                if len(toks) >= 4:
                    bi = int(toks[3])
                self.connbox.append(bi)
            elif op2 == "Y":
                self.zone[ci] = 1
                self.connx.append(x)
                self.conny.append(y)
                self.conntype.append(2)
                bi = -1
                if len(toks) >= 4:
                    bi = int(toks[3])
                self.connbox.append(bi)
            elif op2 == "L":
                self.zone[ci] = 2
                self.connx.append(x)
                self.conny.append(y)
                self.conntype.append(3)
                bi = -1
                if len(toks) >= 4:
                    bi = int(toks[3])
                self.connbox.append(bi)
            elif op2 == "H":
                self.trapst[ci] = 0
            elif op2 == "G":
                self.swarmx.append(x)
                self.swarmy.append(y)
            elif op2 == "R":
                pair = int(toks[3])
                self.porx.append(x)
                self.pory.append(y)
                self.porpair.append(pair)
                r = int(toks[4])
                g = int(toks[5])
                b = int(toks[6])
                self.porcol.append(r * 65536 + g * 256 + b)
                self.porocc.append(0)
                bi = -1
                if len(toks) >= 8:
                    bi = int(toks[7])
                self.porbox.append(bi)
                if pair >= npairs:
                    npairs = pair + 1
            elif op2 == "D":
                pi = len(self.padx)
                self.padx.append(x)
                self.pady.append(y)
                r = int(toks[3])
                g = int(toks[4])
                b = int(toks[5])
                self.padcol.append(r * 65536 + g * 256 + b)
                self.padpressed.append(0)
                self.padopen.append(0)
                self.padpend.append(0)
                nd = int(toks[6])
                j = 0
                while j < nd:
                    dx = int(toks[7 + 3 * j])
                    dy = int(toks[8 + 3 * j])
                    di = self.idx(dx, dy)
                    self.doorx.append(dx)
                    self.doory.append(dy)
                    self.doorpadidx.append(pi)
                    self.dooropen0.append(int(toks[9 + 3 * j]))
                    self.doorat[di] = len(self.doorx) - 1
                    j = j + 1
            elif op2 == "S":
                if self.nsnakes < MAX_SNAKES:
                    s = self.snake(self.nsnakes)
                    s.gone = 0
                    s.alive = 1
                    s.swarmed = 0
                    s.colr = int(toks[1])
                    s.colg = int(toks[2])
                    s.colb = int(toks[3])
                    np = int(toks[4])
                    j = 0
                    while j < np:
                        s.xs.append(int(toks[5 + 2 * j]))
                        s.ys.append(int(toks[6 + 2 * j]))
                        s.zs.append(0)
                        j = j + 1
                    if np >= 2:
                        s.fdx = s.xs[0] - s.xs[1]
                        s.fdy = s.ys[0] - s.ys[1]
                    s.snap_visual()
                    self.nsnakes = self.nsnakes + 1

        i = 0
        while i < npairs:
            self.pairlock.append(0)
            i = i + 1

        # screen geometry: fit the original camera view into 1920x1080
        sw = engine.width()
        sh = engine.height()
        cw = sw // self.vw
        chh = sh // self.vh
        if cw < chh:
            self.cell = cw
        else:
            self.cell = chh
        # camera center (camx/100, camy/100) maps to screen center
        self.ox = sw // 2 - (self.camx * self.cell) // 100 - self.cell // 2
        self.oy = sh // 2 + (self.camy * self.cell) // 100 - self.cell // 2

        self.ser()
        self._copybuf(0, 5)                 # snapbuf -> initdata
        # no checkpoint until a save zone triggers (empty chkdata = none,
        # like the original's null checkpointState)
        self._buf_clear(4)
        self.update_pads_initial()
        _log("[game] level " + str(idx + 1) + " start")

    def update_pads_initial(self) -> None:
        i = 0
        n = len(self.padx)
        while i < n:
            self.padpressed[i] = self.solid_on(self.padx[i], self.pady[i])
            i = i + 1

    # -------------------------------------------------------- serialization
    # Buffers are selected by small int codes so list values never cross a
    # function boundary (see the note above the class).

    def _buf_len(self, sel: int) -> int:
        if sel == 0:
            return len(self.snapbuf)
        if sel == 1:
            return len(self.applybuf)
        if sel == 2:
            return len(self.beforebuf)
        if sel == 3:
            return len(self.beforechk)
        if sel == 4:
            return len(self.chkdata)
        if sel == 5:
            return len(self.initdata)
        if sel == 6:
            return len(self.hist)
        return len(self.rhist)

    def _buf_get(self, sel: int, i: int) -> int:
        if sel == 0:
            return self.snapbuf[i]
        if sel == 1:
            return self.applybuf[i]
        if sel == 2:
            return self.beforebuf[i]
        if sel == 3:
            return self.beforechk[i]
        if sel == 4:
            return self.chkdata[i]
        if sel == 5:
            return self.initdata[i]
        if sel == 6:
            return self.hist[i]
        return self.rhist[i]

    def _buf_clear(self, sel: int) -> None:
        if sel == 0:
            self.snapbuf = []
        elif sel == 1:
            self.applybuf = []
        elif sel == 2:
            self.beforebuf = []
        elif sel == 3:
            self.beforechk = []
        elif sel == 4:
            self.chkdata = []
        elif sel == 5:
            self.initdata = []
        elif sel == 6:
            self.hist = []
        else:
            self.rhist = []

    def _buf_push(self, sel: int, v: int) -> None:
        if sel == 0:
            self.snapbuf.append(v)
        elif sel == 1:
            self.applybuf.append(v)
        elif sel == 2:
            self.beforebuf.append(v)
        elif sel == 3:
            self.beforechk.append(v)
        elif sel == 4:
            self.chkdata.append(v)
        elif sel == 5:
            self.initdata.append(v)
        elif sel == 6:
            self.hist.append(v)
        else:
            self.rhist.append(v)

    def _copybuf(self, src: int, dst: int) -> None:
        self._buf_clear(dst)
        i = 0
        n = self._buf_len(src)
        while i < n:
            self._buf_push(dst, self._buf_get(src, i))
            i = i + 1

    def ser(self) -> None:
        """Snapshot the dynamic state into snapbuf."""
        self.snapbuf = []
        d = self.snapbuf
        d.append(self.turn)
        d.append(self.active)
        n = self.gw * self.gh
        i = 0
        while i < n:
            d.append(self.filled[i])
            d.append(self.fillbox[i])
            d.append(self.trapst[i])
            d.append(self.wall[i])          # weak walls can be destroyed
            i = i + 1
        i = 0
        n = len(self.padx)
        while i < n:
            d.append(self.padpressed[i])
            d.append(self.padopen[i])
            d.append(self.padpend[i])
            i = i + 1
        i = 0
        n = len(self.pairlock)
        while i < n:
            d.append(self.pairlock[i])
            i = i + 1
        i = 0
        n = len(self.porocc)
        while i < n:
            d.append(self.porocc[i])
            d.append(self.porx[i])
            d.append(self.pory[i])
            i = i + 1
        i = 0
        n = len(self.connx)
        while i < n:
            d.append(self.connx[i])
            d.append(self.conny[i])
            i = i + 1
        i = 0
        n = len(self.starx)
        while i < n:
            d.append(self.starcol[i])
            i = i + 1
        i = 0
        n = len(self.boxx)
        while i < n:
            d.append(self.boxx[i])
            d.append(self.boxy[i])
            d.append(self.boxz[i])
            d.append(self.boxlive[i])
            i = i + 1
        i = 0
        n = len(self.bombx)
        while i < n:
            d.append(self.bombx[i])
            d.append(self.bomby[i])
            d.append(self.bomblive[i])
            i = i + 1
        # snake count varies at runtime (portal exit clones), so the
        # snapshot carries it plus each snake's colour
        d.append(self.nsnakes)
        i = 0
        while i < self.nsnakes:
            s = self.snake(i)
            d.append(s.alive)
            d.append(s.gone)
            d.append(s.swarmed)
            d.append(s.fdx)
            d.append(s.fdy)
            d.append(s.colr)
            d.append(s.colg)
            d.append(s.colb)
            d.append(s.npart())
            j = 0
            while j < s.npart():
                d.append(s.xs[j])
                d.append(s.ys[j])
                d.append(s.zs[j])
                j = j + 1
            i = i + 1

    def apply_buf(self) -> None:
        """Restore the dynamic state from applybuf."""
        d = self.applybuf
        k = 0
        self.turn = d[k]
        k = k + 1
        self.active = d[k]
        k = k + 1
        n = self.gw * self.gh
        i = 0
        while i < n:
            self.filled[i] = d[k]
            self.fillbox[i] = d[k + 1]
            self.trapst[i] = d[k + 2]
            self.wall[i] = d[k + 3]
            k = k + 4
            i = i + 1
        i = 0
        n = len(self.padx)
        while i < n:
            self.padpressed[i] = d[k]
            self.padopen[i] = d[k + 1]
            self.padpend[i] = d[k + 2]
            k = k + 3
            i = i + 1
        i = 0
        n = len(self.pairlock)
        while i < n:
            self.pairlock[i] = d[k]
            k = k + 1
            i = i + 1
        i = 0
        n = len(self.porocc)
        while i < n:
            self.porocc[i] = d[k]
            self.porx[i] = d[k + 1]
            self.pory[i] = d[k + 2]
            k = k + 3
            i = i + 1
        i = 0
        n = len(self.connx)
        while i < n:
            self.connx[i] = d[k]
            self.conny[i] = d[k + 1]
            k = k + 2
            i = i + 1
        i = 0
        n = len(self.starx)
        while i < n:
            self.starcol[i] = d[k]
            k = k + 1
            i = i + 1
        i = 0
        n = len(self.boxx)
        while i < n:
            self.boxx[i] = d[k]
            self.boxy[i] = d[k + 1]
            self.boxz[i] = d[k + 2]
            self.boxlive[i] = d[k + 3]
            k = k + 4
            i = i + 1
        i = 0
        n = len(self.bombx)
        while i < n:
            self.bombx[i] = d[k]
            self.bomby[i] = d[k + 1]
            self.bomblive[i] = d[k + 2]
            k = k + 3
            i = i + 1
        ns = d[k]
        k = k + 1
        i = 0
        while i < ns:
            s = self.snake(i)
            s.alive = d[k]
            s.gone = d[k + 1]
            s.swarmed = d[k + 2]
            s.fdx = d[k + 3]
            s.fdy = d[k + 4]
            s.colr = d[k + 5]
            s.colg = d[k + 6]
            s.colb = d[k + 7]
            np = d[k + 8]
            k = k + 9
            s.xs = []
            s.ys = []
            s.zs = []
            j = 0
            while j < np:
                s.xs.append(d[k])
                s.ys.append(d[k + 1])
                s.zs.append(d[k + 2])
                k = k + 3
                j = j + 1
            s.snap_visual()
            i = i + 1
        # clones past the restored count vanish (undo before their creation)
        while i < self.nsnakes:
            s = self.snake(i)
            s.gone = 1
            s.alive = 0
            s.xs = []
            s.ys = []
            s.zs = []
            i = i + 1
        self.nsnakes = ns
        if self.active >= ns:
            self.active = 0

    # undo/redo entries are [corelen, core..., chklen, chk...] appended to a
    # flat int array; a parallel array holds each entry's total length. The
    # checkpoint rides inside every snapshot, like the original GameState's
    # nested checkpointState.

    def _bufs_equal(self, a: int, b: int) -> int:
        na = self._buf_len(a)
        nb = self._buf_len(b)
        if na != nb:
            return 0
        i = 0
        while i < na:
            if self._buf_get(a, i) != self._buf_get(b, i):
                return 0
            i = i + 1
        return 1

    def _hist_push(self, side: int, coresel: int, chksel: int) -> None:
        """Append [corelen, core..., chklen, chk...] to hist (0) / rhist (1)."""
        ncore = self._buf_len(coresel)
        nchk = self._buf_len(chksel)
        hsel = 6
        if side == 1:
            hsel = 7
        self._buf_push(hsel, ncore)
        i = 0
        while i < ncore:
            self._buf_push(hsel, self._buf_get(coresel, i))
            i = i + 1
        self._buf_push(hsel, nchk)
        i = 0
        while i < nchk:
            self._buf_push(hsel, self._buf_get(chksel, i))
            i = i + 1
        if side == 0:
            self.histlen.append(2 + ncore + nchk)
        else:
            self.rhistlen.append(2 + ncore + nchk)

    def _hist_pop_apply(self, side: int) -> None:
        """Pop the newest entry off hist/rhist and restore it."""
        hsel = 6
        if side == 1:
            hsel = 7
        if side == 0:
            n = self.histlen.pop()
        else:
            n = self.rhistlen.pop()
        start = self._buf_len(hsel) - n
        corelen = self._buf_get(hsel, start)
        self.applybuf = []
        i = 0
        while i < corelen:
            self.applybuf.append(self._buf_get(hsel, start + 1 + i))
            i = i + 1
        chkstart = start + 1 + corelen
        chklen = self._buf_get(hsel, chkstart)
        self.chkdata = []
        i = 0
        while i < chklen:
            self.chkdata.append(self._buf_get(hsel, chkstart + 1 + i))
            i = i + 1
        while self._buf_len(hsel) > start:
            if side == 0:
                self.hist.pop()
            else:
                self.rhist.pop()
        self.apply_buf()

    # ------------------------------------------------------------ explosions

    def blast_shielded(self, cx: int, cy: int, dx: int, dy: int) -> int:
        """A diagonal is spared if both adjacent cardinals block the blast."""
        if dx == 0:
            return 0
        if dy == 0:
            return 0
        if self.wall_at(cx + dx, cy) == 1:
            if self.wall_at(cx, cy + dy) == 1:
                return 1
        return 0

    def explode(self, bi: int, depth: int) -> None:
        if self.bomblive[bi] == 0:
            return
        cx = self.bombx[bi]
        cy = self.bomby[bi]
        self.bomblive[bi] = 0
        _log("[game] bomb exploded")
        dy = -1
        while dy <= 1:
            dx = -1
            while dx <= 1:
                if self.blast_shielded(cx, cy, dx, dy) == 0:
                    self.blast_cell(cx + dx, cy + dy, depth)
                dx = dx + 1
            dy = dy + 1

    def blast_cell(self, x: int, y: int, depth: int) -> None:
        i = self.idx(x, y)
        if i >= 0:
            if self.wall[i] == 5:           # weak wall crumbles
                self.wall[i] = 0
            if self.filled[i] == 1:         # un-fill filled pits
                self.filled[i] = 0
                bfi = self.fillbox[i]
                if bfi >= 0:
                    self.boxlive[bfi] = 0
                self.fillbox[i] = -1
            if self.trapst[i] >= 0:
                self.trapst[i] = -1
        bx = self.box_at(x, y)
        if bx >= 0:
            self.boxlive[bx] = 0
        bb = self.bomb_at(x, y)
        if bb >= 0:
            if self.bomblive[bb] == 1:
                self.explode(bb, depth)
        si = 0
        while si < len(self.swarmx):
            if self.swarmx[si] == x:
                if self.swarmy[si] == y:
                    self.swarmx[si] = -30000
                    self.swarmy[si] = -30000
            si = si + 1
        i = 0
        while i < self.nsnakes:
            self.snake_hit(i, x, y)
            i = i + 1
        # blast rides portals to an unoccupied exit
        if depth < 2:
            pi = 0
            while pi < len(self.porx):
                if self.porx[pi] == x:
                    if self.pory[pi] == y:
                        oi = self.portal_other(pi)
                        if oi >= 0:
                            if self.solid_on(self.porx[oi],
                                             self.pory[oi]) == 0:
                                self.blast_cell(self.porx[oi],
                                                self.pory[oi], depth + 1)
                pi = pi + 1

    def snake_hit(self, si: int, x: int, y: int) -> None:
        """Remove the part at (x,y); keep the run holding the head."""
        s = self.snake(si)
        if s.gone == 1:
            return
        hit = -1
        j = 0
        while j < s.npart():
            if s.xs[j] == x:
                if s.ys[j] == y:
                    hit = j
            j = j + 1
        if hit < 0:
            return
        if hit == 0:
            s.alive = 0
            s.gone = 1                      # head destroyed: snake is gone
            s.xs = []
            s.ys = []
            s.zs = []
            s.snap_visual()
            _log("[game] snake destroyed")
        else:
            while s.npart() > hit:          # drop the fragment behind the hit
                s.xs.pop()
                s.ys.pop()
                s.zs.pop()
            s.snap_visual()
            _log("[game] snake lost its tail")
        if si == self.active:
            self.auto_switch()

    # ------------------------------------------------------------- movement

    def try_push_at(self, x: int, y: int, dx: int, dy: int,
                    depth: int) -> int:
        """Push whatever occupies (x,y) one cell. 1 = cell was vacated."""
        if depth > 32:
            return 0
        nx = x + dx
        ny = y + dy
        bx = self.box_at(x, y)
        bb = self.bomb_at(x, y)
        if bx < 0:
            if bb < 0:
                si = self.snake_at(x, y)
                if si >= 0:
                    return self.try_push_snake(si, dx, dy)
                return 0
        # box/bomb chain
        if self.wall_at(nx, ny) == 1:
            if bb >= 0:
                self.explode(bb, 0)         # bomb against a wall blows up
            return 0
        if self.apple_at(nx, ny) == 1:
            return 0
        blocked = 0
        if self.box_at(nx, ny) >= 0:
            blocked = 1
        if self.bomb_at(nx, ny) >= 0:
            blocked = 1
        if self.snake_at(nx, ny) >= 0:
            blocked = 1
        if blocked == 1:
            if self.try_push_at(nx, ny, dx, dy, depth + 1) == 0:
                if bb >= 0:
                    self.explode(bb, 0)
                return 0
        if bx >= 0:
            self.boxx[bx] = nx
            self.boxy[bx] = ny
            self.move_box_connectables(bx, dx, dy)
            self.object_landed(bx, -1)
        else:
            self.bombx[bb] = nx
            self.bomby[bb] = ny
            self.object_landed(-1, bb)
        return 1

    def try_push_snake(self, si: int, dx: int, dy: int) -> int:
        """Translate the whole snake rigidly by one cell."""
        s = self.snake(si)
        j = 0
        while j < s.npart():
            tx = s.xs[j] + dx
            ty = s.ys[j] + dy
            if s.on_cell_any(tx, ty) == 0:
                if self.wall_at(tx, ty) == 1:
                    return 0
                if self.apple_at(tx, ty) == 1:
                    return 0
                if self.box_at(tx, ty) >= 0:
                    return 0
                if self.bomb_at(tx, ty) >= 0:
                    return 0
                oi = self.snake_at(tx, ty)
                if oi >= 0:
                    if oi != si:
                        return 0
            j = j + 1
        s.translate(dx, dy)
        _log("[game] snake pushed")
        return 1

    def ice_at(self, x: int, y: int) -> int:
        """1 if a propel zone currently covers (x, y)."""
        i = 0
        while i < len(self.connx):
            if self.conntype[i] == 1:
                if self.connx[i] == x:
                    if self.conny[i] == y:
                        return 1
            i = i + 1
        return 0

    def zone_at(self, x: int, y: int) -> int:
        """1 save / 2 load if a save/load zone covers (x, y), else 0."""
        i = 0
        while i < len(self.connx):
            t = self.conntype[i]
            if t >= 2:
                if self.connx[i] == x:
                    if self.conny[i] == y:
                        return t - 1
            i = i + 1
        return 0

    def move_box_connectables(self, bx: int, dx: int, dy: int) -> None:
        """Translate every Connectable anchored to box bx by (dx, dy).

        Mirrors Connectable.MoveWithConnectedToByDelta: portals and
        propel/save/load zones keep their offset from the box.
        """
        if dx == 0:
            if dy == 0:
                return
        i = 0
        n = len(self.porx)
        while i < n:
            if self.porbox[i] == bx:
                self.porx[i] = self.porx[i] + dx
                self.pory[i] = self.pory[i] + dy
            i = i + 1
        i = 0
        n = len(self.connx)
        while i < n:
            if self.connbox[i] == bx:
                self.connx[i] = self.connx[i] + dx
                self.conny[i] = self.conny[i] + dy
            i = i + 1

    def object_landed(self, bx: int, bb: int) -> None:
        """Pit interactions for a box/bomb that arrived on a new cell."""
        if bx >= 0:
            x = self.boxx[bx]
            y = self.boxy[bx]
            t = self.pit_open_at(x, y)
            i = self.idx(x, y)
            if t == 2:                       # deep: sink + fill
                self.filled[i] = 1
                self.fillbox[i] = bx
                self.boxz[bx] = 1
                _log("[game] box filled a pit")
            elif t == 3:                     # bottomless: swallowed
                self.boxlive[bx] = 0
                _log("[game] box lost to the void")
        if bb >= 0:
            x = self.bombx[bb]
            y = self.bomby[bb]
            t = self.pit_open_at(x, y)
            if t == 1:
                self.explode(bb, 0)
            elif t == 2:
                self.explode(bb, 0)
            elif t == 3:
                self.bomblive[bb] = 0

    def move_active(self, dx: int, dy: int) -> int:
        """The original Snake.Move: 1 if the move happened, 2 if it won."""
        s = self.snake(self.active)
        if s.alive == 0:
            return 0
        if s.swarmed == 1:
            return 0
        hx = s.head_x()
        hy = s.head_y()
        tx = hx + dx
        ty = hy + dy
        if self.apple_at(tx, ty) == 1:
            self.begin_win(dx, dy)
            return 2
        if self.wall_at(tx, ty) == 1:
            return 0
        if s.on_cell_any(tx, ty) == 1:
            return 0
        oi = self.snake_at(tx, ty)
        if oi >= 0:
            if self.try_push_snake(oi, dx, dy) == 0:
                return 0
        elif self.box_at(tx, ty) >= 0:
            if self.try_push_at(tx, ty, dx, dy, 0) == 0:
                return 0
        elif self.bomb_at(tx, ty) >= 0:
            if self.try_push_at(tx, ty, dx, dy, 0) == 0:
                return 0
        # body follow: shift each part into the one ahead of it
        j = s.npart() - 1
        while j > 0:
            s.xs[j] = s.xs[j - 1]
            s.ys[j] = s.ys[j - 1]
            j = j - 1
        s.xs[0] = tx
        s.ys[0] = ty
        s.fdx = dx
        s.fdy = dy
        return 1

    # -------------------------------------------------------------- portals

    def portal_other(self, pi: int) -> int:
        j = 0
        n = len(self.porx)
        while j < n:
            if j != pi:
                if self.porpair[j] == self.porpair[pi]:
                    return j
            j = j + 1
        return -1

    def portal_at(self, x: int, y: int) -> int:
        j = 0
        n = len(self.porx)
        while j < n:
            if self.porx[j] == x:
                if self.pory[j] == y:
                    return j
            j = j + 1
        return -1

    def resolve_portals(self) -> None:
        """Teleport entities that newly entered a portal cell. Mirrors the
        original Portal.HandleTeleports(): passes repeat while something
        teleports, so an arrival standing on another pair's portal chains
        onward in the same turn; a snake entering portals of two pairs at
        once exits from all of them and the extra exits are clones."""
        guard = 0
        while guard < 16:
            if self.portal_pass() == 0:
                break
            guard = guard + 1
        # refresh occupancy + locks
        n = len(self.porx)
        pi = 0
        while pi < n:
            self.porocc[pi] = self.solid_on(self.porx[pi], self.pory[pi])
            pi = pi + 1
        pi = 0
        while pi < len(self.pairlock):
            occ = 0
            j = 0
            while j < n:
                if self.porpair[j] == pi:
                    if self.porocc[j] == 1:
                        occ = 1
                j = j + 1
            if occ == 0:
                self.pairlock[pi] = 0
            pi = pi + 1

    def portal_eligible(self, pi: int) -> int:
        """Portal may fire: linked, freshly entered (cell was clear at the
        last resolution), and its pair is not locked."""
        if self.portal_other(pi) < 0:
            return 0
        if self.porocc[pi] == 1:
            return 0
        if self.pairlock[self.porpair[pi]] == 1:
            return 0
        return 1

    def portal_pass(self) -> int:
        """One HandleTeleport(): multi-portal snake exits (with clones)
        first, then the first single teleport. 1 when something moved."""
        blockmask = 0
        si = 0
        while si < self.nsnakes:
            r = self.try_multi_teleport(si)
            if r == 1:
                return 1
            if r == 2:
                # conjugate-pair entry that cannot clone: applying just one
                # delta would be wrong, so bar this snake from singles
                blockmask = blockmask | (1 << si)
            si = si + 1
        n = len(self.porx)
        pi = 0
        while pi < n:
            if self.portal_eligible(pi) == 1:
                oi = self.portal_other(pi)
                if self.try_teleport(pi, oi, self.porx[pi], self.pory[pi],
                                     blockmask) == 1:
                    return 1
            pi = pi + 1
        return 0

    def snake_fits(self, si: int, dx: int, dy: int) -> int:
        """1 if snake si translated by (dx,dy) lands on free cells."""
        s = self.snake(si)
        j = 0
        while j < s.npart():
            tx = s.xs[j] + dx
            ty = s.ys[j] + dy
            if self.wall_at(tx, ty) == 1:
                return 0
            if self.apple_at(tx, ty) == 1:
                return 0
            if self.box_at(tx, ty) >= 0:
                return 0
            if self.bomb_at(tx, ty) >= 0:
                return 0
            osnk = self.snake_at(tx, ty)
            if osnk >= 0:
                if osnk != si:
                    return 0
            j = j + 1
        return 1

    def images_collide(self, si: int, dxa: int, dya: int,
                       dxb: int, dyb: int) -> int:
        """1 when the two translated images of snake si share a cell."""
        s = self.snake(si)
        n = s.npart()
        j = 0
        while j < n:
            k = 0
            while k < n:
                if s.xs[j] + dxa == s.xs[k] + dxb:
                    if s.ys[j] + dya == s.ys[k] + dyb:
                        return 1
                k = k + 1
            j = j + 1
        return 0

    def clone_snake(self, si: int, dx: int, dy: int) -> int:
        """Duplicate snake si translated by (dx,dy); -1 when out of slots."""
        if self.nsnakes >= MAX_SNAKES:
            return -1
        src = self.snake(si)
        d = self.snake(self.nsnakes)
        d.gone = 0
        d.alive = src.alive
        d.swarmed = src.swarmed
        d.colr = src.colr
        d.colg = src.colg
        d.colb = src.colb
        d.fdx = src.fdx
        d.fdy = src.fdy
        d.xs = []
        d.ys = []
        d.zs = []
        d.vx = []
        d.vy = []
        j = 0
        while j < src.npart():
            d.xs.append(src.xs[j] + dx)
            d.ys.append(src.ys[j] + dy)
            d.zs.append(src.zs[j])
            j = j + 1
        d.snap_visual()
        self.nsnakes = self.nsnakes + 1
        return self.nsnakes - 1

    def try_multi_teleport(self, si: int) -> int:
        """Snake si standing on two or more eligible portals exits all of
        them at once: the first entry moves the snake, every extra entry
        spawns a clone (the original's TryMultiPortalSnakeTeleport).
        Returns 0 no-op, 1 teleported, 2 infeasible conjugate entry (the
        snake must not single-teleport this pass either)."""
        s = self.snake(si)
        if s.gone == 1:
            return 0
        if s.alive == 0:
            return 0
        self.entbuf = []
        n = len(self.porx)
        pi = 0
        while pi < n:
            if self.portal_eligible(pi) == 1:
                if self.snake_at(self.porx[pi], self.pory[pi]) == si:
                    self.entbuf.append(pi)
            pi = pi + 1
        if len(self.entbuf) < 2:
            return 0
        # feasibility: every exit image fits and no two images overlap
        conj = 0
        feasible = 1
        a = 0
        while a < len(self.entbuf):
            ea = self.entbuf[a]
            oa = self.portal_other(ea)
            dxa = self.porx[oa] - self.porx[ea]
            dya = self.pory[oa] - self.pory[ea]
            if self.snake_fits(si, dxa, dya) == 0:
                feasible = 0
            b = a + 1
            while b < len(self.entbuf):
                eb = self.entbuf[b]
                if self.porpair[eb] == self.porpair[ea]:
                    conj = 1
                ob = self.portal_other(eb)
                dxb = self.porx[ob] - self.porx[eb]
                dyb = self.pory[ob] - self.pory[eb]
                if self.images_collide(si, dxa, dya, dxb, dyb) == 1:
                    feasible = 0
                b = b + 1
            a = a + 1
        if feasible == 0:
            if conj == 1:
                return 2
            return 0
        # commit: clones copy the pre-teleport shape, so make them before
        # translating the original through the first entry
        k = 1
        while k < len(self.entbuf):
            ek = self.entbuf[k]
            ok = self.portal_other(ek)
            ci = self.clone_snake(si, self.porx[ok] - self.porx[ek],
                                  self.pory[ok] - self.pory[ek])
            if ci >= 0:
                self.pairlock[self.porpair[ek]] = 1
                _log("[game] snake cloned through portal")
            k = k + 1
        e0 = self.entbuf[0]
        o0 = self.portal_other(e0)
        s.translate(self.porx[o0] - self.porx[e0],
                    self.pory[o0] - self.pory[e0])
        s.snap_visual()
        self.pairlock[self.porpair[e0]] = 1
        _log("[game] teleported")
        return 1

    def try_teleport(self, pi: int, oi: int, px: int, py: int,
                     blockmask: int) -> int:
        dx = self.porx[oi] - px
        dy = self.pory[oi] - py
        si = self.snake_at(px, py)
        if si >= 0:
            if ((blockmask >> si) & 1) == 1:
                return 0
            if self.snake_fits(si, dx, dy) == 1:
                s = self.snake(si)
                s.translate(dx, dy)
                s.snap_visual()
                self.pairlock[self.porpair[pi]] = 1
                _log("[game] teleported")
                return 1
            self.mark_blocked(self.porx[oi], self.pory[oi])
            return 0
        bx = self.box_at(px, py)
        if bx >= 0:
            if self.cell_free(px + dx, py + dy, -1) == 1:
                self.boxx[bx] = px + dx
                self.boxy[bx] = py + dy
                self.move_box_connectables(bx, dx, dy)
                self.pairlock[self.porpair[pi]] = 1
                self.object_landed(bx, -1)
                _log("[game] box teleported")
                return 1
            self.mark_blocked(self.porx[oi], self.pory[oi])
            return 0
        bb = self.bomb_at(px, py)
        if bb >= 0:
            if self.cell_free(px + dx, py + dy, -1) == 1:
                self.bombx[bb] = px + dx
                self.bomby[bb] = py + dy
                self.pairlock[self.porpair[pi]] = 1
                self.object_landed(-1, bb)
                return 1
            self.mark_blocked(self.porx[oi], self.pory[oi])
        return 0

    def mark_blocked(self, x: int, y: int) -> None:
        # chained passes retry blocked portals; keep one marker per cell
        i = 0
        while i < len(self.blockx):
            if self.blockx[i] == x:
                if self.blocky[i] == y:
                    self.blockms[i] = engine.ms()
                    return
            i = i + 1
        self.blockx.append(x)
        self.blocky.append(y)
        self.blockms.append(engine.ms())

    # ------------------------------------------------------------ ice slides

    def snake_on_ice(self, si: int) -> int:
        s = self.snake(si)
        if s.gone == 1:
            return 0
        if s.alive == 0:
            return 0
        j = 0
        while j < s.npart():
            if self.ice_at(s.xs[j], s.ys[j]) == 1:
                return 1
            j = j + 1
        return 0

    def slide_snake(self, si: int, dx: int, dy: int) -> int:
        s = self.snake(si)
        j = 0
        while j < s.npart():
            tx = s.xs[j] + dx
            ty = s.ys[j] + dy
            if s.on_cell_any(tx, ty) == 0:
                if self.wall_at(tx, ty) == 1:
                    return 0
                if self.apple_at(tx, ty) == 1:
                    return 0
                if self.box_at(tx, ty) >= 0:
                    return 0
                if self.bomb_at(tx, ty) >= 0:
                    return 0
                oi = self.snake_at(tx, ty)
                if oi >= 0:
                    if oi != si:
                        return 0
            j = j + 1
        s.translate(dx, dy)
        return 1

    def resolve_slides(self, dx: int, dy: int) -> None:
        if dx == 0:
            if dy == 0:
                return
        if len(self.swarmx) + len(self.porx) + self.gw == 0:
            return
        steps = 0
        while steps < 2000:
            moved = 0
            # phase A: snakes (active first)
            order = 0
            while order < self.nsnakes:
                si = self.active + order
                if si >= self.nsnakes:
                    si = si - self.nsnakes
                if self.snake_on_ice(si) == 1:
                    if self.slide_snake(si, dx, dy) == 1:
                        moved = 1
                        self.resolve_portals()
                        self.check_pits()
                order = order + 1
            # phase B: boxes and bombs standing on ice
            bi = 0
            while bi < len(self.boxx):
                if self.boxlive[bi] == 1:
                    if self.boxz[bi] == 0:
                        if self.ice_at(self.boxx[bi], self.boxy[bi]) == 1:
                            nx = self.boxx[bi] + dx
                            ny = self.boxy[bi] + dy
                            if self.cell_free(nx, ny, -1) == 1:
                                self.boxx[bi] = nx
                                self.boxy[bi] = ny
                                self.move_box_connectables(bi, dx, dy)
                                self.object_landed(bi, -1)
                                moved = 1
                                self.resolve_portals()
                bi = bi + 1
            bi = 0
            while bi < len(self.bombx):
                if self.bomblive[bi] == 1:
                    if self.ice_at(self.bombx[bi], self.bomby[bi]) == 1:
                        nx = self.bombx[bi] + dx
                        ny = self.bomby[bi] + dy
                        if self.cell_free(nx, ny, -1) == 1:
                            self.bombx[bi] = nx
                            self.bomby[bi] = ny
                            self.object_landed(-1, bi)
                            moved = 1
                            self.resolve_portals()
                bi = bi + 1
            if moved == 0:
                return
            steps = steps + 1

    # ------------------------------------------------------------- pits etc.

    def check_pits(self) -> None:
        """A snake falls when EVERY surface part is over an unfilled pit."""
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            if s.gone == 0:
                if s.alive == 1:
                    n = s.npart()
                    over = 0
                    j = 0
                    while j < n:
                        if self.pit_open_at(s.xs[j], s.ys[j]) > 0:
                            over = over + 1
                        j = j + 1
                    if n > 0:
                        if over == n:
                            self.snake_falls(si)
            si = si + 1

    def snake_falls(self, si: int) -> None:
        s = self.snake(si)
        s.alive = 0
        allgone = 1
        j = 0
        while j < s.npart():
            t = self.pit_open_at(s.xs[j], s.ys[j])
            i = self.idx(s.xs[j], s.ys[j])
            if t == 1:
                # shallow: the corpse stays at the surface, solid terrain
                allgone = 0
            elif t == 2:                    # deep: fills the pit
                self.filled[i] = 1
                self.fillbox[i] = -1
                s.zs[j] = 2
            else:                           # bottomless
                s.zs[j] = 2
            j = j + 1
        if allgone == 1:
            s.gone = 1
        _log("[game] snake fell into a pit")
        if si == self.active:
            self.auto_switch()

    def update_swarms(self) -> None:
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            if s.gone == 0:
                sw = 0
                j = 0
                while j < s.npart():
                    g = 0
                    while g < len(self.swarmx):
                        if self.swarmx[g] == s.xs[j]:
                            if self.swarmy[g] == s.ys[j]:
                                sw = 1
                        g = g + 1
                    j = j + 1
                if sw == 1:
                    if s.swarmed == 0:
                        _log("[game] snake caught by the swarm")
                        s.swarmed = 1
                        if si == self.active:
                            self.auto_switch()
                else:
                    s.swarmed = 0
            si = si + 1

    def update_pads(self) -> None:
        i = 0
        n = len(self.padx)
        while i < n:
            now = self.solid_on(self.padx[i], self.pady[i])
            if now != self.padpressed[i]:
                self.padpressed[i] = now
                self.padpend[i] = self.padpend[i] + 1
            # apply deferred toggles when legal: every door that would
            # close (currently open) must have a clear cell; doors that
            # would open under an occupant are fine (Weightpad.cs
            # CanToggleAllDoors).
            while self.padpend[i] > 0:
                ok = 1
                j = 0
                while j < len(self.doorx):
                    if self.doorpadidx[j] == i:
                        if self.door_open(j) == 1:
                            if self.solid_on(self.doorx[j],
                                             self.doory[j]) == 1:
                                ok = 0
                    j = j + 1
                if ok == 0:
                    break
                if self.padopen[i] == 1:
                    self.padopen[i] = 0
                else:
                    self.padopen[i] = 1
                _log("[game] doors toggled")
                self.padpend[i] = self.padpend[i] - 1
            i = i + 1

    def update_traps_arming(self) -> None:
        n = self.gw * self.gh
        i = 0
        while i < n:
            if self.trapst[i] == 0:
                x = self.minx + i - (i // self.gw) * self.gw
                y = self.miny + i // self.gw
                if self.solid_on(x, y) == 1:
                    self.trapst[i] = 1
                    _log("[game] trapdoor armed")
            i = i + 1

    def update_traps_turn_end(self) -> None:
        n = self.gw * self.gh
        i = 0
        while i < n:
            if self.trapst[i] == 2:
                x = self.minx + i - (i // self.gw) * self.gw
                y = self.miny + i // self.gw
                self.trapst[i] = -1
                self.pit[i] = 3             # the hidden pit: bottomless
                _log("[game] trapdoor opened")
                bx = self.box_at(x, y)
                if bx >= 0:
                    self.object_landed(bx, -1)
                bb = self.bomb_at(x, y)
                if bb >= 0:
                    self.object_landed(-1, bb)
                self.check_pits()
            i = i + 1
        i = 0
        while i < n:
            if self.trapst[i] == 1:
                self.trapst[i] = 2
            i = i + 1

    def update_stars(self) -> None:
        i = 0
        while i < len(self.starx):
            if self.starcol[i] == 0:
                if self.solid_on(self.starx[i], self.stary[i]) == 1:
                    self.starcol[i] = 1
                    _log("[game] star collected")
            i = i + 1

    def auto_switch(self) -> None:
        i = 1
        while i <= self.nsnakes:
            c = self.active + i
            while c >= self.nsnakes:
                c = c - self.nsnakes
            s = self.snake(c)
            if s.gone == 0:
                if s.alive == 1:
                    if s.swarmed == 0:
                        self.active = c
                        return
            i = i + 1

    # ---------------------------------------------------------- the turn

    def do_move(self, dx: int, dy: int) -> None:
        if self.winning == 1:
            return
        self.ser()
        self._copybuf(0, 2)                 # snapbuf -> beforebuf
        self._copybuf(4, 3)                 # chkdata -> beforechk
        self.turn = self.turn + 1
        r = self.move_active(dx, dy)
        if r == 2:
            return                          # won: no history
        if r == 0:
            self.turn = self.turn - 1
            return                          # refused, nothing changed
        self.lastdx = dx
        self.lastdy = dy
        self.resolve_portals()
        self.check_pits()
        self.resolve_slides(dx, dy)
        self.resolve_portals()
        self.update_pads()
        # save zones win over load zones in the same turn
        saved = 0
        loaded = 0
        i = 0
        while i < len(self.connx):
            t = self.conntype[i]
            if t >= 2:
                if self.solid_on(self.connx[i], self.conny[i]) == 1:
                    if t == 2:
                        saved = 1
                    else:
                        loaded = 1
            i = i + 1
        if saved == 1:
            self.ser()
            self._copybuf(0, 4)             # snapbuf -> chkdata
            _log("[game] checkpoint saved")
        elif loaded == 1:
            if len(self.chkdata) > 0:       # no save yet: do nothing
                _log("[game] checkpoint loaded")
                self._copybuf(4, 1)         # chkdata -> applybuf
                self.apply_buf()
        self.update_traps_arming()
        self.update_swarms()
        self.check_pits()
        self.update_stars()
        self.update_traps_turn_end()
        self.update_pads()
        self.ser()                          # the after-state, in snapbuf
        if self._bufs_equal(2, 0) == 1:
            self.turn = self.turn - 1
            return
        self._hist_push(0, 2, 3)            # beforebuf+beforechk -> undo
        self.rhist = []
        self.rhistlen = []
        _log("[game] turn " + str(self.turn))

    def do_undo(self) -> None:
        if self.winning == 1:
            return
        if len(self.histlen) == 0:
            return
        self.ser()
        self._hist_push(1, 0, 4)            # current state -> redo
        self._hist_pop_apply(0)
        _log("[game] undo")

    def do_redo(self) -> None:
        if self.winning == 1:
            return
        if len(self.rhistlen) == 0:
            return
        self.ser()
        self._hist_push(0, 0, 4)            # current state -> undo
        self._hist_pop_apply(1)
        _log("[game] redo")

    def do_reset(self) -> None:
        if self.winning == 1:
            return
        self.ser()
        self._hist_push(0, 0, 4)
        self.rhist = []
        self.rhistlen = []
        self._copybuf(5, 1)                 # initdata -> applybuf
        self.apply_buf()
        self._buf_clear(4)                  # reset also clears the checkpoint
        _log("[game] reset")

    def do_switch(self) -> None:
        if self.winning == 1:
            return
        n = self.nsnakes
        i = 1
        while i <= n:
            c = self.active + i
            while c >= n:
                c = c - n
            s = self.snake(c)
            if s.gone == 0:
                if s.alive == 1:
                    if s.swarmed == 0:
                        if c != self.active:
                            self.active = c
                            _log("[game] switched snake")
                        return
            i = i + 1

    def begin_win(self, dx: int, dy: int) -> None:
        s = self.snake(self.active)
        # the head advances onto the apple; the body follows
        j = s.npart() - 1
        while j > 0:
            s.xs[j] = s.xs[j - 1]
            s.ys[j] = s.ys[j - 1]
            j = j - 1
        s.xs[0] = self.ax
        s.ys[0] = self.ay
        s.fdx = dx
        s.fdy = dy
        self.winning = 1
        self.grew = 0
        self.winms = engine.ms()
        self.won[self.levelidx] = 1
        star_ok = 1
        i = 0
        while i < len(self.starx):
            if self.starcol[i] == 0:
                star_ok = 0
            i = i + 1
        if len(self.starx) > 0:
            if star_ok == 1:
                self.stashed[self.levelidx] = 1
                _log("[game] star stashed")
        self.confx = []
        self.confy = []
        self.confc = []
        i = 0
        while i < 9:
            self.confx.append(((i * 7919 + 131) * 2654435761) & 65535)
            self.confy.append(((i * 104729 + 17) * 2246822519) & 65535)
            self.confc.append(i)
            i = i + 1
        _log("[game] won level " + str(self.levelidx + 1))

    def grow_tail(self) -> None:
        """The win growth: one part appended straight behind the tail."""
        s = self.snake(self.active)
        n = s.npart()
        if n >= 2:
            gx = s.xs[n - 1] * 2 - s.xs[n - 2]
            gy = s.ys[n - 1] * 2 - s.ys[n - 2]
        else:
            gx = s.xs[n - 1] - s.fdx
            gy = s.ys[n - 1] - s.fdy
        if self.cell_free(gx, gy, -1) == 0:
            gx = s.xs[n - 1]
            gy = s.ys[n - 1]
        s.xs.append(gx)
        s.ys.append(gy)
        s.zs.append(0)
        s.vx.append(s.xs[n - 1] * 256)
        s.vy.append(s.ys[n - 1] * 256)
        self.grew = 1

    # ------------------------------------------------------------ rendering

    def sx(self, cx: int) -> int:
        return self.ox + (cx - self.minx) * self.cell + \
            (self.minx * self.cell)

    def cell_px(self, cx: int) -> int:
        return self.ox + cx * self.cell

    def cell_py(self, cy: int) -> int:
        return self.oy - cy * self.cell

    def cspr(self, sid: int, px: int, py: int, wf: int, hf: int) -> None:
        """Draw a sprite centred in the cell at (px, py), sized wf x hf in
        1/256ths of a cell. Unity sizes each plain SpriteRenderer as
        texture / pixelsPerUnit * prefab scale, so most objects do not
        fill their whole cell."""
        cell = self.cell
        w = cell * wf // 256
        h = cell * hf // 256
        engine.sprite(sid, px + (cell - w) // 2, py + (cell - h) // 2, w, h)

    def cspr_ex(self, sid: int, px: int, py: int, wf: int, hf: int,
                tint: int, alpha: int, rot: int) -> None:
        cell = self.cell
        w = cell * wf // 256
        h = cell * hf // 256
        engine.sprite_ex(sid, px + (cell - w) // 2, py + (cell - h) // 2,
                         w, h, tint, alpha, rot)

    def draw_board(self, tms: int) -> None:
        # static layer from the scene cache, animated entities on top
        if self.dirty == 1:
            self.draw_board_static()
            engine.bake()
            self.dirty = 0
        else:
            engine.restore()
        self.draw_board_dynamic(tms)

    def draw_board_static(self) -> None:
        cell = self.cell
        sw = engine.width()
        sh = engine.height()
        engine.clear(0)
        # the letterboxed background art behind the play field
        vwpx = self.vw * cell
        vhpx = self.vh * cell
        vx0 = (sw - vwpx) // 2
        vy0 = (sh - vhpx) // 2
        engine.sprite(engine.SPR_BACKGROUND, vx0, vy0, vwpx, vhpx)
        # pits / walls / doors / trapdoors: Y-sorted like the original's
        # SetZOrders editor tool (sortingOrder = 200 - y), so southern
        # tiles paint in front of northern ones when soft edges meet. Unity
        # draws walls at size 1 x 1.0833334 (Wall.prefab), top-aligned to
        # their logical cell; the extra 1/12-cell skirt creates the overlap
        # that makes this ordering visible.
        wallh = cell * 13 // 12
        y = self.miny + self.gh - 1
        while y >= self.miny:
            x = self.minx
            while x < self.minx + self.gw:
                i = self.idx(x, y)
                px = self.cell_px(x)
                py = self.cell_py(y)
                if self.pit[i] > 0:
                    if self.filled[i] == 1:
                        engine.rect_a(px, py, cell, cell, 4139348, 120)
                        if self.fillbox[i] >= 0:
                            self.cspr_ex(engine.SPR_BOX, px, py, 179, 173,
                                         8421504, 256, 0)
                    elif self.pit[i] == 1:
                        engine.sprite(engine.SPR_SHALLOW, px, py, cell, cell)
                    elif self.pit[i] == 2:
                        engine.sprite(engine.SPR_PIT, px, py, cell, cell)
                    else:
                        engine.sprite(engine.SPR_BOTTOMLESS, px, py,
                                      cell, cell)
                w = self.wall[i]
                if w > 0:
                    sid = engine.SPR_WALL
                    if w == 2:
                        sid = engine.SPR_WALL2
                    if w == 3:
                        sid = engine.SPR_WALL3
                    if w == 4:
                        sid = engine.SPR_WALL4
                    if w == 5:
                        sid = engine.SPR_WEAKWALL
                    engine.sprite(sid, px, py, cell, wallh)
                dj = self.doorat[i]
                if dj >= 0:
                    a = 256
                    if self.door_open(dj) == 1:
                        a = 96
                    engine.sprite_ex(engine.SPR_DOOR, px, py, cell, cell,
                                     self.padcol[self.doorpadidx[dj]], a, 0)
                if self.trapst[i] >= 0:
                    engine.sprite(engine.SPR_TRAPDOOR, px, py, cell, cell)
                x = x + 1
            y = y - 1
        # weightpads (order 250), tinted like SpriteRenderer.color
        i = 0
        while i < len(self.padx):
            px = self.cell_px(self.padx[i])
            py = self.cell_py(self.pady[i])
            sid = engine.SPR_PAD
            if self.padpressed[i] == 1:
                sid = engine.SPR_PADDOWN
            # 300px art at 250 ppu, prefab scale 0.97 -> 1.164 cells
            self.cspr_ex(sid, px, py, 298, 298, self.padcol[i], 256, 0)
            i = i + 1
        # portal pair links (rotating portal sprites are dynamic, order 250)
        i = 0
        while i < len(self.porx):
            oi = self.portal_other(i)
            if oi > i:
                x0 = self.cell_px(self.porx[i]) + cell // 2
                y0 = self.cell_py(self.pory[i]) + cell // 2
                x1 = self.cell_px(self.porx[oi]) + cell // 2
                y1 = self.cell_py(self.pory[oi]) + cell // 2
                seg = 0
                while seg <= 24:
                    lx = x0 + (x1 - x0) * seg // 24
                    ly = y0 + (y1 - y0) * seg // 24
                    engine.rect_a(lx - 1, ly - 1, 3, 3, self.porcol[i], 64)
                    seg = seg + 1
            i = i + 1
        # grid overlay last in the static bake so it sits on walls/pads
        # (the original Show Grid setting, default on)
        if self.gridon == 1:
            gx = 0
            while gx <= self.vw:
                engine.rect_a(vx0 + gx * cell, vy0, 1, vhpx, 16777215, 40)
                gx = gx + 1
            gy = 0
            while gy <= self.vh:
                engine.rect_a(vx0, vy0 + gy * cell, vwpx, 1, 16777215, 40)
                gy = gy + 1

    def draw_board_dynamic(self, tms: int) -> None:
        cell = self.cell
        # Cell-relative sprite sizes (1/256ths) below mirror Unity's
        # texture / pixelsPerUnit * prefab scale for each object.
        # rotating portal sprites (order 250), prefab scale 0.9
        i = 0
        while i < len(self.porx):
            self.cspr_ex(engine.SPR_PORTAL, self.cell_px(self.porx[i]),
                         self.cell_py(self.pory[i]), 230, 230,
                         self.porcol[i], 256, (tms // 300) & 3)
            i = i + 1
        # apple (275) then boxes/bombs (300) -- above portals, below snakes.
        # Kept out of the static bake so rotating portals stay underneath.
        # End prefab: 236x260 art at 260 ppu, scale 0.53
        self.cspr(engine.SPR_APPLE, self.cell_px(self.ax),
                  self.cell_py(self.ay), 123, 136)
        i = 0
        while i < len(self.boxx):
            if self.boxlive[i] == 1:
                if self.boxz[i] == 0:
                    # Box prefab: 244x236 art at 244 ppu, scale 0.7
                    self.cspr(engine.SPR_BOX, self.cell_px(self.boxx[i]),
                              self.cell_py(self.boxy[i]), 179, 173)
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if self.bomblive[i] == 1:
                # Bomb prefab: 257x286 art at 286 ppu, scale 0.9
                self.cspr(engine.SPR_BOMB, self.cell_px(self.bombx[i]),
                          self.cell_py(self.bomby[i]), 207, 230)
            i = i + 1
        # snakes (order 300/302)
        si = 0
        while si < self.nsnakes:
            self.draw_snake(si, tms)
            si = si + 1
        # Bugs and the foreground renderer of propel zones are order 400 in
        # the Unity prefabs, so both belong in front of snakes.
        i = 0
        while i < len(self.connx):
            if self.conntype[i] == 1:
                # Propel Zone prefab: square art, scale 0.97
                self.cspr(engine.SPR_PROPEL, self.cell_px(self.connx[i]),
                          self.cell_py(self.conny[i]), 248, 248)
            i = i + 1
        i = 0
        while i < len(self.swarmx):
            if self.swarmx[i] > -30000:
                px = self.cell_px(self.swarmx[i])
                py = self.cell_py(self.swarmy[i])
                bw = cell * 38 // 256   # Bug prefab scale 0.15
                b = 0
                while b < 3:
                    ph = tms // 90 + b * 37 + i * 11
                    wx = (ph * 1103515245 + 12345) & 255
                    wy = ((ph + 7) * 214013 + 2531011) & 255
                    engine.sprite(engine.SPR_BUG,
                                  px + cell // 8 + wx * (cell // 2) // 256,
                                  py + cell // 8 + wy * (cell // 2) // 256,
                                  bw, bw)
                    b = b + 1
            i = i + 1
        # Save/load zones (order 400) and stars (order 500) also sit above
        # snakes in the original.
        i = 0
        while i < len(self.connx):
            t = self.conntype[i]
            if t == 2:
                # Save Icon: 340x393 at 393 ppu, zone scale 0.97
                self.cspr(engine.SPR_SAVE, self.cell_px(self.connx[i]),
                          self.cell_py(self.conny[i]), 215, 248)
            if t == 3:
                # Load Icon: 176x86 at 176 ppu, zone scale 0.97
                self.cspr(engine.SPR_LOAD, self.cell_px(self.connx[i]),
                          self.cell_py(self.conny[i]), 248, 121)
            i = i + 1
        i = 0
        while i < len(self.starx):
            if self.starcol[i] == 0:
                a = 256
                if self.stashed[self.levelidx] == 1:
                    a = 64                  # replay ghost, the original 1/4
                # Star prefab: 230x251 art at 251 ppu, scale 0.99
                self.cspr_ex(engine.SPR_STAR, self.cell_px(self.starx[i]),
                             self.cell_py(self.stary[i]), 232, 253,
                             16777215, a, 0)
            i = i + 1
        # blocked-teleport indicators (fade out over a second)
        i = 0
        while i < len(self.blockx):
            age = tms - self.blockms[i]
            if age < 1000:
                engine.sprite_ex(engine.SPR_BLOCKED,
                                 self.cell_px(self.blockx[i]),
                                 self.cell_py(self.blocky[i]), cell, cell,
                                 16777215, 256 - age * 256 // 1000, 0)
            i = i + 1
        self.draw_hud()

    def draw_snake(self, si: int, tms: int) -> None:
        s = self.snake(si)
        if s.gone == 1:
            return
        cell = self.cell
        n = s.npart()
        col = s.colr * 65536 + s.colg * 256 + s.colb
        if s.alive == 0:
            col = (s.colr // 2) * 65536 + (s.colg // 2) * 256 + s.colb // 2
        elif si == self.active:
            hr = s.colr + (255 - s.colr) // 4
            hg = s.colg + (255 - s.colg) // 4
            hb = s.colb + (255 - s.colb) // 4
            col = hr * 65536 + hg * 256 + hb
        inset = cell // 12
        rad = (cell * 38) // 100
        # bridges along the seams between consecutive parts: a few plain
        # rects between the two cell positions, so the caps keep their
        # rounded outer corners and corner turns fill in smoothly
        j = 1
        while j < n:
            if s.zs[j] <= s.zs[j - 1]:
                ax = self.ox + (s.vx[j - 1] * cell) // 256
                ay = self.oy - (s.vy[j - 1] * cell) // 256
                bx = self.ox + (s.vx[j] * cell) // 256
                by = self.oy - (s.vy[j] * cell) // 256
                k = 1
                while k < 4:
                    mx = ax + (bx - ax) * k // 4
                    my = ay + (by - ay) * k // 4
                    engine.rect(mx + inset, my + inset, cell - 2 * inset,
                                cell - 2 * inset, col)
                    k = k + 1
            j = j + 1
        # the parts themselves; head and tail get fully rounded caps
        j = n - 1
        while j >= 0:
            px = self.ox + (s.vx[j] * cell) // 256
            py = self.oy - (s.vy[j] * cell) // 256
            corners = 0
            if j == 0:
                corners = 15
            if j == n - 1:
                corners = 15
            if s.zs[j] < 2:
                dark = col
                if s.zs[j] == 1:
                    dark = (s.colr // 3) * 65536 + (s.colg // 3) * 256 + \
                        s.colb // 3
                engine.round_rect(px + inset, py + inset, cell - 2 * inset,
                                  cell - 2 * inset, rad, corners, dark)
            j = j - 1
        # face
        if n > 0:
            if s.zs[0] < 2:
                hx = self.ox + (s.vx[0] * cell) // 256
                hy = self.oy - (s.vy[0] * cell) // 256
                rot = 0
                if s.fdx == 1:
                    rot = 3
                elif s.fdx == -1:
                    rot = 1
                elif s.fdy == -1:
                    rot = 2
                if s.alive == 0:
                    engine.sprite_ex(engine.SPR_DEADEYES, hx + cell // 8,
                                     hy + cell // 8, cell * 3 // 4,
                                     cell * 3 // 4, 16777215, 256, rot)
                else:
                    # googly eyes: white balls astride the facing axis,
                    # pupils wandering (re-aim ~1 s), blinking every ~4 s
                    ecx = hx + cell // 2 + s.fdx * cell // 6
                    ecy = hy + cell // 2 - s.fdy * cell // 6
                    exo = s.fdy * cell // 5      # perpendicular offset
                    eyo = s.fdx * cell // 5
                    er = cell // 6
                    wob = (tms // 900 + si * 3) % 8
                    wx = (wob % 3) - 1
                    wy = (wob // 3) - 1
                    blink = 0
                    if (tms // 130 + si * 7) % 32 == 0:
                        blink = 1
                    if s.swarmed == 1:
                        blink = 1
                    e = 0
                    while e < 2:
                        sgn = 1
                        if e == 1:
                            sgn = -1
                        cxp = ecx + sgn * exo
                        cyp = ecy + sgn * eyo
                        if blink == 1:
                            engine.rect(cxp - er, cyp - er // 4, 2 * er,
                                        er // 2, 5335875)
                        else:
                            engine.circle(cxp, cyp, er, 16777215)
                            engine.circle(cxp + s.fdx * er // 2 + wx * er // 4,
                                          cyp - s.fdy * er // 2 + wy * er // 4,
                                          er // 2, 2236962)
                        e = e + 1

    def draw_hud(self) -> None:
        sh = engine.height()
        engine.text(24, 16, "LEVEL " + str(self.levelidx + 1), 16777215, 3)
        hint: "char*" = "Z UNDO   X REDO   R RESET"
        if self.nsnakes > 1:
            hint = hint + "   TAB SWITCH"
        hint = hint + "   G GRID   ESC MENU"
        engine.text(24, sh - 40, hint, 11184810, 2)

    def draw_win(self, tms: int) -> None:
        sw = engine.width()
        sh = engine.height()
        age = tms - self.winms
        cw = sw * 2 // 5
        chh = cw * engine.sprite_h(engine.SPR_CONGRATS) // \
            engine.sprite_w(engine.SPR_CONGRATS)
        engine.rect_a(0, 0, sw, sh, 0, 100)
        engine.sprite(engine.SPR_CONGRATS, (sw - cw) // 2, sh // 6, cw, chh)
        # nine confetti, drifting down
        i = 0
        while i < 9:
            fx = (self.confx[i] * sw) // 65536
            fy = (self.confy[i] * sh) // 65536 + age // 6
            while fy > sh:
                fy = fy - sh
            c = self.confc[i] % 3
            colr = 16729156
            if c == 1:
                colr = 4521796
            if c == 2:
                colr = 16773188
            engine.rect(fx, fy, 10, 16, colr)
            i = i + 1

    # ------------------------------------------------------------ animation

    def animate(self, dtms: int) -> None:
        step = (dtms * 256) // CRAWL_MS     # 1 cell per CRAWL_MS
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            j = 0
            n = s.npart()
            while j < n:
                if j < len(s.vx):
                    tx = s.xs[j] * 256
                    ty = s.ys[j] * 256
                    vx = s.vx[j]
                    vy = s.vy[j]
                    ddx = tx - vx
                    if ddx > 512:
                        vx = tx             # teleports snap
                    elif ddx < -512:
                        vx = tx
                    elif ddx > step:
                        vx = vx + step
                    elif ddx < 0 - step:
                        vx = vx - step
                    else:
                        vx = tx
                    ddy = ty - vy
                    if ddy > 512:
                        vy = ty
                    elif ddy < -512:
                        vy = ty
                    elif ddy > step:
                        vy = vy + step
                    elif ddy < 0 - step:
                        vy = vy - step
                    else:
                        vy = ty
                    s.vx[j] = vx
                    s.vy[j] = vy
                j = j + 1
            si = si + 1

    # ------------------------------------------------------------- screens

    def draw_title(self, tms: int) -> None:
        sw = engine.width()
        sh = engine.height()
        engine.clear(0)
        engine.sprite(engine.SPR_BACKGROUND, 0, 0, sw, sh)
        tw = sw * 2 // 5
        th = tw * engine.sprite_h(engine.SPR_TITLE) // \
            engine.sprite_w(engine.SPR_TITLE)
        engine.sprite(engine.SPR_TITLE, (sw - tw) // 2, sh // 8, tw, th)
        if (tms // 500) % 2 == 0:
            engine.text_centered(sw // 2, sh * 2 // 3,
                                 "PRESS ENTER", 16777215, 4)
        engine.text_centered(sw // 2, sh - 60,
                             "A BAREMETAL RPYTHON DEMAKE OF SNAKE-GAME",
                             9474192, 2)
        # a little snake swimming along the bottom
        t = tms // 16
        i = 0
        while i < 6:
            px = (t + 640 - i * 40) % (sw + 400) - 200
            py = sh * 4 // 5 + 0
            engine.round_rect(px, py, 36, 36, 14, 15, 65280)
            i = i + 1

    def draw_select(self, tms: int) -> None:
        sw = engine.width()
        sh = engine.height()
        engine.clear(0)
        engine.sprite(engine.SPR_BACKGROUND, 0, 0, sw, sh)
        engine.text_centered(sw // 2, 30, "SELECT LEVEL", 16777215, 5)
        cols = 8
        bw = 108
        gap = 30
        rows = (self.nlevels + cols - 1) // cols
        x0 = (sw - cols * bw - (cols - 1) * gap) // 2
        y0 = 140
        i = 0
        while i < self.nlevels:
            r = i // cols
            c = i - r * cols
            bx = x0 + c * (bw + gap)
            by = y0 + r * (bw + gap)
            sid = engine.SPR_LVLBTN
            if self.won[i] == 1:
                sid = engine.SPR_LVLWON
            grow = 0
            if i == self.cursor:
                grow = 10
            engine.sprite(sid, bx - grow, by - grow, bw + 2 * grow,
                          bw + 2 * grow)
            if i == self.cursor:
                engine.round_rect(bx - grow - 6, by - grow - 6, 3,
                                  bw + 2 * grow + 12, 1, 0, 16777215)
                engine.round_rect(bx + bw + grow + 3, by - grow - 6, 3,
                                  bw + 2 * grow + 12, 1, 0, 16777215)
                engine.round_rect(bx - grow - 6, by - grow - 6,
                                  bw + 2 * grow + 12, 3, 1, 0, 16777215)
                engine.round_rect(bx - grow - 6, by + bw + grow + 3,
                                  bw + 2 * grow + 12, 3, 1, 0, 16777215)
            engine.text_centered(bx + bw // 2, by + bw // 2 - 16,
                                 str(i + 1), 2236962, 3)
            if self.hasstar[i] == 1:
                ssid = engine.SPR_STAROFF
                if self.stashed[i] == 1:
                    ssid = engine.SPR_STARON
                engine.sprite(ssid, bx + bw - 34, by + bw - 34, 30, 30)
            i = i + 1
        engine.text_centered(sw // 2, sh - 50,
                             "ARROWS MOVE   ENTER PLAY   ESC BACK",
                             9474192, 2)

    # --------------------------------------------------------------- loop

    def handle_key_game(self, k: int) -> None:
        # any action can change the board; rebake the static layer
        self.dirty = 1
        if k == engine.K_ESC:
            self.screen = SCR_SELECT
            _log("[game] back to menu")
            return
        if self.winning == 1:
            return
        if k == engine.K_UP:
            self.do_move(0, 1)
        elif k == engine.K_DOWN:
            self.do_move(0, -1)
        elif k == engine.K_LEFT:
            self.do_move(-1, 0)
        elif k == engine.K_RIGHT:
            self.do_move(1, 0)
        elif k == engine.K_UNDO:
            self.do_undo()
        elif k == engine.K_REDO:
            self.do_redo()
        elif k == engine.K_RESET:
            self.do_reset()
        elif k == engine.K_SWITCH:
            self.do_switch()
        elif k == engine.K_GRID:
            self.gridon = 1 - self.gridon

    def run(self) -> None:
        self.boot_scan()
        _log("[game] title")
        self.lastms = engine.ms()
        while 1:
            tms = engine.ms()
            dt = tms - self.lastms
            self.lastms = tms
            if dt < 0:
                dt = 0
            if dt > 100:
                dt = 100
            self.fpsframes = self.fpsframes + 1
            if tms - self.fpsms >= 2000:
                if self.fpsms > 0:
                    _log("[game] fps " +
                         str(self.fpsframes * 1000 // (tms - self.fpsms)))
                self.fpsms = tms
                self.fpsframes = 0
            k = engine.poll_key()
            if self.screen == SCR_TITLE:
                if k == engine.K_ENTER:
                    self.screen = SCR_SELECT
                    _log("[game] level select")
                self.draw_title(tms)
            elif self.screen == SCR_SELECT:
                if k == engine.K_LEFT:
                    if self.cursor > 0:
                        self.cursor = self.cursor - 1
                elif k == engine.K_RIGHT:
                    if self.cursor < self.nlevels - 1:
                        self.cursor = self.cursor + 1
                elif k == engine.K_UP:
                    if self.cursor >= 8:
                        self.cursor = self.cursor - 8
                elif k == engine.K_DOWN:
                    if self.cursor + 8 < self.nlevels:
                        self.cursor = self.cursor + 8
                    elif self.cursor // 8 < (self.nlevels - 1) // 8:
                        self.cursor = self.nlevels - 1
                elif k == engine.K_ENTER:
                    self.load_level(self.cursor)
                    self.screen = SCR_GAME
                elif k == engine.K_ESC:
                    self.screen = SCR_TITLE
                self.draw_select(tms)
            else:
                if k > 0:
                    self.handle_key_game(k)
                self.animate(dt)
                self.draw_board(tms)
                if self.winning == 1:
                    age = tms - self.winms
                    if age > 600:
                        if self.grew == 0:
                            self.grow_tail()
                    self.draw_win(tms)
                    if age > WIN_HOLD_MS + GROW_MS:
                        self.winning = 0
                        self.screen = SCR_SELECT
                        _log("[game] back to level select")
            engine.present()


def snake_main() -> int:
    _log("[game] boot")
    g = Game()
    g.run()
    return 0
