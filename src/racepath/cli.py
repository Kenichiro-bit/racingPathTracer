# -*- coding: utf-8 -*-
"""
racepath - transistor-level race path finder with automatic probe selection.

Finds pairs of signal paths that start from a common launch net (e.g. CLK),
split, and reconverge on the same channel-connected block (CCC), classifies
the race (SAE vs BL, WL vs precharge, latch hold, clock pair, generic),
picks the internal nodes worth probing, and writes .measure statements,
PVT-corner decks and a result summary.

Subcommands
  analyze  NETLIST              find races, write races.json/.csv/.meas + report.html
  decks    RACES_JSON CORNERS   write one simulation deck per PVT corner
  collect  RACES_JSON RESULTS.. gather .measure results into a margin table

Only the Python standard library is required (PyYAML is optional for YAML config).
"""
from __future__ import annotations

import argparse
import csv
import fnmatch
import glob
import html
import json
import os
import re
import sys
from collections import Counter, defaultdict, deque
from itertools import combinations

from racepath import __version__ as VERSION

DEFAULT_CFG = {
    # net-name globs treated as supplies (case-insensitive, matched on leaf name)
    "supplies": ["VDD*", "VSS*", "GND*", "VCC*", "VPWR*", "VGND*", "VPP*", "VBB*",
                 "AVDD*", "AVSS*", "DVDD*", "DVSS*", "0"],
    # supplies that are "high" (used for deck generation); others are low
    "high_supplies": ["VDD*", "VCC*", "VPWR*", "VPP*", "AVDD*", "DVDD*"],
    "pmos_models": r"^p|pch|pmos|pfet",
    "nmos_models": r"^n|nch|nmos|nfet",
    "sources": [],              # launch nets; empty = input ports (PININFO) or all ports
    "static_inputs": [],        # globs; pins held constant (test mode, scan enable...): never launch nets
    "hier_sep": ".",
    "probe_prefix": "XDUT.",    # instance path of the DUT in the testbench
    "merge_resistors": True,    # treat resistors as shorts (post-layout netlists)
    "ignore_nets": [],          # globs; races touching these nets are dropped
    "extra_checks": [],         # manual checks: {"class": "...", "a": net, "b": net, ...}
    "max_races": 2000,
    "keep_bus_pairs": False,    # pair WL<0> with WL<1> etc. (normally decoded, so off)
    # measurement parameters (written into decks; referenced by name in .meas)
    "vdd": 0.8,
    "t_m": "1n",                # measurement window start (launch edge time)
    "th_mid": 0.5,              # logic threshold, fraction of VDD
    "th_pre": 0.9,              # precharge PMOS gate "off" threshold, fraction of VDD
    "sa_dv_req": 0.05,          # required SA input split [V]
}

CLASS_INFO = {
    "sae_bl":     ("SAE vs BL振幅", "SAE到達時のSA入力差電圧 - 必要差電圧"),
    "wl_pre":     ("WL vs プリチャージ", "WL立上り - プリチャージOFF時刻(開始側) / プリチャージON - WL立下り(終了側)"),
    "hold":       ("ラッチ ホールド", "データ到達 - クロック閉じ時刻"),
    "clock_pair": ("クロック対(重なり)", "2入力の到達時間差(スキュー)"),
    "generic":    ("汎用競合", "2入力の到達時間差"),
}
CLASS_ORDER = ["sae_bl", "wl_pre", "hold", "clock_pair", "generic"]


# --------------------------------------------------------------------------- config

def load_cfg(path=None, overrides=None):
    cfg = json.loads(json.dumps(DEFAULT_CFG))
    if path:
        with open(path, encoding="utf-8") as f:
            txt = f.read()
        if path.lower().endswith((".yaml", ".yml")):
            import yaml  # optional dependency
            user = yaml.safe_load(txt) or {}
        else:
            user = json.loads(txt)
        cfg.update(user)
    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})
    return cfg


def glob_match(name, patterns):
    n = name.lower()
    return any(fnmatch.fnmatchcase(n, p.lower()) for p in patterns)


# --------------------------------------------------------------------------- parsing

class Elem:
    __slots__ = ("name", "nodes", "master", "hint", "pins", "line")

    def __init__(self, name, nodes, master, hint, pins=None, line=0):
        self.name, self.nodes, self.master, self.hint = name, nodes, master, hint
        self.pins, self.line = pins, line


class Subckt:
    def __init__(self, name, ports):
        self.name, self.ports = name, ports
        self.elems: list[Elem] = []
        self.pininfo: dict[str, str] = {}


class ParseState:
    def __init__(self):
        self.subckts: dict[str, Subckt] = {}
        self.main = Subckt("__main__", [])
        self.cur: Subckt | None = None
        self.globals: set[str] = set()
        self.warnings: list[str] = []

    @property
    def owner(self):
        return self.cur if self.cur is not None else self.main

    def warn(self, msg):
        if len(self.warnings) < 200:
            self.warnings.append(msg)


SPECTRE_HINT = re.compile(r"^\s*(simulator\s+lang\s*=\s*spectre|subckt\s|inline\s+subckt\s|parameters\s)",
                          re.I | re.M)
SPECTRE_SKIP = {"parameters", "include", "ahdl_include", "model", "statistics", "library",
                "endlibrary", "section", "endsection", "save", "options", "real", "if", "else",
                "ic", "nodeset", "simulator", "export", "paramset", "check", "checklimit",
                "info", "alter", "altergroup"}


def detect_format(path, raw_lines, fmt):
    if fmt in ("cdl", "spice", "hspice"):
        return "spice"
    if fmt == "spectre":
        return "spectre"
    if path.lower().endswith((".scs", ".spectre")):
        return "spectre"
    head = "\n".join(raw_lines[:5000])
    return "spectre" if SPECTRE_HINT.search(head) else "spice"


def logical_lines(path, fmt):
    with open(path, encoding="utf-8", errors="replace") as f:
        raw = f.read().splitlines()
    lang = detect_format(path, raw, fmt)
    out = []  # [lang, text, lineno]
    cont = False
    for i, line in enumerate(raw, 1):
        st = line.strip()
        m = re.match(r"^simulator\s+lang\s*=\s*(\w+)", st, re.I)
        if m:
            lang = "spectre" if m.group(1).lower() == "spectre" else "spice"
            cont = False
            continue
        if lang == "spice":
            if not st:
                continue
            if st.startswith("*"):
                if st.upper().startswith("*.PININFO"):
                    out.append(["spice", st, i])
                continue
            if st.startswith("+") and out and out[-1][0] == "spice":
                out[-1][1] += " " + st[1:]
                continue
            out.append(["spice", st, i])
        else:
            if st.startswith("*"):
                continue
            s2 = re.sub(r"//.*$", "", st).rstrip()
            if not s2:
                continue
            joined = s2.endswith("\\")
            if joined:
                s2 = s2[:-1].rstrip()
            if cont and out:
                out[-1][1] += " " + s2
            else:
                out.append(["spectre", s2, i])
            cont = joined
    return out, lang


def _norm_eq(text):
    return re.sub(r"\s*=\s*", "=", text)


def parse_spice_line(text, ln, st: ParseState):
    if text.upper().startswith("*.PININFO"):
        if st.cur is not None:
            for tok in text.split()[1:]:
                if ":" in tok:
                    n, d = tok.rsplit(":", 1)
                    st.cur.pininfo[n] = d.upper()
        return
    pins = None
    m = re.search(r"\$PINS\b(.*)$", text, re.I)
    if m:
        pins = dict(kv.split("=", 1) for kv in _norm_eq(m.group(1)).split() if "=" in kv)
        text = text[:m.start()]
    mm = re.search(r"\$\[([^\]]+)\]", text)
    bracket_model = mm.group(1) if mm else None
    text = re.sub(r"\$\[[^\]]*\]", " ", text)
    text = re.split(r"(?:^|\s)\$|;", text, maxsplit=1)[0]
    text = _norm_eq(text)
    toks = text.split()
    if not toks:
        return
    t0 = toks[0]
    k = t0.lower()
    if k.startswith("."):
        if k == ".subckt" and len(toks) >= 2:
            ports = [t for t in toks[2:] if "=" not in t and t.lower() != "params:"]
            sub = Subckt(toks[1], ports)
            st.subckts[toks[1].lower()] = sub
            st.cur = sub
        elif k == ".ends":
            st.cur = None
        elif k == ".global":
            st.globals.update(toks[1:])
        elif k in (".include", ".inc", ".lib"):
            st.warn(f"line {ln}: {k} is not followed (pass a flattened netlist or concatenate files)")
        return
    c = k[0]
    pos = [t for t in toks[1:] if "=" not in t]
    if c == "m":
        if bracket_model and len(pos) >= 4:
            nodes, model = pos[:4], bracket_model
        elif len(pos) >= 5:
            nodes, model = pos[:4], pos[4]
        else:
            st.warn(f"line {ln}: cannot parse MOS '{text[:60]}'")
            return
        st.owner.elems.append(Elem(t0, nodes, model, "m", line=ln))
    elif c == "x":
        master = None
        nodes = []
        for j, t in enumerate(pos):
            if t == "/":
                master = pos[j + 1] if j + 1 < len(pos) else None
                break
            if t.startswith("/") and len(t) > 1:
                master = t[1:]
                break
            nodes.append(t)
        if master is None:
            if not pos:
                return
            nodes, master = pos[:-1], pos[-1]
        st.owner.elems.append(Elem(t0, nodes, master, "x", pins=pins, line=ln))
    elif c == "r":
        if len(pos) >= 2:
            st.owner.elems.append(Elem(t0, pos[:2], bracket_model or "r", "r", line=ln))


