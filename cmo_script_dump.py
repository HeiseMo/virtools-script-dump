#!/usr/bin/env python3
"""
cmo_script_dump.py — read Virtools behavior scripts out of a .CMO/.NMO/.VMO file
on any OS, with no Virtools editor.

Virtools (.cmo/.nmo/.vmo, magic "Nemo Fi") stores its gameplay logic as a graph
of CKBehavior "building blocks" wired by links. Normally you need the Windows
Virtools Dev editor to look at those scripts. This tool reconstructs the graph —
nodes, oriented execution links, and decoded parameter values (including message
and attribute names) — straight from the file bytes, from the command line.

It drives a small patched build of yyc12345's LibCmo `Unvirt`
(https://github.com/yyc12345/libcmo21) — see README and the included
libcmo-virtools-script-dump.patch. Background on the format and the
player-only FileWriteMode flag: phaicm, "Opening Virtools Files"
(https://www.phaicm.com/2020/04/opening-virtools-files.html).

Usage
-----
  cmo_script_dump.py histogram                 # class-id counts
  cmo_script_dump.py behaviors                 # every named CKBEHAVIOR (the scripts/BBs)
  cmo_script_dump.py search <text>             # any object whose name matches
  cmo_script_dump.py table                     # dump full object table TSV to stdout
  cmo_script_dump.py chunk <index>             # raw CKStateChunk for object #index
  cmo_script_dump.py scripts                   # list all behavior-graph "scripts"
  cmo_script_dump.py script <index|name>       # render one script: sub-behaviors + exec links
  cmo_script_dump.py dot <index|name>          # emit Graphviz .dot for one script
  cmo_script_dump.py json [index|name]         # one script (or all) as JSON, for diff/tooling
  cmo_script_dump.py dataflow <param|#index>   # trace backward what produces a parameter's value
  cmo_script_dump.py blocks [--dll strings.txt] # leaf building-block types + proto GUID (C++ blocks)
  cmo_script_dump.py messages                  # the Message Manager name table (index->name)
  cmo_script_dump.py attributes [filter]       # Attribute Manager strings (name reference)

The full object table is cached (see --cache) so repeated queries are instant.

Graph reconstruction
--------------------
`scripts`/`script`/`dot` reconstruct the actual behavior wiring. They use a
second Unvirt pass (the `test` command, which this repo's Unvirt build repurposes
to dump every object's CKStateChunk decoded fields) cached at --graphcache.

How it works: modern Virtools (4.0) packs each behavior/link/IO into a single
CK_STATESAVE_*_NEWDATA (0x20) blob with inline object references stored as file
indices. We decode:
  * LINK  NEWDATA = [delay, inIO_idx, outIO_idx]            (execution edge)
  * IO    flags (0x08) = 1:input  2:output                  (link orientation)
  * BEHAV NEWDATA = [flags, (guid_lo,guid_hi if leaf), scalars..., then
                     count-prefixed lists of object refs]   (IOs/params/children)
A behavior's ref-lists are classified by element class (PARAMIN/OUT/LOCAL/OP,
BEHAVIORIO, sub-BEHAVIOR, sub-LINK). This was validated globally: 24553/24555
IOs resolve to exactly one owner, 13114/13117 link endpoints resolve.

Parameter values are decoded too (shown in `script` output as name=value):
  * PARAM value (id 0x40) = [guid_lo, guid_hi, mode, ...]
      mode 1 -> raw buffer [1, size, data]: float / int / bool / string /
                vector decoded by parameter-type GUID + size
      mode 2 -> object reference [2, idx]  (rendered @<object name>, or null)
      message/attribute types -> manager-table index (rendered msg#/attr#)
  * data-source link (id 0x1000): param reads from another param; followed one
    hop to show the source's literal, e.g. Message=msg_EndGame (<-Message)

Manager name tables are read from the file's manager chunks and resolved inline
in script output: Message Manager (GUID 0x466a0fac) gives msg#N -> real name
(e.g. msg_intro_go); Attribute Manager (0x3d242466) gives attr#N -> real name
(e.g. avatarMP_localEntity = #877). See `messages` / `attributes` for the tables.
"""
import argparse
import os
import re
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))

# Path to the patched LibCmo `Unvirt` binary: $UNVIRT, else "Unvirt" on PATH.
DEFAULT_UNVIRT = os.environ.get("UNVIRT", "Unvirt")
# The .cmo/.nmo/.vmo to read: $CMO_FILE, else must be given with --cmo.
DEFAULT_CMO = os.environ.get("CMO_FILE")


def _cache_path(cmo, suffix):
    """Per-file cache next to the tool, keyed by the input file name."""
    base = os.path.basename(cmo) if cmo else "cmo"
    return os.path.join(HERE, f".cache_{base}{suffix}")

