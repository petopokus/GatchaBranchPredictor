#!/usr/bin/env python3
"""
Gacha Branch Predictor
======================

Plan your pulls across a list of upcoming limited characters and see every way
it can play out as a clickable decision tree.

You give it:
  * banked pull items (e.g. ZZZ tapes) and banked currency (e.g. Polychrome)
  * the currency -> pull ratio (160 : 1 in ZZZ)
  * whether your first character is already guaranteed (lost the last 50/50)
  * how many pulls you assume it takes to get a drop (90 hard pity, 75 soft
    pity, 1 if you drank the lucky potion)
  * the characters, in banner order

The tree (drawn left to right):

  for each character      -> pull, or skip?
    if you pull and are guaranteed      -> you win, guarantee is used up
    if you pull and are not guaranteed  -> split: WIN the 50/50  /  LOSE it
      after a loss you are guaranteed   -> pull again, or give up and move on?

A branch ends when every character has been decided, or when you can no
longer afford a full drop. Each ending card shows what you got and what you
have left. The best endings (most characters, then most left over) glow gold.

Mouse:  click a node = select it (and open it if it's closed)
        click the +/- circle on a node = open / close it
        drag = pan · Ctrl+wheel = zoom · wheel = scroll · Shift+wheel = sideways

Run:  python gacha_branch_predictor.py      (needs only the standard library)
"""

from __future__ import annotations

import json
import sys
from collections import deque
from dataclasses import dataclass, replace
from pathlib import Path

try:
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # engine stays importable without tkinter (handy for tests)
    tk = None


# =============================================================================
# Engine
# =============================================================================

@dataclass(frozen=True)
class Config:
    tapes: int                 # banked pull items (1 pull each)
    polychrome: int            # banked currency
    currency_per_pull: int     # e.g. 160
    guaranteed: bool           # first character starts guaranteed
    pity: int                  # pulls assumed to get one drop
    win_chance: float          # 0..1, chance of winning the 50/50
    characters: tuple
    item_name: str = "Tapes"
    currency_name: str = "Polychrome"


@dataclass(frozen=True)
class State:
    idx: int                   # character currently being decided
    tapes: int
    poly: int
    guaranteed: bool
    lost_here: bool = False    # just lost the 50/50 on characters[idx]
    prob: float = 1.0          # chance of reaching this state, given our choices
    spent: int = 0             # pulls spent so far
    got: tuple = ()
    skipped: tuple = ()
    lost_stopped: tuple = ()   # lost the 50/50 and chose not to pull again
    log: tuple = ()


@dataclass(frozen=True)
class Child:
    label: str
    kind: str                  # start | win | lose | skip | stop
    state: State
    title: str = ""            # short text for graph nodes
    sub: str = ""


def pulls_available(cfg: Config, tapes: int, poly: int) -> int:
    return tapes + poly // cfg.currency_per_pull


def leftover_value(cfg: Config, s: State) -> int:
    """Everything left over, in raw currency (used to rank endings)."""
    return s.tapes * cfg.currency_per_pull + s.poly


def spend(cfg: Config, tapes: int, poly: int, pulls: int):
    """Spend `pulls` pulls, using pull items first, then currency."""
    from_items = min(tapes, pulls)
    return tapes - from_items, poly - (pulls - from_items) * cfg.currency_per_pull


def start_state(cfg: Config) -> State:
    return State(idx=0, tapes=cfg.tapes, poly=cfg.polychrome, guaranteed=cfg.guaranteed)


def state_key(s: State):
    return (s.idx, s.tapes, s.poly, s.guaranteed, s.lost_here)


def end_reason(cfg: Config, s: State):
    """None while there is still a decision to make, else 'done' or 'broke'."""
    if s.idx >= len(cfg.characters):
        return "done"
    if pulls_available(cfg, s.tapes, s.poly) < cfg.pity:
        return "broke"
    return None


def children(cfg: Config, s: State) -> list:
    """All options available from state `s`."""
    if end_reason(cfg, s) is not None:
        return []

    name = cfg.characters[s.idx]
    cost = cfg.pity
    tapes, poly = spend(cfg, s.tapes, s.poly, cost)
    spent = s.spent + cost
    verb = "Pull again" if s.lost_here else "Pull"
    out = []

    # ---- pull -----------------------------------------------------------
    if s.guaranteed:
        out.append(Child(
            f"{verb} for {name} → guaranteed WIN",
            "win",
            replace(
                s, idx=s.idx + 1, tapes=tapes, poly=poly, guaranteed=False,
                lost_here=False, spent=spent, got=s.got + (name,),
                log=s.log + (f"{verb} for {name}: guaranteed win (-{cost} pulls)",),
            ),
            title=f"{verb} {name}",
            sub="WIN · guaranteed",
        ))
    else:
        w = cfg.win_chance
        if w > 0:
            out.append(Child(
                f"Pull for {name} → WIN the 50/50 ({w * 100:g}%)",
                "win",
                replace(
                    s, idx=s.idx + 1, tapes=tapes, poly=poly, guaranteed=False,
                    lost_here=False, prob=s.prob * w, spent=spent,
                    got=s.got + (name,),
                    log=s.log + (f"Pull for {name}: won the 50/50 (-{cost} pulls)",),
                ),
                title=f"Pull {name}",
                sub=f"WIN · {w * 100:g}%",
            ))
        if w < 1:
            out.append(Child(
                f"Pull for {name} → LOSE the 50/50 ({(1 - w) * 100:g}%)",
                "lose",
                replace(
                    s, tapes=tapes, poly=poly, guaranteed=True, lost_here=True,
                    prob=s.prob * (1 - w), spent=spent,
                    log=s.log + (f"Pull for {name}: lost the 50/50 (-{cost} pulls)",),
                ),
                title=f"Pull {name}",
                sub=f"LOSE · {(1 - w) * 100:g}%",
            ))

    # ---- don't pull (skip, or give up after a loss) -----------------------
    if s.lost_here:
        out.append(Child(
            f"Stop — give up on {name} (guarantee carries over)",
            "stop",
            replace(
                s, idx=s.idx + 1, lost_here=False,
                lost_stopped=s.lost_stopped + (name,),
                log=s.log + (f"Gave up on {name} after losing the 50/50",),
            ),
            title=f"Stop {name}",
            sub="gave up · guarantee kept",
        ))
    else:
        out.append(Child(
            f"Skip {name}",
            "skip",
            replace(
                s, idx=s.idx + 1, skipped=s.skipped + (name,),
                log=s.log + (f"Skipped {name}",),
            ),
            title=f"Skip {name}",
            sub="no pulls spent",
        ))
    return out