def parse_spectre_line(text, ln, st: ParseState):
    text = _norm_eq(text)
    m = re.match(r"^(inline\s+)?subckt\s+(\S+)\s*(.*)$", text, re.I)
    if m:
        ports = m.group(3).replace("(", " ").replace(")", " ").split()
        ports = [p for p in ports if "=" not in p]
        sub = Subckt(m.group(2), ports)
        st.subckts[m.group(2).lower()] = sub
        st.cur = sub
        return
    if re.match(r"^ends\b", text, re.I):
        st.cur = None
        return
    m = re.match(r"^global\s+(.*)$", text, re.I)
    if m:
        st.globals.update(m.group(1).replace("(", " ").replace(")", " ").split())
        return
    first = text.split()[0].lower()
    if first in SPECTRE_SKIP:
        return
    m = re.match(r"^(\S+?)\s*\(([^)]*)\)\s*(\S+)\s*(.*)$", text)
    if m:
        name, nodes, master = m.group(1), m.group(2).split(), m.group(3)
    else:
        pos = [t for t in text.split() if "=" not in t]
        if len(pos) < 3:
            return  # analysis / option statements
        name, nodes, master = pos[0], pos[1:-1], pos[-1]
    hint = "r" if master.lower() == "resistor" else "s"
    st.owner.elems.append(Elem(name, nodes, master, hint, line=ln))


def parse_netlist(path, fmt="auto"):
    st = ParseState()
    lines, lang = logical_lines(path, fmt)
    for lg, text, ln in lines:
        try:
            if lg == "spice":
                parse_spice_line(text, ln, st)
            else:
                parse_spectre_line(text, ln, st)
        except Exception as e:  # keep going on odd lines
            st.warn(f"line {ln}: parse error {e!r}")
    st.detected_format = lang
    return st


# --------------------------------------------------------------------------- flatten

class UnionFind:
    def __init__(self, prefer=None):
        self.p = {}
        self.prefer = prefer  # function(a, b) -> preferred root

    def find(self, x):
        p = self.p
        if x not in p:
            p[x] = x
            return x
        root = x
        while p[root] != root:
            root = p[root]
        while p[x] != root:
            p[x], x = root, p[x]
        return root

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return ra
        keep = self.prefer(ra, rb) if self.prefer else ra
        drop = rb if keep == ra else ra
        self.p[drop] = keep
        return keep


class FlatNetlist:
    def __init__(self):
        self.mos = []        # dict(name, type, d, g, s, b, model)
        self.top = None
        self.top_ports = []
        self.pininfo = {}
        self.supplies = set()
        self.nets = set()
        self.warnings = []


def flatten(st: ParseState, cfg, top=None):
    sep = cfg["hier_sep"]
    subs = st.subckts
    gl = set(st.globals)
    sup_pats = cfg["supplies"]
    pre_p = re.compile(cfg["pmos_models"], re.I)
    pre_n = re.compile(cfg["nmos_models"], re.I)
    fn = FlatNetlist()
    unknown_models = Counter()

    def mos_type(model):
        if pre_p.search(model):
            return "p"
        if pre_n.search(model):
            return "n"
        return None

    def is_supply_local(n):
        return glob_match(n, sup_pats)

    # choose top
    if top:
        t = subs.get(top.lower())
        if t is None:
            raise SystemExit(f"error: top subckt '{top}' not found")
    elif any(e.hint in ("x", "m", "s") and (e.master or "").lower() in subs or e.hint == "m"
             for e in st.main.elems):
        t = st.main
    else:
        inst = {(e.master or "").lower() for s in subs.values() for e in s.elems}
        cands = [s for k, s in subs.items() if k not in inst]
        if not cands:
            raise SystemExit("error: cannot determine top cell; use --top")

        def size(s, depth=0, memo={}):
            if s.name in memo:
                return memo[s.name]
            if depth > 64:
                return 0
            n = 0
            for e in s.elems:
                ch = subs.get((e.master or "").lower())
                n += size(ch, depth + 1) if ch is not None and e.hint != "m" else 1
            memo[s.name] = n
            return n
        cands.sort(key=lambda s: -size(s))
        t = cands[0]
        if len(cands) > 1:
            fn.warnings.append(f"several top candidates ({', '.join(c.name for c in cands[:5])}); "
                               f"using '{t.name}'. Use --top to choose.")
    fn.top = t.name
    fn.top_ports = list(t.ports)
    fn.pininfo = dict(t.pininfo)

    def node_name(local, prefix, nmap):
        if local in nmap:
            return nmap[local]
        if local in gl or local == "0" or is_supply_local(local):
            return local
        return prefix + local

    res_pairs = []

    def walk(sub, prefix, nmap, depth):
        if depth > 64:
            fn.warnings.append(f"hierarchy too deep at {prefix}")
            return
        for e in sub.elems:
            master_l = (e.master or "").lower()
            child = subs.get(master_l) if e.hint in ("x", "s") else None
            if child is not None:
                if e.pins:
                    pins_l = {k.lower(): v for k, v in e.pins.items()}
                    cmap = {p: node_name(pins_l.get(p.lower(), p), prefix, nmap) for p in child.ports}
                else:
                    if len(e.nodes) != len(child.ports):
                        fn.warnings.append(f"{prefix}{e.name}: {len(e.nodes)} nodes vs "
                                           f"{len(child.ports)} ports of {child.name}")
                    cmap = {p: node_name(n, prefix, nmap) for p, n in zip(child.ports, e.nodes)}
                walk(child, prefix + e.name + sep, cmap, depth + 1)
                continue
            if e.hint == "r" or master_l == "resistor":
                if len(e.nodes) >= 2:
                    res_pairs.append((node_name(e.nodes[0], prefix, nmap),
                                      node_name(e.nodes[1], prefix, nmap)))
                continue
            if e.hint == "m" or (len(e.nodes) in (3, 4) and e.hint in ("x", "s")):
                ty = mos_type(e.master or "")
                if ty is None:
                    if e.hint == "m" or e.hint == "x":
                        unknown_models[e.master] += 1
                    continue
                nodes = [node_name(n, prefix, nmap) for n in e.nodes]
                if len(nodes) == 3:
                    nodes.append(nodes[2])
                d, g, s, b = nodes[:4]
                fn.mos.append({"name": prefix + e.name, "type": ty, "d": d, "g": g, "s": s,
                               "b": b, "model": e.master})

    top_map = {p: p for p in t.ports}
    walk(t, "", top_map, 0)

    for m, c in unknown_models.most_common(10):
        fn.warnings.append(f"model '{m}' not recognised as MOS ({c} instances) - "
                           f"check pmos_models/nmos_models or subckt definitions")

    # supplies
    all_nets = set()
    for m in fn.mos:
        all_nets.update((m["d"], m["g"], m["s"]))
    for a, b in res_pairs:
        all_nets.update((a, b))

    def is_sup(n):
        leaf = n.rsplit(sep, 1)[-1]
        return n == "0" or n in gl and is_supply_local(n) or is_supply_local(leaf)

    sup = {n for n in all_nets if is_sup(n)}

    if cfg.get("merge_resistors", True) and res_pairs:
        def prefer(a, b):
            if (a in sup) != (b in sup):
                return a if a in sup else b
            return a if (len(a), a) <= (len(b), b) else b
        uf = UnionFind(prefer)
        for a, b in res_pairs:
            uf.union(a, b)
        for m in fn.mos:
            for k in ("d", "g", "s", "b"):
                m[k] = uf.find(m[k])
        all_nets = {uf.find(n) for n in all_nets}
        sup = {n for n in all_nets if n in sup or is_sup(n)}
    fn.supplies = sup
    fn.nets = all_nets
    fn.warnings = st.warnings + fn.warnings
    return fn


# --------------------------------------------------------------------------- graph

