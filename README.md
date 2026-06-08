# virtools-script-dump

Read **Virtools behavior scripts** out of a `.cmo` / `.nmo` / `.vmo` file — on any
OS, **without the Virtools editor**.

Virtools (the 3D engine behind many 2000s games, kiosks and web experiences)
stores its gameplay logic as a graph of **CKBehavior** "building blocks" wired by
links. Normally you need the Windows **Virtools Dev** editor to look at those
scripts. This tool reconstructs the graph straight from the file bytes and prints
it as readable text or Graphviz:

- the **behavior graphs** ("scripts") in the file,
- each behaviour's **execution links** (`A.Out -> B.In`),
- and **decoded parameter values** — floats, vectors, bools, strings, object
  references, and resolved **message** and **attribute** names.

```text
SCRIPT 'controller'  (#1287, 19 sub-behaviors)
--- nodes ---
  #1290 [graph] intro          Up=(0, 1, 0), Message=msg_intro_go, MIN=5.4, MAX=5.6
  #1296 [BB]    Wait Message    Message=msg_ready (<-Message)
  #1301 [BB]    Send Message    Message=msg_start, Dest=@Level
  #1340 [BB]    Has Attribute   Attribute=player_ready
--- execution links ---
  intro.Out 0  ->  Send Message.In
  Wait Message.Out  ->  Has Attribute.In
```

## Why
Existing tools that surface Virtools scripts (e.g. SuperScriptMaterializer,
VirtoolsScriptDeobfuscation) run **inside Virtools Dev on Windows**. This one
needs only the file and a terminal — useful for game preservation, modding,
asset archaeology and reverse engineering on Linux/macOS.

## How it works
It drives a small **patched build** of [yyc12345's LibCmo
`Unvirt`](https://github.com/yyc12345/libcmo21) (MIT). LibCmo already parses the
CMO container; the patch (`libcmo-virtools-script-dump.patch`) adds three things:

1. **CRC-tolerant loading** — some files ship a tampered/zeroed header CRC that
   makes stock `Unvirt` reject them; the patch downgrades the CRC mismatch to a
   warning (the compressed body is intact).
2. A **generic chunk dump** (repurposing the `test` command): every object's
   `CKStateChunk` identifiers as raw dwords, plus the manager chunks.
3. A `CKStateChunk::GetObjectList()` accessor.

`cmo_script_dump.py` then decodes those dumps offline. Modern Virtools (4.0)
packs each behaviour/link/IO/parameter into a `CK_STATESAVE_*_NEWDATA` blob with
inline object references stored as file indices:

| element | layout |
|---|---|
| link    | `[delay, inIO_idx, outIO_idx]` |
| IO flag | `1` = input, `2` = output (for link orientation) |
| behaviour | `[flags, (typeGUID if leaf BB), scalars…, count-prefixed ref-lists]` |
| param value | `[guid, mode, …]` — raw buffer / object-ref / manager index |

Message names come from the Message Manager chunk; attribute names from the
Attribute Manager chunk. On the game this was developed against the
reconstruction was validated globally: **24553/24555** IOs resolve to exactly one
owning behaviour and **13114/13117** link endpoints resolve.

> Note: the parameter-type GUIDs and the behaviour-blob heuristics were recovered
> from one Virtools 4.0 title. They cover the standard Virtools types, but exotic
> custom parameter types may show as raw hex. PRs with more GUIDs welcome.

## Setup (Linux)

You need the patched `Unvirt` binary, Python 3.10+, and `script(1)` (util-linux);
`graphviz` only if you want to render the `dot` output.

### Option A — download the prebuilt Unvirt (recommended)

A patched `Unvirt` is built by CI and published here:
**https://github.com/HeiseMo/libcmo21/releases/tag/unvirt-linux**

```bash
curl -L -o unvirt.tgz \
  https://github.com/HeiseMo/libcmo21/releases/download/unvirt-linux/unvirt-linux-x64.tar.gz
tar xzf unvirt.tgz
export UNVIRT="$PWD/unvirt-linux-x64/run-unvirt.sh"   # wrapper finds bundled libs
export CMO_FILE=/path/to/your.cmo                     # or pass --cmo each time
```

### Option B — build it yourself

The patched source lives in the fork **https://github.com/HeiseMo/libcmo21**
(LibCmo `v0.4.0` + the included `libcmo-virtools-script-dump.patch`). Needs
CMake ≥ 3.23 and a C++23 compiler; the exact dependency pins and build steps are
in `.github/workflows/unvirt-release.yml` there (YYCCommonplace v2.0.0, zlib
v1.3.1, stb). Then:

```bash
export UNVIRT=/path/to/your/build/Unvirt
```

## Usage

```bash
cmo_script_dump.py scripts                 # list the behavior-graph "scripts"
cmo_script_dump.py script <index|name>     # render one: nodes + params + exec links
cmo_script_dump.py dot <index|name> | dot -Tsvg -o script.svg
cmo_script_dump.py search <text>           # any object by name
cmo_script_dump.py messages                # Message Manager table (index -> name)
cmo_script_dump.py attributes [filter]     # Attribute Manager table (index -> name)
cmo_script_dump.py histogram               # CK class-id counts
cmo_script_dump.py chunk <index>           # raw CKStateChunk for one object
```

The first `scripts`/`script` call runs `Unvirt` once and caches the decoded dump
next to the script (`.cache_<file>_*.tsv`); later calls are instant. Use
`--refresh-graph` to rebuild.

## License
MIT (see `LICENSE`). The included patch modifies
[libcmo21](https://github.com/yyc12345/libcmo21) by yyc12345, also MIT.

This tool reads file formats; it ships no game assets. Respect the copyright of
any files you open with it.