def count_endings(cfg: Config) -> int:
    """Number of distinct branch endings (memoised, so it stays fast)."""
    memo = {}

    def go(s: State) -> int:
        key = state_key(s)
        if key in memo:
            return memo[key]
        kids = children(cfg, s)
        result = 1 if not kids else sum(go(k.state) for k in kids)
        memo[key] = result
        return result

    return go(start_state(cfg))


class Evaluator:
    """Ranks endings: most characters first, then most left over.

    `best` is (characters, leftover) of the best ending anywhere in the tree.
    """

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._memo = {}
        add, left = self.future(start_state(cfg))
        self.best = (add, left)

    def future(self, s: State):
        """Best (extra characters, leftover) reachable from `s`."""
        key = state_key(s)
        if key in self._memo:
            return self._memo[key]
        kids = children(self.cfg, s)
        if not kids:
            result = (0, leftover_value(self.cfg, s))
        else:
            result = max(
                ((1 if k.kind == "win" else 0) + sub[0], sub[1])
                for k in kids
                for sub in [self.future(k.state)]
            )
        self._memo[key] = result
        return result

    def grade(self, s: State) -> str:
        """'best', 'good' or '' for a finished state."""
        most, left = self.best
        if most == 0 or len(s.got) < most:
            return ""
        return "best" if leftover_value(self.cfg, s) == left else "good"

    def on_best_path(self, s: State) -> bool:
        add, left = self.future(s)
        return (len(s.got) + add, left) == self.best


# ---- formatting helpers ------------------------------------------------------

def fmt_names(names) -> str:
    return ", ".join(names) if names else "—"


def fmt_pct(p: float) -> str:
    pct = p * 100
    if pct <= 0:
        return "0%"
    if pct < 0.01:
        return "<0.01%"
    return f"{pct:.2f}".rstrip("0").rstrip(".") + "%"


def leftover_short(cfg: Config, s: State) -> str:
    return f"{s.tapes:,} {cfg.item_name} + {s.poly:,} {cfg.currency_name}"


def leftover_long(cfg: Config, s: State) -> str:
    n = pulls_available(cfg, s.tapes, s.poly)
    return f"{leftover_short(cfg, s)}  (= {n:,} pulls)"


def loss_details(cfg: Config, s: State, reason):
    """(lost the 50/50 and no more pulls, characters never reached) for a state."""
    lost = s.lost_stopped
    unreached = ()
    if reason == "broke":
        if s.lost_here:
            lost = lost + (cfg.characters[s.idx],)
            unreached = cfg.characters[s.idx + 1:]
        else:
            unreached = cfg.characters[s.idx:]
    return lost, unreached


def describe(cfg: Config, s: State) -> str:
    """Multi-line summary for the details panel."""
    reason = end_reason(cfg, s)
    avail = pulls_available(cfg, s.tapes, s.poly)
    lines = []

    if reason == "done":
        lines.append("FINAL RESULT — every character decided")
    elif reason == "broke":
        lines.append(f"FINAL RESULT — out of pulls (a drop needs {cfg.pity}, you have {avail})")
    else:
        name = cfg.characters[s.idx]
        how = "guaranteed" if s.guaranteed else f"{cfg.win_chance * 100:g}% to win"
        lines.append(f"NEXT DECISION — {name} ({how})")
    lines.append("")

    lines.append("What was done:")
    if s.log:
        lines.extend(f"  {i}. {entry}" for i, entry in enumerate(s.log, 1))
    else:
        lines.append("  (nothing decided yet)")
    lines.append("")

    lost, unreached = loss_details(cfg, s, reason)

    lines.append(f"Characters obtained : {fmt_names(s.got)}")
    lines.append(f"Skipped             : {fmt_names(s.skipped)}")
    if lost:
        lines.append(f"Lost 50/50, no more : {fmt_names(lost)}")
    if unreached:
        lines.append(f"Couldn't afford     : {fmt_names(unreached)}")
    lines.append(f"Pulls spent         : {s.spent:,}")
    lines.append(f"Left over           : {leftover_long(cfg, s)}")
    if reason is None:
        lines.append(f"Guarantee           : {'active' if s.guaranteed else 'none'}")
    else:
        lines.append(f"Guarantee           : {'carried into the next banner' if s.guaranteed else 'none'}")
    lines.append(f"Chance of this path : {fmt_pct(s.prob)}  (given the choices above)")
    return "\n".join(lines)


# ---- exporting results --------------------------------------------------------

EXPORT_CAP = 50_000            # most endings written when exporting "everything"


def ending_summary(cfg: Config, s: State, grade: str = "") -> dict:
    """One finished branch as a plain dict (for the JSON export)."""
    reason = end_reason(cfg, s)
    lost, unreached = loss_details(cfg, s, reason)
    return {
        "rank": {"best": "best", "good": "ties_most_characters"}.get(grade, ""),
        "characters_obtained": list(s.got),
        "skipped": list(s.skipped),
        "lost_5050_and_gave_up": list(lost),
        "could_not_afford": list(unreached),
        "ended_because": "all characters decided" if reason == "done" else "ran out of pulls",
        "pulls_spent": s.spent,
        "left_over": {
            "pull_items": s.tapes,
            "currency": s.poly,
            "pulls_equivalent": pulls_available(cfg, s.tapes, s.poly),
        },
        "guarantee_carried_over": s.guaranteed,
        "chance": round(s.prob, 10),
        "steps": list(s.log),
    }


def iter_endings(cfg: Config, limit: int):
    """Every finished branch, depth-first, stopping after `limit`."""
    stack = [start_state(cfg)]
    n = 0
    while stack and n < limit:
        s = stack.pop()
        kids = children(cfg, s)
        if not kids:
            n += 1
            yield s
        else:
            stack.extend(k.state for k in reversed(kids))