class CCCGraph:
    """Channel-connected-component graph over a flat transistor list."""

    def __init__(self, fn: FlatNetlist):
        self.fn = fn
        self.sup = fn.supplies
        uf = UnionFind()
        for m in fn.mos:
            d, s = m["d"], m["s"]
            ds, ss = d in self.sup, s in self.sup
            if not ds and not ss:
                uf.union(d, s)
            elif not ds:
                uf.find(d)
            elif not ss:
                uf.find(s)
        roots = sorted({uf.find(n) for n in uf.p})
        rid = {r: i for i, r in enumerate(roots)}
        self.n_ccc = len(roots)
        self.members = [set() for _ in roots]
        self.devs = [[] for _ in roots]
        self.ccc_of = {}
        for n in uf.p:
            c = rid[uf.find(n)]
            self.members[c].add(n)
            self.ccc_of[n] = c
        for m in fn.mos:
            ch = m["d"] if m["d"] not in self.sup else (m["s"] if m["s"] not in self.sup else None)
            if ch is None:
                m["ccc"] = None
                continue
            c = self.ccc_of[ch]
            m["ccc"] = c
            self.devs[c].append(m)
        self.inputs = [set() for _ in roots]
        fan = defaultdict(set)
        for c, devs in enumerate(self.devs):
            for m in devs:
                g = m["g"]
                if g in self.sup or g in self.members[c]:
                    continue
                self.inputs[c].add(g)
                fan[g].add(c)
        self.fanout = {g: sorted(cs) for g, cs in fan.items()}
        self.members_sorted = [sorted(ms, key=lambda x: (len(x), x)) for ms in self.members]
        self._sa_cache = {}
        self._fb_cache = {}

    # ---- traversal
    def bfs(self, src):
        dist_n = {src: 0}
        pred_n = {src: None}
        pred_c = {}
        q = deque([src])
        while q:
            n = q.popleft()
            for c in self.fanout.get(n, ()):
                if c in pred_c:
                    continue
                pred_c[c] = n
                d = dist_n[n] + 1
                for mnet in self.members_sorted[c]:
                    if mnet not in dist_n:
                        dist_n[mnet] = d
                        pred_n[mnet] = c
                        q.append(mnet)
        return dist_n, pred_n, pred_c

    @staticmethod
    def path(net, pred_n, pred_c):
        nets, cccs = [net], []
        cur = net
        guard = 0
        while pred_n.get(cur) is not None and guard < 100000:
            c = pred_n[cur]
            cccs.append(c)
            cur = pred_c[c]
            nets.append(cur)
            guard += 1
        nets.reverse()
        cccs.reverse()
        return nets, cccs

    # ---- structure helpers
    def is_sup(self, n):
        return n in self.sup

    def input_role(self, c, g):
        devs = [m for m in self.devs[c] if m["g"] == g]
        pull = [m for m in devs if m["d"] in self.sup or m["s"] in self.sup]
        return {
            "n_dev": len(devs),
            "pass_only": len(devs) > 0 and not pull,
            "drives": bool(pull),
            "precharge": bool(devs) and all(m["type"] == "p" for m in devs) and bool(pull),
            "types": "".join(sorted({m["type"] for m in devs})),
        }

    def sense_amps(self, c):
        if c in self._sa_cache:
            return self._sa_cache[c]
        devs, mem = self.devs[c], self.members[c]
        found, seen = [], set()
        for ty in ("n", "p"):
            ds = [m for m in devs if m["type"] == ty and m["g"] in mem]
            if len(ds) > 400:  # very large CCC (array column): restrict to non-cell pairs
                ds = [m for m in ds if m["s"] not in self.sup and m["d"] not in self.sup]
            by_term = defaultdict(list)
            for m in ds:
                by_term[m["d"]].append(m)
                by_term[m["s"]].append(m)
            for i in ds:
                ti = {i["d"], i["s"]}
                for j in by_term.get(i["g"], ()):
                    if j is i or j["g"] == i["g"] or j["g"] not in ti:
                        continue
                    tj = {j["d"], j["s"]}
                    for cn in (ti & tj):
                        if cn in self.sup:
                            continue
                        for t in devs:
                            if t is i or t is j or cn not in (t["d"], t["s"]):
                                continue
                            other = t["s"] if t["d"] == cn else t["d"]
                            if other in self.sup and t["g"] not in mem:
                                key = (t["g"], frozenset((i["g"], j["g"])))
                                if key in seen:
                                    continue
                                seen.add(key)
                                found.append({"enable": t["g"], "nodes": (j["g"], i["g"]),
                                              "tail": cn, "tail_type": t["type"]})
        self._sa_cache[c] = found
        return found

    def neighbour_via_pass(self, c, node):
        """Nets connected to `node` through a pass device gated by an input of the CCC."""
        out = []
        for m in self.devs[c]:
            if node in (m["d"], m["s"]) and m["g"] not in self.members[c]:
                other = m["s"] if m["d"] == node else m["d"]
                if other not in self.sup and other != node:
                    out.append((other, m["g"]))
        return out

    def driven_node(self, c, g):
        """Member net driven by a supply-connected device gated by g (e.g. data driver output)."""
        for m in self.devs[c]:
            if m["g"] != g:
                continue
            if m["s"] in self.sup and m["d"] not in self.sup:
                return m["d"]
            if m["d"] in self.sup and m["s"] not in self.sup:
                return m["s"]
        return None

    def label(self, c, sep):
        devs = self.devs[c]
        names = [m["name"] for m in devs]
        pref = os.path.commonprefix(names)
        if sep in pref:
            pref = pref[:pref.rfind(sep) + 1]
        else:
            pref = ""
        nets = self.members_sorted[c][:4]
        more = "…" if len(self.members[c]) > 4 else ""
        where = pref.rstrip(sep) or "(top)"
        return f"CCC#{c} [{where}] {len(devs)}Tr: {', '.join(nets)}{more}"


# --------------------------------------------------------------------------- race search

def resolve_net(name, nets):
    if name in nets:
        return name
    low = {n.lower(): n for n in nets}
    return low.get(name.lower())


def default_sources(fn: FlatNetlist, G: CCCGraph):
    ins = [p for p, d in fn.pininfo.items() if d.startswith("I") and p not in G.sup]
    if ins:
        return ins
    # no PININFO: ports that are not driven by any transistor channel are inputs
    ins = [p for p in fn.top_ports if p not in G.sup and p not in G.ccc_of]
    return ins or [p for p in fn.top_ports if p not in G.sup]


_IDX = re.compile(r"<[^>]*>|\[[^\]]*\]|\d+")
_BUS = re.compile(r"\\?<[^>]*>|\[[^\]]*\]")


def bus_base(s):
    """'WL<12>' -> 'WL<#>' ; used to treat bus siblings as one decoded signal."""
    return _BUS.sub("<#>", s)


def _norm(s):
    return _IDX.sub("#", s)


def group_races(races):
    """Collapse structurally identical races (same class, same inputs modulo indices)."""
    groups = {}
    order = []
    for r in races:
        cl = re.sub(r"^CCC#\d+ ", "", r["ccc_label"])
        sig = (r["class"], _norm(r["a"]), _norm(r["b"]), _norm(cl.split(":")[0]),
               r["stages_a"], r["stages_b"])
        if sig not in groups:
            groups[sig] = r
            r["group_size"] = 1
            r["group_members"] = []
            r["_pins"] = list(r["sources"])
            order.append(r)
        else:
            g = groups[sig]
            g["group_size"] += 1
            g["_pins"] += [s for s in r["sources"] if s not in g["_pins"]]
            if len(g["group_members"]) < 50:
                g["group_members"].append({"ccc": r["ccc_label"], "a": r["a"], "b": r["b"],
                                           "probes": r["probes"]})
    return order


def stage_parity_note(n):
    return "同相" if n % 2 == 0 else "逆相"


def stage_summary(r):
    """Console wording: stages counted from the divergence point, e.g.
    '分岐点 CLKBUF から SAE 6段 / WL0 2段(差4段)'."""
    if r.get("manual"):
        return f"{r['a']} / {r['b']}(手動)"

    def one(net, n):
        return f"{net} {n}段" + ("(分岐点そのもの)" if n == 0 else "")
    return (f"分岐点 {r['divergence']} から {one(r['a'], r['stages_a'])} / {one(r['b'], r['stages_b'])}"
            f"(差{r['stage_diff']}段)")


def classify(G: CCCGraph, T, a, b, has_fb):
    ra, rb = G.input_role(T, a), G.input_role(T, b)
    sas = G.sense_amps(T)
    sa_by_en = {s["enable"]: s for s in sas}
    probes = {}
    cls = "generic"
    if a in sa_by_en and rb["pass_only"] or b in sa_by_en and ra["pass_only"]:
        en, other = (a, b) if a in sa_by_en and rb["pass_only"] else (b, a)
        sa = sa_by_en[en]
        cls = "sae_bl"
        n1, n2 = sa["nodes"]
        probes = {"sae": en, "wl": other, "sa_t": n1, "sa_c": n2}
        bl1 = [x for x, _g in G.neighbour_via_pass(T, n1)]
        bl2 = [x for x, _g in G.neighbour_via_pass(T, n2)]
        if bl1 and bl2:
            probes["bl_t"], probes["bl_c"] = bl1[0], bl2[0]
    elif has_fb and (ra["pass_only"] and rb["drives"] or rb["pass_only"] and ra["drives"]):
        clk, dat = (a, b) if ra["pass_only"] else (b, a)
        cls = "hold"
        probes = {"clk": clk, "data_in": dat}
        dn = G.driven_node(T, dat)
        if dn:
            probes["data_node"] = dn
    elif ra["precharge"] and rb["pass_only"] or rb["precharge"] and ra["pass_only"]:
        pre, wl = (a, b) if ra["precharge"] else (b, a)
        cls = "wl_pre"
        probes = {"pre": pre, "wl": wl}
    elif ra["pass_only"] and rb["pass_only"]:
        cls = "clock_pair"
        probes = {"a": a, "b": b}
    else:
        probes = {"a": a, "b": b}
    return cls, probes, ra, rb


