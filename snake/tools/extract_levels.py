#!/usr/bin/env python3
"""extract_levels.py -- pull the real levels + art out of the Unity project.

Reads ~/snake-game's scene YAML (each level is a set of PrefabInstance blocks
whose m_Modifications override positions and cross-references) and the prefab
+ texture assets, and writes:

  rpy/levels.py   LEVELS: one compact string per level (opcode lines), plus
                  per-level view geometry and object links -- parsed at
                  runtime by the rpython game
  sprites.c       the game's actual sprite art, downsampled + tinted into
                  static RGBA tables the kernel blitter draws

Usage:  python3 tools/extract_levels.py [--game DIR] [--out DIR] [--solve N]

Excluded Unity scene numbers (see EXCLUDE_LEVELS) are skipped; remaining
levels are renumbered so remake indices stay consecutive (Unity 34 becomes
remake level 33 when 33 is excluded, etc.).

The level record grammar (one record per line, ints only):
  V vw vh cx100 cy100      view size in cells + camera center *100
  W x y kind               wall (kind 0..3 = Wall/2/3/4), 4 = weak wall
  P x y type               pit: 0 shallow, 1 deep, 2 bottomless
  O x y unload             box (unload=1 if Unity Unloadable is attached)
  M x y unload             bomb (unload=1 if Unity Unloadable is attached)
  A x y                    apple (End)
  * x y                    star
  I x y                    propel (ice) zone
  Y x y                    save zone
  L x y                    load zone
  H x y                    trapdoor
  G x y                    bug swarm
  D x y r g b ncells x y.. weightpad + its door cells (color 0..255)
  R x y pair r g b         portal (pair = link index; both ends share it)
  S r g b npart x y ...    snake (color 0..255; parts head first)
"""
import argparse
import os
import re
import sys
from collections import deque

HERE = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------- yaml bits

DOC_RE = re.compile(r"^--- !u!(\d+) &(\d+)( stripped)?\s*$", re.M)


def split_docs(text):
    """Yield (class_id, file_id, stripped, body) unity yaml documents."""
    marks = list(DOC_RE.finditer(text))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        yield int(m.group(1)), int(m.group(2)), bool(m.group(3)), \
            text[m.start():end]


def guid_map(game):
    """asset guid -> asset path, from every .meta under Assets/."""
    out = {}
    for root, _dirs, files in os.walk(os.path.join(game, "Assets")):
        for f in files:
            if not f.endswith(".meta"):
                continue
            p = os.path.join(root, f)
            try:
                head = open(p, encoding="utf-8", errors="replace").read(400)
            except OSError:
                continue
            m = re.search(r"guid: ([0-9a-f]{32})", head)
            if m:
                out[m.group(1)] = p[:-5]
    return out


class Instance:
    def __init__(self, fid, guid, parent):
        self.fid = fid
        self.guid = guid
        self.parent = parent          # transform fileID it is parented to
        self.mods = {}                # (target_fid, propertyPath) -> value
        self.refs = {}                # (target_fid, propertyPath) -> objectRef
        self.name = "?"
        self.valid = None             # target fids present in the prefab

    def target_ok(self, t):
        """Unity drops overrides whose target is not a document of the
        source prefab (stale leftovers from converting an object to a
        different prefab, e.g. scene 13's portals carry End-prefab
        transform overrides). Mirror that."""
        if self.valid is None:
            return True
        return t in self.valid

    def prop(self, path, default=0.0):
        """Any-target property lookup (root transform paths are unique)."""
        for (t, p), v in self.mods.items():
            if p == path and self.target_ok(t):
                try:
                    return float(v)
                except ValueError:
                    return default
        return default

    def prop_of(self, target, path, default=0.0):
        v = self.mods.get((target, path))
        if v is None:
            return default
        try:
            return float(v)
        except ValueError:
            return default


MOD_RE = re.compile(
    r"- target: \{fileID: (\d+), guid: ([0-9a-f]{32}), type: \d+\}\s*\n"
    r"\s*propertyPath: '?([^\n']+)'?\s*\n"
    r"\s*value: ?([^\n]*)\s*\n"
    r"\s*objectReference: \{fileID: (-?\d+)\}")


# Unloadable.cs — scene PrefabInstances add this as m_AddedComponents.
UNLOADABLE_GUID = "5a113f1e8e09ff03c94a2fc1e2ab1fab"