# One row of `ls obj`:
# 0xffffffff CK_FO_DEFAULT 0 88 0x0022561c (RVA: 0x0002cb80) 0x00004978 #0 CKCID_LEVEL No Yes Level
ROW_RE = re.compile(
    r"^(0x[0-9a-fA-F]{8})\s+"          # 1 save flags
    r"(CK_FO_\w+)\s+"                  # 2 options
    r"(\d+)\s+"                        # 3 CK ID
    r"(\d+)\s+"                        # 4 File CK ID
    r"(0x[0-9a-fA-F]+)\s+"            # 5 file index
    r"\(RVA:\s*(0x[0-9a-fA-F]+)\)\s+"# 6 RVA
    r"(0x[0-9a-fA-F]+)\s+"            # 7 pack size
    r"#(\d+)\s+"                       # 8 index
    r"(CKCID_\w+)\s+"                 # 9 class id
    r"(Yes|No)\s+"                     # 10 has CKObject
    r"(Yes|No)\s+"                     # 11 has CKStateChunk
    r"(.*?)\s*$"                       # 12 name
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def run_unvirt(unvirt: str, cmo: str, commands: list[str], items: int = 0) -> str:
    """Drive the interactive Unvirt REPL non-interactively via script(1)."""
    import shutil
    exe = unvirt if os.path.exists(unvirt) else shutil.which(unvirt)
    if not exe:
        sys.exit(f"Unvirt not found: {unvirt!r}\n"
                 f"Build the patched LibCmo Unvirt (see README) and point to it "
                 f"with --unvirt or the $UNVIRT env var.")
    if not cmo:
        sys.exit("No input file. Pass --cmo <file.cmo> or set $CMO_FILE.")
    cmo = os.path.abspath(cmo)
    if not os.path.exists(cmo):
        sys.exit(f"Input file not found: {cmo}")
    # Unvirt's `load` command splits its argument on spaces and has no quoting,
    # so a path containing spaces (very common: "Program Files", "My Game") fails.
    # Work around it by loading through a space-free symlink in a temp dir.
    tmp = None
    load_path = cmo
    if " " in cmo:
        tmp = tempfile.mkdtemp(prefix="vsd_")
        load_path = os.path.join(tmp, os.path.basename(cmo).replace(" ", "_"))
        os.symlink(cmo, load_path)
    try:
        script_lines = ["encoding Windows-1252"]
        if items:
            script_lines.append(f"items {items}")
        script_lines.append(f"load deep {load_path}")
        script_lines.extend(commands)
        script_lines.append("exit")
        stdin = "\n".join(script_lines) + "\n"
        # `script -q -c CMD /dev/null` gives Unvirt a tty so its editor behaves.
        proc = subprocess.run(
            ["script", "-q", "-c", exe, "/dev/null"],
            input=stdin, text=True, capture_output=True,
        )
        return ANSI_RE.sub("", proc.stdout)
    finally:
        if tmp:
            import shutil as _sh
            _sh.rmtree(tmp, ignore_errors=True)


def load_table(args) -> list[dict]:
    """Return the full object table, building/reading the cache as needed."""
    if not args.refresh and os.path.exists(args.cache):
        with open(args.cache, encoding="utf-8") as fh:
            header = fh.readline().rstrip("\n").split("\t")
            return [dict(zip(header, line.rstrip("\n").split("\t")))
                    for line in fh]

    out = run_unvirt(args.unvirt, args.cmo, ["ls obj 1"], items=400000)
    rows = []
    for line in out.splitlines():
        m = ROW_RE.match(line.strip())
        if not m:
            continue
        rows.append({
            "index": m.group(8),
            "ckid": m.group(4),
            "classid": m.group(9),
            "has_obj": m.group(10),
            "has_chunk": m.group(11),
            "name": m.group(12),
        })
    if not rows:
        # Don't cache a failed/empty parse — surface the error instead of poisoning.
        sys.stderr.write("Could not parse any objects. Unvirt output head:\n")
        sys.stderr.write("\n".join(out.splitlines()[:15]) + "\n")
        return rows
    cols = ["index", "ckid", "classid", "has_obj", "has_chunk", "name"]
    with open(args.cache, "w", encoding="utf-8") as fh:
        fh.write("\t".join(cols) + "\n")
        for r in rows:
            fh.write("\t".join(r[c] for c in cols) + "\n")
    return rows


def cmd_histogram(args):
    rows = load_table(args)
    counts = {}
    for r in rows:
        counts[r["classid"]] = counts.get(r["classid"], 0) + 1
    for cid, n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"{n:8d}  {cid}")
    print(f"{'-'*8}")
    print(f"{len(rows):8d}  TOTAL")


def cmd_behaviors(args):
    rows = load_table(args)
    behs = [r for r in rows if r["classid"] == "CKCID_BEHAVIOR"]
    for r in behs:
        print(f"#{r['index']:<7} id={r['ckid']:<7} {r['name']}")
    sys.stderr.write(f"\n{len(behs)} CKBEHAVIOR objects\n")