def find_races(G: CCCGraph, fn: FlatNetlist, sources, cfg, log=print):
    races = {}
    ign = cfg.get("ignore_nets") or []
    nets_all = set(G.fanout) | set(G.ccc_of)
    for s0 in sources:
        s = resolve_net(s0, nets_all)
        if s is None:
            log(f"  warning: source '{s0}' not found in netlist")
            continue
        dist_n, pred_n, pred_c = G.bfs(s)
        for T in sorted(pred_c):
            ins = sorted(g for g in G.inputs[T] if g in dist_n)
            if len(ins) < 2:
                continue
            paths, fb = {}, set()
            for g in ins:
                nets, cccs = G.path(g, pred_n, pred_c)
                if T in cccs:
                    fb.add(g)
                else:
                    paths[g] = (nets, cccs)
            live = sorted(paths)
            if not cfg.get("keep_bus_pairs"):
                # bus siblings (WL<0>, WL<1>, ...) are decoded / mutually exclusive:
                # keep one representative per bus so they are not paired with each other
                seen_b, reps = set(), []
                for g in live:
                    bb = bus_base(g)
                    if bb in seen_b:
                        continue
                    seen_b.add(bb)
                    reps.append(g)
                live = reps
            if len(live) < 2:
                continue
            # structural feedback: any input of T reachable from T itself
            if T not in G._fb_cache:
                G._fb_cache[T] = bool(fb) or _has_structural_fb(G, T)
            has_fb = G._fb_cache[T]
            for a, b in combinations(live, 2):
                if ign and (glob_match(a, ign) or glob_match(b, ign)):
                    continue
                key = (T, a, b)
                if key in races:
                    if s not in races[key]["sources"]:
                        races[key]["sources"].append(s)
                    continue
                races[key] = build_race(G, T, a, b, paths[a], paths[b], has_fb, s, cfg)
    out = list(races.values())
    # tightest races first (smallest stage difference); classes are not used for ordering
    out.sort(key=lambda r: (r["stage_diff"], r["ccc"], r["a"], r["b"]))
    if len(out) > cfg["max_races"]:
        log(f"  note: {len(out)} races found, keeping the first {cfg['max_races']} (max_races)")
        out = out[:cfg["max_races"]]
    return out


def _has_structural_fb(G: CCCGraph, T, limit=6):
    """True if some input of T is driven (within `limit` stages) from T's own nets."""
    targets = G.inputs[T]
    frontier = set(G.members[T])
    seen_c = {T}
    for _ in range(limit):
        nxt = set()
        for n in frontier:
            for c in G.fanout.get(n, ()):
                if c in seen_c:
                    continue
                seen_c.add(c)
                if G.members[c] & targets:
                    return True
                nxt |= G.members[c]
        frontier = nxt
        if not frontier:
            break
    return False


def build_race(G, T, a, b, pa, pb, has_fb, src, cfg):
    nets_a, _ = pa
    nets_b, _ = pb
    k = 0
    while k < min(len(nets_a), len(nets_b)) and nets_a[k] == nets_b[k]:
        k += 1
    div = nets_a[k - 1] if k > 0 else src
    br_a = nets_a[k - 1:] if k > 0 else nets_a
    br_b = nets_b[k - 1:] if k > 0 else nets_b
    sa_, sb_ = len(br_a) - 1, len(br_b) - 1
    cls, probes, ra, rb = classify(G, T, a, b, has_fb)
    derived = (b in nets_a) or (a in nets_b)
    return {
        "class": cls,
        "ccc": T,
        "ccc_label": G.label(T, cfg["hier_sep"]),
        "a": a, "b": b,
        "role_a": ra, "role_b": rb,
        "divergence": div,
        "path_a": br_a, "path_b": br_b,
        "full_path_a": nets_a, "full_path_b": nets_b,
        "stages_a": sa_, "stages_b": sb_,
        "stage_diff": abs(sa_ - sb_),
        "edge_a": stage_parity_note(len(nets_a) - 1),
        "edge_b": stage_parity_note(len(nets_b) - 1),
        "derived": derived,
        "has_feedback": has_fb,
        "sources": [src],
        "probes": probes,
    }


def add_extra_checks(races, cfg, nets_all, log=print):
    for i, chk in enumerate(cfg.get("extra_checks") or []):
        cls = chk.get("class", "generic")
        probes = {}
        ok = True
        for k, v in chk.items():
            if k in ("class", "name"):
                continue
            n = resolve_net(v, nets_all)
            if n is None:
                log(f"  warning: extra_checks[{i}]: net '{v}' not found")
                ok = False
            probes[k] = n
        if not ok:
            continue
        races.append({
            "class": cls, "ccc": -1, "ccc_label": f"manual: {chk.get('name', f'check{i}')}",
            "a": probes.get("a") or next(iter(probes.values())),
            "b": probes.get("b") or list(probes.values())[-1],
            "role_a": {}, "role_b": {}, "divergence": "", "path_a": [], "path_b": [],
            "full_path_a": [], "full_path_b": [], "stages_a": 0, "stages_b": 0,
            "stage_diff": 0, "edge_a": "", "edge_b": "", "derived": False,
            "has_feedback": False, "sources": [], "probes": probes, "manual": True,
        })


# --------------------------------------------------------------------------- measures

def _v(net, prefix, sup):
    net_out = net.replace("\\", "")  # Spectre-escaped bus names BL\<0\> -> BL<0>
    if net in sup:
        return f"v({net_out})"
    return f"v({prefix}{net_out})"


def measure_lines(r, prefix, sup):
    rid = r["id"]
    p = r["probes"]
    V = lambda n: _v(n, prefix, sup)
    mid = "'th_mid*vdd_m'"
    L = [f"* [{rid}] " + ("手動の測定" if r.get("manual") else "入力端子 " + ", ".join(r.get("pins", [])))
         + f" : {r['ccc_label']}"]
    if r.get("manual"):
        L.append("*   " + ", ".join(f"{k}={v}" for k, v in p.items()))
    else:
        src = r["sources"][0] if r.get("sources") else "起点"

        def one(net, n, edge):
            if n == 0:
                return f"{net} は分岐点そのもの(0段、{src} と{edge})"
            return f"{net} まで{n}段({src} と{edge})"
        L.append(f"*   分岐点 {r['divergence']} から {one(r['a'], r['stages_a'], r['edge_a'])}、"
                 f"{one(r['b'], r['stages_b'], r['edge_b'])}、差{r['stage_diff']}段")
        if r.get("explain"):
            L.append(f"*   NG条件: {r['explain']}")
        if r.get("group_size", 1) > 1:
            L.append(f"*   同型の競合 {r['group_size']}件の代表 (全件は races.json の group_members / --no-group)")
    cls = r["class"]
    if cls == "sae_bl":
        L += [f".measure tran {rid}_tsae when {V(p['sae'])}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_twl when {V(p['wl'])}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_vt find {V(p['sa_t'])} at={rid}_tsae",
              f".measure tran {rid}_vc find {V(p['sa_c'])} at={rid}_tsae",
              f".measure tran {rid}_dv param='abs({rid}_vt-{rid}_vc)'",
              f".measure tran {rid}_margin param='{rid}_dv-sa_dv_req'",
              f".measure tran {rid}_dt_wl_sae param='{rid}_tsae-{rid}_twl'"]
        if "bl_t" in p:
            L += [f".measure tran {rid}_blt find {V(p['bl_t'])} at={rid}_tsae",
                  f".measure tran {rid}_blc find {V(p['bl_c'])} at={rid}_tsae",
                  f".measure tran {rid}_dvbl param='abs({rid}_blt-{rid}_blc)'"]
    elif cls == "wl_pre":
        pre_th = "'th_pre*vdd_m'"
        L += [f".measure tran {rid}_tpre_off when {V(p['pre'])}={pre_th} cross=1 td='t_m'",
              f".measure tran {rid}_twl_on when {V(p['wl'])}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_margin param='{rid}_twl_on-{rid}_tpre_off'",
              f".measure tran {rid}_twl_off when {V(p['wl'])}={mid} cross=2 td='t_m'",
              f".measure tran {rid}_tpre_on when {V(p['pre'])}={pre_th} cross=2 td='t_m'",
              f".measure tran {rid}_margin_end param='{rid}_tpre_on-{rid}_twl_off'"]
    elif cls == "hold":
        dn = p.get("data_node") or p["data_in"]
        L += [f".measure tran {rid}_tclk when {V(p['clk'])}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_tdata when {V(dn)}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_margin param='{rid}_tdata-{rid}_tclk'"]
    else:
        a = p.get("a", r["a"])
        b = p.get("b", r["b"])
        L += [f".measure tran {rid}_ta when {V(a)}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_tb when {V(b)}={mid} cross=1 td='t_m'",
              f".measure tran {rid}_skew param='{rid}_ta-{rid}_tb'"]
    return L