def parse_scene(path):
    """-> (instances, stripped, unloadable_fids)

    unloadable_fids: PrefabInstance fileIDs that have an Unloadable
    MonoBehaviour in m_AddedComponents (boxes/bombs that trigger load zones).
    """
    text = open(path, encoding="utf-8", errors="replace").read()
    instances, stripped = {}, {}
    unload_mbs = set()
    prefab_bodies = {}
    for cls, fid, is_stripped, body in split_docs(text):
        if cls == 1001:  # PrefabInstance
            g = re.search(r"m_SourcePrefab: \{fileID: \d+, guid: "
                          r"([0-9a-f]{32})", body)
            par = re.search(r"m_TransformParent: \{fileID: (\d+)\}", body)
            inst = Instance(fid, g.group(1) if g else "",
                            int(par.group(1)) if par else 0)
            for mm in MOD_RE.finditer(body):
                key = (int(mm.group(1)), mm.group(3).strip())
                inst.mods[key] = mm.group(4).strip()
                ref = int(mm.group(5))
                if ref:
                    inst.refs[key] = ref
            instances[fid] = inst
            prefab_bodies[fid] = body
        elif cls == 114 and UNLOADABLE_GUID in body:
            unload_mbs.add(fid)
        elif is_stripped:
            m = re.search(r"m_PrefabInstance: \{fileID: (\d+)\}", body)
            if m:
                stripped[fid] = int(m.group(1))
    unloadable = set()
    for mid in unload_mbs:
        needle = "addedObject: {fileID: %d}" % mid
        for fid, body in prefab_bodies.items():
            if needle in body:
                unloadable.add(fid)
    return instances, stripped, unloadable


# ------------------------------------------------------------ prefab lookup

# Snake prefab internals (stable fileIDs inside Snake.prefab / Snake Part):
SNAKE_COMPONENT = 6511202892623660480      # the Snake MonoBehaviour
SNAKE_ROOT_TRS = 9206719234441084136
SNAKE_PART0_TRS = 160637136455441819       # head (stripped nested id)
SNAKE_PART1_TRS = 1353585068579724393      # second part
PART_ROOT_TRS = 8755860299376948906        # Snake Part.prefab root transform


def rnd(v):
    return int(round(v))


_PREFAB_TARGETS = {}
_FID_MASK = 0x7FFFFFFFFFFFFFFF


def prefab_targets(path, guids):
    """fileIDs a scene override may legally target in this prefab: its own
    document anchors plus, recursively, every base/nested prefab's ids
    composed the way Unity flattens them ((instance_fid ^ inner_fid)
    masked to 63 bits)."""
    if path in _PREFAB_TARGETS:
        return _PREFAB_TARGETS[path]
    ids = None
    _PREFAB_TARGETS[path] = ids      # cycle guard
    if path and os.path.exists(path):
        text = open(path, encoding="utf-8", errors="replace").read()
        ids = set()
        for _cls, fid, _stripped, body in split_docs(text):
            ids.add(fid)
            if _cls != 1001:
                continue
            g = re.search(r"m_SourcePrefab: \{fileID: \d+, guid: "
                          r"([0-9a-f]{32})", body)
            inner = prefab_targets(guids.get(g.group(1), ""), guids) \
                if g else None
            if inner:
                ids.update((fid ^ t) & _FID_MASK for t in inner)
    _PREFAB_TARGETS[path] = ids
    return ids


def instance_pos(inst):
    """Root position of a prefab instance.

    Instances can override child transforms too (e.g. Trapdoor's graphics
    child carries an m_LocalPosition.y of its own), so x and y must come
    from the same target: the transform that owns the x override.
    """
    tgt = 0
    found = 0
    for (t, p) in inst.mods:
        if p == "m_LocalPosition.x" and inst.target_ok(t):
            tgt = t
            found = 1
            break
    if not found:
        return (inst.prop("m_LocalPosition.x"),
                inst.prop("m_LocalPosition.y"))
    return (inst.prop_of(tgt, "m_LocalPosition.x"),
            inst.prop_of(tgt, "m_LocalPosition.y"))