def cmd_search(args):
    rows = load_table(args)
    needle = args.text.lower()
    hits = [r for r in rows if needle in r["name"].lower()]
    for r in hits:
        print(f"#{r['index']:<7} id={r['ckid']:<7} {r['classid']:<22} {r['name']}")
    sys.stderr.write(f"\n{len(hits)} match(es) for {args.text!r}\n")


def cmd_table(args):
    rows = load_table(args)
    print("index\tckid\tclassid\thas_obj\thas_chunk\tname")
    for r in rows:
        print("\t".join(r[c] for c in
                         ("index", "ckid", "classid", "has_obj", "has_chunk", "name")))


def cmd_chunk(args):
    out = run_unvirt(args.unvirt, args.cmo, [f"chunk obj {args.index}"])
    # print only from the CKStateChunk dump onward, dropping echoed input + banner
    started = False
    for line in out.splitlines():
        if "CKStateChunk" in line:
            started = True
        if not started:
            continue
        if line.strip() in ("", ">") or line.strip() == "exit":
            continue
        print(line.lstrip("> "))


# ---------------------------------------------------------------------------
# Behavior-graph reconstruction
# ---------------------------------------------------------------------------

# CK_CLASSID integers (from LibCmo CKTypes.hpp)
CID = {2: "PIN", 3: "POUT", 4: "POP", 6: "LINK", 8: "BEH", 9: "IO", 45: "PLOC"}
REF_CLASSES = {2, 3, 4, 6, 8, 9, 45}   # classes that appear as object references

# Parameter-type GUIDs (guid_lo, guid_hi) recovered empirically from AvatarMP.cmo
# by correlating decoded values with parameter names. Size alone disambiguates
# vectors/colors; for the 4-byte types the GUID tells int vs float vs bool.
FLOAT_GUIDS = {
    (0x47884C3F, 0x432C2C20),   # Float (Pos X/Y = 10.0)
    (0x54B4422B, 0x730F0F4F),   # Time/Float seconds (frequency, time)
}
INT_GUIDS = {
    (0x5A5716FD, 0x44E276D7),   # Int (Z Order)
    (0x79B90856, 0x75D070FF),   # Int/enum (Test operator)
}
BOOL_GUIDS = {
    (0x1AD52A8E, 0x5E741920),   # Bool (Pickable)
}
STRING_GUIDS = {
    (0x6BD010E2, 0x115617EA),   # String (name)
}
ATTR_MSG_GUID = {
    (0x03881E12, 0x5BA34E2B): "msg",    # Message (-> message-manager index)
    (0x3EA34EE9, 0x09FA5366): "attr",   # Attribute (-> attribute-manager index)
}


def decode_value(guid, raw):
    """Decode a raw CKParameter value buffer into a readable string using the
    parameter-type GUID and the buffer size."""
    n = len(raw)
    if n == 0:
        return "(empty)"
    if guid in STRING_GUIDS or (n > 4 and _looks_ascii(raw)):
        return '"' + raw.split(b"\x00", 1)[0].decode("latin-1") + '"'
    if guid in BOOL_GUIDS and n >= 4:
        return "true" if int.from_bytes(raw[:4], "little") else "false"
    if guid in INT_GUIDS and n >= 4:
        return str(int.from_bytes(raw[:4], "little", signed=True))
    if guid in FLOAT_GUIDS and n >= 4:
        return _fmt_float(raw[:4])
    if n == 4:
        i = int.from_bytes(raw, "little", signed=True)
        f = _fmt_float(raw)
        return f"{i}" if (i == 0 or abs(i) < 1 << 20) and "e" not in f else f"i={i}/f={f}"
    if n in (8, 12, 16) and n % 4 == 0:
        comps = [_fmt_float(raw[i:i + 4]) for i in range(0, n, 4)]
        return "(" + ", ".join(comps) + ")"
    return "0x" + raw.hex()


def _fmt_float(b4):
    import struct
    f = struct.unpack("<f", b4)[0]
    if f == int(f) and abs(f) < 1e9:
        return str(int(f))
    return f"{f:.4g}"


def _looks_ascii(raw):
    body = raw.split(b"\x00", 1)[0]
    return len(body) >= 2 and all(32 <= c < 127 for c in body)