def best_ending_states(cfg: Config, ev: Evaluator) -> list:
    """Only the branches that reach the best possible ending."""
    out = []
    if ev.best[0] == 0:
        return out
    stack = [start_state(cfg)]
    while stack:
        s = stack.pop()
        kids = children(cfg, s)
        if not kids:
            if ev.grade(s) == "best":
                out.append(s)
        else:
            stack.extend(k.state for k in reversed(kids) if ev.on_best_path(k.state))
    return out


def build_results(cfg: Config, ev: Evaluator, opened_states=()) -> dict:
    """The "results" block of an export.

    Small trees are exported completely. If there are more than EXPORT_CAP
    endings, only the ones currently opened in the graph are written (plus the
    best endings, which are always included).
    """
    total = count_endings(cfg)
    if total <= EXPORT_CAP:
        states, scope = list(iter_endings(cfg, EXPORT_CAP)), "all"
    else:
        states, scope = list(opened_states), "only_those_opened_in_the_graph"

    def rank_key(s):
        return (-len(s.got), -leftover_value(cfg, s), -s.prob)

    best_states = sorted(best_ending_states(cfg, ev), key=rank_key)
    return {
        "total_possible_endings": total,
        "best_case": {
            "characters": ev.best[0],
            "left_over_currency": ev.best[1],
        },
        "endings_included": scope,
        "best_endings": [ending_summary(cfg, s, "best") for s in best_states],
        "endings": [ending_summary(cfg, s, ev.grade(s)) for s in sorted(states, key=rank_key)],
    }


def next_free_path(folder: Path, stem: str = "result", ext: str = ".json") -> Path:
    """result.json, or result_2.json, result_3.json ... if that name is taken."""
    path = Path(folder) / f"{stem}{ext}"
    n = 2
    while path.exists():
        path = Path(folder) / f"{stem}_{n}{ext}"
        n += 1
    return path


# =============================================================================
# Tree model (no GUI): which nodes exist, which are open, where they sit
# =============================================================================

# layout, in logical units (multiplied by the zoom when drawn)
NODE_W, NODE_H = 170, 54       # ordinary decision node
LEAF_W, LEAF_H = 215, 58       # ending card
X_STEP = NODE_W + 62           # column spacing
Y_STEP = 72                    # row spacing


class TreeNode:
    __slots__ = ("nid", "child", "parent", "depth", "kids", "expanded", "end", "x", "y")

    def __init__(self, nid, child, parent, depth, end):
        self.nid = nid
        self.child = child
        self.parent = parent
        self.depth = depth
        self.kids = None        # None until opened for the first time
        self.expanded = False
        self.end = end          # None, 'done' or 'broke'
        self.x = 0.0
        self.y = 0.0


class TreeModel:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.by_id = {}
        self._next = 0
        self._grades = {}
        self.evaluator = Evaluator(cfg)

        s = start_state(cfg)
        avail = pulls_available(cfg, s.tapes, s.poly)
        root = Child("START", "start", s, title="START",
                     sub="first character guaranteed" if s.guaranteed else f"{avail:,} pulls to spend")
        self.root = self._make(root, None)
        self.expand(self.root)

    # -- structure ---------------------------------------------------------

    def _make(self, child, parent):
        node = TreeNode(self._next, child, parent,
                        0 if parent is None else parent.depth + 1,
                        end_reason(self.cfg, child.state))
        self._next += 1
        self.by_id[node.nid] = node
        return node

    def expand(self, n):
        if n.end:
            return
        if n.kids is None:
            n.kids = [self._make(k, n) for k in children(self.cfg, n.child.state)]
        n.expanded = True

    def toggle(self, n):
        if n.end:
            return
        if n.expanded:
            n.expanded = False
        else:
            self.expand(n)

    def expand_all(self, start=None, cap=800):
        """Open everything below `start`, stopping after `cap` new nodes."""
        queue = deque([start or self.root])
        created = 0
        while queue:
            n = queue.popleft()
            if n.end:
                continue
            before = len(self.by_id)
            self.expand(n)
            created += len(self.by_id) - before
            queue.extend(n.kids)
            if created >= cap:
                return created, True
        return created, False

    def collapse_all(self):
        for n in self.by_id.values():
            n.expanded = False
        self.root.expanded = True

    def reveal_best(self):
        """Open only the paths that lead to the best endings; return those endings."""
        ev = self.evaluator
        found = []
        if ev.best[0] == 0:
            return found

        def go(n):
            if n.end:
                if ev.grade(n.child.state) == "best":
                    found.append(n)
                return
            self.expand(n)
            for k in n.kids:
                if ev.on_best_path(k.child.state):
                    go(k)

        go(self.root)
        return found

    def grade(self, n) -> str:
        if not n.end:
            return ""
        if n.nid not in self._grades:
            self._grades[n.nid] = self.evaluator.grade(n.child.state)
        return self._grades[n.nid]

    # -- queries ---------------------------------------------------------------

    def visible(self):
        out, stack = [], [self.root]
        while stack:
            n = stack.pop()
            out.append(n)
            if n.expanded and n.kids:
                stack.extend(reversed(n.kids))
        return out

    def path_to_root(self, n):
        out = []
        while n is not None:
            out.append(n)
            n = n.parent
        return out

    def visible_ancestor(self, n):
        """`n` itself if it's on screen, else the closed node hiding it."""
        cur = self.root
        for node in list(reversed(self.path_to_root(n)))[1:]:
            if cur.expanded:
                cur = node
            else:
                break
        return cur

    def layout(self):
        """Assign x/y to visible nodes. Returns (width, height) in logical units."""
        slot = 0
        max_x = 0.0

        def go(n):
            nonlocal slot, max_x
            n.x = n.depth * X_STEP
            max_x = max(max_x, n.x + (LEAF_W if n.end else NODE_W))
            if n.expanded and n.kids:
                ys = [go(k) for k in n.kids]
                n.y = (ys[0] + ys[-1]) / 2
            else:
                n.y = slot * Y_STEP + Y_STEP / 2
                slot += 1
            return n.y

        go(self.root)
        return max_x, slot * Y_STEP