def extract_level(scene_path, guids):
    instances, stripped, unloadable = parse_scene(scene_path)
    for inst in instances.values():
        inst.valid = prefab_targets(guids.get(inst.guid, ""), guids)

    def pname(inst):
        p = guids.get(inst.guid, "")
        return os.path.splitext(os.path.basename(p))[0]

    def resolve(ref):
        """objectReference fileID -> owning PrefabInstance fid."""
        if ref in stripped:
            return stripped[ref]
        if ref in instances:
            return ref
        return 0

    recs, portals, snakes, pads = [], [], [], []
    boxes = []
    zones = []          # (letter, cx, cy, connected fid) emitted post-loop
    view = None

    def connected(inst):
        """PrefabInstance fid this Connectable's connectedTo anchors to."""
        for (_t, p), ref in inst.refs.items():
            if p == "connectedTo":
                return resolve(ref)
        return 0

    for fid, inst in sorted(instances.items()):
        name = pname(inst)
        inst.name = name
        x, y = instance_pos(inst)
        cx, cy = rnd(x), rnd(y)
        if name in ("Wall", "Wall 2", "Wall 3", "Wall 4", "Weak Wall"):
            kind = {"Wall": 0, "Wall 2": 1, "Wall 3": 2, "Wall 4": 3,
                    "Weak Wall": 4}[name]
            recs.append("W %d %d %d" % (cx, cy, kind))
        elif name == "Pit":
            recs.append("P %d %d 1" % (cx, cy))
        elif name == "Shallow Pit":
            recs.append("P %d %d 0" % (cx, cy))
        elif name == "Bottomless Pit":
            recs.append("P %d %d 2" % (cx, cy))
        elif name == "Box":
            u = 1 if fid in unloadable else 0
            boxes.append((fid, cx, cy))
            recs.append("O %d %d %d" % (cx, cy, u))
        elif name == "Bomb":
            u = 1 if fid in unloadable else 0
            recs.append("M %d %d %d" % (cx, cy, u))
        elif name == "End":
            recs.append("A %d %d" % (cx, cy))
        elif name == "Star":
            recs.append("* %d %d" % (cx, cy))
        elif name == "Propel Zone":
            zones.append(("I", cx, cy, connected(inst)))
        elif name == "Save Zone":
            zones.append(("Y", cx, cy, connected(inst)))
        elif name == "Load Zone":
            zones.append(("L", cx, cy, connected(inst)))
        elif name == "Trapdoor":
            recs.append("H %d %d" % (cx, cy))
        elif name == "Bug Swarm":
            recs.append("G %d %d" % (cx, cy))
        elif name == "Portal":
            other = 0
            for (t, p), ref in inst.refs.items():
                if p == "other":
                    other = resolve(ref)
            # Pair color: the Portal.lineColor field is only copied to the
            # partner by an editor-time OnValidate and is often missing from
            # one portal of a pair; the LineRenderer gradient is what both
            # portals of a pair reliably serialize.
            col = [inst.prop("m_Parameters.colorGradient.key0." + c, d)
                   for c, d in (("r", 0.0), ("g", 1.0), ("b", 1.0))]
            portals.append((fid, cx, cy, other, col, connected(inst)))
        elif name == "Weightpad":
            doors = []
            for (t, p), ref in sorted(inst.refs.items(),
                                      key=lambda kv: kv[0][1]):
                if p.startswith("doors.Array.data"):
                    doors.append(resolve(ref))
            col = [inst.prop("doorColor." + c, d)
                   for c, d in (("r", 0.0), ("g", 1.0), ("b", 0.8235294))]
            pads.append((fid, cx, cy, doors, col))
        elif name == "Snake":
            col = [inst.prop_of(SNAKE_COMPONENT, "color." + c, d)
                   for c, d in (("r", 0.0), ("g", 1.0), ("b", 0.0))]
            rx = inst.prop_of(SNAKE_ROOT_TRS, "m_LocalPosition.x")
            ry = inst.prop_of(SNAKE_ROOT_TRS, "m_LocalPosition.y")
            # An explicit parts.Array.size override is authoritative: Unity
            # truncates the array to it and ignores stale data[i] overrides
            # past the end (scene 2 keeps a leftover data[3]). Only infer
            # the count from data indices when no size override exists.
            nparts = 2
            size_override = 0
            for (t, p), v in inst.mods.items():
                if p == "parts.Array.size":
                    size_override = max(size_override, int(float(v)))
            if size_override:
                nparts = size_override
            else:
                for (t, p), v in inst.mods.items():
                    m = re.match(r"parts\.Array\.data\[(\d+)\]$", p)
                    if m and inst.refs.get((t, p)):
                        nparts = max(nparts, int(m.group(1)) + 1)
            parts = []
            for i in range(nparts):
                if i == 0:
                    px = inst.prop_of(SNAKE_PART0_TRS, "m_LocalPosition.x")
                    py = inst.prop_of(SNAKE_PART0_TRS, "m_LocalPosition.y")
                elif i == 1:
                    px = inst.prop_of(SNAKE_PART1_TRS, "m_LocalPosition.x")
                    py = inst.prop_of(SNAKE_PART1_TRS, "m_LocalPosition.y")
                else:
                    ref = None
                    for (t, p), r in inst.refs.items():
                        if p == "parts.Array.data[%d]" % i:
                            ref = r
                    if ref is None:
                        continue
                    part_inst = instances.get(resolve(ref))
                    if part_inst is None:
                        continue
                    px = part_inst.prop_of(PART_ROOT_TRS, "m_LocalPosition.x")
                    py = part_inst.prop_of(PART_ROOT_TRS, "m_LocalPosition.y")
                parts.append((rnd(rx + px), rnd(ry + py)))
            snakes.append((fid, col, parts))
        elif name == "Camera":
            vw = inst.prop("viewSize.x", 32.0)
            vh = inst.prop("viewSize.y", 18.0)
            view = (vw, vh, x, y)

    # door instances referenced by weightpads (Door prefab instances).
    # A door whose GameObject starts inactive is an OPEN doorway; pad
    # presses then flip it opposite to its siblings (Weightpad.cs toggles
    # each door's activeSelf individually).
    door_pos = {}
    for fid, inst in instances.items():
        if pname(inst) == "Door":
            px, py = instance_pos(inst)
            starts_open = 0
            for (_t, p), v in inst.mods.items():
                if p == "m_IsActive" and v.strip() == "0":
                    starts_open = 1
            door_pos[fid] = (rnd(px), rnd(py), starts_open)

    # PrefabInstance fids of Box records, in the same order the O lines
    # appear after sorted(recs) -- string sort of "O x y", so zone/portal
    # box indices match the order load_level appends into boxx/boxy.
    box_idx = {}
    for i, (fid, _cx, _cy) in enumerate(
            sorted(boxes, key=lambda t: "O %d %d" % (t[1], t[2]))):
        box_idx[fid] = i

    out = []
    if view is None:
        view = (32.0, 18.0, 0.0, 0.0)
    out.append("V %d %d %d %d" % (rnd(view[0]), rnd(view[1]),
                                  rnd(view[2] * 100), rnd(view[3] * 100)))
    out.extend(sorted(recs))
    for letter, cx, cy, cfid in sorted(zones):
        bi = box_idx.get(cfid, -1) if cfid else -1
        out.append("%s %d %d %d" % (letter, cx, cy, bi))

    pair_of = {}
    nextpair = 0
    for fid, cx, cy, other, col, cfid in portals:
        if fid in pair_of:
            pid = pair_of[fid]
        elif other and other in pair_of:
            pid = pair_of[other]
        else:
            pid = nextpair
            nextpair += 1
        pair_of[fid] = pid
        if other:
            pair_of.setdefault(other, pid)
        bi = box_idx.get(cfid, -1) if cfid else -1
        out.append("R %d %d %d %d %d %d %d" % (
            cx, cy, pid,
            rnd(col[0] * 255), rnd(col[1] * 255), rnd(col[2] * 255), bi))

    for fid, cx, cy, doors, col in pads:
        cells = [door_pos[d] for d in doors if d in door_pos]
        rec = "D %d %d %d %d %d %d" % (
            cx, cy, rnd(col[0] * 255), rnd(col[1] * 255), rnd(col[2] * 255),
            len(cells))
        for dx, dy, dopen in cells:
            rec += " %d %d %d" % (dx, dy, dopen)
        out.append(rec)

    # active snake first: the original activates "Snake" (base name) first
    for fid, col, parts in snakes:
        rec = "S %d %d %d %d" % (rnd(col[0] * 255), rnd(col[1] * 255),
                                 rnd(col[2] * 255), len(parts))
        for px, py in parts:
            rec += " %d %d" % (px, py)
        out.append(rec)
    return "\n".join(out)