class GraphModel:
    """Decoded behavior graph: object metadata + ownership + execution links."""

    MESSAGE_MGR = (0x466A0FAC, 0)
    ATTRIBUTE_MGR = (0x3D242466, 0)

    def __init__(self, rows, managers=None):
        # rows: idx -> dict(cid,name,ids); managers: guid -> [dwords]
        self.rows = rows
        self.managers = managers or {}
        self.messages = self._parse_string_table(self.MESSAGE_MGR, header_bytes=12)
        self.attributes = self._parse_attributes()   # {global attr index -> name}
        self.io_owner = {}                       # io idx -> owning behavior idx
        self.children = {}                       # graph beh idx -> [sub-behavior idx]
        self.parent = {}                         # sub-behavior idx -> graph beh idx
        self.beh_inputs = {}                     # beh idx -> [input IO idx]
        self.beh_outputs = {}                    # beh idx -> [output IO idx]
        self.beh_params = {}                     # beh idx -> {role: [param idx]}
        self.links = []                          # (inIO, outIO) per CKBehaviorLink
        self.unparsed = []                       # behaviors we couldn't decode
        self._build()

    def _newdata(self, idx):
        return self.rows[idx]["ids"].get(0x20, [])

    def _parse_string_table(self, guid, header_bytes):
        """Extract the ordered length-prefixed string list from a manager chunk.
        Message Manager: strings are stored sequentially as [len][bytes(padded)],
        so list position == message-type index. (Attribute Manager entries are
        variable-length / interleaved, so its list is a best-effort name index
        only — do not rely on exact attribute indices.)"""
        dw = self.managers.get(guid)
        if not dw:
            return []
        blob = b"".join(d.to_bytes(4, "little") for d in dw)
        names, i = [], header_bytes
        while i + 4 <= len(blob):
            n = int.from_bytes(blob[i:i + 4], "little")
            if 1 <= n <= 64 and i + 4 + n <= len(blob):
                s = blob[i + 4:i + 4 + n].split(b"\x00", 1)[0]
                if s and all(32 <= c < 127 for c in s):
                    names.append(s.decode("latin-1"))
                    i += 4 + ((n + 3) // 4) * 4
                    continue
            i += 4
        return names

    def _parse_attributes(self):
        """Decode the Attribute Manager into {global_attribute_index -> name}.

        Each attribute entry is [paramTypeGUID(2)][4 dwords][nameLen][name]. The
        first ~46 built-in entries carry extra default-value fields and don't
        chain cleanly, so we strict-chain the trailing run (which reaches the end
        of the chunk) and offset it by (declared_count - chain_len). This places
        the game attributes at their true global indices (validated:
        avatarMP_localEntity = 877, matching the engine's attribute-type id)."""
        dw = self.managers.get(self.ATTRIBUTE_MGR)
        if not dw or len(dw) < 4:
            return {}
        blob = b"".join(d.to_bytes(4, "little") for d in dw)
        N = len(blob)
        declared = int.from_bytes(blob[12:16], "little")

        def chain_from(start):
            i, out = start, []
            while i + 24 <= N:
                j = i + 24                       # skip type GUID(2) + 4 dwords
                L = int.from_bytes(blob[j:j + 4], "little")
                if not (1 <= L <= 64) or j + 4 + L > N:
                    break
                s = blob[j + 4:j + 4 + L].split(b"\x00", 1)[0]
                if not (s and all(32 <= c < 127 for c in s)):
                    break
                out.append(s.decode("latin-1"))
                i = j + 4 + ((L + 3) // 4) * 4
            return out, i

        for st in range(32, 6000, 4):
            names, end = chain_from(st)
            if len(names) > 100 and N - end < 24:
                offset = max(0, declared - len(names))
                return {offset + k: nm for k, nm in enumerate(names)}
        return {}

    def _is_ref(self, d):
        r = self.rows.get(d)
        return r is not None and r["cid"] in REF_CLASSES

    def _try_parse(self, d, start):
        """Parse d[start:] as a sequence of count-prefixed object-ref lists."""
        i, out, n = start, [], len(d)
        while i < n:
            c = d[i]
            if c == 0:
                out.append([]); i += 1; continue
            if c < 1 or c > 1024 or i + 1 + c > n:
                return None
            refs = d[i + 1:i + 1 + c]
            if not all(self._is_ref(r) for r in refs):
                return None
            out.append(refs); i += 1 + c
        return out

    def _parse_behavior(self, d):
        """Return (head_len, [ref_list,...]) by finding the smallest head that
        makes the remainder parse as exact count-prefixed ref-lists."""
        for h in range(1, 13):
            if h > len(d):
                break
            r = self._try_parse(d, h)
            if r is not None and len(r) >= 1:
                return h, r
        return None, None

    def _build(self):
        rows = self.rows
        for idx, r in rows.items():
            r["iof"] = (r["ids"].get(0x08) or [None])[0]
        for idx, r in rows.items():
            if r["cid"] != 8:
                continue
            _, lists = self._parse_behavior(self._newdata(idx))
            if not lists:
                self.unparsed.append(idx)
                continue
            ins, outs, kids = [], [], []
            params = {"PIN": [], "POUT": [], "PLOC": [], "POP": []}
            for lst in lists:
                for ref in lst:
                    c = rows[ref]["cid"]
                    if c == 9:
                        if (rows[ref]["iof"] or 0) & 2:
                            outs.append(ref)
                        else:
                            ins.append(ref)
                        self.io_owner[ref] = idx
                    elif c == 8:
                        kids.append(ref); self.parent[ref] = idx
                    elif c in (2, 3, 45, 4):
                        params[CID[c]].append(ref)
            self.beh_inputs[idx] = ins
            self.beh_outputs[idx] = outs
            self.beh_params[idx] = params
            if kids:
                self.children[idx] = kids
        for idx, r in rows.items():
            if r["cid"] != 6:
                continue
            d = self._newdata(idx)
            if len(d) >= 3:
                self.links.append((d[1], d[2]))

    # -- queries -----------------------------------------------------------
    def name(self, idx):
        r = self.rows.get(idx)
        return r["name"] if r else f"?{idx}"

    def param_value(self, idx):
        """Decode a CKParameter's literal value into a readable string, or None.
        I40 (saved value) = [guid_lo, guid_hi, mode, ...]:
          mode 1 -> raw buffer: [1, size_bytes, data dwords...]
          mode 2 -> object reference: [2, object_index] (0xffffffff = null)
          message/attribute types -> trailing dword is a manager-table index.
        I1000 (data source) means the param reads from another param (no literal).
        """
        r = self.rows.get(idx)
        if not r:
            return None
        ids = r["ids"]
        if 0x1000 in ids and 0x40 not in ids:
            src = ids[0x1000][-1] if ids[0x1000] else None
            if src in self.rows:
                # follow the data link one hop, but only into a source that holds a
                # literal value (id 0x40) — never another source link, so no loops.
                if 0x40 in self.rows[src]["ids"]:
                    sval = self.param_value(src)
                    if sval:
                        return f"{sval} (<-{self.name(src)})"
                return f"<-{self.name(src)}"
            return None
        body = ids.get(0x40)
        if not body or len(body) < 3:
            return None
        guid = (body[0], body[1])
        mode = body[2]
        rest = body[3:]
        if mode == 2:                                   # object reference
            ref = rest[0] if rest else 0xFFFFFFFF
            if ref == 0xFFFFFFFF:
                return "null"
            return "@" + self.name(ref) if ref in self.rows else f"@#{ref}"
        if mode == 1 and rest:                          # raw value buffer
            size = rest[0]
            data = rest[1:]
            raw = b"".join(d.to_bytes(4, "little") for d in data)[:size]
            return decode_value(guid, raw)
        if mode == 3:
            return "(default)"
        # message / attribute / enum: manager-index types
        tail = body[-1]
        kind = ATTR_MSG_GUID.get(guid, "idx")
        if kind == "msg" and tail < len(self.messages):
            return self.messages[tail]            # resolved message name
        if kind == "attr" and tail in self.attributes:
            return self.attributes[tail]          # resolved attribute name
        return f"{kind}#{tail}"

    def resolve(self, key):
        """Accept an index or a (case-insensitive) behavior name. When a name is
        ambiguous, prefer a graph behavior (one with sub-behaviors) over a leaf,
        and an exact match over a substring match."""
        if key.isdigit() and int(key) in self.rows:
            return int(key)
        k = key.lower()
        exact = [i for i, r in self.rows.items()
                 if r["cid"] == 8 and r["name"].lower() == k]
        sub = [i for i, r in self.rows.items()
               if r["cid"] == 8 and k in r["name"].lower()]
        for pool in (exact, sub):
            graphs = [i for i in pool if i in self.children]
            if graphs:
                return max(graphs, key=lambda i: len(self.children[i]))
            if pool:
                return pool[0]
        return None

    def script_roots(self):
        """Graph behaviors (have children) that are not nested in another graph."""
        return sorted(g for g in self.children if g not in self.parent)

    def leaf_block_guid(self, idx):
        """Prototype GUID of a leaf building block (None for graph behaviors).
        A leaf's NEWDATA is [flags, guid_lo, guid_hi, ...]; graphs carry no GUID.
        The GUID + name is the bridge to the block's C++ implementation (custom
        blocks are declared in the game DLL, findable by the name string)."""
        if idx in self.children:
            return None
        nd = self._newdata(idx)
        return (nd[1], nd[2]) if len(nd) >= 3 else None

    def leaf_blocks(self):
        """Counter of distinct leaf building-block types keyed by (name, guid)."""
        import collections
        out = collections.Counter()
        for idx, r in self.rows.items():
            if r["cid"] == 8 and idx not in self.children:
                out[(r["name"], self.leaf_block_guid(idx))] += 1
        return out

    # -- parameter data-flow -------------------------------------------------
    def _dataflow_index(self):
        """Build reverse indices for tracing how a parameter value is produced:
          pin_src[pin]   -> source param it reads from (PIN data link, id 0x1000)
          pout_op[pout]  -> Parameter Operation producing it; op_in[op] -> inputs
          pout_beh[pout] -> behaviour producing it (computed by the BB's C++)
        """
        if hasattr(self, "_df"):
            return self._df
        pin_src, pout_op, op_in, pout_beh = {}, {}, {}, {}
        for idx, r in self.rows.items():
            if r["cid"] == 2:                              # PIN
                s = r["ids"].get(0x1000)
                if s:
                    pin_src[idx] = s[-1]
            elif r["cid"] == 4:                            # Parameter Operation
                d = r["ids"].get(0x400)
                if d and len(d) >= 4:
                    refs = d[3:3 + d[2]]
                    if len(refs) >= 1:
                        op_in[idx] = refs[:-1]
                        pout_op[refs[-1]] = idx
        for beh, prm in self.beh_params.items():
            for p in prm.get("POUT", []):
                pout_beh[p] = beh
        self._df = {"pin_src": pin_src, "pout_op": pout_op,
                    "op_in": op_in, "pout_beh": pout_beh}
        return self._df

    def trace_param(self, idx, depth=0, seen=None, maxdepth=14):
        """Backward data-flow tree: what produces this parameter's value, down to
        leaves (literals, attribute refs, or a BB output we can't see past)."""
        df = self._dataflow_index()
        seen = seen or set()
        r = self.rows.get(idx)
        if not r:
            return [f"{'  ' * depth}?{idx}"]
        cid, nm = r["cid"], r["name"]
        val = self.param_value(idx)
        shown = f" = {val}" if (val and not val.startswith("<-")) else ""
        line = f"{'  ' * depth}{nm}{shown}  [{CID.get(cid, cid)} #{idx}]"
        if idx in seen or depth >= maxdepth:
            return [line + ("  (cycle)" if idx in seen else "  (…)")]
        seen = seen | {idx}
        out = [line]
        if cid == 2:                                       # PIN -> its source
            s = df["pin_src"].get(idx)
            if s is not None and s in self.rows:
                out += self.trace_param(s, depth + 1, seen, maxdepth)
        elif cid == 3:                                     # POUT -> producer
            if idx in df["pout_op"]:
                op = df["pout_op"][idx]
                out.append(f"{'  ' * (depth + 1)}└ op: {self.name(op)}")
                for inp in df["op_in"].get(op, []):
                    out += self.trace_param(inp, depth + 2, seen, maxdepth)
            elif idx in df["pout_beh"]:
                beh = df["pout_beh"][idx]
                ins = self.beh_params.get(beh, {}).get("PIN", [])
                out.append(f"{'  ' * (depth + 1)}└ computed by BB '{self.name(beh)}'"
                           f" (#{beh}) from {len(ins)} input(s):")
                for p in ins:
                    out += self.trace_param(p, depth + 2, seen, maxdepth)
        return out


def load_graph(args):
    rows = {}
    managers = {}                # (guid_lo, guid_hi) -> [dwords]
    if not args.refresh_graph and os.path.exists(args.graphcache):
        src = open(args.graphcache, encoding="utf-8", errors="replace")
    else:
        out = run_unvirt(args.unvirt, args.cmo, ["test"], items=400000)
        kept = [ln for ln in out.splitlines()
                if ln.startswith("G\t") or ln.startswith("M\t")]
        if not kept:
            # Don't cache a failed load — surface it instead of poisoning the cache.
            sys.stderr.write("Could not read any objects. Unvirt output head:\n")
            sys.stderr.write("\n".join(out.splitlines()[:15]) + "\n")
            return GraphModel({}, {})
        with open(args.graphcache, "w", encoding="utf-8", errors="replace") as fh:
            fh.write("\n".join(kept) + "\n")
        src = open(args.graphcache, encoding="utf-8", errors="replace")
    with src as fh:
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if p[0] == "M" and len(p) >= 4:        # manager: M\t<g1>\t<g2>\t<dwords>
                dw = [int(x, 16) for x in p[3].split(",")] if p[3] else []
                managers[(int(p[1], 16), int(p[2], 16))] = dw
                continue
            if len(p) < 5 or p[0] != "G":
                continue
            # tokens after name: I<idHex>=<dword,dword,...>  (one per CKStateChunk identifier)
            ids = {}
            for tok in p[5:]:
                if not tok.startswith("I") or "=" not in tok:
                    continue
                k, v = tok[1:].split("=", 1)
                ids[int(k, 16)] = [int(x, 16) for x in v.split(",")] if v else []
            rows[int(p[1])] = {"cid": int(p[3]), "name": p[4], "ids": ids}
    return GraphModel(rows, managers)


def cmd_scripts(args):
    g = load_graph(args)
    roots = g.script_roots()
    rooted = [(len(g.children[r]), r) for r in roots]
    for nkids, r in sorted(rooted, reverse=True):
        print(f"#{r:<7} {nkids:4d} sub-behaviors   {g.name(r)}")
    sys.stderr.write(f"\n{len(roots)} top-level script graphs "
                     f"({len(g.children)} graphs total, "
                     f"{len(g.unparsed)} behaviors unparsed)\n")


def _render_script(g, root, max_items=10000):
    kids = g.children.get(root, [])
    chset = set(kids)
    print(f"=== SCRIPT '{g.name(root)}'  (#{root}, {len(kids)} sub-behaviors) ===")
    print("--- nodes ---")
    for c in kids:
        ins = len(g.beh_inputs.get(c, []))
        outs = len(g.beh_outputs.get(c, []))
        prm = g.beh_params.get(c, {})
        tag = "graph" if c in g.children else "BB"
        parts = []
        for p in prm.get("PIN", []) + prm.get("PLOC", []):
            val = g.param_value(p)
            parts.append(f"{g.name(p)}={val}" if val is not None else g.name(p))
        extra = "  " + ", ".join(parts) if parts else ""
        print(f"  #{c:<7} [{tag}] {g.name(c)}   <{ins}in/{outs}out>{extra}")
    print("--- execution links ---")
    n = 0
    for a, b in g.links:
        oa, ob = g.io_owner.get(a), g.io_owner.get(b)
        if oa in chset and ob in chset:
            print(f"  {g.name(oa)}.{g.name(a)}  ->  {g.name(ob)}.{g.name(b)}")
            n += 1
            if n >= max_items:
                print("  ... (truncated)"); break
    if n == 0:
        print("  (no internal links)")


def cmd_script(args):
    g = load_graph(args)
    root = g.resolve(args.key)
    if root is None:
        sys.exit(f"No behavior matching {args.key!r}")
    _render_script(g, root)


def _script_dict(g, root, seen=None):
    """Serialize a script graph to a plain dict: nodes (with decoded params),
    nested sub-graphs, and internal execution links. Stable/ordered so two
    versions of a file can be diffed."""
    seen = seen if seen is not None else set()
    seen.add(root)
    kids = g.children.get(root, [])
    chset = set(kids)
    nodes = []
    for c in kids:
        prm = g.beh_params.get(c, {})
        params = {}
        for role, key in (("PIN", "in"), ("POUT", "out"), ("PLOC", "local")):
            if prm.get(role):
                params[key] = [{"name": g.name(p), "value": g.param_value(p)}
                               for p in prm[role]]
        node = {
            "index": c,
            "name": g.name(c),
            "kind": "graph" if c in g.children else "bb",
            "inputs": [g.name(io) for io in g.beh_inputs.get(c, [])],
            "outputs": [g.name(io) for io in g.beh_outputs.get(c, [])],
            "params": params,
        }
        if c in g.children and c not in seen:
            sub = _script_dict(g, c, seen)
            node["children"] = sub["nodes"]
            node["links"] = sub["links"]
        nodes.append(node)
    links = []
    for a, b in g.links:
        oa, ob = g.io_owner.get(a), g.io_owner.get(b)
        if oa in chset and ob in chset:
            links.append({"from": {"behavior": g.name(oa), "io": g.name(a)},
                          "to": {"behavior": g.name(ob), "io": g.name(b)}})
    return {"index": root, "name": g.name(root), "nodes": nodes, "links": links}


def cmd_blocks(args):
    """List distinct leaf building-block types with their prototype GUID + usage
    count — the blocks whose implementation is C++ (Virtools' DLLs for standard
    blocks, the game DLL for custom ones), not in the scene.

    With --dll <strings-file> (e.g. `strings game.dll`, or
    `rizin -qc izzj game.dll | jq -r .[].string`), blocks whose name appears in
    that file are flagged [DLL] — i.e. implemented in the game binary."""
    g = load_graph(args)
    blocks = g.leaf_blocks()
    needle = args.filter.lower() if args.filter else None
    dll_names = None
    if args.dll:
        with open(args.dll, encoding="utf-8", errors="replace") as fh:
            dll_names = {ln.rstrip("\n") for ln in fh}
    shown = in_dll = 0
    for (name, guid), cnt in sorted(blocks.items(),
                                    key=lambda kv: (-kv[1], kv[0][0].lower())):
        if needle and needle not in name.lower():
            continue
        gs = f"{guid[0]:08x},{guid[1]:08x}" if guid else "(no guid)"
        tag = ""
        if dll_names is not None:
            hit = name in dll_names
            tag = "  [DLL]" if hit else ""
            in_dll += hit
        print(f"{cnt:5d}x  {gs}  {name}{tag}")
        shown += 1
    msg = f"\n{shown} of {len(blocks)} distinct leaf building-block types"
    if dll_names is not None:
        msg += f"; {in_dll} confirmed in the supplied DLL strings (custom/game blocks)"
    sys.stderr.write(msg + "\n")


def cmd_dataflow(args):
    """Trace backward what produces a parameter's value (by name or #index)."""
    g = load_graph(args)
    key = args.key
    if key.isdigit() and int(key) in g.rows:
        targets = [int(key)]
    else:
        k = key.lower()
        targets = [i for i, r in g.rows.items()
                   if r["cid"] in (3, 45) and r["name"].lower() == k]
        if not targets:
            targets = [i for i, r in g.rows.items()
                       if r["cid"] in (3, 45, 2) and k in r["name"].lower()]
    if not targets:
        sys.exit(f"No parameter matching {args.key!r}")
    for i, t in enumerate(targets[:args.limit]):
        if i:
            print()
        print("\n".join(g.trace_param(t)))
    if len(targets) > args.limit:
        sys.stderr.write(f"\n…{len(targets) - args.limit} more matches "
                         f"(raise --limit or use #index)\n")


def cmd_json(args):
    import json
    g = load_graph(args)
    if args.key:
        root = g.resolve(args.key)
        if root is None:
            sys.exit(f"No behavior matching {args.key!r}")
        out = _script_dict(g, root)
    else:
        out = {"file": os.path.basename(args.cmo or ""),
               "scripts": [_script_dict(g, r) for r in g.script_roots()]}
    json.dump(out, sys.stdout, indent=2, ensure_ascii=False)
    print()


def cmd_dot(args):
    g = load_graph(args)
    root = g.resolve(args.key)
    if root is None:
        sys.exit(f"No behavior matching {args.key!r}")
    kids = g.children.get(root, [])
    chset = set(kids)
    print(f'digraph "{g.name(root)}" {{')
    print('  rankdir=LR; node [shape=box, fontname="monospace"];')
    for c in kids:
        shape = "box3d" if c in g.children else "box"
        print(f'  n{c} [label="{g.name(c)}", shape={shape}];')
    for a, b in g.links:
        oa, ob = g.io_owner.get(a), g.io_owner.get(b)
        if oa in chset and ob in chset:
            print(f'  n{oa} -> n{ob} [label="{g.name(a)}>{g.name(b)}", '
                  f'fontsize=8];')
    print("}")


def cmd_messages(args):
    g = load_graph(args)
    for i, name in enumerate(g.messages):
        print(f"{i:4d}  {name}")
    sys.stderr.write(f"\n{len(g.messages)} message types\n")


def cmd_attributes(args):
    g = load_graph(args)
    needle = args.filter.lower() if args.filter else None
    shown = 0
    for i in sorted(g.attributes):
        name = g.attributes[i]
        if needle and needle not in name.lower():
            continue
        print(f"{i:4d}  {name}")
        shown += 1
    sys.stderr.write(f"\n{shown} attribute types "
                     f"({len(g.attributes)} resolved; built-in low indices omitted)\n")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--unvirt", default=DEFAULT_UNVIRT,
                   help="path to the patched LibCmo Unvirt (or $UNVIRT)")
    p.add_argument("--cmo", default=DEFAULT_CMO,
                   help="the .cmo/.nmo/.vmo to read (or $CMO_FILE)")
    p.add_argument("--cache", help="object-table cache path (default: auto)")
    p.add_argument("--graphcache", help="graph-dump cache path (default: auto)")
    p.add_argument("--refresh", action="store_true",
                   help="rebuild the object-table cache from the file")
    p.add_argument("--refresh-graph", action="store_true",
                   help="rebuild the graph-dump cache from the file")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("histogram").set_defaults(func=cmd_histogram)
    sub.add_parser("behaviors").set_defaults(func=cmd_behaviors)
    sp = sub.add_parser("search"); sp.add_argument("text"); sp.set_defaults(func=cmd_search)
    sub.add_parser("table").set_defaults(func=cmd_table)
    sp = sub.add_parser("chunk"); sp.add_argument("index"); sp.set_defaults(func=cmd_chunk)
    sub.add_parser("scripts").set_defaults(func=cmd_scripts)
    sp = sub.add_parser("script"); sp.add_argument("key"); sp.set_defaults(func=cmd_script)
    sp = sub.add_parser("dot"); sp.add_argument("key"); sp.set_defaults(func=cmd_dot)
    sp = sub.add_parser("json"); sp.add_argument("key", nargs="?")
    sp.set_defaults(func=cmd_json)
    sp = sub.add_parser("dataflow"); sp.add_argument("key")
    sp.add_argument("--limit", type=int, default=6)
    sp.set_defaults(func=cmd_dataflow)
    sp = sub.add_parser("blocks"); sp.add_argument("filter", nargs="?")
    sp.add_argument("--dll", help="strings file of the game DLL; flags blocks "
                                  "implemented there with [DLL]")
    sp.set_defaults(func=cmd_blocks)
    sub.add_parser("messages").set_defaults(func=cmd_messages)
    sp = sub.add_parser("attributes"); sp.add_argument("filter", nargs="?")
    sp.set_defaults(func=cmd_attributes)
    args = p.parse_args()
    if not args.cache:
        args.cache = _cache_path(args.cmo, "_obj.tsv")
    if not args.graphcache:
        args.graphcache = _cache_path(args.cmo, "_graph.tsv")
    args.func(args)


if __name__ == "__main__":
    main()