PARAM_NAMES = ("vdd_m", "t_m", "th_mid", "th_pre", "sa_dv_req")


def param_values(cfg, vdd=None):
    return {"vdd_m": vdd if vdd is not None else cfg["vdd"], "t_m": cfg["t_m"],
            "th_mid": cfg["th_mid"], "th_pre": cfg["th_pre"], "sa_dv_req": cfg["sa_dv_req"]}


def primary_metric(cls):
    return {"sae_bl": "margin", "wl_pre": "margin", "hold": "margin"}.get(cls, "skew")



def _where(r):
    m = re.search(r"\[([^\]]*)\]", r.get("ccc_label", ""))
    w = m.group(1) if m else ""
    return "トップ階層のブロック" if w in ("", "(top)") else f"{w} 内のブロック"


def explain_race(r, cfg):
    """One-paragraph Japanese description of what goes wrong if this race is lost."""
    p, cls = r["probes"], r["class"]
    if r.get("manual"):
        return "手動で追加した測定です(" + ", ".join(f"{k}={v}" for k, v in p.items()) + ")。"
    if cls == "sae_bl":
        bl = f"({p['bl_t']}/{p['bl_c']})" if "bl_t" in p else ""
        return (f"{p['wl']} が開いてBL{bl}の差電圧が十分に広がる前に {p['sae']} が立ち上がると、"
                f"SAが小さな入力差のまま増幅を始めて誤読出しになります。"
                f"SAE立上り時点のSA入力差({p['sa_t']}/{p['sa_c']})が {cfg['sa_dv_req']}V を下回るとNG。")
    if cls == "wl_pre":
        return (f"プリチャージ({p['pre']})が切れる前に {p['wl']} が開くと、プリチャージ電流とセル電流がぶつかって"
                f"BLが下がりきらず、読出し不良や貫通電流の原因になります。"
                f"サイクルの終わりも同様で、{p['wl']} が閉じる前にプリチャージが再開するとNG。")
    if cls == "hold":
        dn = p.get("data_node", p["data_in"])
        return (f"ラッチが {p['clk']} で閉じきる前に、{p['data_in']} 経由の次のデータ({dn})が届くと、"
                f"保持すべき値が上書きされてホールド違反になります。データ到達がクロック閉じより早いとNG。")
    if cls == "clock_pair":
        return (f"{r['a']} と {r['b']} は同じブロック(トランスミッションゲートやクロックドインバータ)を開閉する組です。"
                f"ずれが大きいと両方が開く、または両方が閉じる瞬間ができ、データの突き抜けやノードの浮きが起きます。")
    if r.get("derived"):
        return (f"{r['a']} と {r['b']} は一方がもう一方を遅らせた信号で、{_where(r)}で再び合流します。"
                f"2本の時間差がそのままパルス幅(グリッチ幅)になるため、差が小さすぎると後段が反応せず、"
                f"大きすぎると意図しないパルスが残ります。")
    return (f"{r['a']} と {r['b']} が{_where(r)}で合流しています。到達順がコーナーによって入れ替わると、"
            f"出力にグリッチや誤動作が出るおそれがあります。全コーナーで順序が変わらないことを確認してください。")


# --------------------------------------------------------------------------- reports

def write_csv(races, path):
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "pins", "ccc", "input_a", "input_b", "divergence",
                    "stages_a", "stages_b", "stage_diff", "derived", "group_size", "probes", "path_a", "path_b"])
        for r in races:
            w.writerow([r["id"], " ".join(r.get("pins", [])), r["ccc_label"], r["a"], r["b"],
                        r["divergence"], r["stages_a"], r["stages_b"], r["stage_diff"], r["derived"],
                        r.get("group_size", 1),
                        "; ".join(f"{k}={v}" for k, v in r["probes"].items()),
                        " -> ".join(r["path_a"]), " -> ".join(r["path_b"])])


def _netkey(n):
    return n.replace("\\", "")


def _netlink(net, rid, cls="net"):
    k = html.escape(_netkey(net), quote=True)
    return f'<a class="{cls}" href="#m-{rid}" data-rid="{rid}" data-net="{k}">{html.escape(net)}</a>'


def _chain(nets, rid):
    return '<span class="arrow"> → </span>'.join(_netlink(n, rid) for n in nets)


_VREF = re.compile(r"v\(([^()]*)\)")


def _meas_html(lines, prefix):
    out = []
    for ln in lines:
        if ln.startswith("*"):
            out.append(f'<span class="cm">{html.escape(ln)}</span>')
            continue
        pos, buf = 0, []
        for m in _VREF.finditer(ln):
            buf.append(html.escape(ln[pos:m.start()]))
            full = m.group(1)
            net = full[len(prefix):] if prefix and full.startswith(prefix) else full
            buf.append(f'v(<span class="v" data-net="{html.escape(net, quote=True)}">{html.escape(full)}</span>)')
            pos = m.end()
        buf.append(html.escape(ln[pos:]))
        out.append("".join(buf))
    return "\n".join(out)


PROBE_JA = {"sae": "SAE", "wl": "WL", "sa_t": "SA入力(T)", "sa_c": "SA入力(C)", "bl_t": "BL", "bl_c": "BLB",
            "pre": "プリチャージ", "clk": "クロック", "data_in": "データ入力", "data_node": "データ(内部)",
            "a": "入力A", "b": "入力B"}


def _pins_text(r):
    return "手動の測定" if r.get("manual") else "入力端子 " + ", ".join(r.get("pins", []))