# ------------------------------------------------------------------ sprites

# name -> (texture file, tint rgba, bake size)
SPRITES = [
    ("WALL",      "Wall.png",            None, 96),
    ("WALL2",     "Wall 2.png",          None, 96),
    ("WALL3",     "Wall 3.png",          None, 96),
    ("WALL4",     "Wall 4.png",          None, 96),
    ("WEAKWALL",  "Weak Wall.png",       None, 96),
    ("BOX",       "Box.png",             None, 96),
    ("BOMB",      "Bomb.png",            (0.502, 0.502, 0.502, 1.0), 96),
    ("PIT",       "Deep Pit.png",        None, 96),
    ("SHALLOW",   "Shallow Pit.png",     None, 96),
    ("BOTTOMLESS", "Bottomless Pit.png", None, 96),
    ("TRAPDOOR",  "Trapdoor.png",        None, 96),
    ("APPLE",     "Apple.png",           None, 96),
    ("STAR",      "Star.png",            None, 96),
    ("PORTAL",    "Portal.png",          None, 96),
    ("SAVE",      "Save Icon.png",       (0.0, 0.0, 1.0, 0.502), 96),
    ("LOAD",      "Load Icon.png",       (1.0, 0.502, 0.0, 0.502), 96),
    ("PROPEL",    "Propel Zone.png",     (1.0, 0.502, 1.0, 0.502), 96),
    ("PAD",       "Weightpad.png",       None, 96),
    ("PADDOWN",   "Weightpad (Pressed).png", None, 96),
    ("DOOR",      "Door.png",            None, 96),
    ("BUG",       "Bug.png",             None, 48),
    ("EYES",      "Snake Eyes (Outer).png", None, 96),
    ("EYEIN",     "Snake Eye (Inner).png", None, 32),
    ("DEADEYES",  "Dead Eyes And Tongue.png", None, 96),
    ("TITLE",     "Title.png",           None, 960),
    ("CONGRATS",  "Congrats.png",        None, 960),
    ("LVLBTN",    "Level Button.png",    None, 96),
    ("LVLWON",    "Level Button (Won).png", None, 96),
    ("STARON",    "Level Button Star (Collected).png", None, 48),
    ("STAROFF",   "Level Button Star (Uncollected).png", None, 48),
    ("BLOCKED",   "Teleport Blocked Indicator.png", None, 96),
    ("BACKGROUND", "Level Background.png", None, 960),
    # Unity Explosion prefab: 900ppu sprite × scale 3 → 3 cells; 9 frames.
    ("EXPLO1",    "Explosion/Explosion_1.png", None, 288),
    ("EXPLO2",    "Explosion/Explosion_2.png", None, 288),
    ("EXPLO3",    "Explosion/Explosion_3.png", None, 288),
    ("EXPLO4",    "Explosion/Explosion_4.png", None, 288),
    ("EXPLO5",    "Explosion/Explosion_5.png", None, 288),
    ("EXPLO6",    "Explosion/Explosion_6.png", None, 288),
    ("EXPLO7",    "Explosion/Explosion_7.png", None, 288),
    ("EXPLO8",    "Explosion/Explosion_8.png", None, 288),
    ("EXPLO9",    "Explosion/Explosion_9.png", None, 288),
]