# =============================================================================
# GUI
# =============================================================================

NODE_COLORS = {            # kind -> (fill, outline)
    "start": ("#dbe8ff", "#3b6fd4"),
    "win":   ("#dcf3e3", "#2e8b4a"),
    "lose":  ("#fbe0de", "#c0392b"),
    "skip":  ("#eeeeee", "#8a8a8a"),
    "stop":  ("#fde9d3", "#d9822b"),
}
EDGE_COLORS = {"start": "#3b6fd4", "win": "#2e8b4a", "lose": "#c0392b",
               "skip": "#a0a0a0", "stop": "#d9822b"}
BEST_FILL, BEST_LINE, BEST_GLOW = "#ffd54a", "#b8860b", "#ffe9a0"
GOOD_FILL, GOOD_LINE = "#fff1b8", "#e0a800"
SELECT_COLOR = "#1a73e8"
INK = "#1f2328"
CANVAS_BG = "#fbfbfc"


class App:
    PAD = 40
    EXPAND_CAP = 800           # graph drawing is heavier than a list, so keep this modest
    MIN_ZOOM, MAX_ZOOM = 0.15, 2.0
    SETTINGS = Path(__file__).with_name("gacha_branch_predictor_settings.json")

    def __init__(self, root):
        self.root = root
        root.title("Gacha Branch Predictor")
        root.geometry("1320x800")
        root.minsize(1000, 620)

        self.chars: list = []
        self.model = None
        self.selected = None
        self.zoom = 1.0
        self._font_zoom = None
        self._fit_cache = {}
        self._sr = (1.0, 1.0)
        self._press_xy = (0, 0)
        self._moved = False

        self.v_tapes = tk.StringVar(value="0")
        self.v_poly = tk.StringVar(value="0")
        self.v_ratio = tk.StringVar(value="160")
        self.v_guar = tk.BooleanVar(value=False)
        self.v_pity = tk.StringVar(value="90")
        self.v_win = tk.StringVar(value="50")
        self.v_item_name = tk.StringVar(value="Tapes")
        self.v_cur_name = tk.StringVar(value="Polychrome")
        self.v_new_char = tk.StringVar()
        self.v_avail = tk.StringVar()
        self.v_status = tk.StringVar(value="Fill in the left panel, add characters, then Build tree.")

        self._load_default_settings()
        self._build_ui()
        self._refresh_chars()
        self.redraw()

        for v in (self.v_tapes, self.v_poly, self.v_ratio):
            v.trace_add("write", lambda *_: self._update_avail())
        self._update_avail()
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ---- layout ----------------------------------------------------------

    def _build_ui(self):
        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True)
        left = ttk.Frame(paned, padding=10)
        right = ttk.Frame(paned, padding=(0, 10, 10, 10))
        paned.add(left, weight=0)
        paned.add(right, weight=1)
        self._build_left(left)
        self._build_right(right)

    def _field(self, parent, row, text, var):
        ttk.Label(parent, text=text).grid(row=row, column=0, sticky="w", padx=(0, 8), pady=2)
        ttk.Entry(parent, textvariable=var, width=12, justify="right").grid(
            row=row, column=1, sticky="e", pady=2)

    def _build_left(self, left):
        io = ttk.Frame(left)
        io.pack(fill="x", pady=(0, 8))
        ttk.Button(io, text="Load inputs…", command=self.load_inputs).pack(side="left", expand=True, fill="x")
        ttk.Button(io, text="Save inputs…", command=self.save_inputs).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

        cur = ttk.LabelFrame(left, text="Currency", padding=8)
        cur.pack(fill="x")
        cur.columnconfigure(1, weight=1)
        self._field(cur, 0, "Banked pull items", self.v_tapes)
        self._field(cur, 1, "Banked currency", self.v_poly)
        self._field(cur, 2, "Currency per pull", self.v_ratio)
        ttk.Label(cur, textvariable=self.v_avail, foreground="#555").grid(
            row=3, column=0, columnspan=2, sticky="w", pady=(2, 4))
        ttk.Checkbutton(cur, text="First character is guaranteed",
                        variable=self.v_guar).grid(row=4, column=0, columnspan=2, sticky="w")

        odds = ttk.LabelFrame(left, text="Odds", padding=8)
        odds.pack(fill="x", pady=(8, 0))
        odds.columnconfigure(1, weight=1)
        self._field(odds, 0, "Pulls per drop", self.v_pity)
        ttk.Label(odds, text="90 = hard pity · 75 = soft pity · 1 = lucky potion",
                  foreground="#555", wraplength=300).grid(row=1, column=0, columnspan=2, sticky="w")
        self._field(odds, 2, "50/50 win chance (%)", self.v_win)

        names = ttk.LabelFrame(left, text="Labels (optional)", padding=8)
        names.pack(fill="x", pady=(8, 0))
        names.columnconfigure(1, weight=1)
        self._field(names, 0, "Pull item name", self.v_item_name)
        self._field(names, 1, "Currency name", self.v_cur_name)

        ch = ttk.LabelFrame(left, text="Characters (in banner order)", padding=8)
        ch.pack(fill="both", expand=True, pady=(8, 0))
        ch.columnconfigure(0, weight=1)
        ch.rowconfigure(1, weight=1)

        entry = ttk.Entry(ch, textvariable=self.v_new_char)
        entry.grid(row=0, column=0, sticky="ew")
        entry.bind("<Return>", lambda _e: self.add_chars())
        ttk.Button(ch, text="Add", width=6, command=self.add_chars).grid(row=0, column=1, padx=(4, 0))

        self.lb = tk.Listbox(ch, height=8, exportselection=False, activestyle="none")
        self.lb.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        sb = ttk.Scrollbar(ch, orient="vertical", command=self.lb.yview)
        sb.grid(row=1, column=1, sticky="ns", pady=(6, 0))
        self.lb.configure(yscrollcommand=sb.set)

        btns = ttk.Frame(ch)
        btns.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        ttk.Button(btns, text="↑", width=3, command=lambda: self.move_char(-1)).pack(side="left")
        ttk.Button(btns, text="↓", width=3, command=lambda: self.move_char(1)).pack(side="left", padx=2)
        ttk.Button(btns, text="Remove", command=self.remove_char).pack(side="left", padx=(6, 2))
        ttk.Button(btns, text="Clear", command=self.clear_chars).pack(side="left")
        ttk.Label(ch, text="Tip: paste several names separated by commas.",
                  foreground="#555").grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))

        ttk.Button(left, text="Build tree", command=self.build_tree).pack(fill="x", pady=(10, 0))

    def _build_right(self, right):
        bar = ttk.Frame(right)
        bar.pack(fill="x")
        ttk.Button(bar, text="Expand all", command=self.expand_all).pack(side="left")
        ttk.Button(bar, text="Expand branch", command=self.expand_branch).pack(side="left", padx=4)
        ttk.Button(bar, text="Collapse all", command=self.collapse_all).pack(side="left")
        ttk.Button(bar, text="★ Reveal best endings", command=self.reveal_best).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Export results", command=self.export_results).pack(side="left", padx=(10, 0))
        ttk.Button(bar, text="Fit", width=5, command=self.fit).pack(side="right")
        ttk.Button(bar, text="+", width=3, command=lambda: self._zoom_by(1.25)).pack(side="right", padx=2)
        ttk.Button(bar, text="−", width=3, command=lambda: self._zoom_by(0.8)).pack(side="right")

        legend = ttk.Frame(right)
        legend.pack(fill="x", pady=(6, 6))
        for text, bg, fg in (
            (" Win ", NODE_COLORS["win"][0], "#1b5e32"),
            (" Lose 50/50 ", NODE_COLORS["lose"][0], "#8e2a20"),
            (" Skip ", NODE_COLORS["skip"][0], "#444444"),
            (" Gave up ", NODE_COLORS["stop"][0], "#8a4d0b"),
            (" ★ Best ending ", BEST_FILL, "#5c4300"),
            (" ✔ Ties most characters ", GOOD_FILL, "#5c4300"),
        ):
            tk.Label(legend, text=text, bg=bg, fg=fg).pack(side="left", padx=(0, 6))
        ttk.Label(legend, textvariable=self.v_status, foreground="#555").pack(side="left", padx=8)

        vp = ttk.PanedWindow(right, orient="vertical")
        vp.pack(fill="both", expand=True)

        cframe = ttk.Frame(vp)
        vp.add(cframe, weight=4)
        cframe.rowconfigure(0, weight=1)
        cframe.columnconfigure(0, weight=1)
        self.canvas = tk.Canvas(cframe, background=CANVAS_BG, highlightthickness=1,
                                highlightbackground="#d0d4da", xscrollincrement=20, yscrollincrement=20)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        vs = ttk.Scrollbar(cframe, orient="vertical", command=self.canvas.yview)
        vs.grid(row=0, column=1, sticky="ns")
        hs = ttk.Scrollbar(cframe, orient="horizontal", command=self.canvas.xview)
        hs.grid(row=1, column=0, sticky="ew")
        self.canvas.configure(yscrollcommand=vs.set, xscrollcommand=hs.set)

        c = self.canvas
        c.bind("<ButtonPress-1>", self._on_press)
        c.bind("<B1-Motion>", self._on_drag)
        c.bind("<ButtonRelease-1>", self._on_release)
        c.bind("<MouseWheel>", self._on_wheel)                       # Windows / macOS
        c.bind("<Button-4>", lambda e: self._on_wheel(e, 1))         # Linux
        c.bind("<Button-5>", lambda e: self._on_wheel(e, -1))
        c.bind("<Enter>", lambda _e: c.focus_set())

        det = ttk.LabelFrame(vp, text="Selected node", padding=4)
        vp.add(det, weight=1)
        det.rowconfigure(0, weight=1)
        det.columnconfigure(0, weight=1)
        self.details = tk.Text(det, height=9, wrap="word", font="TkFixedFont",
                               relief="flat", state="disabled")
        self.details.grid(row=0, column=0, sticky="nsew")
        ds = ttk.Scrollbar(det, orient="vertical", command=self.details.yview)
        ds.grid(row=0, column=1, sticky="ns")
        self.details.configure(yscrollcommand=ds.set)
        self._set_details("Build the tree, then click any node to see exactly what that branch did.")

    # ---- character list ------------------------------------------------------

    def _refresh_chars(self, select=None):
        self.lb.delete(0, "end")
        for i, name in enumerate(self.chars, 1):
            self.lb.insert("end", f"{i}. {name}")
        if select is not None and 0 <= select < len(self.chars):
            self.lb.selection_set(select)
            self.lb.see(select)

    def _selected_char(self):
        sel = self.lb.curselection()
        return sel[0] if sel else None

    def add_chars(self):
        raw = self.v_new_char.get()
        names = [n.strip() for n in raw.replace("\n", ",").split(",") if n.strip()]
        if not names:
            return
        self.chars.extend(names)
        self.v_new_char.set("")
        self._refresh_chars(select=len(self.chars) - 1)

    def remove_char(self):
        i = self._selected_char()
        if i is None:
            return
        del self.chars[i]
        self._refresh_chars(select=min(i, len(self.chars) - 1))

    def move_char(self, delta):
        i = self._selected_char()
        if i is None:
            return
        j = i + delta
        if not 0 <= j < len(self.chars):
            return
        self.chars[i], self.chars[j] = self.chars[j], self.chars[i]
        self._refresh_chars(select=j)

    def clear_chars(self):
        if self.chars and messagebox.askyesno("Clear characters", "Remove every character from the list?"):
            self.chars.clear()
            self._refresh_chars()

    # ---- inputs ------------------------------------------------------------

    @staticmethod
    def _int(var, label, lo):
        raw = var.get().strip().replace(",", "")
        try:
            val = int(raw)
        except ValueError:
            raise ValueError(f"{label} must be a whole number.")
        if val < lo:
            raise ValueError(f"{label} must be at least {lo}.")
        return val

    def _read_config(self) -> Config:
        if not self.chars:
            raise ValueError("Add at least one character.")
        tapes = self._int(self.v_tapes, "Banked pull items", 0)
        poly = self._int(self.v_poly, "Banked currency", 0)
        ratio = self._int(self.v_ratio, "Currency per pull", 1)
        pity = self._int(self.v_pity, "Pulls per drop", 1)
        try:
            win = float(self.v_win.get().strip().rstrip("%"))
        except ValueError:
            raise ValueError("50/50 win chance must be a number.")
        if not 0 <= win <= 100:
            raise ValueError("50/50 win chance must be between 0 and 100.")
        return Config(
            tapes=tapes, polychrome=poly, currency_per_pull=ratio,
            guaranteed=self.v_guar.get(), pity=pity, win_chance=win / 100,
            characters=tuple(self.chars),
            item_name=self.v_item_name.get().strip() or "Items",
            currency_name=self.v_cur_name.get().strip() or "Currency",
        )

    def _update_avail(self):
        try:
            t = int(self.v_tapes.get().strip().replace(",", "") or 0)
            p = int(self.v_poly.get().strip().replace(",", "") or 0)
            r = int(self.v_ratio.get().strip().replace(",", ""))
            if r < 1 or t < 0 or p < 0:
                raise ValueError
            self.v_avail.set(f"= {t + p // r:,} pulls available")
        except ValueError:
            self.v_avail.set("")

    # ---- building / editing the tree ------------------------------------------

    def build_tree(self):
        try:
            cfg = self._read_config()
        except ValueError as exc:
            messagebox.showerror("Check your inputs", str(exc))
            return
        self._save_settings(self.SETTINGS)

        self.model = TreeModel(cfg)
        self._built_settings = self._collect_settings()   # what the export reports
        self.selected = self.model.root
        self.zoom = 1.0
        self.redraw()
        self._scroll_to_canvas(0, 0)
        self._show_details(self.selected)

        most, _ = self.model.evaluator.best
        n = count_endings(cfg)
        haul = f"best case: {most} character{'s' if most != 1 else ''}" if most else "no character is reachable"
        self.v_status.set(f"{n:,} possible endings · {haul} · click a node to open it")

    def _after_structure_change(self):
        if self.selected and self.model:
            self.selected = self.model.visible_ancestor(self.selected)
        self.redraw()
        if self.selected:
            self._show_details(self.selected)

    def expand_all(self):
        if not self.model:
            return
        created, capped = self.model.expand_all(cap=self.EXPAND_CAP)
        self.redraw()
        self.fit()
        if capped:
            self.v_status.set(f"Opened {created:,} new nodes, then stopped — click nodes to go deeper.")
        else:
            self.v_status.set(f"Everything is open ({len(self.model.by_id):,} nodes).")

    def expand_branch(self):
        if not (self.model and self.selected):
            return
        created, capped = self.model.expand_all(start=self.selected, cap=self.EXPAND_CAP)
        self.redraw()
        self._ensure_visible(self.selected)
        if capped:
            self.v_status.set(f"Opened {created:,} new nodes, then stopped — click nodes to go deeper.")

    def collapse_all(self):
        if not self.model:
            return
        self.model.collapse_all()
        self._after_structure_change()
        self._scroll_to_canvas(0, 0)

    def reveal_best(self):
        if not self.model:
            return
        found = self.model.reveal_best()
        if not found:
            self.v_status.set("No ending gets you any character with these settings.")
            return
        self.selected = found[0]
        self.zoom = max(self.zoom, 0.8)      # keep the text readable
        self.redraw()
        self._center_on(found[0])
        self._show_details(found[0])
        self.v_status.set(f"Showing the path to {len(found)} best ending{'s' if len(found) != 1 else ''} (gold).")

    # ---- drawing ------------------------------------------------------------------

    def _ensure_fonts(self):
        if self._font_zoom == self.zoom:
            return
        fam = tkfont.nametofont("TkDefaultFont").actual("family")
        size = max(6, int(round(9 * self.zoom)))
        self.f_norm = tkfont.Font(root=self.root, family=fam, size=size)
        self.f_bold = tkfont.Font(root=self.root, family=fam, size=size, weight="bold")
        self._font_zoom = self.zoom
        self._fit_cache.clear()

    def _fit(self, text, bold, maxpx):
        key = (text, bold, int(maxpx))
        hit = self._fit_cache.get(key)
        if hit is not None:
            return hit
        font = self.f_bold if bold else self.f_norm
        out = text
        if font.measure(out) > maxpx:
            while out and font.measure(out + "…") > maxpx:
                out = out[:-1]
            out += "…"
        self._fit_cache[key] = out
        return out

    def _rrect(self, x1, y1, x2, y2, r, **kw):
        r = max(0.0, min(r, (x2 - x1) / 2, (y2 - y1) / 2))
        pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
               x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
        return self.canvas.create_polygon(pts, smooth=True, **kw)

    def redraw(self):
        c = self.canvas
        c.delete("all")
        m = self.model
        if not m:
            c.configure(scrollregion=(0, 0, 10, 10))
            c.create_text(30, 30, anchor="nw", fill="#777777",
                          text="Add your characters on the left, then press “Build tree”.")
            return

        self._ensure_fonts()
        z, pad = self.zoom, self.PAD
        ext_w, ext_h = m.layout()
        self._sr = (ext_w * z + 2 * pad, ext_h * z + 2 * pad)
        c.configure(scrollregion=(0, 0, self._sr[0], self._sr[1]))

        nodes = m.visible()
        on_path = {n.nid for n in m.path_to_root(self.selected)} if self.selected else set()

        # edges (selected path drawn last so it sits on top)
        hot = []
        for n in nodes:
            if not (n.expanded and n.kids):
                continue
            px, py = n.x * z + pad + NODE_W * z, n.y * z + pad
            for k in n.kids:
                kx, ky = k.x * z + pad, k.y * z + pad
                mid = (px + kx) / 2
                pts = (px, py, mid, py, mid, ky, kx, ky)
                if k.nid in on_path:
                    hot.append(pts)
                else:
                    c.create_line(*pts, smooth=True, fill=EDGE_COLORS[k.child.kind],
                                  width=max(1.0, 1.8 * z), tags="edge")
        for pts in hot:
            c.create_line(*pts, smooth=True, fill=SELECT_COLOR, width=max(2.0, 3.2 * z), tags="edge")

        for n in nodes:
            self._draw_node(n)

    def _node_lines(self, n, grade):
        s, cfg = n.child.state, self.model.cfg
        if n.end:
            prefix = "★ " if grade == "best" else ("✔ " if grade == "good" else "")
            return [
                (prefix + n.child.title, True, INK),
                ("Got: " + fmt_names(s.got), False, INK),
                ("Left: " + leftover_short(cfg, s), False, "#555555"),
            ]
        avail = pulls_available(cfg, s.tapes, s.poly)
        return [
            (n.child.title, True, INK),
            (n.child.sub, False, NODE_COLORS[n.child.kind][1]),
            (f"{avail:,} pulls left", False, "#666666"),
        ]

    def _draw_node(self, n):
        c, z, pad = self.canvas, self.zoom, self.PAD
        leaf = n.end is not None
        w = (LEAF_W if leaf else NODE_W) * z
        h = (LEAF_H if leaf else NODE_H) * z
        x1, yc = n.x * z + pad, n.y * z + pad
        y1, x2, y2 = yc - h / 2, x1 + w, yc + h / 2
        tag = f"N:{n.nid}"
        tags = ("node", tag)
        r = 8 * z

        fill, outline = NODE_COLORS[n.child.kind]
        width = 1.5
        grade = self.model.grade(n)
        if grade == "best":
            self._rrect(x1 - 5 * z, y1 - 5 * z, x2 + 5 * z, y2 + 5 * z, r + 4 * z,
                        fill=BEST_GLOW, outline="#ffd24d", width=2, tags=tags)
            fill, outline, width = BEST_FILL, BEST_LINE, 3
        elif grade == "good":
            fill, outline, width = GOOD_FILL, GOOD_LINE, 2.5

        if n is self.selected:
            self._rrect(x1 - 4 * z, y1 - 4 * z, x2 + 4 * z, y2 + 4 * z, r + 3 * z,
                        fill="", outline=SELECT_COLOR, width=3, tags=tags)

        self._rrect(x1, y1, x2, y2, r, fill=fill, outline=outline, width=width, tags=tags)

        if z >= 0.5:
            lines = self._node_lines(n, grade)
            maxpx = w - 16 * z - (0 if leaf else 8 * z)
            for i, (text, bold, color) in enumerate(lines):
                c.create_text(x1 + 8 * z, y1 + h * (i + 0.5) / len(lines), anchor="w",
                              text=self._fit(text, bold, maxpx), fill=color,
                              font=self.f_bold if bold else self.f_norm, tags=tags)

        if not leaf:
            br = 9 * z
            btags = tags + (f"B:{n.nid}",)
            c.create_oval(x2 - br, yc - br, x2 + br, yc + br, fill="#ffffff",
                          outline=outline, width=1.5, tags=btags)
            if z >= 0.5:
                c.create_text(x2, yc, text="−" if n.expanded else "+", fill=outline,
                              font=self.f_bold, tags=btags)

    # ---- mouse -------------------------------------------------------------------

    def _on_press(self, e):
        self.canvas.focus_set()
        self._press_xy = (e.x, e.y)
        self._moved = False
        self.canvas.scan_mark(e.x, e.y)

    def _on_drag(self, e):
        if not self._moved and abs(e.x - self._press_xy[0]) + abs(e.y - self._press_xy[1]) > 4:
            self._moved = True
        if self._moved:
            self.canvas.scan_dragto(e.x, e.y, gain=1)

    def _on_release(self, e):
        if self._moved or not self.model:
            return
        hit = self._hit(self.canvas.canvasx(e.x), self.canvas.canvasy(e.y))
        if hit is None:
            return
        kind, nid = hit
        node = self.model.by_id.get(nid)
        if node is None:
            return
        if kind == "badge":
            self.model.toggle(node)
            self._after_structure_change()
            if node.expanded:
                self._ensure_visible(node)
        else:
            self._select(node)

    def _hit(self, cx, cy):
        c = self.canvas
        for item in reversed(c.find_overlapping(cx, cy, cx, cy)):
            tags = c.gettags(item)
            for t in tags:
                if t.startswith("B:"):
                    return ("badge", int(t[2:]))
            for t in tags:
                if t.startswith("N:"):
                    return ("node", int(t[2:]))
        return None

    def _select(self, node):
        opened = False
        if not node.end and not node.expanded:
            self.model.expand(node)
            opened = True
        self.selected = node
        self.redraw()
        self._show_details(node)
        if opened:
            self._ensure_visible(node)

    def _on_wheel(self, e, direction=None):
        if direction is None:
            direction = 1 if e.delta > 0 else -1
        if e.state & 0x4:            # Ctrl
            self._zoom_by(1.15 if direction > 0 else 1 / 1.15, (e.x, e.y))
        elif e.state & 0x1:          # Shift
            self.canvas.xview_scroll(-direction * 3, "units")
        else:
            self.canvas.yview_scroll(-direction * 3, "units")

    # ---- view control -----------------------------------------------------------

    def _scroll_to_canvas(self, left, top):
        w, h = self._sr
        self.canvas.xview_moveto(max(0.0, left) / max(1.0, w))
        self.canvas.yview_moveto(max(0.0, top) / max(1.0, h))

    def _zoom_by(self, factor, focus=None):
        if not self.model:
            return
        new = min(self.MAX_ZOOM, max(self.MIN_ZOOM, self.zoom * factor))
        if abs(new - self.zoom) < 1e-6:
            return
        c = self.canvas
        if focus is None:
            focus = (c.winfo_width() / 2, c.winfo_height() / 2)
        lx = c.canvasx(focus[0]) / self.zoom
        ly = c.canvasy(focus[1]) / self.zoom
        self.zoom = new
        self.redraw()
        self._scroll_to_canvas(lx * new - focus[0], ly * new - focus[1])

    def fit(self):
        if not self.model:
            return
        ext_w, ext_h = self.model.layout()
        c = self.canvas
        c.update_idletasks()
        cw, ch = max(300, c.winfo_width()), max(200, c.winfo_height())
        z = min((cw - 2 * self.PAD) / ext_w, (ch - 2 * self.PAD) / ext_h, 1.0)
        self.zoom = max(self.MIN_ZOOM, z)
        self.redraw()
        self._scroll_to_canvas(0, 0)

    def _center_on(self, n):
        c, z = self.canvas, self.zoom
        c.update_idletasks()
        w = (LEAF_W if n.end else NODE_W) * z
        cx, cy = n.x * z + self.PAD + w / 2, n.y * z + self.PAD
        self._scroll_to_canvas(cx - c.winfo_width() / 2, cy - c.winfo_height() / 2)

    def _ensure_visible(self, n):
        """Scroll just enough to show a node and the children it just opened."""
        c, z, pad = self.canvas, self.zoom, self.PAD
        c.update_idletasks()
        cw, ch = c.winfo_width(), c.winfo_height()
        kids = n.kids or []
        x1 = n.x * z + pad
        x2 = x1 + ((NODE_W + (X_STEP - NODE_W) + LEAF_W) * z if kids else NODE_W * z)
        ys = [k.y for k in kids] + [n.y]
        y1 = min(ys) * z + pad - LEAF_H * z / 2
        y2 = max(ys) * z + pad + LEAF_H * z / 2
        left, top = c.canvasx(0), c.canvasy(0)
        new_left, new_top = left, top
        if x2 > left + cw:
            new_left = x2 - cw
        if x1 < new_left:
            new_left = x1 - 20
        if y2 > top + ch:
            new_top = y2 - ch
        if y1 < new_top:
            new_top = y1 - 20
        if (new_left, new_top) != (left, top):
            self._scroll_to_canvas(new_left, new_top)

    # ---- details panel --------------------------------------------------------------

    def _show_details(self, n):
        cfg = self.model.cfg
        text = describe(cfg, n.child.state)
        grade = self.model.grade(n)
        if grade == "best":
            text = ("★ BEST POSSIBLE ENDING — the most characters you can get, "
                    "with the most left over.\n\n") + text
        elif grade == "good":
            text = ("✔ STRONG ENDING — ties the most characters you can get "
                    "(but with less left over than the best).\n\n") + text
        self._set_details(text)

    def _set_details(self, text):
        self.details.configure(state="normal")
        self.details.delete("1.0", "end")
        self.details.insert("1.0", text)
        self.details.configure(state="disabled")

    # ---- saving / loading inputs ----------------------------------------------------

    def _collect_settings(self) -> dict:
        return {
            "tapes": self.v_tapes.get(), "polychrome": self.v_poly.get(),
            "ratio": self.v_ratio.get(), "pity": self.v_pity.get(), "win": self.v_win.get(),
            "item_name": self.v_item_name.get(), "currency_name": self.v_cur_name.get(),
            "guaranteed": self.v_guar.get(), "characters": list(self.chars),
        }

    def _apply_settings(self, data: dict):
        mapping = {
            "tapes": self.v_tapes, "polychrome": self.v_poly, "ratio": self.v_ratio,
            "pity": self.v_pity, "win": self.v_win,
            "item_name": self.v_item_name, "currency_name": self.v_cur_name,
        }
        for key, var in mapping.items():
            if key in data:
                var.set(str(data[key]))
        self.v_guar.set(bool(data.get("guaranteed", False)))
        chars = data.get("characters")
        if isinstance(chars, list):
            self.chars = [str(c) for c in chars]

    def _load_default_settings(self):
        try:
            data = json.loads(self.SETTINGS.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(data, dict):
            self._apply_settings(data)

    def _save_settings(self, path: Path) -> bool:
        try:
            Path(path).write_text(json.dumps(self._collect_settings(), indent=2), encoding="utf-8")
            return True
        except OSError:
            return False

    def load_inputs(self):
        path = filedialog.askopenfilename(
            title="Load inputs", initialdir=str(self.SETTINGS.parent),
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
        except (OSError, ValueError) as exc:
            messagebox.showerror("Couldn't load inputs", f"{Path(path).name}: {exc}")
            return
        self._apply_settings(data)
        self._refresh_chars()
        self._update_avail()
        self.v_status.set(f"Loaded inputs from {Path(path).name} — press Build tree.")

    def save_inputs(self):
        path = filedialog.asksaveasfilename(
            title="Save inputs", initialdir=str(self.SETTINGS.parent),
            initialfile="my_banners.json", defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        if self._save_settings(Path(path)):
            self.v_status.set(f"Saved inputs to {Path(path).name}.")
        else:
            messagebox.showerror("Couldn't save", f"Could not write {path}.")

    def export_results(self):
        """Write settings + results to the next free result.json (only when asked)."""
        if not self.model:
            messagebox.showinfo("Nothing to export", "Build a tree first, then export.")
            return
        data = dict(self._built_settings)            # same fields as the settings file, on top
        opened = [n.child.state for n in self.model.visible() if n.end]
        data["results"] = build_results(self.model.cfg, self.model.evaluator, opened)

        for folder in (self.SETTINGS.parent, Path.home()):
            path = next_free_path(folder)
            try:
                path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
            except OSError:
                continue
            res = data["results"]
            note = "" if res["endings_included"] == "all" else \
                " (tree is huge, so only the endings opened in the graph were included)"
            self.v_status.set(f"Exported {len(res['endings']):,} endings to {path.name}{note}.")
            messagebox.showinfo("Results exported", f"Saved to:\n{path}{note and chr(10) + note.strip()}")
            return
        messagebox.showerror("Couldn't export", "Could not write result.json to the app folder or your home folder.")

    def _on_close(self):
        self._save_settings(self.SETTINGS)
        self.root.destroy()


def main():
    if tk is None:
        raise SystemExit("tkinter isn't available in this Python install. "
                         "On Debian/Ubuntu: sudo apt install python3-tk")
    root = tk.Tk()
    if sys.platform.startswith("linux"):
        try:
            ttk.Style().theme_use("clam")
        except tk.TclError:
            pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