def write_html(races, meta, path, pins):
    prefix = meta.get("probe_prefix", "")
    cards, blocks = {}, []
    for r in races:
        rid = r["id"]
        flags = []
        if r.get("derived"):
            flags.append("派生")
        if r.get("manual"):
            flags.append("手動")
        if r.get("group_size", 1) > 1:
            flags.append(f"同型×{r['group_size']}")
        flag_html = "".join(f'<span class="flag">{f}</span>' for f in flags)
        probes = "".join(
            '<span class="pb">' + ("" if PROBE_JA.get(k, k) == v else f"<em>{html.escape(PROBE_JA.get(k, k))}</em>")
            + _netlink(v, rid, "net pbn") + "</span>"
            for k, v in r["probes"].items() if v)
        if r.get("manual"):
            paths = ""
        else:
            paths = f"""<div class="paths">
<div class="pl"><span class="lab">A</span><span class="chain">{_chain(r['path_a'], rid)}</span><span class="st">{r['stages_a']}段</span></div>
<div class="pl"><span class="lab">B</span><span class="chain">{_chain(r['path_b'], rid)}</span><span class="st">{r['stages_b']}段</span></div>
<div class="sub">分岐点 {_netlink(r['divergence'], rid)} から数えた段数(差{r['stage_diff']}段) ・ 合流 {html.escape(r['ccc_label'])}</div></div>"""
        cards[rid] = f"""<section class="card" id="r-{rid}">
<header><a class="rid" href="#m-{rid}">{rid}</a>{flag_html}</header>
<div class="sub">{html.escape(_pins_text(r))}</div>
{paths}
<div class="probes"><span class="lab2">プローブ</span>{probes}</div>
<div class="sub"><a href="#m-{rid}">測定文を見る ↓</a></div>
</section>"""
        blocks.append(f"""<div class="mblk" id="m-{rid}" data-rid="{rid}">
<div class="mh"><b>{rid}</b> <span class="pins">{html.escape(_pins_text(r))}</span> <a href="#r-{rid}">↑ カードへ戻る</a></div>
<div class="note"></div>
<pre>{_meas_html(r.get('meas', []), prefix)}</pre></div>""")
    # one section per input pin; a race shared by several pins is drawn under its first pin
    # and linked from the others
    by_id = {r["id"]: r for r in races}
    nav, sections = [], []
    for i, pe in enumerate(pins):
        p, ids = pe["pin"], pe["races"]
        nav.append(f'<a class="tag t-pin" href="#p-{i}">{html.escape(p)} {len(ids)}</a>')
        own = [cards[x] for x in ids if by_id[x]["pins"][0] == p]
        others = [x for x in ids if by_id[x]["pins"][0] != p]
        sec = f'<h2 id="p-{i}">入力端子 {html.escape(p)} <span class="cnt">競合 {len(ids)}件</span></h2>\n' + "".join(own)
        if others:
            sec += ('<p class="also">ほかの入力端子の下に載せた競合: ' + "、".join(
                f'<a href="#r-{x}">{x}</a>({html.escape(by_id[x]["pins"][0])})' for x in others) + "</p>")
        if not ids:
            sec += '<p class="meta">この端子から始まる競合はありません。</p>'
        sections.append(sec)
    manual = [r for r in races if r.get("manual")]
    if manual:
        nav.append(f'<a class="tag t-pin" href="#p-manual">手動の測定 {len(manual)}</a>')
        sections.append(f'<h2 id="p-manual">手動の測定 <span class="cnt">{len(manual)}件</span></h2>\n'
                        + "".join(cards[r["id"]] for r in manual))
    summary = " ".join(nav)
    warn = ""
    if meta.get("warnings"):
        warn = "<details><summary>警告 {}件</summary><ul>{}</ul></details>".format(
            len(meta["warnings"]), "".join(f"<li>{html.escape(w)}</li>" for w in meta["warnings"]))
    js_prefix = json.dumps(prefix)
    static = (" ・ 固定端子 " + html.escape(", ".join(meta["static_inputs"]))) if meta.get("static_inputs") else ""
    doc = f"""<!DOCTYPE html><html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>racepath report - {html.escape(meta['top'])}</title>
<style>
:root{{--bg:#f7f6f2;--card:#fff;--fg:#1d1d1b;--mute:#6b6a64;--line:#e4e1d8;--hit:#ffe58a;
--wl:#2a6fb4;--gen:#76746c}}
*{{box-sizing:border-box}}
body{{font-family:-apple-system,"Hiragino Sans","Noto Sans JP",sans-serif;background:var(--bg);color:var(--fg);
margin:0;padding:20px;line-height:1.55;max-width:1100px;margin:0 auto}}
h1{{font-size:20px;margin:0 0 4px}} h2{{font-size:16px;margin:28px 0 10px}}
.meta{{color:var(--mute);font-size:13px;margin-bottom:10px;word-break:break-all}}
.mono,.chain,.pb,pre{{font-family:ui-monospace,Menlo,Consolas,monospace}}
.card{{background:var(--card);border:1px solid var(--line);border-left:4px solid var(--gen);border-radius:10px;
padding:14px 16px;margin:12px 0}}
header{{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-bottom:6px}}
.rid{{font-family:ui-monospace,monospace;font-weight:700;color:var(--fg);text-decoration:none;margin-right:2px}}
.tag{{display:inline-block;font-size:12px;padding:2px 9px;border-radius:10px;color:#fff;white-space:nowrap}}
.t-pin{{background:var(--wl);text-decoration:none;margin:2px 0}} .cnt{{font-size:13px;color:var(--mute);font-weight:400}}
.pins{{font-size:12px;color:var(--mute)}} .also{{font-size:13px;color:var(--mute)}}
.flag{{font-size:11px;border:1px solid var(--line);border-radius:8px;padding:1px 7px;color:var(--mute)}}
.ex{{margin:4px 0 10px;font-size:14.5px}}
.paths{{background:#faf9f5;border:1px solid var(--line);border-radius:8px;padding:8px 10px;margin-bottom:8px}}
.pl{{display:flex;gap:8px;align-items:baseline;margin:3px 0}}
.lab{{font-size:11px;font-weight:700;color:#fff;background:var(--mute);border-radius:4px;padding:0 5px;flex:none}}
.chain{{font-size:13px;flex:1;word-break:break-all}} .arrow{{color:var(--mute)}}
.st{{font-size:12px;color:var(--mute);flex:none}}
.sub{{font-size:12px;color:var(--mute);margin-top:4px;word-break:break-all}}
a.net{{color:#1f5fa8;text-decoration:none;border-bottom:1px dotted #9db7d8}}
a.net:active,a.net:hover{{background:#e8f0fb}}
.probes{{display:flex;flex-wrap:wrap;gap:6px;align-items:center;font-size:12.5px}}
.lab2{{font-size:12px;color:var(--mute);margin-right:2px}}
.pb{{border:1px solid var(--line);border-radius:6px;padding:1px 7px;background:#fff}}
.pb em{{font-style:normal;color:var(--mute);font-family:-apple-system,"Hiragino Sans",sans-serif;margin-right:5px;font-size:11.5px}}
.mblk{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:10px 12px;margin:10px 0;
transition:box-shadow .3s}}
.mblk.flash{{box-shadow:0 0 0 3px #f2c94c}}
.mh{{display:flex;gap:8px;align-items:center;flex-wrap:wrap;font-size:13px}} .mh a{{font-size:12px}}
pre{{font-size:12px;overflow-x:auto;white-space:pre;margin:8px 0 0;line-height:1.5}}
.cm{{color:#7c7a72}} .v.hit{{background:var(--hit);border-radius:3px;outline:2px solid #f2c94c}}
.note{{font-size:13px;margin-top:6px;display:none;padding:8px 10px;border-radius:8px}}
.note.ok{{display:block;background:#fff7d6}} .note.miss{{display:block;background:#eef3fb}}
.note code{{display:block;margin-top:6px;font-size:12px;user-select:all;-webkit-user-select:all;
background:#fff;border:1px solid var(--line);border-radius:6px;padding:6px 8px;overflow-x:auto;white-space:pre}}
.note button{{margin-top:6px;font-size:12px;padding:3px 10px;border:1px solid var(--line);border-radius:6px;background:#fff}}
details{{margin:10px 0;font-size:13px}}
</style></head><body>
<h1>racepath レポート: {html.escape(meta['top'])}</h1>
<div class="meta">{html.escape(meta['netlist'])} ({meta['format']}) ・ Tr {meta['n_mos']} ・ CCC {meta['n_ccc']} ・
起点 {html.escape(', '.join(meta['sources']))}{static} ・ 競合 {len(races)}件</div>
<div>{summary}</div>{warn}
<p class="meta">ノード名をタップすると、その競合の測定文へ移動して該当箇所をハイライトします。段数は、分岐点(2本の経路が分かれる最後の共通ネット)から、合流するブロックの入力までに通る論理段の数です。インバータや NAND など、チャネルでつながったトランジスタのまとまり(CCC)1つを1段と数えます。0段はその入力が分岐点そのものであることを、同相/逆相は起点(CLK など)から見た信号の向きを表します。</p>
{''.join(sections)}
<h2>測定文 (races.meas)</h2>
{''.join(blocks)}
<script>
(function(){{
  var PREFIX = {js_prefix};
  function clear(){{
    document.querySelectorAll('.v.hit').forEach(function(x){{x.classList.remove('hit');}});
    document.querySelectorAll('.note').forEach(function(x){{x.className='note';x.innerHTML='';}});
  }}
  function esc(t){{return t.replace(/[&<>"]/g,function(c){{return {{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}}[c];}});}}
  function jump(id){{
    var t = document.getElementById(id); if(!t) return false;
    t.scrollIntoView({{behavior:'smooth', block:'start'}});
    try {{ if(history.replaceState) history.replaceState(null, '', '#'+id); }} catch(err) {{}}
    return true;
  }}
  document.addEventListener('click', function(e){{
    var a = e.target.closest ? e.target.closest('a.net') : null;
    if(!a){{
      // in-page links (card <-> measures, pin sections) scrolled by script, so they also work
      // when this report is shown from a blob: URL inside the single-file racepath.html
      var h = e.target.closest ? e.target.closest('a[href^="#"]') : null;
      if(h && jump(h.getAttribute('href').slice(1))) e.preventDefault();
      return;
    }}
    e.preventDefault();
    var rid = a.getAttribute('data-rid'), net = a.getAttribute('data-net');
    var blk = document.getElementById('m-'+rid); if(!blk) return;
    clear();
    var hits = [];
    blk.querySelectorAll('.v').forEach(function(x){{ if(x.getAttribute('data-net')===net) hits.push(x); }});
    var note = blk.querySelector('.note');
    if(hits.length){{
      hits.forEach(function(x){{x.classList.add('hit');}});
      note.className = 'note ok';
      note.textContent = net + ' はこの競合の ' + hits.length + ' 行で測定しています。';
    }} else {{
      var nm = rid + '_t_' + net.replace(/[^A-Za-z0-9_]/g,'_');
      var line = '.measure tran ' + nm + " when v(" + PREFIX + net + ")='th_mid*vdd_m' cross=1 td='t_m'";
      note.className = 'note miss';
      note.innerHTML = esc(net) + ' はパス上の中間ノードで、まだ測定していません。到達時刻を測るなら次の1行を races.meas に追加してください。'
        + '<code>' + esc(line) + '</code><button type="button">コピー</button>';
      note.querySelector('button').addEventListener('click', function(ev){{
        var b = ev.target;
        try {{ navigator.clipboard.writeText(line).then(function(){{b.textContent='コピーしました';}},
               function(){{b.textContent='長押しで選択してコピー';}}); }}
        catch(err){{ b.textContent='長押しで選択してコピー'; }}
      }});
    }}
    blk.scrollIntoView({{behavior:'smooth', block:'start'}});
    blk.classList.add('flash'); setTimeout(function(){{blk.classList.remove('flash');}}, 1400);
    try {{ if(history.replaceState) history.replaceState(null, '', '#m-'+rid); }} catch(err) {{}}
  }});
}})();
</script>
</body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)


# --------------------------------------------------------------------------- commands

def cmd_analyze(args):
    cfg = load_cfg(args.config, {"probe_prefix": args.prefix})
    if args.source:
        cfg["sources"] = args.source
    if args.static:
        cfg["static_inputs"] = list(cfg.get("static_inputs") or []) + args.static
    os.makedirs(args.out, exist_ok=True)
    log = (lambda *a, **k: None) if args.quiet else print
    log(f"racepath {VERSION}: reading {args.netlist}")
    st = parse_netlist(args.netlist, args.format)
    fn = flatten(st, cfg, args.top)
    G = CCCGraph(fn)
    log(f"  format={st.detected_format} top={fn.top} transistors={len(fn.mos)} CCC={G.n_ccc} "
        f"supplies={len(fn.supplies)}")
    static_pats = cfg.get("static_inputs") or []
    static_ports = [p for p in fn.top_ports if p not in fn.supplies and glob_match(p, static_pats)]
    if cfg["sources"]:
        srcs = []
        for s in cfg["sources"]:
            if static_pats and glob_match(s, static_pats):
                log(f"  warning: source '{s}' is a static input (static_inputs); ignored")
            else:
                srcs.append(s)
    else:
        srcs = [s for s in default_sources(fn, G) if not (static_pats and glob_match(s, static_pats))]
    log(f"  sources: {', '.join(srcs)}")
    if static_pats:
        log("  static inputs: " + (", ".join(static_ports) if static_ports
                                   else f"(none matched {', '.join(static_pats)})")
            + "  (held constant: not used as launch nets)")
    races = find_races(G, fn, srcs, cfg, log)
    n_raw = len(races)
    if not args.no_group:
        races = group_races(races)
        if len(races) < n_raw:
            log(f"  grouped {n_raw} races into {len(races)} structural groups (--no-group to keep all)")
    nets_all = set(G.fanout) | set(G.ccc_of)
    add_extra_checks(races, cfg, nets_all, log)
    # input pins (launch nets as resolved in the netlist), in the order given
    pin_order = []
    for s in srcs:
        n = resolve_net(s, nets_all)
        if n is not None and n not in pin_order:
            pin_order.append(n)
    rank = {p: i for i, p in enumerate(pin_order)}
    for r in races:
        rp = r.pop("_pins", r["sources"])
        r["pins"] = sorted(rp, key=lambda x: (rank.get(x, len(rank)), x))
    prefix = cfg["probe_prefix"]
    for i, r in enumerate(races, 1):
        r["id"] = f"rp{i:03d}"
        r["explain"] = explain_race(r, cfg)
        r["meas"] = measure_lines(r, prefix, fn.supplies)

    # .measure file
    meas = ["* racepath auto-probe measures (HSPICE / Spectre SPICE-mode syntax)",
            f"* netlist: {os.path.basename(args.netlist)}  top: {fn.top}  probe prefix: '{prefix}'",
            "* required params: " + " ".join(PARAM_NAMES) + "  (see races_params.inc)", ""]
    for r in races:
        meas += r["meas"] + [""]
    with open(os.path.join(args.out, "races.meas"), "w", encoding="utf-8") as f:
        f.write("\n".join(meas))
    pv = param_values(cfg)
    with open(os.path.join(args.out, "races_params.inc"), "w", encoding="utf-8") as f:
        f.write("* default measurement parameters for races.meas\n.param "
                + " ".join(f"{k}={v}" for k, v in pv.items()) + "\n")

    meta = {
        "tool": f"racepath {VERSION}", "netlist": os.path.abspath(args.netlist),
        "format": st.detected_format, "top": fn.top, "top_ports": fn.top_ports,
        "pininfo": fn.pininfo, "sources": srcs, "n_mos": len(fn.mos), "n_ccc": G.n_ccc,
        "supplies_top": [p for p in fn.top_ports if p in fn.supplies],
        "static_inputs": static_ports,
        "probe_prefix": prefix, "cfg": cfg, "warnings": fn.warnings,
    }
    pins = [{"pin": p, "races": [r["id"] for r in races if p in r["pins"]]} for p in pin_order]
    with open(os.path.join(args.out, "races.json"), "w", encoding="utf-8") as f:
        json.dump({"meta": meta, "pins": pins, "races": races}, f, ensure_ascii=False, indent=1)
    write_csv(races, os.path.join(args.out, "races.csv"))
    write_html(races, meta, os.path.join(args.out, "report.html"), pins)

    log(f"  races: {len(races)}  (入力端子 {len(pin_order)}本)")
    by_id = {r["id"]: r for r in races}
    groups = [(f"入力端子 {pe['pin']}  競合 {len(pe['races'])}件", [by_id[x] for x in pe["races"]]) for pe in pins]
    manual = [r for r in races if r.get("manual")]
    if manual:
        groups.append((f"手動の測定  {len(manual)}件", manual))
    shown = 0
    for title, rs in groups:
        log(f"  ■ {title}")
        for r in rs:
            if shown >= args.show:
                break
            shown += 1
            g = f"  x{r['group_size']}" if r.get("group_size", 1) > 1 else ""
            log(f"   {r['id']} {stage_summary(r)}  @ {r['ccc_label']}{g}")
    for w in fn.warnings[:10]:
        log(f"  warning: {w}")
    log(f"  wrote {args.out}/races.json, races.csv, races.meas, races_params.inc, report.html")
    return races


def _is_high(net, cfg):
    return glob_match(net, cfg.get("high_supplies", DEFAULT_CFG["high_supplies"]))


def cmd_decks(args):
    with open(args.races, encoding="utf-8") as f:
        data = json.load(f)
    meta = data["meta"]
    cfg = meta["cfg"]
    with open(args.corners, encoding="utf-8") as f:
        cc = json.load(f)
    sim = (args.sim or cc.get("simulator", "spectre")).lower()
    os.makedirs(args.out, exist_ok=True)
    meas_path = os.path.abspath(os.path.join(os.path.dirname(args.races), "races.meas"))
    netlist = meta["netlist"]
    top, ports = meta["top"], meta["top_ports"]
    inst = cfg["probe_prefix"].rstrip(cfg["hier_sep"]) or "XDUT"
    stim = cc.get("stimulus", {})
    static = cc.get("static", {})
    supply_vals = cc.get("supplies", {})
    tstop = cc.get("tran_stop", "20n")
    tstep = cc.get("tran_step", "1p")
    stim_file = cc.get("stimulus_file")
    first_delay = None
    written = []
    for corner in cc["corners"]:
        name = corner["name"]
        vdd = corner.get("vdd", cfg["vdd"])
        temp = corner.get("temp", 25)
        lib = corner.get("lib")  # [path, section] or "path"
        pv = param_values(cfg, vdd)
        pv.update(corner.get("params", {}))
        L = []
        src_lines = []
        for p in ports:
            if p in meta["supplies_top"]:
                val = supply_vals.get(p, "vdd_m" if _is_high(p, cfg) else 0)
                src_lines.append(("dc", f"V_{p}", p, val))
            elif p in stim or p in meta["sources"] and not stim_file:
                s = {"delay": "1n", "period": "4n", "width": "2n", "tr": "20p", "tf": None,
                     "v0": 0, "v1": "vdd_m"}
                s.update(stim.get(p, {}))
                s["tf"] = s["tf"] or s["tr"]
                first_delay = s["delay"] if first_delay is None else first_delay
                src_lines.append(("pulse", f"V_{p}", p, s))
            elif p in static:
                src_lines.append(("dc", f"V_{p}", p, static[p]))
            elif not stim_file:
                src_lines.append(("tie", f"R_{p}", p, "1e9"))
        if "t_m" not in corner.get("params", {}) and first_delay is not None and "t_m" not in cc:
            pv["t_m"] = first_delay
        if "t_m" in cc:
            pv["t_m"] = cc["t_m"]
        if sim == "hspice":
            L += [f"* racepath deck  corner={name}", ""]
            if lib:
                L.append(f".lib '{lib[0]}' {lib[1]}" if isinstance(lib, list) else f".include '{lib}'")
            L += [f".temp {temp}", ".param " + " ".join(f"{k}={v}" for k, v in pv.items()),
                  ".option post=0 measout=1", f".include '{netlist}'"]
            if stim_file:
                L.append(f".include '{stim_file}'")
            L.append(f".include '{meas_path}'")
            L.append(f"{inst} {' '.join(ports)} {top}")
            for kind, nm, p, val in src_lines:
                if kind == "dc":
                    L.append(f"{nm} {p} 0 '{val}'" if isinstance(val, str) else f"{nm} {p} 0 {val}")
                elif kind == "tie":
                    L.append(f"{nm} {p} 0 {val}")
                else:
                    s = val
                    L.append(f"{nm} {p} 0 pulse({s['v0']} '{s['v1']}' {s['delay']} {s['tr']} {s['tf']} "
                             f"{s['width']} {s['period']})")
            L += [f".tran {tstep} {tstop}", ".end"]
            ext = ".sp"
        else:
            L += [f"// racepath deck  corner={name}", "simulator lang=spectre", ""]
            if lib:
                L.append(f'include "{lib[0]}" section={lib[1]}' if isinstance(lib, list) else f'include "{lib}"')
            L.append("parameters " + " ".join(f"{k}={v}" for k, v in pv.items()))
            if meta["format"] == "spectre":
                L.append(f'include "{netlist}"')
            else:
                L += ["simulator lang=spice", f".include '{netlist}'", "simulator lang=spectre"]
            if stim_file:
                L.append(f'include "{stim_file}"')
            L.append(f"{inst} ({' '.join(ports)}) {top}")
            for kind, nm, p, val in src_lines:
                if kind == "dc":
                    L.append(f"{nm} ({p} 0) vsource dc={val}")
                elif kind == "tie":
                    L.append(f"{nm} ({p} 0) resistor r={val}")
                else:
                    s = val
                    L.append(f"{nm} ({p} 0) vsource type=pulse val0={s['v0']} val1={s['v1']} "
                             f"delay={s['delay']} rise={s['tr']} fall={s['tf']} width={s['width']} "
                             f"period={s['period']}")
            L += ["simulator lang=spice", f".include '{meas_path}'", "simulator lang=spectre",
                  f"racepathOpts options temp={temp} tnom={cc.get('tnom', 25)}",
                  f"tran1 tran stop={tstop}"]
            ext = ".scs"
        fp = os.path.join(args.out, f"{name}{ext}")
        with open(fp, "w", encoding="utf-8") as f:
            f.write("\n".join(L) + "\n")
        written.append(fp)
    # helper run script
    runner = os.path.join(args.out, "run_all.sh")
    with open(runner, "w", encoding="utf-8") as f:
        f.write("#!/bin/sh\n# run every corner deck; results land in <corner>/\ncd \"$(dirname \"$0\")\"\n")
        for fp in written:
            b = os.path.splitext(os.path.basename(fp))[0]
            if sim == "hspice":
                f.write(f"mkdir -p {b} && hspice -i {os.path.basename(fp)} -o {b}/{b}\n")
            else:
                f.write(f"spectre {os.path.basename(fp)} -raw {b} +aps =log {b}.log\n")
    os.chmod(runner, 0o755)
    print(f"wrote {len(written)} {sim} decks + run_all.sh to {args.out}")


NUM = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?[a-zA-Z]*"
SCALE = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "meg": 1e6, "g": 1e9}


def to_float(s):
    s = s.strip()
    if s.lower() in ("failed", "fail", "nan", "n/a"):
        return None
    m = re.match(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)([a-zA-Z]*)$", s)
    if not m:
        return None
    v = float(m.group(1))
    suf = m.group(2).lower()
    for k in ("meg", "f", "p", "n", "u", "m", "k", "g"):
        if suf.startswith(k):
            return v * SCALE[k]
    return v


def parse_results_file(path):
    vals = {}
    with open(path, encoding="utf-8", errors="replace") as f:
        txt = f.read()
    # key = value style (Spectre .measure, HSPICE .lis/measout)
    for m in re.finditer(r"(?im)^\s*(rp\d{3}_\w+)\s*=\s*(" + NUM + r"|failed)", txt):
        vals[m.group(1).lower()] = to_float(m.group(2))
    if vals:
        return vals
    # HSPICE .mt0 tabular style
    lines = [ln for ln in txt.splitlines() if ln.strip() and not ln.startswith(("$", "."))]
    toks = " ".join(lines).split()
    if "alter#" in [t.lower() for t in toks]:
        idx = [t.lower() for t in toks].index("alter#")
        names = [t.lower() for t in toks[: idx + 1]]
        values = toks[idx + 1: idx + 1 + len(names)]
        for n, v in zip(names, values):
            if n.startswith("rp"):
                vals[n] = to_float(v)
    return vals


def cmd_collect(args):
    with open(args.races, encoding="utf-8") as f:
        data = json.load(f)
    races = data["races"]
    files = []
    for p in args.results:
        if os.path.isdir(p):
            for ext in ("*.mt*", "*.ms*", "*.measure", "*.lis", "*.log", "*.out"):
                files += glob.glob(os.path.join(p, "**", ext), recursive=True)
        else:
            files += glob.glob(p)
    corners = {}
    for fp in sorted(set(files)):
        v = parse_results_file(fp)
        if not v:
            continue
        name = os.path.splitext(os.path.basename(fp))[0]
        if name.lower() in ("tran1", "tran", "input", "log", "spectre"):
            parent = os.path.basename(os.path.dirname(os.path.abspath(fp)))
            name = re.sub(r"\.(raw|psf)$", "", parent) or name
        corners.setdefault(name, {}).update(v)
    if not corners:
        raise SystemExit("no measurement results found")
    cnames = sorted(corners)
    rows = []
    for r in races:
        metrics = [primary_metric(r["class"])]
        if r["class"] == "wl_pre":
            metrics.append("margin_end")
        for mt in metrics:
            key = f"{r['id']}_{mt}".lower()
            vals = [corners[c].get(key) for c in cnames]
            good = [x for x in vals if x is not None]
            if mt == "skew":
                worst = min(good, key=abs) if good else None
                signs = {x > 0 for x in good if x != 0}
                verdict = "順序反転" if len(signs) > 1 else ("要確認" if worst is not None and abs(worst) < args.skew_warn else "")
            else:
                worst = min(good) if good else None
                lim = args.warn_v if r["class"] == "sae_bl" else args.warn
                verdict = "" if worst is None else ("NG" if worst < 0 else ("要注意" if worst < lim else "OK"))
            if any(x is None for x in vals):
                verdict = "測定失敗あり" if verdict in ("", "OK") else verdict + " / 測定失敗あり"
            wc = cnames[vals.index(worst)] if worst is not None else ""
            rows.append([r["id"], mt, r["a"], r["b"]] + vals + [worst, wc, verdict])
    with open(args.out, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["id", "metric", "input_a", "input_b"] + cnames + ["worst", "worst_corner", "verdict"])
        w.writerows(rows)
    print(f"{len(cnames)} corners, {len(rows)} metrics -> {args.out}")
    for row in rows:
        if row[-1] and row[-1] not in ("OK",):
            print(f"  {row[0]} {row[1]:<10} worst={row[-3]} @ {row[-2]}  {row[-1]}")


def cmd_serve(args):
    from racepath.web import cmd_serve as _serve  # imported lazily: CLI-only users never load it
    _serve(args)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="racepath", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=VERSION)
    sp = ap.add_subparsers(dest="cmd", required=True)

    a = sp.add_parser("analyze", help="find races and generate auto-probe measures")
    a.add_argument("netlist")
    a.add_argument("-f", "--format", default="auto", choices=["auto", "cdl", "spice", "hspice", "spectre"])
    a.add_argument("-t", "--top")
    a.add_argument("-s", "--source", action="append", help="launch net (repeatable)")
    a.add_argument("--static", action="append",
                   help="input pin held constant, e.g. test mode (glob, repeatable; adds to static_inputs)")
    a.add_argument("-c", "--config")
    a.add_argument("-o", "--out", default="racepath_out")
    a.add_argument("--prefix", help="probe prefix = DUT instance path in testbench (default XDUT.)")
    a.add_argument("--show", type=int, default=30)
    a.add_argument("--no-group", action="store_true", help="do not merge structurally identical races")
    a.add_argument("-q", "--quiet", action="store_true")
    a.set_defaults(func=cmd_analyze)

    d = sp.add_parser("decks", help="write one deck per PVT corner")
    d.add_argument("races")
    d.add_argument("corners")
    d.add_argument("-o", "--out", default="racepath_decks")
    d.add_argument("--sim", choices=["spectre", "hspice"])
    d.set_defaults(func=cmd_decks)

    c = sp.add_parser("collect", help="gather measure results into a margin table")
    c.add_argument("races")
    c.add_argument("results", nargs="+", help="result files or directories")
    c.add_argument("-o", "--out", default="racepath_summary.csv")
    c.add_argument("--warn", type=float, default=20e-12, help="timing margin warning level [s]")
    c.add_argument("--warn-v", type=float, default=0.02, help="SA split margin warning level [V]")
    c.add_argument("--skew-warn", type=float, default=10e-12)
    c.set_defaults(func=cmd_collect)

    w = sp.add_parser("serve", help="open a local browser UI (analyze / decks / collect)")
    w.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1 = this PC only)")
    w.add_argument("--port", type=int, default=8765)
    w.add_argument("--workdir", default="racepath_web", help="where uploaded netlists and results are kept")
    w.add_argument("--no-browser", action="store_true", help="do not open a browser automatically")
    w.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