def unity_sprite_rect(meta_path):
    """Return Unity's imported sprite rect, or None for whole-texture.

    Only Multiple-mode imports (spriteMode: 2) use the spriteSheet rects;
    Single-mode metas often carry a stale sprites list from an earlier
    Multiple import that Unity ignores. Unity stores sprite-rect Y from the
    texture's bottom edge, unlike Pillow. Each Multiple texture used here
    has exactly one sprite.
    """
    if not os.path.exists(meta_path):
        return None
    lines = open(meta_path).read().splitlines()
    multiple = False
    for line in lines:
        if line.strip() == "spriteMode: 2":
            multiple = True
            break
    if not multiple:
        return None
    in_sheet = False
    in_rect = False
    vals = {}
    for line in lines:
        if line == "  spriteSheet:":
            in_sheet = True
            continue
        if not in_sheet:
            continue
        if line == "      rect:":
            in_rect = True
            vals = {}
            continue
        if in_rect:
            stripped = line.strip()
            for key in ("x", "y", "width", "height"):
                prefix = key + ":"
                if stripped.startswith(prefix):
                    vals[key] = int(round(float(stripped[len(prefix):].strip())))
            if len(vals) == 4:
                if vals["width"] > 0 and vals["height"] > 0:
                    return (vals["x"], vals["y"],
                            vals["width"], vals["height"])
                return None
    return None


