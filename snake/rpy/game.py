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
MAX_SN_DRAW = 64                        # scratch capacity for Bezier snake draw
CRAWL_MS = 200          # 0.2 s per cell, the original moveSpeed=5
WIN_HOLD_MS = 2000      # congrats hold, the original endAnimDur=2
EXPLODE_MS = 350        # Unity Explode.anim: 9 frames
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
        self.hashead = 1                   # 0: original head part was blown off
        self.colr = 0
        self.colg = 255
        self.colb = 0
        self.fdx = 1
        self.fdy = 0
        # After a portal teleport: crawl visually to the entry cell first
        # (xs - wdx), then snap to the exit (xs).
        self.warp = 0
        self.wdx = 0
        self.wdy = 0
        # Propel anim: crawl to mid (post-move) pose first, then to final xs.
        self.mx: "list[int]" = []
        self.my: "list[int]" = []
        self.phase = 0                     # 0 → mid, 1 → final

    def npart(self) -> int:
        return len(self.xs)

    def head_x(self) -> int:
        return self.xs[0]

    def head_y(self) -> int:
        return self.ys[0]

    def capture_mid(self) -> None:
        """Snapshot logical pose (used as the end of the crawl, before propel)."""
        self.mx = []
        self.my = []
        j = 0
        n = len(self.xs)
        while j < n:
            self.mx.append(self.xs[j])
            self.my.append(self.ys[j])
            j = j + 1
        self.phase = 0

    def mid_is_final(self) -> int:
        """1 if mid pose already matches logical xs (no propel slide)."""
        n = len(self.xs)
        if len(self.mx) != n:
            return 0
        j = 0
        while j < n:
            if self.mx[j] != self.xs[j]:
                return 0
            if self.my[j] != self.ys[j]:
                return 0
            j = j + 1
        return 1

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
        self.warp = 0
        self.wdx = 0
        self.wdy = 0
        self.mx = []
        self.my = []
        self.phase = 0


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
        self.boxvx: "list[int]" = []       # visual pos, 1/256 cell
        self.boxvy: "list[int]" = []
        self.box_unload: "list[int]" = []  # 1: Unity Unloadable (load-zone trigger)
        self.box_warp: "list[int]" = []    # portal: crawl to entry before snap
        self.box_wdx: "list[int]" = []
        self.box_wdy: "list[int]" = []
        self.box_coast: "list[int]" = []   # 1: propelled; crawl without push wait
        # 1: snake/object already hit this box during the current propel; keep
        # coasting together on later slide cells without re-waiting for contact.
        self.box_coupled: "list[int]" = []

        self.bombx: "list[int]" = []
        self.bomby: "list[int]" = []
        self.bomblive: "list[int]" = []
        self.bombvx: "list[int]" = []
        self.bombvy: "list[int]" = []
        self.bomb_unload: "list[int]" = []
        self.bomb_warp: "list[int]" = []
        self.bomb_wdx: "list[int]" = []
        self.bomb_wdy: "list[int]" = []
        self.bomb_coast: "list[int]" = []
        self.bomb_coupled: "list[int]" = []
        # pit-triggered blasts wait for the move to finish (the original
        # queues in Pit/TryPush and flushes at the end of Snake.Move), so
        # the pusher ends up inside the 3x3 blast zone before it fires
        self.bombq: "list[int]" = []

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
        self.activems = 0                   # when active last changed (pulse phase)
        self.pushmask = 0                   # snakes currently in a push chain
        # turn-start propel-zone bitmasks (bit = index among type-1 zones).
        # Coast only when an entity overlaps a zone it did not already cover
        # at turn start (enter ice, or step from zone A onto zone B).
        self.icesnake: "list[int]" = []
        self.icebox: "list[int]" = []
        self.icebomb: "list[int]" = []
        self.snake_coast: "list[int]" = []  # 1: propelled; visual still catching up
        self.in_slides = 0                  # 1 during a slide tick commit
        self.slide_on = 0                   # 1: gradual propel in progress
        self.slide_dx = 0
        self.slide_dy = 0
        self.slide_sn: "list[int]" = []     # who still wants to coast
        self.slide_bx: "list[int]" = []
        self.slide_bm: "list[int]" = []
        self.turn_pending = 0               # finish hist/pits after slides
        self.box_land_pend: "list[int]" = []
        self.bomb_land_pend: "list[int]" = []
        self.pend_move = 0                  # queued arrow after propel
        self.pend_dx = 0
        self.pend_dy = 0

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
        self.explox: "list[int]" = []      # bomb explosion FX (cell coords)
        self.exploy: "list[int]" = []
        self.exploms: "list[int]" = []
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

        # Reusable snake-draw scratch (bump-arena has no free: never alloc
        # fresh list[int]s per frame).
        self.sn_cx: "list[int]" = []
        self.sn_cy: "list[int]" = []
        self.sn_cz: "list[int]" = []
        self.sn_tinx: "list[int]" = []
        self.sn_tiny: "list[int]" = []
        self.sn_toux: "list[int]" = []
        self.sn_touy: "list[int]" = []
        self.sn_corner: "list[int]" = []
        i = 0
        while i < MAX_SN_DRAW:
            self.sn_cx.append(0)
            self.sn_cy.append(0)
            self.sn_cz.append(0)
            self.sn_tinx.append(0)
            self.sn_tiny.append(0)
            self.sn_toux.append(0)
            self.sn_touy.append(0)
            self.sn_corner.append(0)
            i = i + 1

        # HUD / select labels cached so draw never calls str() per frame
        self.level_label: "char*" = "LEVEL 1"
        self.hud_hint: "char*" = \
            "Z UNDO   X REDO   R RESET   G GRID   ESC MENU"
        self.hud_hint_tab: "char*" = \
            "Z UNDO   X REDO   R RESET   TAB SWITCH   G GRID   ESC MENU"
        self.lvlnum: "list" = []

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

    def in_wall_or_door(self, x: int, y: int) -> int:
        """1 if the cell has a wall or a door (open or closed).

        Portals / save / load / propel stay hidden under them; open doors
        still cover the cell visually.
        """
        i = self.idx(x, y)
        if i < 0:
            return 1
        if self.wall[i] > 0:
            return 1
        if self.doorat[i] >= 0:
            return 1
        return 0

    def pit_open_at(self, x: int, y: int) -> int:
        """pit type 1..3 if an unfilled pit is there, else 0."""
        i = self.idx(x, y)
        if i < 0:
            return 0
        # Closed trapdoors cover their hidden pit (Unity: pitGo inactive
        # while the door is active). Without this, a stale pit[i] after
        # undo would swallow objects on the still-closed door.
        if self.trapst[i] >= 0:
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
            self.lvlnum.append(str(i + 1))
            d = levels.level_data(i)
            if d.find("\n* ") >= 0:
                self.hasstar.append(1)
            else:
                self.hasstar.append(0)
            i = i + 1

    def load_level(self, idx: int) -> None:
        self.levelidx = idx
        self.dirty = 1
        self.level_label = "LEVEL " + str(idx + 1)
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
        self.boxvx = []
        self.boxvy = []
        self.box_unload = []
        self.box_warp = []
        self.box_wdx = []
        self.box_wdy = []
        self.box_coast = []
        self.box_coupled = []
        self.box_land_pend = []
        self.bombx = []
        self.bomby = []
        self.bomblive = []
        self.bombvx = []
        self.bombvy = []
        self.bomb_unload = []
        self.bomb_warp = []
        self.bomb_wdx = []
        self.bomb_wdy = []
        self.bomb_coast = []
        self.bomb_coupled = []
        self.bomb_land_pend = []
        self.bombq = []
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
        self.activems = engine.ms()
        self.snake_coast = []
        self.in_slides = 0
        self.slide_on = 0
        self.slide_dx = 0
        self.slide_dy = 0
        self.slide_sn = []
        self.slide_bx = []
        self.slide_bm = []
        self.turn_pending = 0
        self.box_land_pend = []
        self.bomb_land_pend = []
        self.box_coupled = []
        self.bomb_coupled = []
        self.pend_move = 0
        self.pend_dx = 0
        self.pend_dy = 0
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
        self.explox = []
        self.exploy = []
        self.exploms = []
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
                self.boxvx.append(x * 256)
                self.boxvy.append(y * 256)
                u = 0
                if len(toks) >= 4:
                    u = int(toks[3])
                self.box_unload.append(u)
                self.box_warp.append(0)
                self.box_wdx.append(0)
                self.box_wdy.append(0)
                self.box_coast.append(0)
                self.box_coupled.append(0)
                self.box_land_pend.append(0)
            elif op2 == "M":
                self.bombx.append(x)
                self.bomby.append(y)
                self.bomblive.append(1)
                self.bombvx.append(x * 256)
                self.bombvy.append(y * 256)
                u = 0
                if len(toks) >= 4:
                    u = int(toks[3])
                self.bomb_unload.append(u)
                self.bomb_warp.append(0)
                self.bomb_wdx.append(0)
                self.bomb_wdy.append(0)
                self.bomb_coast.append(0)
                self.bomb_coupled.append(0)
                self.bomb_land_pend.append(0)
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
                    s.hashead = 1
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
                    self.snake_coast.append(0)
                    self.nsnakes = self.nsnakes + 1

        i = 0
        while i < npairs:
            self.pairlock.append(0)
            i = i + 1

        # screen geometry: fit the camera view into the window
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
            d.append(self.pit[i])           # trapdoors reveal bottomless pits
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
        i = 0
        n = len(self.swarmx)
        while i < n:
            d.append(self.swarmx[i])    # blasts remove swarms (-30000)
            d.append(self.swarmy[i])
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
            d.append(s.hashead)
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
        """Restore the dynamic state from applybuf (undo / redo / reset)."""
        self._apply_buf(0)

    def apply_checkpoint(self) -> None:
        """Restore applybuf as a load-zone checkpoint.

        Mirrors GameManager.LoadCheckpoint(skipUnloadable): Unloadable
        boxes/bombs keep their live pose; other objects whose checkpoint
        pose would overlap a live Unloadable (or another object already
        blocked from restore — cascade) also stay live; pits filled by an
        unrestored filler stay filled.
        """
        self._apply_buf(1)

    def _applybuf_box_off(self) -> int:
        """Index in applybuf where box records begin."""
        k = 2
        k = k + self.gw * self.gh * 5
        k = k + len(self.padx) * 3
        k = k + len(self.pairlock)
        k = k + len(self.porocc) * 3
        k = k + len(self.connx) * 2
        k = k + len(self.starx)
        return k

    def _unloadable_at(self, x: int, y: int) -> int:
        """1 if an Unloadable box/bomb (any depth / alive flag) is on (x,y)."""
        i = 0
        while i < len(self.boxx):
            if i < len(self.box_unload):
                if self.box_unload[i] == 1:
                    if self.boxx[i] == x:
                        if self.boxy[i] == y:
                            return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i < len(self.bomb_unload):
                if self.bomb_unload[i] == 1:
                    if self.bombx[i] == x:
                        if self.bomby[i] == y:
                            return 1
            i = i + 1
        return 0

    def _checkpoint_restore_blocked_at(
            self, x: int, y: int, skipb: "list[int]", skipm: "list[int]",
            skips: "list[int]") -> int:
        """1 if a checkpoint pose at (x,y) must stay live.

        Mirrors GameManager.CheckpointRestorePoseBlockedByLiveOccupant:
        Unloadables (incl. inactive/sunk) and live poses of objects already
        blocked from restore both occupy the cell.
        """
        if self._unloadable_at(x, y) == 1:
            return 1
        i = 0
        while i < len(self.boxx):
            if i < len(skipb):
                if skipb[i] == 1:
                    if self.boxx[i] == x:
                        if self.boxy[i] == y:
                            return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i < len(skipm):
                if skipm[i] == 1:
                    if self.bombx[i] == x:
                        if self.bomby[i] == y:
                            return 1
            i = i + 1
        si = 0
        while si < self.nsnakes:
            if si < len(skips):
                if skips[si] == 1:
                    s = self.snake(si)
                    j = 0
                    while j < len(s.xs):
                        if s.xs[j] == x:
                            if s.ys[j] == y:
                                return 1
                        j = j + 1
            si = si + 1
        return 0

    def _unloadable_sunk_at(self, x: int, y: int) -> int:
        """1 if an Unloadable at (x,y) is inactive or sunk (pit fill corpse)."""
        i = 0
        while i < len(self.boxx):
            if i < len(self.box_unload):
                if self.box_unload[i] == 1:
                    if self.boxx[i] == x:
                        if self.boxy[i] == y:
                            if self.boxlive[i] == 0:
                                return 1
                            if self.boxz[i] > 0:
                                return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i < len(self.bomb_unload):
                if self.bomb_unload[i] == 1:
                    if self.bombx[i] == x:
                        if self.bomby[i] == y:
                            if self.bomblive[i] == 0:
                                return 1
            i = i + 1
        return 0

    def _apply_buf(self, skip_unload: int) -> None:
        """Restore applybuf. skip_unload=1 is checkpoint-load semantics."""
        d = self.applybuf
        nbox = len(self.boxx)
        nbomb = len(self.bombx)
        ncell = self.gw * self.gh
        skipb: "list[int]" = []
        skipm: "list[int]" = []
        skips: "list[int]" = []
        live_filled: "list[int]" = []
        live_fillsbox: "list[int]" = []
        live_wall: "list[int]" = []
        live_connx: "list[int]" = []
        live_conny: "list[int]" = []
        live_porx: "list[int]" = []
        live_pory: "list[int]" = []
        i = 0
        while i < nbox:
            skipb.append(0)
            i = i + 1
        i = 0
        while i < nbomb:
            skipm.append(0)
            i = i + 1
        i = 0
        while i < ncell:
            live_filled.append(self.filled[i])
            live_fillsbox.append(self.fillsbox[i])
            live_wall.append(self.wall[i])
            i = i + 1
        i = 0
        while i < len(self.connx):
            live_connx.append(self.connx[i])
            live_conny.append(self.conny[i])
            i = i + 1
        i = 0
        while i < len(self.porx):
            live_porx.append(self.porx[i])
            live_pory.append(self.pory[i])
            i = i + 1
        old_ns = self.nsnakes
        si = 0
        while si < old_ns:
            skips.append(0)
            si = si + 1

        if skip_unload == 1:
            i = 0
            while i < nbox:
                if i < len(self.box_unload):
                    if self.box_unload[i] == 1:
                        skipb[i] = 1
                i = i + 1
            i = 0
            while i < nbomb:
                if i < len(self.bomb_unload):
                    if self.bomb_unload[i] == 1:
                        skipm[i] = 1
                i = i + 1
            # CollectCheckpointRestoreOverlapBlocks: grow until a pass adds
            # nothing. Blocked live poses then block further restores.
            k0 = self._applybuf_box_off()
            kb = k0 + nbox * 4
            ks_snakes = kb + nbomb * 3 + len(self.swarmx) * 2
            grew = 1
            while grew == 1:
                grew = 0
                i = 0
                while i < nbox:
                    if skipb[i] == 0:
                        cpx = d[k0 + i * 4]
                        cpy = d[k0 + i * 4 + 1]
                        cpl = d[k0 + i * 4 + 3]
                        if cpl == 1:
                            if self._checkpoint_restore_blocked_at(
                                    cpx, cpy, skipb, skipm, skips) == 1:
                                skipb[i] = 1
                                grew = 1
                    i = i + 1
                i = 0
                while i < nbomb:
                    if skipm[i] == 0:
                        cpx = d[kb + i * 3]
                        cpy = d[kb + i * 3 + 1]
                        cpl = d[kb + i * 3 + 2]
                        if cpl == 1:
                            if self._checkpoint_restore_blocked_at(
                                    cpx, cpy, skipb, skipm, skips) == 1:
                                skipm[i] = 1
                                grew = 1
                    i = i + 1
                ks = ks_snakes
                ns_peek = d[ks]
                ks = ks + 1
                si = 0
                while si < ns_peek:
                    while si >= len(skips):
                        skips.append(0)
                    np = d[ks + 9]
                    if skips[si] == 0:
                        hit = 0
                        j = 0
                        while j < np:
                            cpx = d[ks + 10 + j * 3]
                            cpy = d[ks + 10 + j * 3 + 1]
                            if self._checkpoint_restore_blocked_at(
                                    cpx, cpy, skipb, skipm, skips) == 1:
                                hit = 1
                            j = j + 1
                        if hit == 1:
                            skips[si] = 1
                            grew = 1
                    ks = ks + 10 + np * 3
                    si = si + 1

        k = 0
        self.turn = d[k]
        k = k + 1
        self.set_active(d[k])
        k = k + 1
        n = self.gw * self.gh
        i = 0
        while i < n:
            self.filled[i] = d[k]
            self.fillsbox[i] = d[k + 1]
            self.trapst[i] = d[k + 2]
            self.wall[i] = d[k + 3]
            self.pit[i] = d[k + 4]
            k = k + 5
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
        while i < nbox:
            if skip_unload == 1:
                if skipb[i] == 1:
                    k = k + 4
                    i = i + 1
                    continue
            self.boxx[i] = d[k]
            self.boxy[i] = d[k + 1]
            self.boxz[i] = d[k + 2]
            self.boxlive[i] = d[k + 3]
            k = k + 4
            i = i + 1
        i = 0
        while i < nbomb:
            if skip_unload == 1:
                if skipm[i] == 1:
                    k = k + 3
                    i = i + 1
                    continue
            self.bombx[i] = d[k]
            self.bomby[i] = d[k + 1]
            self.bomblive[i] = d[k + 2]
            k = k + 3
            i = i + 1
        self.snap_obj_visuals()
        if skip_unload == 1:
            # Zones/portals mounted on a skipped box stay with the live box.
            i = 0
            while i < len(self.connx):
                bi = self.connbox[i]
                if bi >= 0:
                    if bi < nbox:
                        if skipb[bi] == 1:
                            self.connx[i] = live_connx[i]
                            self.conny[i] = live_conny[i]
                i = i + 1
            i = 0
            while i < len(self.porx):
                bi = self.porbox[i]
                if bi >= 0:
                    if bi < nbox:
                        if skipb[bi] == 1:
                            self.porx[i] = live_porx[i]
                            self.pory[i] = live_pory[i]
                i = i + 1
        i = 0
        n = len(self.swarmx)
        while i < n:
            self.swarmx[i] = d[k]
            self.swarmy[i] = d[k + 1]
            k = k + 2
            i = i + 1
        ns = d[k]
        k = k + 1
        i = 0
        while i < ns:
            if skip_unload == 1:
                if i < len(skips):
                    if skips[i] == 1:
                        np = d[k + 9]
                        k = k + 10 + np * 3
                        i = i + 1
                        continue
            s = self.snake(i)
            s.alive = d[k]
            s.gone = d[k + 1]
            s.swarmed = d[k + 2]
            s.hashead = d[k + 3]
            s.fdx = d[k + 4]
            s.fdy = d[k + 5]
            s.colr = d[k + 6]
            s.colg = d[k + 7]
            s.colb = d[k + 8]
            np = d[k + 9]
            k = k + 10
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
        # Clones past the restored count vanish unless checkpoint-skipped.
        new_ns = ns
        i = ns
        while i < old_ns:
            keep = 0
            if skip_unload == 1:
                if i < len(skips):
                    if skips[i] == 1:
                        keep = 1
            if keep == 1:
                if i + 1 > new_ns:
                    new_ns = i + 1
            else:
                s = self.snake(i)
                s.gone = 1
                s.alive = 0
                s.xs = []
                s.ys = []
                s.zs = []
            i = i + 1
        self.nsnakes = new_ns
        if self.active >= self.nsnakes:
            self.set_active(0)
        self.bombq = []

        if skip_unload == 1:
            # Live pit fills whose filler was not restored stay filled.
            i = 0
            while i < ncell:
                if live_filled[i] == 1:
                    if self.filled[i] == 0:
                        keep = 0
                        cx = self.minx + (i % self.gw)
                        cy = self.miny + (i // self.gw)
                        if self._unloadable_sunk_at(cx, cy) == 1:
                            keep = 1
                        fb = live_fillsbox[i]
                        if fb >= 0:
                            if fb < nbox:
                                if skipb[fb] == 1:
                                    if self.boxx[fb] == cx:
                                        if self.boxy[fb] == cy:
                                            if self.boxlive[fb] == 0:
                                                keep = 1
                                            if self.boxz[fb] > 0:
                                                keep = 1
                        if keep == 1:
                            self.filled[i] = 1
                            self.fillsbox[i] = live_fillsbox[i]
                # Exploded weak walls stay gone if an unrestored box/bomb/
                # snake still occupies the cell (Unloadable or overlap-
                # blocked, including cascade).
                if live_wall[i] == 0:
                    if self.wall[i] == 5:
                        cx = self.minx + (i % self.gw)
                        cy = self.miny + (i // self.gw)
                        if self._unrestored_obj_at(cx, cy, skipb, skipm,
                                                   skips) == 1:
                            self.wall[i] = 0
                i = i + 1
            self.dirty = 1

    def _unrestored_obj_at(self, x: int, y: int, skipb: "list[int]",
                           skipm: "list[int]", skips: "list[int]") -> int:
        """1 if a skipped (Unloadable / overlap-blocked) box, bomb, or snake
        is on the surface at (x, y) after a checkpoint load."""
        i = 0
        while i < len(self.boxx):
            if i < len(skipb):
                if skipb[i] == 1:
                    if self.boxlive[i] == 1:
                        if self.boxz[i] == 0:
                            if self.boxx[i] == x:
                                if self.boxy[i] == y:
                                    return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i < len(skipm):
                if skipm[i] == 1:
                    if self.bomblive[i] == 1:
                        if self.bombx[i] == x:
                            if self.bomby[i] == y:
                                return 1
            i = i + 1
        si = 0
        while si < self.nsnakes:
            if si < len(skips):
                if skips[si] == 1:
                    s = self.snake(si)
                    if s.alive == 1:
                        if s.gone == 0:
                            j = 0
                            while j < len(s.xs):
                                if s.xs[j] == x:
                                    if s.ys[j] == y:
                                        if j < len(s.zs):
                                            if s.zs[j] == 0:
                                                return 1
                                        else:
                                            return 1
                                j = j + 1
            si = si + 1
        return 0

    # undo/redo entries are [corelen, core..., chklen, chk...] appended to a
    # flat int array; a parallel array holds each entry's total length. The
    # checkpoint rides inside every snapshot, like the original GameState's
    # nested checkpointState.

    def _bufs_equal(self, a: int, b: int) -> int:
        """Compare snapshots ignoring element 0 (the turn counter): it is
        bumped before the after-state is serialized, so including it would
        make refused moves look like real turns and pollute the undo
        history with duplicate states."""
        na = self._buf_len(a)
        nb = self._buf_len(b)
        if na != nb:
            return 0
        i = 1
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

    def queue_bomb(self, bb: int) -> None:
        """The original Bomb.QueueExplosion: pit-landing and wall-squeeze
        blasts are deferred to the end of the move, so pushers that follow
        into the blast zone are hit too."""
        if self.bomblive[bb] == 0:
            return
        i = 0
        while i < len(self.bombq):
            if self.bombq[i] == bb:
                return
            i = i + 1
        self.bombq.append(bb)

    def flush_bombs(self) -> None:
        """The original Bomb.FlushExplosionQueue."""
        while len(self.bombq) > 0:
            bb = self.bombq.pop()
            self.explode(bb, 0)

    def explode(self, bi: int, depth: int) -> None:
        if self.bomblive[bi] == 0:
            return
        cx = self.bombx[bi]
        cy = self.bomby[bi]
        self.bomblive[bi] = 0
        # Drop finished flashes so the FX lists stay short.
        i = 0
        now = engine.ms()
        while i < len(self.exploms):
            if now - self.exploms[i] >= EXPLODE_MS:
                self.explox[i] = self.explox[len(self.explox) - 1]
                self.exploy[i] = self.exploy[len(self.exploy) - 1]
                self.exploms[i] = self.exploms[len(self.exploms) - 1]
                self.explox.pop()
                self.exploy.pop()
                self.exploms.pop()
            else:
                i = i + 1
        # Unity Instantiates Explosion.prefab at the bomb (9-frame flash).
        self.explox.append(cx)
        self.exploy.append(cy)
        self.exploms.append(now)
        self.dirty = 1
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
        """Remove the part at (x,y). Like the original's
        DestroyPartsAndSplit: any partially destroyed snake dies (dead
        corpse, uncontrollable); the first surviving run stays this
        snake, a run behind the gap splits off as a dead fragment."""
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
        n = s.npart()
        if n == 1:
            s.alive = 0
            s.gone = 1                      # nothing survives
            s.xs = []
            s.ys = []
            s.zs = []
            s.snap_visual()
            _log("[game] snake destroyed")
            if si == self.active:
                self.auto_switch()
            return
        if hit == 0:
            # head destroyed: the body remains as a headless corpse
            k = 1
            while k < n:
                s.xs[k - 1] = s.xs[k]
                s.ys[k - 1] = s.ys[k]
                s.zs[k - 1] = s.zs[k]
                k = k + 1
            s.xs.pop()
            s.ys.pop()
            s.zs.pop()
            s.hashead = 0
        else:
            if hit + 1 < n:
                # a run survives behind the gap: dead headless fragment
                if self.nsnakes < MAX_SNAKES:
                    f = self.snake(self.nsnakes)
                    f.gone = 0
                    f.alive = 0
                    f.swarmed = 0
                    f.hashead = 0
                    f.colr = s.colr
                    f.colg = s.colg
                    f.colb = s.colb
                    f.fdx = s.fdx
                    f.fdy = s.fdy
                    f.xs = []
                    f.ys = []
                    f.zs = []
                    f.vx = []
                    f.vy = []
                    k = hit + 1
                    while k < n:
                        f.xs.append(s.xs[k])
                        f.ys.append(s.ys[k])
                        f.zs.append(s.zs[k])
                        k = k + 1
                    f.snap_visual()
                    self.nsnakes = self.nsnakes + 1
            while s.npart() > hit:
                s.xs.pop()
                s.ys.pop()
                s.zs.pop()
        s.alive = 0                         # partial destruction kills it
        s.snap_visual()
        _log("[game] snake lost a part and died")
        if si == self.active:
            self.auto_switch()

    # ------------------------------------------------------------- movement

    def _snake_in_push(self, si: int) -> int:
        """1 if snake si is already being rigidly pushed in this chain."""
        bit = 1 << si
        if (self.pushmask & bit) != 0:
            return 1
        return 0

    def try_push_at(self, x: int, y: int, dx: int, dy: int,
                    depth: int, do_push: int) -> int:
        """Push whatever occupies (x,y) one cell. 1 = cell was vacated.

        do_push=0 probes without mutating (and without queueing bombs),
        matching the original Snake.TryPush(push:false) feasibility pass.
        """
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
                    return self.try_push_snake(si, dx, dy, do_push)
                return 0
        # box/bomb chain
        if self.wall_at(nx, ny) == 1:
            if bb >= 0:
                if do_push == 1:
                    self.queue_bomb(bb)     # bomb against a wall blows up
            return 0
        if self.apple_at(nx, ny) == 1:
            return 0
        blocked = 0
        if self.box_at(nx, ny) >= 0:
            blocked = 1
        if self.bomb_at(nx, ny) >= 0:
            blocked = 1
        oi = self.snake_at(nx, ny)
        if oi >= 0:
            # A snake already moving in this push chain vacates its cell
            # (Unity sequential part Push). Do not treat it as a blocker.
            if self._snake_in_push(oi) == 0:
                blocked = 1
        if blocked == 1:
            if self.try_push_at(nx, ny, dx, dy, depth + 1, do_push) == 0:
                if bb >= 0:
                    if do_push == 1:
                        self.queue_bomb(bb)
                return 0
        if do_push == 0:
            return 1
        if bx >= 0:
            self.boxx[bx] = nx
            self.boxy[bx] = ny
            self.move_box_connectables(bx, dx, dy)
            if self.in_slides == 1:
                # Logical moves now; sprite waits for first contact unless
                # already coupled from an earlier hit this propel.
                self._queue_box_land(bx)
                if bx < len(self.box_coupled):
                    if self.box_coupled[bx] == 1:
                        self._mark_box_coast(bx)
            else:
                self.object_landed(bx, -1)
        else:
            self.bombx[bb] = nx
            self.bomby[bb] = ny
            if self.in_slides == 1:
                self._queue_bomb_land(bb)
                if bb < len(self.bomb_coupled):
                    if self.bomb_coupled[bb] == 1:
                        self._mark_bomb_coast(bb)
            else:
                self.object_landed(-1, bb)
        return 1

    def _try_push_snake_part(self, si: int, j: int, dx: int, dy: int,
                             do_push: int) -> int:
        """Resolve obstacles in front of one snake part (Unity TryPush on part).

        Any surface part can push boxes/bombs/snakes — not only the head.
        1 = that part can advance.
        """
        s = self.snake(si)
        if j >= s.npart():
            return 0
        if j < len(s.zs):
            if s.zs[j] != 0:
                return 1
        tx = s.xs[j] + dx
        ty = s.ys[j] + dy
        # Boxes/bombs first — any part can shove them (including into a cell
        # another of our parts is vacating this same rigid slide).
        if self.box_at(tx, ty) >= 0:
            return self.try_push_at(tx, ty, dx, dy, 0, do_push)
        if self.bomb_at(tx, ty) >= 0:
            return self.try_push_at(tx, ty, dx, dy, 0, do_push)
        # Own surface body ahead vacates simultaneously — clear.
        if s.occupies(tx, ty) == 1:
            return 1
        if self.wall_at(tx, ty) == 1:
            return 0
        if self.apple_at(tx, ty) == 1:
            return 0
        oi = self.snake_at(tx, ty)
        if oi >= 0:
            if oi == si:
                return 1
            if self._snake_in_push(oi) == 1:
                return 1
            return self.try_push_snake(oi, dx, dy, do_push)
        return 1

    def try_push_snake(self, si: int, dx: int, dy: int,
                       do_push: int) -> int:
        """Translate the whole snake rigidly by one cell.

        Like the original TryPushSnake: each active part runs TryPush so a
        tail/body can shove boxes and bombs, then the part advances.
        pushmask marks snakes in the current chain; re-entry is success
        (already moving), matching Unity's pushedSnakes set.
        """
        s = self.snake(si)
        if s.gone == 1:
            return 0
        bit = 1 << si
        if (self.pushmask & bit) != 0:
            return 1                        # already moving in this chain
        self.pushmask = self.pushmask | bit
        ok = 1
        # --- feasibility pass (no mutations) ---
        j = 0
        while j < s.npart():
            if self._try_push_snake_part(si, j, dx, dy, 0) == 0:
                ok = 0
                break
            j = j + 1
        if ok == 0:
            self.pushmask = self.pushmask ^ bit
            return 0
        if do_push == 0:
            self.pushmask = self.pushmask ^ bit
            return 1
        # --- commit: shove obstacles for every part, then slide together ---
        j = 0
        while j < s.npart():
            if self._try_push_snake_part(si, j, dx, dy, 1) == 0:
                self.pushmask = self.pushmask ^ bit
                return 0
            j = j + 1
        s.translate(dx, dy)
        self.pushmask = self.pushmask ^ bit
        if self.in_slides == 1:
            self._mark_snake_coast(si)
        _log("[game] snake pushed")
        return 1

    def _mark_box_coast(self, bi: int) -> None:
        while len(self.box_coast) <= bi:
            self.box_coast.append(0)
        self.box_coast[bi] = 1

    def _mark_bomb_coast(self, bi: int) -> None:
        while len(self.bomb_coast) <= bi:
            self.bomb_coast.append(0)
        self.bomb_coast[bi] = 1

    def _mark_box_coupled(self, bi: int) -> None:
        while len(self.box_coupled) <= bi:
            self.box_coupled.append(0)
        self.box_coupled[bi] = 1
        self._mark_box_coast(bi)

    def _mark_bomb_coupled(self, bi: int) -> None:
        while len(self.bomb_coupled) <= bi:
            self.bomb_coupled.append(0)
        self.bomb_coupled[bi] = 1
        self._mark_bomb_coast(bi)

    def _clear_push_couples(self) -> None:
        i = 0
        while i < len(self.box_coupled):
            self.box_coupled[i] = 0
            i = i + 1
        i = 0
        while i < len(self.bomb_coupled):
            self.bomb_coupled[i] = 0
            i = i + 1

    def _mark_snake_coast(self, si: int) -> None:
        while len(self.snake_coast) <= si:
            self.snake_coast.append(0)
        self.snake_coast[si] = 1

    def _queue_box_land(self, bi: int) -> None:
        while len(self.box_land_pend) <= bi:
            self.box_land_pend.append(0)
        self.box_land_pend[bi] = 1

    def _queue_bomb_land(self, bi: int) -> None:
        while len(self.bomb_land_pend) <= bi:
            self.bomb_land_pend.append(0)
        self.bomb_land_pend[bi] = 1

    def _flush_box_land(self, bi: int) -> None:
        if bi < 0:
            return
        if bi >= len(self.box_land_pend):
            return
        if self.box_land_pend[bi] == 1:
            self.box_land_pend[bi] = 0
            self.object_landed(bi, -1)
            self.dirty = 1

    def _flush_bomb_land(self, bi: int) -> None:
        if bi < 0:
            return
        if bi >= len(self.bomb_land_pend):
            return
        if self.bomb_land_pend[bi] == 1:
            self.bomb_land_pend[bi] = 0
            self.object_landed(-1, bi)
            self.dirty = 1

    def ice_zone_depth(self, ci: int) -> int:
        """Depth of propel zone ci: follows its connected box into pits.

        Like Unity PropelZoneAt same-z matching: a zone on a fallen box
        (boxz>0) only rides entities at that depth. Dead carriers yield -1
        (zone is not rideable).
        """
        bi = self.connbox[ci]
        if bi < 0:
            return 0
        if self.boxlive[bi] == 0:
            return -1
        return self.boxz[bi]

    def ice_at(self, x: int, y: int, z: int) -> int:
        """1 if a propel zone at depth z covers (x, y)."""
        i = 0
        while i < len(self.connx):
            if self.conntype[i] == 1:
                if self.connx[i] == x:
                    if self.conny[i] == y:
                        d = self.ice_zone_depth(i)
                        if d == z:
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
                self.queue_bomb(bb)
            elif t == 2:
                self.queue_bomb(bb)
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
            if self.try_push_snake(oi, dx, dy, 1) == 0:
                return 0
        elif self.box_at(tx, ty) >= 0:
            if self.try_push_at(tx, ty, dx, dy, 0, 1) == 0:
                return 0
        elif self.bomb_at(tx, ty) >= 0:
            if self.try_push_at(tx, ty, dx, dy, 0, 1) == 0:
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

    def _refresh_pairlocks(self) -> None:
        """Clear pair-lock when both ends are free of surface solids.

        Uses live solid_on (not stale porocc). Call before teleport attempts
        so a box that left an exit earlier this turn does not keep the pair
        locked and falsely refuse a later push into the entry.
        """
        n = len(self.porx)
        pi = 0
        while pi < len(self.pairlock):
            occ = 0
            j = 0
            while j < n:
                if self.porpair[j] == pi:
                    if self.solid_on(self.porx[j], self.pory[j]) == 1:
                        occ = 1
                j = j + 1
            if occ == 0:
                self.pairlock[pi] = 0
            pi = pi + 1

    def resolve_portals(self) -> None:
        """Teleport entities that newly entered a portal cell. Mirrors the
        original Portal.HandleTeleports(): passes repeat while something
        teleports, so an arrival standing on another pair's portal chains
        onward in the same turn; a snake entering portals of two pairs at
        once exits from all of them and the extra exits are clones."""
        # Drop locks vacated during move_active before any attempt. Do not
        # refresh porocc here — that snapshot is "occupied last resolve" for
        # fresh-entry detection.
        self._refresh_pairlocks()
        guard = 0
        while guard < 16:
            if self.portal_pass() == 0:
                break
            guard = guard + 1
        # refresh occupancy + locks for the next turn's eligibility
        n = len(self.porx)
        pi = 0
        while pi < n:
            self.porocc[pi] = self.solid_on(self.porx[pi], self.pory[pi])
            pi = pi + 1
        self._refresh_pairlocks()

    def portal_eligible(self, pi: int) -> int:
        """Portal may fire: linked and freshly entered (cell was clear at
        the last resolution).

        Pair-lock is checked in try_teleport after destination blocking so
        an occupied exit still gets blocked-teleport indicators (Unity marks
        OtherPortal occupancy before teleport-lock refusal).
        """
        if self.portal_other(pi) < 0:
            return 0
        if self.porocc[pi] == 1:
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

    def teleport_occupancy_at(self, x: int, y: int,
                              ignore_snake: int) -> int:
        """1 if a box, bomb, or foreign snake occupies (x, y).

        Used for Unity OtherPortalForeignOccupancyBlocksTeleport: the exit
        portal gets an indicator when an entity blocks that cell (walls and
        apples are handled only via destination-footprint markers).
        """
        if self.bomb_at(x, y) >= 0:
            return 1
        if self.box_at(x, y) >= 0:
            return 1
        # Mid-fall pit fillers: Unity keeps their colliders until Fill
        # finishes. Once fill art is ready the filler is inactive — a portal
        # on that filled pit must accept teleports with no indicators.
        ci = self.idx(x, y)
        if ci >= 0:
            if self.filled[ci] == 1:
                if self._fill_visually_ready(ci) == 0:
                    return 1
        osnk = self.snake_at(x, y)
        if osnk >= 0:
            if osnk != ignore_snake:
                return 1
        return 0

    def teleport_dest_blocked_at(self, x: int, y: int,
                                 ignore_snake: int) -> int:
        """1 if a teleport cannot land a part on (x, y).

        Matches Unity DestinationHasBlockingSolidOverlap during pit fills:
        logical fill sets boxz/gone immediately, but Unity keeps the filler
        collider active until the fall finishes — so a portal over a pit
        that is still being filled must block and show indicators.
        """
        if self.wall_at(x, y) == 1:
            return 1
        if self.apple_at(x, y) == 1:
            return 1
        return self.teleport_occupancy_at(x, y, ignore_snake)

    def portal_dest_depth_blocks(self, oi: int) -> int:
        """1 when the exit portal cannot receive teleports.

        Unity DestinationPortalZProhibitsIncomingTeleport: a portal whose
        carrier has finished sinking into a filled pit sits above floor
        depth 0 and rejects incoming teleports.
        """
        bi = self.porbox[oi]
        if bi < 0:
            return 0
        return self.conn_carrier_hidden(bi)

    def snake_fits(self, si: int, dx: int, dy: int) -> int:
        """1 if snake si translated by (dx,dy) lands on free cells."""
        s = self.snake(si)
        j = 0
        while j < s.npart():
            if self.teleport_dest_blocked_at(s.xs[j] + dx, s.ys[j] + dy,
                                            si) == 1:
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
        d.hashead = src.hashead
        d.colr = src.colr
        d.colg = src.colg
        d.colb = src.colb
        d.fdx = src.fdx
        d.fdy = src.fdy
        d.warp = 0
        d.wdx = 0
        d.wdy = 0
        d.mx = []
        d.my = []
        d.phase = 0
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
        self.snake_coast.append(0)
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
            if self.portal_dest_depth_blocks(oa) == 1:
                # Unity DestinationPortalZProhibits: refuse without markers.
                feasible = 0
            elif self.snake_fits(si, dxa, dya) == 0:
                feasible = 0
                self.mark_failed_teleport(si, oa, dxa, dya)
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
        # Refuse locked pairs (after marking path above for blocked exits).
        a = 0
        while a < len(self.entbuf):
            ea = self.entbuf[a]
            if self.pairlock[self.porpair[ea]] == 1:
                # Unity TeleportLock: silent refuse.
                if conj == 1:
                    return 2
                return 0
            a = a + 1
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
        dx = self.porx[o0] - self.porx[e0]
        dy = self.pory[o0] - self.pory[e0]
        s.translate(dx, dy)
        s.warp = 1
        s.wdx = dx
        s.wdy = dy
        self.pairlock[self.porpair[e0]] = 1
        _log("[game] teleported")
        return 1

    def mark_blocked(self, x: int, y: int) -> None:
        # chained passes retry blocked portals; keep one marker per cell
        i = 0
        while i < len(self.blockx):
            if self.blockx[i] == x:
                if self.blocky[i] == y:
                    self.blockms[i] = engine.ms()
                    self.dirty = 1
                    return
            i = i + 1
        self.blockx.append(x)
        self.blocky.append(y)
        self.blockms.append(engine.ms())
        self.dirty = 1

    def mark_failed_teleport(self, si: int, oi: int,
                             dx: int, dy: int) -> None:
        """Spawn blocked-teleport indicators for a failed wormhole attempt.

        Unity pairs two cues:
        - OtherPortalForeignOccupancy → indicator on the exit portal when an
          entity (box/bomb/snake) blocks that cell
        - DestinationHasBlockingSolidOverlap → indicator on every destination
          footprint cell that actually collides (walls, entities, …)

        So the exit portal is marked when occupied; other colliding cells are
        marked additionally, or alone when the exit itself is free.
        """
        ex = self.porx[oi]
        ey = self.pory[oi]
        if self.teleport_occupancy_at(ex, ey, si) == 1:
            self.mark_blocked(ex, ey)
        if si >= 0:
            s = self.snake(si)
            j = 0
            while j < s.npart():
                tx = s.xs[j] + dx
                ty = s.ys[j] + dy
                if self.teleport_dest_blocked_at(tx, ty, si) == 1:
                    self.mark_blocked(tx, ty)
                j = j + 1
        else:
            # Box/bomb: mark the exit only when that cell actually blocks
            # (wall / apple / occupant). A clear exit — including a portal on
            # a finished filled pit — must not get a marker.
            if self.teleport_dest_blocked_at(ex, ey, -1) == 1:
                self.mark_blocked(ex, ey)

    def try_teleport(self, pi: int, oi: int, px: int, py: int,
                     blockmask: int) -> int:
        dx = self.porx[oi] - px
        dy = self.pory[oi] - py
        if self.portal_dest_depth_blocks(oi) == 1:
            # Unity DestinationPortalZProhibitsIncomingTeleport: refuse
            # without blocked-teleport indicators.
            return 0
        locked = self.pairlock[self.porpair[pi]]
        si = self.snake_at(px, py)
        if si >= 0:
            if ((blockmask >> si) & 1) == 1:
                return 0
            if self.snake_fits(si, dx, dy) == 0:
                self.mark_failed_teleport(si, oi, dx, dy)
                return 0
            if locked == 1:
                # Unity TeleportLock: refuse without new markers when the
                # destination footprint is already clear.
                return 0
            s = self.snake(si)
            s.translate(dx, dy)
            # Crawl into the entry portal first; snap to exit after.
            s.warp = 1
            s.wdx = dx
            s.wdy = dy
            self.pairlock[self.porpair[pi]] = 1
            _log("[game] teleported")
            return 1
        bx = self.box_at(px, py)
        if bx >= 0:
            if self.teleport_dest_blocked_at(px + dx, py + dy, -1) == 1:
                self.mark_failed_teleport(-1, oi, dx, dy)
                return 0
            if locked == 1:
                return 0
            self.boxx[bx] = px + dx
            self.boxy[bx] = py + dy
            self.move_box_connectables(bx, dx, dy)
            self.box_warp[bx] = 1
            self.box_wdx[bx] = dx
            self.box_wdy[bx] = dy
            self.pairlock[self.porpair[pi]] = 1
            self.object_landed(bx, -1)
            _log("[game] box teleported")
            return 1
        bb = self.bomb_at(px, py)
        if bb >= 0:
            if self.teleport_dest_blocked_at(px + dx, py + dy, -1) == 1:
                self.mark_failed_teleport(-1, oi, dx, dy)
                return 0
            if locked == 1:
                return 0
            self.bombx[bb] = px + dx
            self.bomby[bb] = py + dy
            self.bomb_warp[bb] = 1
            self.bomb_wdx[bb] = dx
            self.bomb_wdy[bb] = dy
            self.pairlock[self.porpair[pi]] = 1
            self.object_landed(-1, bb)
            return 1
        return 0

    # ------------------------------------------------------------ ice slides

    def snake_on_ice(self, si: int) -> int:
        s = self.snake(si)
        if s.gone == 1:
            return 0
        if s.alive == 0:
            return 0
        j = 0
        while j < s.npart():
            if self.ice_at(s.xs[j], s.ys[j], s.zs[j]) == 1:
                return 1
            j = j + 1
        return 0

    def propel_bit_at(self, x: int, y: int, z: int) -> int:
        """Bit index among same-depth propel zones covering (x,y), or -1."""
        bit = 0
        i = 0
        while i < len(self.connx):
            if self.conntype[i] == 1:
                if self.connx[i] == x:
                    if self.conny[i] == y:
                        d = self.ice_zone_depth(i)
                        if d == z:
                            return bit
                bit = bit + 1
            i = i + 1
        return -1

    def snake_ice_mask(self, si: int) -> int:
        """Bitmask of same-depth propel zones under any part of snake si."""
        s = self.snake(si)
        if s.gone == 1:
            return 0
        mask = 0
        j = 0
        while j < s.npart():
            b = self.propel_bit_at(s.xs[j], s.ys[j], s.zs[j])
            if b >= 0:
                mask = mask | (1 << b)
            j = j + 1
        return mask

    def capture_ice_start(self) -> None:
        """Snapshot which propel zones each entity already covers.

        Like the original EnteredNewPropelZone: coast when overlapping a
        zone that was not under the entity at turn start. Staying on the
        same zone does not coast; stepping onto a different zone does.
        """
        self.icesnake = []
        i = 0
        while i < self.nsnakes:
            self.icesnake.append(self.snake_ice_mask(i))
            i = i + 1
        self.icebox = []
        i = 0
        while i < len(self.boxx):
            mask = 0
            if self.boxlive[i] == 1:
                b = self.propel_bit_at(self.boxx[i], self.boxy[i],
                                       self.boxz[i])
                if b >= 0:
                    mask = 1 << b
            self.icebox.append(mask)
            i = i + 1
        self.icebomb = []
        i = 0
        while i < len(self.bombx):
            mask = 0
            if self.bomblive[i] == 1:
                b = self.propel_bit_at(self.bombx[i], self.bomby[i], 0)
                if b >= 0:
                    mask = 1 << b
            self.icebomb.append(mask)
            i = i + 1

    def ice_start_snake(self, si: int) -> int:
        if si < 0:
            return 0
        if si >= len(self.icesnake):
            return 0
        return self.icesnake[si]

    def ice_start_box(self, bi: int) -> int:
        if bi < 0:
            return 0
        if bi >= len(self.icebox):
            return 0
        return self.icebox[bi]

    def ice_start_bomb(self, bi: int) -> int:
        if bi < 0:
            return 0
        if bi >= len(self.icebomb):
            return 0
        return self.icebomb[bi]

    def entered_ice_snake(self, si: int) -> int:
        """1 if the snake overlaps any propel zone it did not at turn start."""
        s = self.snake(si)
        if s.gone == 1:
            return 0
        if s.alive == 0:
            return 0
        start = self.ice_start_snake(si)
        j = 0
        while j < s.npart():
            b = self.propel_bit_at(s.xs[j], s.ys[j], s.zs[j])
            if b >= 0:
                if ((start >> b) & 1) == 0:
                    return 1
            j = j + 1
        return 0

    def entered_ice_box(self, bi: int) -> int:
        if self.boxlive[bi] == 0:
            return 0
        b = self.propel_bit_at(self.boxx[bi], self.boxy[bi], self.boxz[bi])
        if b < 0:
            return 0
        if ((self.ice_start_box(bi) >> b) & 1) == 0:
            return 1
        return 0

    def entered_ice_bomb(self, bi: int) -> int:
        if self.bomblive[bi] == 0:
            return 0
        b = self.propel_bit_at(self.bombx[bi], self.bomby[bi], 0)
        if b < 0:
            return 0
        if ((self.ice_start_bomb(bi) >> b) & 1) == 0:
            return 1
        return 0

    def slide_snake(self, si: int, dx: int, dy: int) -> int:
        """One ice step for a snake: push through anything it can shove."""
        return self.try_push_snake(si, dx, dy, 1)

    def slide_object(self, bx: int, bb: int, dx: int, dy: int) -> int:
        """One ice step for a box (bx>=0) or bomb (bb>=0). Like the
        original SlideStepObject: shove whatever is ahead if possible."""
        if bx >= 0:
            x = self.boxx[bx]
            y = self.boxy[bx]
        else:
            x = self.bombx[bb]
            y = self.bomby[bb]
        nx = x + dx
        ny = y + dy
        if self.wall_at(nx, ny) == 1:
            if bb >= 0:
                self.queue_bomb(bb)
            return 0
        if self.apple_at(nx, ny) == 1:
            return 0
        if self.box_at(nx, ny) >= 0:
            if self.try_push_at(nx, ny, dx, dy, 0, 1) == 0:
                return 0
        elif self.bomb_at(nx, ny) >= 0:
            if self.try_push_at(nx, ny, dx, dy, 0, 1) == 0:
                return 0
        else:
            oi = self.snake_at(nx, ny)
            if oi >= 0:
                if self.try_push_snake(oi, dx, dy, 1) == 0:
                    return 0
        if bx >= 0:
            self.boxx[bx] = nx
            self.boxy[bx] = ny
            self.move_box_connectables(bx, dx, dy)
            self._mark_box_coast(bx)
            self._queue_box_land(bx)
        else:
            self.bombx[bb] = nx
            self.bomby[bb] = ny
            self._mark_bomb_coast(bb)
            self._queue_bomb_land(bb)
        return 1

    def begin_slides(self, dx: int, dy: int) -> int:
        """Start gradual propel; 1 if anyone will coast this turn."""
        if dx == 0:
            if dy == 0:
                return 0
        self.slide_sn = []
        self.slide_bx = []
        self.slide_bm = []
        anyc = 0
        i = 0
        while i < self.nsnakes:
            c = self.entered_ice_snake(i)
            self.slide_sn.append(c)
            if c == 1:
                anyc = 1
                self._mark_snake_coast(i)
                s = self.snake(i)
                s.phase = 0
            i = i + 1
        i = 0
        while i < len(self.boxx):
            c = self.entered_ice_box(i)
            self.slide_bx.append(c)
            if c == 1:
                anyc = 1
                self._mark_box_coast(i)
            i = i + 1
        i = 0
        while i < len(self.bombx):
            c = self.entered_ice_bomb(i)
            self.slide_bm.append(c)
            if c == 1:
                anyc = 1
                self._mark_bomb_coast(i)
            i = i + 1
        if anyc == 0:
            return 0
        self.slide_on = 1
        self.slide_dx = dx
        self.slide_dy = dy
        self.turn_pending = 1
        return 1

    def slide_entities_ready(self) -> int:
        """1 when every in-flight slide entity has reached its logical cell.

        Includes objects that were pushed (no coast flag) whose sprites are
        still catching up via push_contact — otherwise the next slide_tick
        would fire while the snake is still one cell away from the box.
        """
        arrive = 8
        si = 0
        while si < self.nsnakes:
            want = 0
            if si < len(self.slide_sn):
                if self.slide_sn[si] == 1:
                    want = 1
            if si < len(self.snake_coast):
                if self.snake_coast[si] == 1:
                    want = 1
            if want == 1:
                s = self.snake(si)
                if s.gone == 0:
                    j = 0
                    n = s.npart()
                    while j < n:
                        if j < len(s.vx):
                            if s.phase == 0:
                                if j < len(s.mx):
                                    tx = s.mx[j] * 256
                                    ty = s.my[j] * 256
                                else:
                                    tx = s.xs[j] * 256
                                    ty = s.ys[j] * 256
                            else:
                                tx = s.xs[j] * 256
                                ty = s.ys[j] * 256
                            if self._manhattan256(s.vx[j], s.vy[j],
                                                  tx, ty) > arrive:
                                return 0
                        j = j + 1
            si = si + 1
        i = 0
        while i < len(self.boxx):
            if self.boxlive[i] == 1:
                if i < len(self.boxvx):
                    if self._manhattan256(self.boxvx[i], self.boxvy[i],
                                          self.boxx[i] * 256,
                                          self.boxy[i] * 256) > arrive:
                        return 0
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if self.bomblive[i] == 1:
                if i < len(self.bombvx):
                    if self._manhattan256(self.bombvx[i], self.bombvy[i],
                                          self.bombx[i] * 256,
                                          self.bomby[i] * 256) > arrive:
                        return 0
            i = i + 1
        return 1

    def slide_tick(self) -> int:
        """One cell of propel for every still-coasting entity. 1 if anyone moved."""
        dx = self.slide_dx
        dy = self.slide_dy
        self.in_slides = 1
        moved = 0
        order = 0
        while order < self.nsnakes:
            si = self.active + order
            if si >= self.nsnakes:
                si = si - self.nsnakes
            if si < len(self.slide_sn):
                if self.slide_sn[si] == 1:
                    if self.snake_on_ice(si) == 1:
                        if self.slide_snake(si, dx, dy) == 1:
                            moved = 1
                            self.snake(si).phase = 1
                            self.resolve_portals()
                            self.flush_bombs()
                    else:
                        self.slide_sn[si] = 0
            order = order + 1
        bi = 0
        while bi < len(self.boxx):
            if bi < len(self.slide_bx):
                if self.slide_bx[bi] == 1:
                    if self.boxlive[bi] == 1:
                        if self.ice_at(self.boxx[bi], self.boxy[bi],
                                       self.boxz[bi]) == 1:
                            if self.slide_object(bi, -1, dx, dy) == 1:
                                moved = 1
                                self.resolve_portals()
                                self.flush_bombs()
                        else:
                            self.slide_bx[bi] = 0
                    else:
                        self.slide_bx[bi] = 0
            bi = bi + 1
        bi = 0
        while bi < len(self.bombx):
            if bi < len(self.slide_bm):
                if self.slide_bm[bi] == 1:
                    if self.bomblive[bi] == 1:
                        if self.ice_at(self.bombx[bi], self.bomby[bi], 0) == 1:
                            if self.slide_object(-1, bi, dx, dy) == 1:
                                moved = 1
                                self.resolve_portals()
                                self.flush_bombs()
                        else:
                            self.slide_bm[bi] = 0
                    else:
                        self.slide_bm[bi] = 0
            bi = bi + 1
        i = 0
        while i < self.nsnakes:
            if i < len(self.slide_sn):
                if self.slide_sn[i] == 0:
                    if self.entered_ice_snake(i) == 1:
                        self.slide_sn[i] = 1
                        self._mark_snake_coast(i)
                        self.snake(i).phase = 1
                        moved = 1
            i = i + 1
        i = 0
        while i < len(self.boxx):
            if i < len(self.slide_bx):
                if self.slide_bx[i] == 0:
                    if self.entered_ice_box(i) == 1:
                        self.slide_bx[i] = 1
                        self._mark_box_coast(i)
                        moved = 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i < len(self.slide_bm):
                if self.slide_bm[i] == 0:
                    if self.entered_ice_bomb(i) == 1:
                        self.slide_bm[i] = 1
                        self._mark_bomb_coast(i)
                        moved = 1
            i = i + 1
        self.in_slides = 0
        # Snakes that left ice may now fall; still-skating ones stay alive.
        self.check_pits(0)
        return moved

    def clear_slide_state(self) -> None:
        self.slide_on = 0
        self.turn_pending = 0
        self.in_slides = 0
        self.slide_sn = []
        self.slide_bx = []
        self.slide_bm = []
        self._clear_push_couples()

    def finish_turn_after_slides(self) -> None:
        """End-of-turn bookkeeping deferred until gradual propel finishes."""
        self.slide_on = 0
        self.turn_pending = 0
        self._clear_push_couples()
        # Wall-stop / end of propel: die even if still on ice over shallow.
        self.check_pits(1)
        self.resolve_portals()
        self.flush_bombs()
        self.update_pads()
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
            self._copybuf(0, 4)
            _log("[game] checkpoint saved")
        elif loaded == 1:
            if len(self.chkdata) > 0:
                _log("[game] checkpoint loaded")
                self._copybuf(4, 1)
                self.apply_checkpoint()
        self.update_traps_arming()
        self.update_swarms()
        self.check_pits(1)
        self.update_stars()
        self.update_traps_turn_end()
        self.update_pads()
        self.ser()
        if self._bufs_equal(2, 0) == 1:
            self.turn = self.turn - 1
            return
        self._hist_push(0, 2, 3)
        self.rhist = []
        self.rhistlen = []
        self.dirty = 1
        _log("[game] turn " + str(self.turn))

    def resolve_slides(self, dx: int, dy: int) -> None:
        """Legacy full resolve — unused; slides run gradually via slide_tick."""
        if self.begin_slides(dx, dy) == 0:
            return
        steps = 0
        while steps < 2000:
            if self.slide_tick() == 0:
                self.slide_on = 0
                self.turn_pending = 0
                return
            steps = steps + 1
        self.slide_on = 0
        self.turn_pending = 0

    # ------------------------------------------------------------- pits etc.

    def check_pits(self, force: int) -> None:
        """A snake falls when EVERY surface part is over an unfilled pit.

        force=0: skip snakes still riding propel ice (mid Move/ResolveSlides).
        force=1: propel finished / end of turn — die even if still on ice
        (Unity KillAllSnakesFullyOverShallowPits / FillAllSnakesOverPitsAfterTurn).
        """
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            if s.gone == 0:
                if s.alive == 1:
                    skip = 0
                    if force == 0:
                        if self.snake_on_ice(si) == 1:
                            skip = 1
                    if skip == 0:
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
        """Kill a snake fully over pits.

        Shallow: corpse stays at surface (alive=0, gone=0) — still drawn with
        dead eyes/tongue, still pushable. If ANY part is over shallow, the
        whole snake stays as a surface corpse (no parts sunk/removed).
        Deep/bottomless only: parts sink and the snake is hidden when every
        part left the surface.
        """
        s = self.snake(si)
        s.alive = 0
        any_shallow = 0
        j = 0
        while j < s.npart():
            if self.pit_open_at(s.xs[j], s.ys[j]) == 1:
                any_shallow = 1
            j = j + 1
        if any_shallow == 1:
            # Mixed or all-shallow: keep every part on the surface.
            _log("[game] snake fell into a pit")
            if si == self.active:
                # Defer switch until sprites finish the move so the snake
                # still looks alive (pulse + eyes) while crawling.
                switch = 1
                if self._snake_vis_arrived(si) == 0:
                    switch = 0
                if switch == 1:
                    self.auto_switch()
            return
        j = 0
        while j < s.npart():
            t = self.pit_open_at(s.xs[j], s.ys[j])
            i = self.idx(s.xs[j], s.ys[j])
            if t == 2:                      # deep: fills the pit
                self.filled[i] = 1
                self.fillbox[i] = -1
                s.zs[j] = 2
            else:                           # bottomless
                s.zs[j] = 2
            j = j + 1
        s.gone = 1
        _log("[game] snake fell into a pit")
        if si == self.active:
            # Deep fall: keep this snake active so its brightness pulse
            # continues while sprites crawl onto the pits; switch after.
            switch = 1
            if self._snake_vis_arrived(si) == 0:
                switch = 0
            if switch == 1:
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
                self.flush_bombs()          # Trapdoor.cs flushes on opening
                self.check_pits(1)
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

    def set_active(self, c: int) -> None:
        """Select snake c and restart its brightness pulse at darkest."""
        if c != self.active:
            self.active = c
            self.activems = engine.ms()

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
                        self.set_active(c)
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
        self.pushmask = 0
        self.capture_ice_start()            # before crawl: who already rides ice
        r = self.move_active(dx, dy)
        if r == 2:
            return                          # won: no history
        if r == 0:
            # a refused push can still have squeezed a bomb against a
            # wall; the queued blast fires and the turn commits
            self.flush_bombs()
            self.ser()
            if self._bufs_equal(2, 0) == 1:
                self.turn = self.turn - 1
                return                      # refused, nothing changed
            self.check_pits(1)
            self.update_pads()
            self.ser()
            self._hist_push(0, 2, 3)
            self.rhist = []
            self.rhistlen = []
            _log("[game] turn " + str(self.turn))
            return
        self.lastdx = dx
        self.lastdy = dy
        self.resolve_portals()
        self.flush_bombs()                  # end-of-move blasts (pit bombs)
        # Mid Move/ResolveSlides: skate over pits while riding propel ice.
        self.check_pits(0)
        # Remember post-crawl pose so visuals finish the turn before propel.
        si = 0
        while si < self.nsnakes:
            self.snake(si).capture_mid()
            si = si + 1
        if self.begin_slides(dx, dy) == 1:
            # Gradual propel: slide_tick from animate; finish_turn later.
            self.dirty = 1
            return
        # No propel this turn — settle immediately.
        self.check_pits(1)
        self.resolve_portals()
        self.flush_bombs()
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
                self.apply_checkpoint()
        self.update_traps_arming()
        self.update_swarms()
        # End of turn: every snake fully over pits dies now (Unity
        # FillAllSnakesOverPitsAfterTurn — not blocked by other snakes).
        self.check_pits(1)
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
        if self.turn_pending == 1:
            # Abort in-progress gradual propel and restore pre-move state.
            self.pend_move = 0
            self.clear_slide_state()
            self._copybuf(2, 1)
            self.apply_buf()
            self.turn = self.turn - 1
            self.dirty = 1
            _log("[game] undo (abort propel)")
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
        if self.turn_pending == 1:
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
        self.pend_move = 0
        self.clear_slide_state()
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
                            self.set_active(c)
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

    def u(self, v: int) -> int:
        """Scale a layout value authored for 1920-wide to the window."""
        return v * engine.width() // 1920

    def uy(self, v: int) -> int:
        """Scale a layout value authored for 1080-tall to the window."""
        return v * engine.height() // 1080

    def ut(self, s: int) -> int:
        """Bitmap text scale authored for 1920-wide (at least 1)."""
        sc = s * engine.width() // 1920
        if sc < 1:
            return 1
        return sc

    def cspr(self, sid: int, px: int, py: int, wf: int, hf: int) -> None:
        """Draw a sprite centred in the cell at (px, py), sized wf x hf in
        1/256ths of a cell. Unity sizes each plain SpriteRenderer as
        texture / pixelsPerUnit * prefab scale, so most objects do not
        fill their whole cell."""
        cell = self.cell
        w = cell * wf // 256
        h = cell * hf // 256
        engine.sprite(sid, px + (cell - w) // 2, py + (cell - h) // 2, w, h)

    def drop_off(self) -> int:
        """Pixel offset for drop shadows (left and down)."""
        soff = self.cell // 16
        if soff < 1:
            return 1
        return soff

    def cspr_drop(self, sid: int, px: int, py: int, wf: int, hf: int,
                  off_scale: int) -> None:
        """Sprite with a soft black drop shadow below and to the left.
        off_scale is in 256ths of the normal drop offset (256 = full)."""
        soff = self.drop_off() * off_scale // 256
        if soff < 1:
            soff = 1
        self.cspr_ex(sid, px - soff, py + soff, wf, hf, 0, 128, 0)
        self.cspr(sid, px, py, wf, hf)

    def cspr_unload_wash(self, sid: int, px: int, py: int,
                         wf: int, hf: int) -> None:
        """Blend 35% toward white using the sprite as an alpha mask."""
        self.cspr_ex(sid, px, py, wf, hf, engine.SOLID_WHITE,
                     engine.UNLOAD_WASH, 0)

    def cspr_ex(self, sid: int, px: int, py: int, wf: int, hf: int,
                tint: int, alpha: int, rot: int) -> None:
        cell = self.cell
        w = cell * wf // 256
        h = cell * hf // 256
        engine.sprite_ex(sid, px + (cell - w) // 2, py + (cell - h) // 2,
                         w, h, tint, alpha, rot)

    def draw_dotted_line(self, x0: int, y0: int, x1: int, y1: int,
                         rgb: int, alpha: int) -> None:
        """Dotted segment matching Connectable LineRenderer (width ~0.1)."""
        dx = x1 - x0
        dy = y1 - y0
        adx = dx
        if adx < 0:
            adx = -adx
        ady = dy
        if ady < 0:
            ady = -ady
        dist = adx + ady
        if dist < 1:
            return
        step = self.cell // 8
        if step < 2:
            step = 2
        n = dist // step
        if n < 1:
            n = 1
        seg = 0
        while seg <= n:
            lx = x0 + dx * seg // n
            ly = y0 + dy * seg // n
            r = self.u(9) // 2             # 1.5× prior u(6) diameter → radius
            if r < 1:
                r = 1
            engine.circle_a(lx, ly, r, rgb, alpha)
            seg = seg + 1

    def conn_draw_px(self, lx: int, ly: int, bi: int) -> int:
        """Screen x for a portal/zone at logical (lx, ly).

        If anchored to box bi, rides the box's visual lerp so the offset
        from the box is preserved during the crawl.
        """
        if bi < 0:
            return self.cell_px(lx)
        if bi >= len(self.boxx):
            return self.cell_px(lx)
        if bi >= len(self.boxvx):
            return self.cell_px(lx)
        return self.ox + (self.boxvx[bi] * self.cell) // 256 + \
            (lx - self.boxx[bi]) * self.cell

    def conn_draw_py(self, lx: int, ly: int, bi: int) -> int:
        """Screen y companion to conn_draw_px."""
        if bi < 0:
            return self.cell_py(ly)
        if bi >= len(self.boxx):
            return self.cell_py(ly)
        if bi >= len(self.boxvy):
            return self.cell_py(ly)
        return self.oy - (self.boxvy[bi] * self.cell) // 256 - \
            (ly - self.boxy[bi]) * self.cell

    def conn_vis_cell(self, lx: int, ly: int, bi: int, axis: int) -> int:
        """Visual cell (axis 0=x, 1=y) for wall/door hide checks."""
        if bi < 0:
            if axis == 0:
                return lx
            return ly
        if bi >= len(self.boxx):
            if axis == 0:
                return lx
            return ly
        if axis == 0:
            if bi >= len(self.boxvx):
                return lx
            return self._vis_cell(self.boxvx[bi] + (lx - self.boxx[bi]) * 256)
        if bi >= len(self.boxvy):
            return ly
        return self._vis_cell(self.boxvy[bi] + (ly - self.boxy[bi]) * 256)

    def conn_carrier_hidden(self, bi: int) -> int:
        """1 if a connectable anchored to box bi should stay hidden.

        Carriers lost to the void, or finished sinking into a deep pit, take
        their portals / save / load / propel zones with them. During the
        crawl into the pit the zone still rides the sprite.
        """
        if bi < 0:
            return 0
        if bi >= len(self.boxx):
            return 1
        if self.boxlive[bi] == 0:
            return 1
        if self.boxz[bi] > 0:
            return self._box_vis_arrived(bi)
        return 0

    def draw_connectable_line(self, zx: int, zy: int, bi: int,
                              rgb: int) -> None:
        """Zone/portal → box/bomb line; alpha matches line.startColor.a."""
        if bi < 0:
            return
        if bi >= len(self.boxx):
            return
        if self.conn_carrier_hidden(bi) == 1:
            return
        cell = self.cell
        x0 = self.conn_draw_px(zx, zy, bi) + cell // 2
        y0 = self.conn_draw_py(zx, zy, bi) + cell // 2
        x1 = self.ox + (self.boxvx[bi] * cell) // 256 + cell // 2
        y1 = self.oy - (self.boxvy[bi] * cell) // 256 + cell // 2
        # startColor.a = 0.2509804 → 64/256
        self.draw_dotted_line(x0, y0, x1, y1, rgb, 64)

    def draw_board(self, tms: int) -> None:
        # static layer from the scene cache, animated entities on top.
        # A box may already have filled a pit in logic while its sprite is
        # still crawling onto the cell — keep rebaking so the fill art waits.
        if self._any_sinking_fill() == 1:
            self.dirty = 1
        if self.dirty == 1:
            self.draw_board_static()
            engine.bake()
            self.dirty = 0
        else:
            engine.restore()
        self.draw_board_dynamic(tms)

    def _box_vis_arrived(self, bi: int) -> int:
        """1 if box bi's sprite is on its logical cell."""
        if bi < 0:
            return 1
        if bi >= len(self.boxvx):
            return 1
        if self._manhattan256(self.boxvx[bi], self.boxvy[bi],
                              self.boxx[bi] * 256,
                              self.boxy[bi] * 256) <= 8:
            return 1
        return 0

    def _snake_vis_arrived(self, si: int) -> int:
        """1 if every part of snake si is visually on its logical cell."""
        s = self.snake(si)
        j = 0
        n = s.npart()
        while j < n:
            if j < len(s.vx):
                if self._manhattan256(s.vx[j], s.vy[j],
                                      s.xs[j] * 256, s.ys[j] * 256) > 8:
                    return 0
            j = j + 1
        return 1

    def _fill_visually_ready(self, i: int) -> int:
        """1 if filled[i] should draw fill art (filler sprite has arrived)."""
        if self.filled[i] == 0:
            return 0
        bi = self.fillbox[i]
        if bi >= 0:
            return self._box_vis_arrived(bi)
        # Snake-filled pit (fillsbox=-1): wait until every fallen snake that
        # covers this cell has finished crawling onto it.
        x = self.minx + i - (i // self.gw) * self.gw
        y = self.miny + i // self.gw
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            if s.alive == 0:
                j = 0
                n = s.npart()
                while j < n:
                    if s.xs[j] == x:
                        if s.ys[j] == y:
                            if self._snake_vis_arrived(si) == 0:
                                return 0
                    j = j + 1
            si = si + 1
        return 1

    def _any_sinking_fill(self) -> int:
        """1 if some logical fill is waiting on its filler sprite to arrive."""
        i = 0
        n = len(self.filled)
        while i < n:
            if self.filled[i] == 1:
                if self._fill_visually_ready(i) == 0:
                    return 1
            i = i + 1
        return 0

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
                    if self._fill_visually_ready(i) == 1:
                        engine.rect_a(px, py, cell, cell, 4139348, 120)
                        if self.fillbox[i] >= 0:
                            bi = self.fillbox[i]
                            self.cspr_ex(engine.SPR_BOX, px, py, 179, 173,
                                         8421504, 256, 0)
                            if bi < len(self.box_unload):
                                if self.box_unload[bi] == 1:
                                    self.cspr_unload_wash(
                                        engine.SPR_BOX, px, py, 179, 173)
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
        # grid overlay last in the static bake so it sits on walls/pads
        # (the original Show Grid setting, default on). Offset by half a
        # cell so lines cross cell centres rather than edges.
        if self.gridon == 1:
            half = cell // 2
            gx = 0
            while gx < self.vw:
                engine.rect_a(vx0 + gx * cell + half, vy0, 1, vhpx,
                              16777215, 40)
                gx = gx + 1
            gy = 0
            while gy < self.vh:
                engine.rect_a(vx0, vy0 + gy * cell + half, vwpx, 1,
                              16777215, 40)
                gy = gy + 1

    def draw_board_dynamic(self, tms: int) -> None:
        cell = self.cell
        # Cell-relative sprite sizes (1/256ths) below mirror Unity's
        # texture / pixelsPerUnit * prefab scale for each object.
        # Portal→carrier lines: Connectable.line sortingOrder 249, startColor
        # (0,1,1,a≈0.25) from Portal.prefab LineRenderer gradient.
        i = 0
        while i < len(self.porx):
            bi = self.porbox[i]
            vx = self.conn_vis_cell(self.porx[i], self.pory[i], bi, 0)
            vy = self.conn_vis_cell(self.porx[i], self.pory[i], bi, 1)
            if self.in_wall_or_door(vx, vy) == 0:
                if self.conn_carrier_hidden(bi) == 0:
                    self.draw_connectable_line(self.porx[i], self.pory[i],
                                               bi, 65535)
            i = i + 1
        # rotating portal sprites (order 250), prefab scale 0.9
        i = 0
        while i < len(self.porx):
            bi = self.porbox[i]
            vx = self.conn_vis_cell(self.porx[i], self.pory[i], bi, 0)
            vy = self.conn_vis_cell(self.porx[i], self.pory[i], bi, 1)
            if self.in_wall_or_door(vx, vy) == 0:
                if self.conn_carrier_hidden(bi) == 0:
                    self.cspr_ex(engine.SPR_PORTAL,
                                 self.conn_draw_px(self.porx[i], self.pory[i],
                                                   bi),
                                 self.conn_draw_py(self.porx[i], self.pory[i],
                                                   bi),
                                 230, 230, self.porcol[i], 256,
                                 (tms // 300) & 3)
            i = i + 1
        # apple (275) then boxes/bombs (300) -- above portals, below snakes.
        # Kept out of the static bake so rotating portals stay underneath.
        # End prefab: 236x260 art at 260 ppu, scale 0.53
        self.cspr(engine.SPR_APPLE, self.cell_px(self.ax),
                  self.cell_py(self.ay), 123, 136)
        # Propel/Load/Save→carrier lines: drawn before boxes so the dots
        # sit behind the carrier. Colors from each zone prefab's
        # Connectable.line.startColor (a≈0.25).
        i = 0
        while i < len(self.connx):
            t = self.conntype[i]
            bi = self.connbox[i]
            vx = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 0)
            vy = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 1)
            if self.in_wall_or_door(vx, vy) == 0:
                if self.conn_carrier_hidden(bi) == 0:
                    if t == 1:
                        self.draw_connectable_line(self.connx[i],
                                                   self.conny[i], bi, 16744703)
                    if t == 2:
                        self.draw_connectable_line(self.connx[i],
                                                   self.conny[i], bi, 255)
                    if t == 3:
                        self.draw_connectable_line(self.connx[i],
                                                   self.conny[i], bi, 16744448)
            i = i + 1
        i = 0
        while i < len(self.boxx):
            if self.boxlive[i] == 1:
                # Keep drawing a sinking box until its sprite reaches the pit
                # (logical fill may already have set boxz) so it crawls in
                # instead of vanishing one cell away.
                show = 0
                if self.boxz[i] == 0:
                    show = 1
                elif i < len(self.boxvx):
                    if self._manhattan256(self.boxvx[i], self.boxvy[i],
                                          self.boxx[i] * 256,
                                          self.boxy[i] * 256) > 8:
                        show = 1
                if show == 1:
                    # Box prefab: 244x236 art at 244 ppu, scale 0.7
                    bpx = self.ox + (self.boxvx[i] * cell) // 256
                    bpy = self.oy - (self.boxvy[i] * cell) // 256
                    self.cspr_drop(engine.SPR_BOX, bpx, bpy, 179, 173, 256)
                    if i < len(self.box_unload):
                        if self.box_unload[i] == 1:
                            self.cspr_unload_wash(engine.SPR_BOX, bpx, bpy,
                                                  179, 173)
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if self.bomblive[i] == 1:
                # Bomb prefab: 257x286 art at 286 ppu, scale 0.9
                # Shadow sits .65× as far as boxes/snakes (166/256).
                bpx = self.ox + (self.bombvx[i] * cell) // 256
                bpy = self.oy - (self.bombvy[i] * cell) // 256
                self.cspr_drop(engine.SPR_BOMB, bpx, bpy, 207, 230, 166)
                if i < len(self.bomb_unload):
                    if self.bomb_unload[i] == 1:
                        self.cspr_unload_wash(engine.SPR_BOMB, bpx, bpy,
                                              207, 230)
            i = i + 1
        # snakes (order 300/302) — one shadow stamp for all so overlapping
        # Bezier discs (and snakes) keep a single soft opacity.
        engine.shadow_begin()
        si = 0
        while si < self.nsnakes:
            self.draw_snake(si, tms)
            si = si + 1
        # Bugs and the foreground renderer of propel zones are order 400 in
        # the Unity prefabs, so both belong in front of snakes.
        i = 0
        while i < len(self.connx):
            if self.conntype[i] == 1:
                bi = self.connbox[i]
                vx = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 0)
                vy = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 1)
                if self.in_wall_or_door(vx, vy) == 0:
                    if self.conn_carrier_hidden(bi) == 0:
                        # Propel Zone prefab: square art, scale 0.97
                        self.cspr(engine.SPR_PROPEL,
                                  self.conn_draw_px(self.connx[i],
                                                    self.conny[i], bi),
                                  self.conn_draw_py(self.connx[i],
                                                    self.conny[i], bi),
                                  248, 248)
            i = i + 1
        i = 0
        while i < len(self.swarmx):
            if self.swarmx[i] > -30000:
                px = self.cell_px(self.swarmx[i])
                py = self.cell_py(self.swarmy[i])
                bw = cell * 38 // 256   # Bug prefab scale 0.15
                b = 0
                while b < 8:
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
            bi = self.connbox[i]
            vx = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 0)
            vy = self.conn_vis_cell(self.connx[i], self.conny[i], bi, 1)
            if self.in_wall_or_door(vx, vy) == 0:
                if self.conn_carrier_hidden(bi) == 0:
                    if t == 2:
                        # Save Icon: 340x393 at 393 ppu, zone scale 0.97
                        self.cspr(engine.SPR_SAVE,
                                  self.conn_draw_px(self.connx[i],
                                                    self.conny[i], bi),
                                  self.conn_draw_py(self.connx[i],
                                                    self.conny[i], bi),
                                  215, 248)
                    if t == 3:
                        # Load Icon: 176x86 at 176 ppu, zone scale 0.97
                        self.cspr(engine.SPR_LOAD,
                                  self.conn_draw_px(self.connx[i],
                                                    self.conny[i], bi),
                                  self.conn_draw_py(self.connx[i],
                                                    self.conny[i], bi),
                                  248, 121)
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
        # blocked-teleport indicators. Unity prefab lifetime=2s; texture is
        # white with SpriteRenderer.color red — use solid-tint mode so the
        # alpha mask is forced pure red on walls and filled pits.
        i = 0
        while i < len(self.blockx):
            age = tms - self.blockms[i]
            if age < 0:
                age = 0
            if age < 2000:
                a = 256 - age * 256 // 2000
                if a < 1:
                    a = 1
                if a > 256:
                    a = 256
                engine.sprite_ex(engine.SPR_BLOCKED,
                                 self.cell_px(self.blockx[i]),
                                 self.cell_py(self.blocky[i]), cell, cell,
                                 16711680 + 16777216, a, 0)
            i = i + 1
        # Bomb explosions: Unity Explosion.prefab (scale 3 → 3 cells, 9 frames).
        i = 0
        while i < len(self.explox):
            age = tms - self.exploms[i]
            if age < 0:
                age = 0
            if age < EXPLODE_MS:
                fr = age * 9 // EXPLODE_MS
                if fr > 8:
                    fr = 8
                sid = engine.SPR_EXPLO1 + fr
                sz = cell * 3
                px = self.cell_px(self.explox[i]) + (cell - sz) // 2
                py = self.cell_py(self.exploy[i]) + (cell - sz) // 2
                engine.sprite(sid, px, py, sz, sz)
            i = i + 1
        self.draw_hud()

    def _isqrt(self, n: int) -> int:
        """Integer square root (Newton), for Bezier corner tangents."""
        if n <= 0:
            return 0
        x = n
        y = (x + 1) // 2
        while y < x:
            x = y
            y = (x + n // x) // 2
        return x

    def draw_snake(self, si: int, tms: int) -> None:
        s = self.snake(si)
        # Deep-pit death sets gone immediately; keep drawing until sprites
        # finish crawling onto the pits so the fall is gradual.
        if s.gone == 1:
            if self._snake_vis_arrived(si) == 1:
                return
        # self.cell is an obj-typed field; cell_px returns a plain int.
        cell = self.cell_px(1) - self.cell_px(0)
        n = s.npart()
        col = s.colr * 65536 + s.colg * 256 + s.colb
        # Active snake pulses darkest → lightest → darkest, restarting at
        # darkest whenever it becomes active. Lightest is slightly below base.
        # Keep pulsing through a pit-death crawl (alive is already 0 in logic).
        pulsing = 0
        if s.alive == 1:
            pulsing = 1
        elif self._snake_vis_arrived(si) == 0:
            if s.alive == 0:
                pulsing = 1
        if pulsing == 1:
            if si == self.active:
                period = 1400
                age = tms - self.activems
                if age < 0:
                    age = 0
                t = age % period
                half = period // 2
                # t=0 / period: darkest (512); t=half: lightest (0)
                if t < half:
                    lvl = 512 - t * 512 // half
                else:
                    lvl = (t - half) * 512 // half
                # factor 256ths: lightest = 80% of inactive (204), mid 185, dark 155
                if lvl <= 256:
                    fac = 204 - 19 * lvl // 256
                else:
                    fac = 185 - 30 * (lvl - 256) // 256
                hr = s.colr * fac // 256
                hg = s.colg * fac // 256
                hb = s.colb * fac // 256
                col = hr * 65536 + hg * 256 + hb
        inset = cell // 12
        br = (cell - 2 * inset) // 4       # half prior thickness
        if br < 2:
            br = 2
        ox = self.cell_px(0) - 0           # force int screen origin
        oy = self.cell_py(0)
        # Pixel centres of surface / shallow-pit parts. Deep-gone parts are
        # skipped once their sprite has arrived (blast void / sunk fill).
        # Write into preallocated sn_* scratch — no per-frame list allocs.
        m = 0
        j = 0
        while j < n:
            show = 0
            if s.zs[j] < 2:
                show = 1
            elif j < len(s.vx):
                if self._manhattan256(s.vx[j], s.vy[j],
                                      s.xs[j] * 256, s.ys[j] * 256) > 8:
                    show = 1
            if show == 1:
                if m < MAX_SN_DRAW:
                    self.sn_cx[m] = ox + (s.vx[j] * cell) // 256 + cell // 2
                    self.sn_cy[m] = oy - (s.vy[j] * cell) // 256 + cell // 2
                    # Surface color while still crawling into a deep pit;
                    # zs>=2 only darkens after the sprite has arrived (and
                    # those parts are then hidden anyway).
                    self.sn_cz[m] = s.zs[j]
                    if self.sn_cz[m] >= 2:
                        self.sn_cz[m] = 0
                    m = m + 1
            j = j + 1
        if m == 0:
            return
        soff = self.drop_off()
        # Shadow pass (black @ 50% alpha, like box/bomb cspr_drop) then body.
        passn = 0
        while passn < 2:
            dxo = 0
            dyo = 0
            if passn == 0:
                dxo = 0 - soff
                dyo = soff
            if m == 1:
                if passn == 0:
                    engine.circle_shadow(self.sn_cx[0] + dxo,
                                         self.sn_cy[0] + dyo, br, 128)
                else:
                    dark = col
                    if self.sn_cz[0] == 1:
                        dark = (s.colr // 3) * 65536 + (s.colg // 3) * 256 + \
                            s.colb // 3
                    engine.circle(self.sn_cx[0] + dxo, self.sn_cy[0] + dyo, br,
                                  dark)
            else:
                if passn == 0:
                    # Tangents match Snake.BuildGridKnot: straight arms use
                    # 1/2 the neighbour delta; 90° corners use Unity's
                    # (4/3)·tan(π/8) ≈ 0.552 handle along the chord.
                    k = 0
                    while k < m:
                        self.sn_tinx[k] = 0
                        self.sn_tiny[k] = 0
                        self.sn_toux[k] = 0
                        self.sn_touy[k] = 0
                        is_c = 0
                        if k > 0:
                            if k < m - 1:
                                dpx = self.sn_cx[k - 1] - self.sn_cx[k]
                                dpy = self.sn_cy[k - 1] - self.sn_cy[k]
                                dnx = self.sn_cx[k + 1] - self.sn_cx[k]
                                dny = self.sn_cy[k + 1] - self.sn_cy[k]
                                if dpx + dnx != 0:
                                    is_c = 1
                                elif dpy + dny != 0:
                                    is_c = 1
                        self.sn_corner[k] = is_c
                        k = k + 1
                    # KnotCornerTangentScale in 1/256ths of a cell
                    cscale = 141
                    k = 0
                    while k < m:
                        if self.sn_corner[k] == 1:
                            cdx = self.sn_cx[k + 1] - self.sn_cx[k - 1]
                            cdy = self.sn_cy[k + 1] - self.sn_cy[k - 1]
                            L = self._isqrt(cdx * cdx + cdy * cdy)
                            if L > 0:
                                hl = cell * cscale // 256
                                self.sn_tinx[k] = (0 - cdx) * hl // L
                                self.sn_tiny[k] = (0 - cdy) * hl // L
                                self.sn_toux[k] = cdx * hl // L
                                self.sn_touy[k] = cdy * hl // L
                        else:
                            if k > 0:
                                sc = 128
                                if self.sn_corner[k - 1] == 1:
                                    sc = cscale
                                if k < m - 1:
                                    if self.sn_corner[k + 1] == 1:
                                        sc = cscale
                                self.sn_tinx[k] = (self.sn_cx[k - 1] -
                                                   self.sn_cx[k]) * sc // 256
                                self.sn_tiny[k] = (self.sn_cy[k - 1] -
                                                   self.sn_cy[k]) * sc // 256
                            if k < m - 1:
                                sc = 128
                                if self.sn_corner[k + 1] == 1:
                                    sc = cscale
                                if k > 0:
                                    if self.sn_corner[k - 1] == 1:
                                        sc = cscale
                                self.sn_toux[k] = (self.sn_cx[k + 1] -
                                                   self.sn_cx[k]) * sc // 256
                                self.sn_touy[k] = (self.sn_cy[k + 1] -
                                                   self.sn_cy[k]) * sc // 256
                        k = k + 1
                # Stroke each cubic Bezier segment with overlapping circles.
                samples = cell // 4
                if samples < 8:
                    samples = 8
                k = 0
                while k < m - 1:
                    p0x = self.sn_cx[k] + dxo
                    p0y = self.sn_cy[k] + dyo
                    p1x = self.sn_cx[k] + self.sn_toux[k] + dxo
                    p1y = self.sn_cy[k] + self.sn_touy[k] + dyo
                    p2x = self.sn_cx[k + 1] + self.sn_tinx[k + 1] + dxo
                    p2y = self.sn_cy[k + 1] + self.sn_tiny[k + 1] + dyo
                    p3x = self.sn_cx[k + 1] + dxo
                    p3y = self.sn_cy[k + 1] + dyo
                    dark = col
                    if passn == 1:
                        if self.sn_cz[k] == 1:
                            dark = (s.colr // 3) * 65536 + \
                                (s.colg // 3) * 256 + s.colb // 3
                        elif self.sn_cz[k + 1] == 1:
                            dark = (s.colr // 3) * 65536 + \
                                (s.colg // 3) * 256 + s.colb // 3
                    t = 0
                    while t <= samples:
                        # de Casteljau in integer t/samples
                        ax = p0x + (p1x - p0x) * t // samples
                        ay = p0y + (p1y - p0y) * t // samples
                        bx = p1x + (p2x - p1x) * t // samples
                        by = p1y + (p2y - p1y) * t // samples
                        ux = p2x + (p3x - p2x) * t // samples
                        uy = p2y + (p3y - p2y) * t // samples
                        dx0 = ax + (bx - ax) * t // samples
                        dy0 = ay + (by - ay) * t // samples
                        ex = bx + (ux - bx) * t // samples
                        ey = by + (uy - by) * t // samples
                        px = dx0 + (ex - dx0) * t // samples
                        py = dy0 + (ey - dy0) * t // samples
                        if passn == 0:
                            engine.circle_shadow(px, py, br, 128)
                        else:
                            engine.circle(px, py, br, dark)
                        t = t + 1
                    k = k + 1
            passn = passn + 1
        # face (only while the original head part survives: headless
        # corpses and fragments from a bomb blast show no eyes)
        if n > 0:
            face = 0
            if s.zs[0] < 2:
                face = 1
            elif self._snake_vis_arrived(si) == 0:
                # Deep fall in progress: keep the face until sprites arrive.
                face = 1
            if face == 1:
                if s.hashead == 0:
                    return
                hx = ox + (s.vx[0] * cell) // 256
                hy = oy - (s.vy[0] * cell) // 256
                rot = 0
                if s.fdx == 1:
                    rot = 3
                elif s.fdx == -1:
                    rot = 1
                elif s.fdy == -1:
                    rot = 2
                # Dead eyes only once the corpse has finished crawling onto
                # the pits; during the move it still looks alive.
                deadface = 0
                if s.alive == 0:
                    if s.gone == 0:
                        if self._snake_vis_arrived(si) == 1:
                            deadface = 1
                if deadface == 1:
                    ds = br * 8 // 5            # ~0.8 × prior 2*br
                    if ds < 8:
                        ds = 8
                    engine.sprite_ex(engine.SPR_DEADEYES,
                                     hx + cell // 2 - ds // 2,
                                     hy + cell // 2 - ds // 2,
                                     ds, ds, 16777215, 256, rot)
                else:
                    # Eyes sized to the thin head disk (radius br)
                    er = br * 2 // 5            # 20% smaller than br//2
                    if er < 2:
                        er = 2
                    fwd = br // 3
                    sep = br * 2 // 5
                    ecx = hx + cell // 2 + s.fdx * fwd
                    ecy = hy + cell // 2 - s.fdy * fwd
                    exo = s.fdy * sep
                    eyo = s.fdx * sep
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
        engine.text(self.u(24), self.uy(16), self.level_label, 16777215,
                    self.ut(3))
        hint: "char*" = self.hud_hint
        if self.nsnakes > 1:
            hint = self.hud_hint_tab
        engine.text(self.u(24), sh - self.uy(40), hint, 11184810, self.ut(2))

    def draw_win(self, tms: int) -> None:
        sw = engine.width()
        sh = engine.height()
        age = tms - self.winms
        cw = sw * 2 // 5
        chh = cw * engine.sprite_h(engine.SPR_CONGRATS) // \
            engine.sprite_w(engine.SPR_CONGRATS)
        engine.rect_a(0, 0, sw, sh, 0, 100)
        # Centered, then nudged 4% of screen height toward the top.
        engine.sprite(engine.SPR_CONGRATS, (sw - cw) // 2,
                      (sh - chh) // 2 - sh * 4 // 100, cw, chh)
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
            engine.rect(fx, fy, self.u(10), self.uy(16), colr)
            i = i + 1

    # ------------------------------------------------------------ animation

    def snap_obj_visuals(self) -> None:
        """Snap box/bomb visuals to logical cells (undo, load, teleport)."""
        i = 0
        n = len(self.boxx)
        while i < n:
            if i < len(self.boxvx):
                self.boxvx[i] = self.boxx[i] * 256
                self.boxvy[i] = self.boxy[i] * 256
            else:
                self.boxvx.append(self.boxx[i] * 256)
                self.boxvy.append(self.boxy[i] * 256)
            if i < len(self.box_warp):
                self.box_warp[i] = 0
                self.box_wdx[i] = 0
                self.box_wdy[i] = 0
            else:
                self.box_warp.append(0)
                self.box_wdx.append(0)
                self.box_wdy.append(0)
            if i < len(self.box_coast):
                self.box_coast[i] = 0
            else:
                self.box_coast.append(0)
            if i < len(self.box_coupled):
                self.box_coupled[i] = 0
            else:
                self.box_coupled.append(0)
            if i < len(self.box_land_pend):
                self.box_land_pend[i] = 0
            else:
                self.box_land_pend.append(0)
            i = i + 1
        while len(self.boxvx) > len(self.boxx):
            self.boxvx.pop()
            self.boxvy.pop()
        while len(self.box_warp) > len(self.boxx):
            self.box_warp.pop()
            self.box_wdx.pop()
            self.box_wdy.pop()
        while len(self.box_coast) > len(self.boxx):
            self.box_coast.pop()
        while len(self.box_coupled) > len(self.boxx):
            self.box_coupled.pop()
        while len(self.box_land_pend) > len(self.boxx):
            self.box_land_pend.pop()
        i = 0
        n = len(self.bombx)
        while i < n:
            if i < len(self.bombvx):
                self.bombvx[i] = self.bombx[i] * 256
                self.bombvy[i] = self.bomby[i] * 256
            else:
                self.bombvx.append(self.bombx[i] * 256)
                self.bombvy.append(self.bomby[i] * 256)
            if i < len(self.bomb_warp):
                self.bomb_warp[i] = 0
                self.bomb_wdx[i] = 0
                self.bomb_wdy[i] = 0
            else:
                self.bomb_warp.append(0)
                self.bomb_wdx.append(0)
                self.bomb_wdy.append(0)
            if i < len(self.bomb_coast):
                self.bomb_coast[i] = 0
            else:
                self.bomb_coast.append(0)
            if i < len(self.bomb_coupled):
                self.bomb_coupled[i] = 0
            else:
                self.bomb_coupled.append(0)
            if i < len(self.bomb_land_pend):
                self.bomb_land_pend[i] = 0
            else:
                self.bomb_land_pend.append(0)
            i = i + 1
        while len(self.bombvx) > len(self.bombx):
            self.bombvx.pop()
            self.bombvy.pop()
        while len(self.bomb_warp) > len(self.bombx):
            self.bomb_warp.pop()
            self.bomb_wdx.pop()
            self.bomb_wdy.pop()
        while len(self.bomb_coast) > len(self.bombx):
            self.bomb_coast.pop()
        while len(self.bomb_coupled) > len(self.bombx):
            self.bomb_coupled.pop()
        while len(self.bomb_land_pend) > len(self.bombx):
            self.bomb_land_pend.pop()
        si = 0
        while si < self.nsnakes:
            if si < len(self.snake_coast):
                self.snake_coast[si] = 0
            else:
                self.snake_coast.append(0)
            si = si + 1
        self.pend_move = 0
        self.clear_slide_state()

    def _vis_cell(self, v: int) -> int:
        """Nearest cell index for a 1/256 visual coordinate."""
        if v >= 0:
            return (v + 128) // 256
        return (v - 128) // 256

    def _manhattan256(self, ax: int, ay: int, bx: int, by: int) -> int:
        dx = ax - bx
        if dx < 0:
            dx = 0 - dx
        dy = ay - by
        if dy < 0:
            dy = 0 - dy
        return dx + dy

    def push_contact_ready(self, vx: int, vy: int, self_box: int,
                           self_bomb: int) -> int:
        """1 if a pusher has visually touched this object.

        A pushed box/bomb stays put until something crawling onto its
        visual cell reaches sprite-contact distance, then coasts with the
        pusher. Any snake part can be the pusher (tail/body included) —
        not only the head. Returns 0 when nothing is on the cell yet.
        self_box/self_bomb skip this object so it cannot freeze itself.
        """
        cx = self._vis_cell(vx)
        cy = self._vis_cell(vy)
        # Half-extents in 1/256ths of a cell (match drawn sprite scales).
        obj_r = 90                         # box ~179/256 wide
        if self_bomb >= 0:
            obj_r = 104                    # bomb ~207/256 wide
        snake_r = 56                       # head disk ≈ cell/4.5
        box_r = 90
        bomb_r = 104
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            if s.gone == 0:
                j = 0
                while j < s.npart():
                    if j < len(s.zs):
                        if s.zs[j] != 0:
                            j = j + 1
                            continue
                    if j < len(s.vx):
                        # Logical dest on this cell (part crawling onto it)
                        # or visual still covering it.
                        on_cell = 0
                        if s.xs[j] == cx:
                            if s.ys[j] == cy:
                                on_cell = 1
                        if on_cell == 0:
                            if self._vis_cell(s.vx[j]) == cx:
                                if self._vis_cell(s.vy[j]) == cy:
                                    on_cell = 1
                        if on_cell == 1:
                            hit = obj_r + snake_r
                            if self._manhattan256(s.vx[j], s.vy[j],
                                                  vx, vy) <= hit:
                                return 1
                    j = j + 1
            si = si + 1
        i = 0
        while i < len(self.boxx):
            if i != self_box:
                if self.boxlive[i] == 1:
                    if self.boxx[i] == cx:
                        if self.boxy[i] == cy:
                            if i < len(self.boxvx):
                                hit = obj_r + box_r
                                if self._manhattan256(self.boxvx[i],
                                                      self.boxvy[i],
                                                      vx, vy) <= hit:
                                    return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if i != self_bomb:
                if self.bomblive[i] == 1:
                    if self.bombx[i] == cx:
                        if self.bomby[i] == cy:
                            if i < len(self.bombvx):
                                hit = obj_r + bomb_r
                                if self._manhattan256(self.bombvx[i],
                                                      self.bombvy[i],
                                                      vx, vy) <= hit:
                                    return 1
            i = i + 1
        return 0

    def animate(self, dtms: int) -> None:
        step = (dtms * 256) // CRAWL_MS     # 1 cell per CRAWL_MS
        arrive = 8
        si = 0
        while si < self.nsnakes:
            s = self.snake(si)
            falling = 0
            if s.alive == 0:
                if self._snake_vis_arrived(si) == 0:
                    falling = 1
            j = 0
            n = s.npart()
            while j < n:
                if j < len(s.vx):
                    # Portal warp: finish crawling onto the entry cell first.
                    # Propel: phase 0 crawls to mid (post-turn), then phase 1
                    # to the final propel cells — so a 2-cell turn never
                    # collapses into one dot on the way to the coast end.
                    tx = s.xs[j] * 256
                    ty = s.ys[j] * 256
                    coasting = 0
                    if si < len(self.snake_coast):
                        if self.snake_coast[si] == 1:
                            coasting = 1
                    if s.warp == 1:
                        tx = (s.xs[j] - s.wdx) * 256
                        ty = (s.ys[j] - s.wdy) * 256
                    elif coasting == 1:
                        if s.phase == 0:
                            if j < len(s.mx):
                                tx = s.mx[j] * 256
                                ty = s.my[j] * 256
                    vx = s.vx[j]
                    vy = s.vy[j]
                    ddx = tx - vx
                    if s.warp == 0:
                        # Propel coasts crawl cell-by-cell; do not snap when
                        # the logical target is more than two cells away.
                        if coasting == 0:
                            if ddx > 512:
                                vx = tx
                            elif ddx < -512:
                                vx = tx
                            elif ddx > step:
                                vx = vx + step
                            elif ddx < 0 - step:
                                vx = vx - step
                            else:
                                vx = tx
                        elif ddx > step:
                            vx = vx + step
                        elif ddx < 0 - step:
                            vx = vx - step
                        else:
                            vx = tx
                    elif ddx > step:
                        vx = vx + step
                    elif ddx < 0 - step:
                        vx = vx - step
                    else:
                        vx = tx
                    ddy = ty - vy
                    if s.warp == 0:
                        if coasting == 0:
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
                        elif ddy > step:
                            vy = vy + step
                        elif ddy < 0 - step:
                            vy = vy - step
                        else:
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
            if s.warp == 1:
                done = 1
                j = 0
                while j < n:
                    if j < len(s.vx):
                        tx = (s.xs[j] - s.wdx) * 256
                        ty = (s.ys[j] - s.wdy) * 256
                        if self._manhattan256(s.vx[j], s.vy[j],
                                              tx, ty) > arrive:
                            done = 0
                    j = j + 1
                if done == 1:
                    s.snap_visual()
            elif si < len(self.snake_coast):
                if self.snake_coast[si] == 1:
                    done = 1
                    j = 0
                    while j < n:
                        if j < len(s.vx):
                            if s.phase == 0:
                                if j < len(s.mx):
                                    tx = s.mx[j] * 256
                                    ty = s.my[j] * 256
                                else:
                                    tx = s.xs[j] * 256
                                    ty = s.ys[j] * 256
                            else:
                                tx = s.xs[j] * 256
                                ty = s.ys[j] * 256
                            if self._manhattan256(s.vx[j], s.vy[j],
                                                  tx, ty) > arrive:
                                done = 0
                        j = j + 1
                    if done == 1:
                        if s.phase == 0:
                            s.phase = 1
                        else:
                            self.snake_coast[si] = 0
            # After a pit-death crawl: bake fills (deep) and hand off active.
            if falling == 1:
                if self._snake_vis_arrived(si) == 1:
                    if s.gone == 1:
                        self.dirty = 1
                    if si == self.active:
                        self.auto_switch()
            si = si + 1
        # Boxes/bombs: portal warp, propel coast, or wait for pusher contact.
        i = 0
        while i < len(self.boxx):
            if self.boxlive[i] == 1:
                if i < len(self.boxvx):
                    tx = self.boxx[i] * 256
                    ty = self.boxy[i] * 256
                    warping = 0
                    if i < len(self.box_warp):
                        if self.box_warp[i] == 1:
                            warping = 1
                            tx = (self.boxx[i] - self.box_wdx[i]) * 256
                            ty = (self.boxy[i] - self.box_wdy[i]) * 256
                    coasting = 0
                    if i < len(self.box_coast):
                        if self.box_coast[i] == 1:
                            coasting = 1
                    vx = self.boxvx[i]
                    vy = self.boxvy[i]
                    ddx = tx - vx
                    ddy = ty - vy
                    move = 0
                    if warping == 1:
                        move = 1
                    elif coasting == 1:
                        move = 1
                    elif self.slide_on == 1:
                        # During gradual propel: never snap; wait for contact
                        # so pushed objects start only when the snake hits them.
                        if self.push_contact_ready(vx, vy, i, -1) == 1:
                            move = 1
                            self._mark_box_coupled(i)
                            coasting = 1
                    elif ddx > 512:
                        vx = tx
                        vy = ty
                    elif ddx < -512:
                        vx = tx
                        vy = ty
                    elif ddy > 512:
                        vx = tx
                        vy = ty
                    elif ddy < -512:
                        vx = tx
                        vy = ty
                    elif self.push_contact_ready(vx, vy, i, -1) == 1:
                        move = 1
                        # Stick with the pusher for the rest of this cell.
                        self._mark_box_coast(i)
                        coasting = 1
                    if move == 1:
                        if ddx > step:
                            vx = vx + step
                        elif ddx < 0 - step:
                            vx = vx - step
                        else:
                            vx = tx
                        if ddy > step:
                            vy = vy + step
                        elif ddy < 0 - step:
                            vy = vy - step
                        else:
                            vy = ty
                    self.boxvx[i] = vx
                    self.boxvy[i] = vy
                    if warping == 1:
                        if self._manhattan256(vx, vy, tx, ty) <= arrive:
                            self.boxvx[i] = self.boxx[i] * 256
                            self.boxvy[i] = self.boxy[i] * 256
                            self.box_warp[i] = 0
                            self.box_wdx[i] = 0
                            self.box_wdy[i] = 0
                    elif self._manhattan256(vx, vy, self.boxx[i] * 256,
                                            self.boxy[i] * 256) <= arrive:
                        self.boxvx[i] = self.boxx[i] * 256
                        self.boxvy[i] = self.boxy[i] * 256
                        self._flush_box_land(i)
                        if coasting == 1:
                            self.box_coast[i] = 0
                        # Bake fill art now that the sprite has arrived.
                        if self.boxz[i] > 0:
                            self.dirty = 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if self.bomblive[i] == 1:
                if i < len(self.bombvx):
                    tx = self.bombx[i] * 256
                    ty = self.bomby[i] * 256
                    warping = 0
                    if i < len(self.bomb_warp):
                        if self.bomb_warp[i] == 1:
                            warping = 1
                            tx = (self.bombx[i] - self.bomb_wdx[i]) * 256
                            ty = (self.bomby[i] - self.bomb_wdy[i]) * 256
                    coasting = 0
                    if i < len(self.bomb_coast):
                        if self.bomb_coast[i] == 1:
                            coasting = 1
                    vx = self.bombvx[i]
                    vy = self.bombvy[i]
                    ddx = tx - vx
                    ddy = ty - vy
                    move = 0
                    if warping == 1:
                        move = 1
                    elif coasting == 1:
                        move = 1
                    elif self.slide_on == 1:
                        if self.push_contact_ready(vx, vy, -1, i) == 1:
                            move = 1
                            self._mark_bomb_coupled(i)
                            coasting = 1
                    elif ddx > 512:
                        vx = tx
                        vy = ty
                    elif ddx < -512:
                        vx = tx
                        vy = ty
                    elif ddy > 512:
                        vx = tx
                        vy = ty
                    elif ddy < -512:
                        vx = tx
                        vy = ty
                    elif self.push_contact_ready(vx, vy, -1, i) == 1:
                        move = 1
                        self._mark_bomb_coast(i)
                        coasting = 1
                    if move == 1:
                        if ddx > step:
                            vx = vx + step
                        elif ddx < 0 - step:
                            vx = vx - step
                        else:
                            vx = tx
                        if ddy > step:
                            vy = vy + step
                        elif ddy < 0 - step:
                            vy = vy - step
                        else:
                            vy = ty
                    self.bombvx[i] = vx
                    self.bombvy[i] = vy
                    if warping == 1:
                        if self._manhattan256(vx, vy, tx, ty) <= arrive:
                            self.bombvx[i] = self.bombx[i] * 256
                            self.bombvy[i] = self.bomby[i] * 256
                            self.bomb_warp[i] = 0
                            self.bomb_wdx[i] = 0
                            self.bomb_wdy[i] = 0
                    elif self._manhattan256(vx, vy, self.bombx[i] * 256,
                                            self.bomby[i] * 256) <= arrive:
                        self.bombvx[i] = self.bombx[i] * 256
                        self.bombvy[i] = self.bomby[i] * 256
                        self._flush_bomb_land(i)
                        if coasting == 1:
                            self.bomb_coast[i] = 0
            i = i + 1
        if self.slide_on == 1:
            if self.slide_entities_ready() == 1:
                if self.slide_tick() == 0:
                    self.finish_turn_after_slides()
                else:
                    self.dirty = 1
        self.flush_pending_move()

    def _any_obj_visual_lag(self) -> int:
        """1 if any live box/bomb sprite has not reached its logical cell."""
        arrive = 8
        i = 0
        while i < len(self.boxx):
            if self.boxlive[i] == 1:
                if i < len(self.boxvx):
                    if self._manhattan256(self.boxvx[i], self.boxvy[i],
                                          self.boxx[i] * 256,
                                          self.boxy[i] * 256) > arrive:
                        return 1
            i = i + 1
        i = 0
        while i < len(self.bombx):
            if self.bomblive[i] == 1:
                if i < len(self.bombvx):
                    if self._manhattan256(self.bombvx[i], self.bombvy[i],
                                          self.bombx[i] * 256,
                                          self.bomby[i] * 256) > arrive:
                        return 1
            i = i + 1
        return 0

    def snake_propel_busy(self, si: int) -> int:
        """1 if snake si cannot take a new move yet.

        Blocks during gradual propel and while the active snake or any
        pushed box/bomb is still crawling — otherwise a second move can
        start before the push animates and sprites overlap.
        """
        if self.slide_on == 1:
            return 1
        if self.turn_pending == 1:
            return 1
        if si >= 0:
            if si < self.nsnakes:
                if self._snake_vis_arrived(si) == 0:
                    s = self.snake(si)
                    if s.npart() > 0:
                        return 1
            if si < len(self.snake_coast):
                if self.snake_coast[si] == 1:
                    return 1
        if self._any_obj_visual_lag() == 1:
            return 1
        return 0

    def flush_pending_move(self) -> None:
        """Apply a move queued while the active snake was mid-animation."""
        if self.pend_move == 0:
            return
        if self.snake_propel_busy(self.active) == 1:
            return
        s = self.snake(self.active)
        if s.gone == 1:
            self.pend_move = 0
            return
        if s.alive == 0:
            self.pend_move = 0
            return
        dx = self.pend_dx
        dy = self.pend_dy
        self.pend_move = 0
        self.dirty = 1
        self.do_move(dx, dy)

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
            # Halfway between the title's bottom and the old 2/3 mark.
            press_y = (sh // 8 + th + sh * 2 // 3) // 2
            engine.text_centered(sw // 2, press_y,
                                 "PRESS ENTER", 16777215, self.ut(4))
        engine.text_centered(sw // 2, sh - self.uy(60),
                             "A BAREMETAL RPYTHON DEMAKE OF SNAKE-GAME",
                             9474192, self.ut(2))
        # a little snake swimming along the bottom
        t = tms // 16
        bs = self.u(36)
        if bs < 4:
            bs = 4
        i = 0
        while i < 6:
            px = (t + self.u(640) - i * self.u(40)) % (sw + self.u(400)) - \
                self.u(200)
            py = sh * 4 // 5
            engine.round_rect(px, py, bs, bs, bs // 2, 15, 65280)
            i = i + 1

    def draw_select(self, tms: int) -> None:
        sw = engine.width()
        sh = engine.height()
        engine.clear(0)
        engine.sprite(engine.SPR_BACKGROUND, 0, 0, sw, sh)
        engine.text_centered(sw // 2, self.uy(30), "SELECT LEVEL",
                             16777215, self.ut(5))
        cols = 8
        bw = self.u(108)
        gap = self.u(30)
        rows = (self.nlevels + cols - 1) // cols
        x0 = (sw - cols * bw - (cols - 1) * gap) // 2
        y0 = self.uy(140)
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
                grow = self.u(10)
            engine.sprite(sid, bx - grow, by - grow, bw + 2 * grow,
                          bw + 2 * grow)
            if i == self.cursor:
                pad = self.u(6)
                thk = self.u(3)
                if thk < 1:
                    thk = 1
                engine.round_rect(bx - grow - pad, by - grow - pad, thk,
                                  bw + 2 * grow + 2 * pad, 1, 0, 16777215)
                engine.round_rect(bx + bw + grow + thk // 2, by - grow - pad,
                                  thk, bw + 2 * grow + 2 * pad, 1, 0, 16777215)
                engine.round_rect(bx - grow - pad, by - grow - pad,
                                  bw + 2 * grow + 2 * pad, thk, 1, 0, 16777215)
                engine.round_rect(bx - grow - pad, by + bw + grow + thk // 2,
                                  bw + 2 * grow + 2 * pad, thk, 1, 0, 16777215)
            engine.text_centered(bx + bw // 2, by + bw // 2 - self.uy(16),
                                 self.lvlnum[i], 2236962, self.ut(3))
            if self.hasstar[i] == 1:
                ssid = engine.SPR_STAROFF
                if self.stashed[i] == 1:
                    ssid = engine.SPR_STARON
                ss = self.u(30)
                engine.sprite(ssid, bx + bw - self.u(34), by + bw - self.u(34),
                              ss, ss)
            i = i + 1
        engine.text_centered(sw // 2, sh - self.uy(50),
                             "ARROWS MOVE   ENTER PLAY   ESC BACK",
                             9474192, self.ut(2))

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
            if self.snake_propel_busy(self.active) == 1:
                self.pend_dx = 0
                self.pend_dy = 1
                self.pend_move = 1
            else:
                self.do_move(0, 1)
        elif k == engine.K_DOWN:
            if self.snake_propel_busy(self.active) == 1:
                self.pend_dx = 0
                self.pend_dy = -1
                self.pend_move = 1
            else:
                self.do_move(0, -1)
        elif k == engine.K_LEFT:
            if self.snake_propel_busy(self.active) == 1:
                self.pend_dx = -1
                self.pend_dy = 0
                self.pend_move = 1
            else:
                self.do_move(-1, 0)
        elif k == engine.K_RIGHT:
            if self.snake_propel_busy(self.active) == 1:
                self.pend_dx = 1
                self.pend_dy = 0
                self.pend_move = 1
            else:
                self.do_move(1, 0)
        elif k == engine.K_UNDO:
            self.pend_move = 0
            self.do_undo()
        elif k == engine.K_REDO:
            self.pend_move = 0
            self.do_redo()
        elif k == engine.K_RESET:
            self.pend_move = 0
            self.do_reset()
        elif k == engine.K_SWITCH:
            self.pend_move = 0
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