def bake_sprites(game, out_c):
    from PIL import Image
    tex = os.path.join(game, "Assets", "Art", "Textures")
    lines = ["/* sprites.c -- generated by tools/extract_levels.py from the"
             " original\n * snake-game art (downsampled RGBA, tints"
             " premultiplied). Do not edit. */",
             '#include "kernel.h"', "",
             "typedef struct { int w, h; const unsigned int *px; } Sprite;",
             ""]
    table = []
    cropped = 0
    for name, fname, tint, size in SPRITES:
        path = os.path.join(tex, fname)
        img = Image.open(path).convert("RGBA")
        w0, h0 = img.size
        # Compute the old baked dimensions from the full source image first.
        # Cropping must not change the sprite's screen footprint: resize the
        # imported rect back into exactly these dimensions.
        outw, outh = w0, h0
        if max(w0, h0) > size:
            s = size / max(w0, h0)
            outw = max(1, int(w0 * s))
            outh = max(1, int(h0 * s))
        rect = unity_sprite_rect(path + ".meta")
        if rect is not None:
            rx, ry, rw, rh = rect
            # Unity rects have a bottom-left origin. Pillow's crop has a
            # top-left origin and deliberately permits out-of-image bounds,
            # filling those portions with transparency just as the authored
            # import rect requests.
            top = h0 - ry - rh
            img = img.crop((rx, top, rx + rw, top + rh))
            cropped += 1
        if img.size != (outw, outh):
            img = img.resize((outw, outh), Image.LANCZOS)
        w, h = img.size
        data = list(img.getdata())
        words = []
        for (r, g, b, a) in data:
            if tint:
                r = int(r * tint[0]); g = int(g * tint[1])
                b = int(b * tint[2]); a = int(a * tint[3])
            words.append((a << 24) | (r << 16) | (g << 8) | b)
        rows = [", ".join("0x%08xu" % v for v in words[i:i + 8])
                for i in range(0, len(words), 8)]
        lines.append("static const unsigned int SPX_%s[%d] = {" %
                     (name, w * h))
        lines.extend("    " + r + "," for r in rows)
        lines.append("};")
        table.append((name, w, h))
    lines.append("")
    lines.append("const Sprite SPRITES[%d] = {" % len(table))
    for name, w, h in table:
        lines.append("    { %d, %d, SPX_%s },   /* %s */" %
                     (w, h, name, name))
    lines.append("};")
    lines.append("const int SPRITE_COUNT = %d;" % len(table))
    lines.append("")
    open(out_c, "w").write("\n".join(lines) + "\n")
    print("baked %d sprites (%d Unity rects) -> %s" %
          (len(table), cropped, out_c))
    return [name for name, _f, _t, _s in SPRITES]


# ------------------------------------------------------------------- solver

def solve(level_text, max_nodes=2000000):
    """BFS a simple level (walls/pits/boxes/apple) to a win key sequence."""
    walls, pits, boxes, snake, apple = set(), {}, [], [], None
    for line in level_text.splitlines():
        f = line.split()
        if not f:
            continue
        if f[0] == "W":
            walls.add((int(f[1]), int(f[2])))
        elif f[0] == "P":
            pits[(int(f[1]), int(f[2]))] = int(f[3])
        elif f[0] == "O":
            boxes.append((int(f[1]), int(f[2])))
        elif f[0] == "A":
            apple = (int(f[1]), int(f[2]))
        elif f[0] == "S":
            n = int(f[4])
            snake = [(int(f[5 + 2 * i]), int(f[6 + 2 * i]))
                     for i in range(n)]
    start = (tuple(snake), tuple(sorted(boxes)), frozenset())
    seen = {start}
    q = deque([(start, "")])
    dirs = {"u": (0, 1), "d": (0, -1), "l": (-1, 0), "r": (1, 0)}
    while q and len(seen) < max_nodes:
        (parts, bxs, filled), path = q.popleft()
        for key, (dx, dy) in dirs.items():
            hx, hy = parts[0]
            tx, ty = hx + dx, hy + dy
            nparts, nbxs = list(parts), list(bxs)
            if (tx, ty) == apple:
                return path + key
            if (tx, ty) in walls or (tx, ty) in parts:
                continue
            if (tx, ty) in bxs:
                bx, by = tx + dx, ty + dy
                if (bx, by) in walls or (bx, by) in bxs or \
                        (bx, by) in parts or (bx, by) == apple:
                    continue
                nbxs.remove((tx, ty))
                nfilled = set(filled)
                if (bx, by) in pits and pits[(bx, by)] == 1 and \
                        (bx, by) not in filled:
                    nfilled.add((bx, by))
                elif (bx, by) in pits and pits[(bx, by)] == 2 and \
                        (bx, by) not in filled:
                    pass                      # swallowed
                else:
                    nbxs.append((bx, by))
            else:
                nfilled = set(filled)
            nparts = [(tx, ty)] + [nparts[i] for i in range(len(parts) - 1)]
            if all((px, py) in pits and (px, py) not in nfilled
                   for px, py in nparts):
                continue                      # whole snake over pits: dies
            st = (tuple(nparts), tuple(sorted(nbxs)), frozenset(nfilled))
            if st not in seen:
                seen.add(st)
                q.append((st, path + key))
    return None


# --------------------------------------------------------------------- main

# Unity scene numbers (1-based filenames like 33.unity) to skip. Remake
# level indices stay consecutive: after excluding 33 and 35 from 1..44,
# the pack has 42 levels (Unity 34 → remake #33, Unity 36 → remake #34, …).
EXCLUDE_LEVELS = frozenset([33, 35, 42])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--game", default=os.path.expanduser("~/snake-game"))
    ap.add_argument("--out", default=os.path.join(HERE, ".."))
    ap.add_argument("--levels", type=int, default=45,
                    help="highest Unity scene number to consider (inclusive)")
    ap.add_argument("--exclude", type=int, nargs="*", default=None,
                    help="Unity scene numbers to skip (default: %s)"
                    % " ".join(str(n) for n in sorted(EXCLUDE_LEVELS)))
    ap.add_argument("--solve", type=int, default=0,
                    help="1-based remake level index to BFS-solve")
    args = ap.parse_args()

    exclude = EXCLUDE_LEVELS if args.exclude is None else frozenset(args.exclude)

    guids = guid_map(args.game)
    print("guid map: %d assets" % len(guids))
    if exclude:
        print("excluding Unity scenes: %s" %
              ", ".join(str(n) for n in sorted(exclude)))

    texts = []
    sources = []   # Unity scene number for each extracted remake level
    for n in range(1, args.levels + 1):
        if n in exclude:
            print("level --: skip Unity %d" % n)
            continue
        scene = os.path.join(args.game, "Assets", "Scenes", "%d.unity" % n)
        t = extract_level(scene, guids)
        texts.append(t)
        sources.append(n)
        counts = {}
        for line in t.splitlines():
            counts[line[0]] = counts.get(line[0], 0) + 1
        remake_n = len(texts)
        print("level %2d (Unity %2d): %s" % (remake_n, n, " ".join(
            "%s:%d" % kv for kv in sorted(counts.items()))))

    if args.solve:
        sol = solve(texts[args.solve - 1])
        print("level %d (Unity %d) solution: %s" % (
            args.solve, sources[args.solve - 1], sol))

    out_py = os.path.join(args.out, "rpy", "levels.py")
    excl_note = (", ".join(str(n) for n in sorted(exclude))
                 if exclude else "(none)")
    with open(out_py, "w") as f:
        f.write('"""levels -- generated by tools/extract_levels.py from the\n'
                "original Unity scenes (~/snake-game/Assets/Scenes/*.unity).\n"
                "Excluded Unity scenes: %s; remake indices are consecutive.\n"
                'Do not edit by hand; regenerate with `make levels`."""\n\n\n'
                % excl_note)
        f.write("def level_count() -> int:\n"
                "    return %d\n\n\n" % len(texts))
        f.write("def level_data(idx: int) -> \"char*\":\n")
        for i, t in enumerate(texts):
            f.write("    %s idx == %d:\n" % ("if" if i == 0 else "elif", i))
            f.write("        return %r\n" % t)
        f.write("    return \"\"\n")
    print("wrote %s (%d levels)" % (out_py, len(texts)))

    names = bake_sprites(args.game, os.path.join(args.out, "sprites.c"))
    print("sprite ids: " +
          ", ".join("%s=%d" % (n, i) for i, n in enumerate(names)))


if __name__ == "__main__":
    main()
