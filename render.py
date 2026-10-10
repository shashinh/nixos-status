#!/usr/bin/env python3
# nixos-status: static NixOS system status page, regenerated on every switch.
# Copyright (C) 2026 Shashin Halalingaiah
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""nixos-status command line.

Subcommands:
  generate   (root, run by the activation hook) combine the evaluation-time
             manifest with runtime facts, append a record to the history and
             render index.html
  text       print a plain-text summary of the latest record (default)
  open       open the page in the default browser
  path       print the path of the page

Everything is standard library only. The page is rendered here, once; the
browser only displays it.
"""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import html
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
from collections import OrderedDict
from pathlib import Path

DEFAULT_STATE = "/var/lib/nixos-status"
PROFILES = "/nix/var/nix/profiles"
BOOT_PARTS = ("kernel", "kernel-modules", "initrd", "firmware")


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def log(msg: str) -> None:
    print(f"nixos-status: {msg}", file=sys.stderr)


def run(cmd: list[str], timeout: int = 60, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout, check=False, **kw
    )


def realpath(p: str | Path) -> str:
    return os.path.realpath(str(p))


def store_root(p: str) -> str:
    """The store path containing `p` (/nix/store/<hash>-<name>), or `p` itself."""
    parts = p.split("/")
    if len(parts) >= 4 and parts[1] == "nix" and parts[2] == "store":
        return "/".join(parts[:4])
    return p


def human(n: int | float | None, signed: bool = False) -> str:
    if n is None:
        return "–"
    sign = ""
    if signed:
        if n == 0:
            return "±0"
        sign = "+" if n > 0 else "−"
    n = abs(float(n))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if n < 1024 or unit == "TiB":
            if unit == "B":
                return f"{sign}{int(n)} {unit}"
            return f"{sign}{n:.1f} {unit}"
        n /= 1024
    return f"{sign}{n:.1f} TiB"


def fmt_ts(ts: int | None) -> str:
    if not ts:
        return "–"
    return dt.datetime.fromtimestamp(ts).astimezone().strftime("%Y-%m-%d")


def fmt_iso(s: str | None) -> str:
    if not s:
        return "–"
    try:
        return dt.datetime.fromisoformat(s).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return s


def short(rev: str | None, n: int = 7) -> str:
    return rev[:n] if rev else "–"


def compare_url(inp: dict, old: str | None, new: str | None) -> str | None:
    if not (old and new and inp.get("owner") and inp.get("repo")):
        return None
    t = inp.get("type")
    if t == "github":
        return f"https://github.com/{inp['owner']}/{inp['repo']}/compare/{old}...{new}"
    if t == "gitlab":
        return f"https://gitlab.com/{inp['owner']}/{inp['repo']}/-/compare/{old}...{new}"
    return None


def input_url(inp: dict) -> str | None:
    t = inp.get("type")
    if inp.get("owner") and inp.get("repo"):
        if t == "github":
            return f"https://github.com/{inp['owner']}/{inp['repo']}"
        if t == "gitlab":
            return f"https://gitlab.com/{inp['owner']}/{inp['repo']}"
        if t == "sourcehut":
            return f"https://git.sr.ht/{inp['owner']}/{inp['repo']}"
    return inp.get("url")


def flake_rev(flake: dict | None) -> str:
    """Short revision of the configuration flake; '' when unknown."""
    if not flake:
        return ""
    rev = flake.get("shortRev") or short(flake.get("rev"))
    if rev == "–":
        return "unversioned"
    if flake.get("dirty") and not rev.endswith("-dirty"):
        rev += "-dirty"
    return rev


def input_label(inp: dict) -> str:
    if inp.get("owner") and inp.get("repo"):
        s = f"{inp['owner']}/{inp['repo']}"
        if inp.get("ref"):
            s += f"@{inp['ref']}"
        return s
    return inp.get("url") or inp.get("type") or "–"


# --------------------------------------------------------------------------
# generate: runtime facts
# --------------------------------------------------------------------------


def generation_links() -> dict[str, int]:
    """Map resolved system path -> generation number for every kept generation."""
    out: dict[str, int] = {}
    for link in glob.glob(f"{PROFILES}/system-*-link"):
        m = re.search(r"system-(\d+)-link$", link)
        if m:
            out[realpath(link)] = int(m.group(1))
    return out


def closure_sizes(paths: list[str]) -> dict[str, int]:
    """Closure size of each store path, via a single `nix path-info` call."""
    paths = sorted({p for p in paths if p and os.path.exists(p)})
    if not paths:
        return {}
    base = ["nix", "--extra-experimental-features", "nix-command", "path-info", "-S", "--json"]
    out = None
    for extra in (["--json-format", "1"], []):
        r = run(base + extra + paths, timeout=120)
        if r.returncode == 0 and r.stdout.strip():
            out = r.stdout
            break
    if out is None:
        log("nix path-info failed; sizes will be missing")
        return {}
    data = json.loads(out)
    if isinstance(data, dict) and "info" in data and isinstance(data["info"], dict):
        data = {f"/nix/store/{k}": v for k, v in data["info"].items()}
    sizes: dict[str, int] = {}
    if isinstance(data, dict):
        for k, v in data.items():
            if isinstance(v, dict) and v.get("closureSize") is not None:
                sizes[k] = int(v["closureSize"])
    elif isinstance(data, list):  # very old nix: a list of {path, closureSize}
        for v in data:
            if isinstance(v, dict) and v.get("closureSize") is not None:
                sizes[v["path"]] = int(v["closureSize"])
    return sizes


def disk_usage(paths: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for p in paths:
        if not os.path.isdir(p):
            continue
        r = run(["df", "-B1", "--output=size,used,avail,target", p])
        if r.returncode != 0:
            continue
        lines = r.stdout.strip().splitlines()
        if len(lines) < 2:
            continue
        parts = lines[-1].split()
        try:
            out[p] = {"size": int(parts[0]), "used": int(parts[1]), "avail": int(parts[2]),
                      "mount": parts[3] if len(parts) > 3 else p}
        except (ValueError, IndexError):
            continue
    return out


def flatpak_apps(users: list[str]) -> list[dict]:
    exe = shutil.which("flatpak") or (
        "/run/current-system/sw/bin/flatpak" if os.path.exists("/run/current-system/sw/bin/flatpak") else None
    )
    if not exe:
        return []
    cols = "--columns=application,name,version,origin,installation"
    apps: list[dict] = []

    def parse(text: str, user: str | None) -> None:
        for line in text.splitlines():
            f = line.rstrip("\n").split("\t")
            if len(f) < 4 or not f[0]:
                continue
            apps.append({
                "id": f[0], "name": f[1] or f[0], "version": f[2], "origin": f[3],
                "installation": f[4] if len(f) > 4 else ("user" if user else "system"),
                "user": user,
            })

    try:
        r = run([exe, "list", "--app", "--system", cols], timeout=10)
        if r.returncode == 0:
            parse(r.stdout, None)
    except subprocess.TimeoutExpired:
        log("flatpak list --system timed out")
    for u in users:
        try:
            home = pwd.getpwnam(u).pw_dir
        except KeyError:
            continue
        cmd = [exe, "list", "--app", "--user", cols]
        if os.geteuid() == 0 and pwd.getpwnam(u).pw_uid != 0:
            cmd = ["runuser", "-u", u, "--", "env", f"HOME={home}", *cmd]
        try:
            r = run(cmd, timeout=10)
            if r.returncode == 0:
                parse(r.stdout, u)
        except subprocess.TimeoutExpired:
            log(f"flatpak list --user timed out for {u}")
    return apps


def collect_runtime(manifest: dict, system_config: str) -> dict:
    sc = realpath(system_config)
    gens = generation_links()
    generation: int | str = gens.get(sc, "test")

    booted = None
    if os.path.exists("/run/booted-system"):
        b = realpath("/run/booted-system")
        booted = {
            "system": b,
            "generation": gens.get(b),
            "kernel": realpath(f"{b}/kernel") if os.path.exists(f"{b}/kernel") else None,
            "initrd": realpath(f"{b}/initrd") if os.path.exists(f"{b}/initrd") else None,
        }
    kernel = realpath(f"{sc}/kernel") if os.path.exists(f"{sc}/kernel") else None
    initrd = realpath(f"{sc}/initrd") if os.path.exists(f"{sc}/initrd") else None
    reboot_needed = bool(
        booted and ((booted["kernel"] and booted["kernel"] != kernel) or (booted["initrd"] and booted["initrd"] != initrd))
    )

    users = manifest.get("users", [])
    wanted = [sc, f"{sc}/sw"] + [f"{sc}/{p}" for p in BOOT_PARTS]
    wanted += [f"{sc}/etc/profiles/per-user/{u}" for u in users]
    pkg_paths = [op for g in manifest.get("groups", []) for p in g["packages"] for op in p.get("outPaths", [])]
    # `kernel` and `initrd` are links to files inside store paths; size the
    # store path that contains them.
    resolved = {w: store_root(realpath(w)) for w in wanted if os.path.exists(w)}
    sizes = closure_sizes(list(resolved.values()) + pkg_paths)

    def size_of(rel: str) -> int | None:
        r = resolved.get(rel)
        return sizes.get(r) if r else None

    package_sizes = {p: sizes[p] for p in pkg_paths if p in sizes}
    return {
        "format": 1,
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "generation": generation,
        "system_config": sc,
        "kernel": kernel,
        "initrd": initrd,
        "booted": booted,
        "reboot_needed": reboot_needed,
        "sizes": {
            "total": size_of(sc),
            "system_path": size_of(f"{sc}/sw"),
            "users": {u: size_of(f"{sc}/etc/profiles/per-user/{u}") for u in users},
            "boot": {p: size_of(f"{sc}/{p}") for p in BOOT_PARTS},
        },
        "package_sizes": package_sizes,
        "disk": disk_usage(["/nix/store", "/boot"]),
        "generations": {
            "count": len(gens),
            "oldest": min(gens.values()) if gens else None,
            "newest": max(gens.values()) if gens else None,
        },
        "flatpak": flatpak_apps(users) if manifest.get("flatpak") else [],
        "manifest": manifest,
    }


# --------------------------------------------------------------------------
# history
# --------------------------------------------------------------------------


def load_history(state: Path) -> list[dict]:
    """Records, newest first."""
    recs = []
    for f in (state / "history").glob("*.json"):
        try:
            with open(f) as fh:
                r = json.load(fh)
            r["_file"] = str(f)
            r["_mtime"] = f.stat().st_mtime_ns
            recs.append(r)
        except (OSError, ValueError):
            continue
    # Newest first. The write time breaks ties between records generated
    # within the same second.
    recs.sort(key=lambda r: (r.get("generated_at", ""), r["_mtime"]), reverse=True)
    return recs


def write_record(state: Path, rec: dict, keep: int) -> list[dict]:
    hist = state / "history"
    hist.mkdir(parents=True, exist_ok=True)
    recs = load_history(state)
    # Re-activating the generation that is already the newest record (switch
    # run twice, or the boot after `nixos-rebuild boot`) replaces that record
    # instead of adding a duplicate.
    if recs and recs[0].get("system_config") == rec["system_config"]:
        try:
            os.unlink(recs[0]["_file"])
        except OSError:
            pass
        recs = recs[1:]
    stamp = int(dt.datetime.fromisoformat(rec["generated_at"]).timestamp())
    name = f"{rec['generation']}-{stamp}.json"
    with tempfile.NamedTemporaryFile("w", dir=hist, delete=False) as tmp:
        json.dump({k: v for k, v in rec.items() if not k.startswith("_")}, tmp, indent=1, sort_keys=True)
        tmp.write("\n")
    os.chmod(tmp.name, 0o644)
    os.replace(tmp.name, hist / name)
    for old in recs[keep - 1:]:
        try:
            os.unlink(old["_file"])
        except OSError:
            pass
    latest = state / "latest.json"
    tmp_link = state / ".latest.tmp"
    if tmp_link.is_symlink() or tmp_link.exists():
        tmp_link.unlink()
    os.symlink(f"history/{name}", tmp_link)
    os.replace(tmp_link, latest)
    return load_history(state)


# --------------------------------------------------------------------------
# diffing
# --------------------------------------------------------------------------


def index_group(group: dict) -> "OrderedDict[str, dict]":
    """pname -> {versions: [..], source, size, description, entries}."""
    out: OrderedDict[str, dict] = OrderedDict()
    for p in group.get("packages", []):
        e = out.setdefault(p["pname"], {"versions": [], "source": p["source"], "description": p.get("description", ""),
                                        "homepage": p.get("homepage"), "position": p.get("position", ""),
                                        "outPaths": []})
        if p["version"] not in e["versions"]:
            e["versions"].append(p["version"])
        e["outPaths"].extend(p.get("outPaths", []))
        if p["source"] != e["source"]:
            e["source"] = f"{e['source']}, {p['source']}"
    for e in out.values():
        e["versions"].sort()
    return out


def flatpak_group(rec: dict) -> dict | None:
    apps = rec.get("flatpak") or []
    if not apps:
        return None
    return {
        "id": "flatpak",
        "title": "Flatpak applications",
        "packages": [
            {"pname": a["id"], "name": f"{a['id']}-{a['version']}", "version": a.get("version") or "",
             "source": f"{a.get('origin', '')} ({a.get('installation', '')})",
             "description": a.get("name", ""), "outPaths": [], "position": "", "homepage": None}
            for a in apps
        ],
    }


def record_groups(rec: dict) -> list[dict]:
    groups = list(rec["manifest"].get("groups", []))
    fp = flatpak_group(rec)
    if fp:
        groups.append(fp)
    return groups


def pkg_size(rec: dict, entry: dict) -> int | None:
    sizes = rec.get("package_sizes", {})
    vals = [sizes[p] for p in entry.get("outPaths", []) if p in sizes]
    return max(vals) if vals else None


def diff_groups(cur: dict, prev: dict | None) -> list[dict]:
    """Per group: rows with status in {same, changed, added, removed}."""
    prev_groups = {g["id"]: index_group(g) for g in record_groups(prev)} if prev else {}
    result = []
    for g in record_groups(cur):
        ci = index_group(g)
        pi = prev_groups.get(g["id"]) if prev else None
        rows = []
        for pname, e in ci.items():
            pe = pi.get(pname) if pi is not None else None
            if pi is None:
                status = "same"  # no history: nothing to compare against
            elif pe is None:
                status = "added"
            elif pe["versions"] != e["versions"]:
                status = "changed"
            else:
                status = "same"
            rows.append({
                "pname": pname, "versions": e["versions"], "prev_versions": pe["versions"] if pe else [],
                "source": e["source"], "description": e["description"], "homepage": e["homepage"],
                "position": e["position"], "size": pkg_size(cur, e), "status": status,
            })
        if pi is not None:
            for pname, pe in pi.items():
                if pname not in ci:
                    rows.append({
                        "pname": pname, "versions": [], "prev_versions": pe["versions"], "source": pe["source"],
                        "description": pe["description"], "homepage": pe["homepage"], "position": pe["position"],
                        "size": pkg_size(prev, pe), "status": "removed",
                    })
        rows.sort(key=lambda r: (r["pname"].lower(), r["versions"]))
        counts = {s: sum(1 for r in rows if r["status"] == s) for s in ("changed", "added", "removed")}
        counts["total"] = sum(1 for r in rows if r["status"] != "removed")
        result.append({"id": g["id"], "title": g["title"], "rows": rows, "counts": counts, "compared": pi is not None})
    return result


def diff_inputs(cur: dict, prev: dict | None) -> list[dict]:
    prev_by = {i["name"]: i for i in (prev or {}).get("manifest", {}).get("inputs", [])} if prev else {}
    out = []
    for i in cur["manifest"].get("inputs", []):
        p = prev_by.get(i["name"])
        moved = bool(p and p.get("rev") != i.get("rev"))
        out.append({**i, "moved": moved, "prev_rev": p.get("rev") if p else None,
                    "compare": compare_url(i, p.get("rev"), i.get("rev")) if moved else None})
    return out


def changelog_entry(cur: dict, prev: dict) -> dict:
    groups = diff_groups(cur, prev)
    changes = []
    for g in groups:
        for r in g["rows"]:
            if r["status"] != "same":
                changes.append({**r, "group": g["title"]})
    moved = [i for i in diff_inputs(cur, prev) if i["moved"]]
    cs, ps = cur["manifest"]["system"], prev["manifest"]["system"]
    return {
        "cur": cur, "prev": prev, "changes": changes, "moved": moved,
        "size_delta": (cur["sizes"].get("total") or 0) - (prev["sizes"].get("total") or 0)
        if cur["sizes"].get("total") is not None and prev["sizes"].get("total") is not None else None,
        "nixos_changed": cs["nixos"]["version"] != ps["nixos"]["version"],
        "kernel_changed": cs["kernel"]["version"] != ps["kernel"]["version"],
        "counts": {s: sum(1 for c in changes if c["status"] == s) for s in ("changed", "added", "removed")},
    }


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root{--bg:#fff;--fg:#1a1a1a;--muted:#666;--line:#ddd;--accent:#2b5db9;--add:#1b7f3b;--del:#b3261e;--chg:#8a5a00;--warn-bg:#fff4d6;--warn-fg:#5a3b00;--row:#f6f6f6;--code:#f0f0f0}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#121212;--fg:#e4e4e4;--muted:#9a9a9a;--line:#333;--accent:#7aa7ff;--add:#5fbf7a;--del:#ff7b72;--chg:#e0b050;--warn-bg:#3a2d00;--warn-fg:#ffd98a;--row:#1a1a1a;--code:#222}}
:root[data-theme=dark]{--bg:#121212;--fg:#e4e4e4;--muted:#9a9a9a;--line:#333;--accent:#7aa7ff;--add:#5fbf7a;--del:#ff7b72;--chg:#e0b050;--warn-bg:#3a2d00;--warn-fg:#ffd98a;--row:#1a1a1a;--code:#222}
html{background:var(--bg);color:var(--fg)}
body{margin:0 auto;max-width:1200px;padding:1rem 16px 3rem;font:14px/1.45 system-ui,sans-serif;background:var(--bg);color:var(--fg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
h1{font-size:1.4rem;margin:.2rem 0 .4rem}h2{font-size:1.1rem;margin:1.8rem 0 .5rem;border-bottom:1px solid var(--line);padding-bottom:.2rem}
header{display:flex;flex-wrap:wrap;align-items:baseline;gap:.6rem 1.2rem}
header .meta{color:var(--muted)}
button{font:inherit;background:none;border:1px solid var(--line);color:var(--fg);border-radius:4px;padding:.15rem .6rem;cursor:pointer;margin-left:auto}
code,.mono{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:.92em}
code{background:var(--code);padding:0 .25em;border-radius:3px}
table{border-collapse:collapse;width:100%}
th,td{text-align:left;vertical-align:top;padding:.25rem .5rem;border-bottom:1px solid var(--line)}
th{font-weight:600;color:var(--muted);white-space:nowrap}
tbody tr:nth-child(even){background:var(--row)}
td.num,th.num{text-align:right;white-space:nowrap;font-variant-numeric:tabular-nums}
td.v{white-space:nowrap}
.kv td:first-child{color:var(--muted);white-space:nowrap;width:12rem}
.muted{color:var(--muted)}.small{font-size:.85em}
.delta{color:var(--muted)}.delta.up{color:var(--del)}.delta.down{color:var(--add)}
.banner{background:var(--warn-bg);color:var(--warn-fg);padding:.6rem .9rem;border-radius:4px;margin:1rem 0}
.controls{display:flex;flex-wrap:wrap;gap:.4rem 1.2rem;align-items:center;margin:1rem 0 .4rem;position:sticky;top:0;background:var(--bg);padding:.5rem 0;border-bottom:1px solid var(--line);z-index:1}
.controls input[type=search]{font:inherit;padding:.2rem .4rem;border:1px solid var(--line);border-radius:4px;background:var(--bg);color:var(--fg);min-width:14rem}
.controls nav a{margin-right:.8rem}
.prev{display:none;color:var(--muted)}
body.show-prev .prev{display:inline}
tr.removed{display:none}tr.removed td{text-decoration:line-through;color:var(--muted)}
body.show-prev tr.removed,body.only-changed tr.removed{display:table-row}
body.only-changed tr.pkg.same{display:none}
tr.nomatch{display:none!important}
tr.changed td.v{color:var(--chg)}tr.added td.v{color:var(--add)}
.tag{display:inline-block;padding:0 .35em;border-radius:3px;font-size:.8em;border:1px solid var(--line);color:var(--muted);white-space:nowrap}
.tag.moved{color:var(--chg);border-color:var(--chg)}
.tag.add{color:var(--add);border-color:var(--add)}.tag.del{color:var(--del);border-color:var(--del)}.tag.chg{color:var(--chg);border-color:var(--chg)}
details{margin:.4rem 0}summary{cursor:pointer}
details ul{margin:.3rem 0 .6rem 1.2rem;padding:0}
footer{margin-top:3rem;color:var(--muted);font-size:.85em;border-top:1px solid var(--line);padding-top:.6rem}
.cols{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:0 2rem}
"""

JS = """
(function(){
var root=document.documentElement,body=document.body;
function setTheme(t){if(t){root.dataset.theme=t}else{delete root.dataset.theme}try{t?localStorage.setItem('nixos-status-theme',t):localStorage.removeItem('nixos-status-theme')}catch(e){}}
document.getElementById('theme').addEventListener('click',function(){var dark=root.dataset.theme?root.dataset.theme==='dark':matchMedia('(prefers-color-scheme: dark)').matches;setTheme(dark?'light':'dark')});
function bind(id,cls){var el=document.getElementById(id);function apply(){body.classList.toggle(cls,el.checked)}el.addEventListener('change',apply);apply()}
bind('show-prev','show-prev');bind('only-changed','only-changed');
var rows=document.querySelectorAll('tr.pkg'),q=document.getElementById('q');
q.addEventListener('input',function(){var s=q.value.trim().toLowerCase();for(var i=0;i<rows.length;i++){rows[i].classList.toggle('nomatch',s!==''&&rows[i].dataset.k.indexOf(s)<0)}});
})();
"""

THEME_BOOT = "try{var t=localStorage.getItem('nixos-status-theme');if(t)document.documentElement.dataset.theme=t}catch(e){}"


def esc(s) -> str:
    return html.escape(str(s if s is not None else ""), quote=True)


def delta_html(cur: int | None, prev: int | None) -> str:
    if cur is None or prev is None:
        return ""
    d = cur - prev
    cls = "up" if d > 0 else "down" if d < 0 else ""
    return f' <span class="delta {cls}">{esc(human(d, signed=True))}</span>'


def version_cell(r: dict) -> str:
    cur = ", ".join(r["versions"]) if r["versions"] else ""
    if r["status"] == "removed":
        return f'<span class="prev">{esc(", ".join(r["prev_versions"]))} → </span><span class="muted">removed</span>'
    if r["status"] == "changed":
        return f'<span class="prev">{esc(", ".join(r["prev_versions"]))} → </span>{esc(cur)}'
    if r["status"] == "added":
        return f'<span class="prev">new → </span>{esc(cur)}'
    return esc(cur)


def render_html(records: list[dict]) -> str:
    cur = records[0]
    prev = next((r for r in records[1:] if r.get("system_config") != cur.get("system_config")), None)
    m = cur["manifest"]
    sysinfo = m["system"]
    flake = sysinfo.get("flake") or {}
    groups = diff_groups(cur, prev)
    inputs = diff_inputs(cur, prev)
    gen = cur["generation"]
    out: list[str] = []
    w = out.append

    title = f"{sysinfo['hostname']} · NixOS status"
    w("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n")
    w("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n")
    w(f"<title>{esc(title)}</title>\n<script>{THEME_BOOT}</script>\n<style>{CSS}</style>\n</head>\n<body>\n")

    # header
    rev_txt = flake_rev(flake)
    w("<header>")
    w(f"<h1>{esc(sysinfo['hostname'])}</h1>")
    w(f"<span class=\"meta\">generation <b>{esc(gen)}</b> · activated {esc(fmt_iso(cur['generated_at']))}")
    if rev_txt:
        w(f" · flake <span class=\"mono\">{esc(rev_txt)}</span>")
    w("</span>")
    w("<button id=\"theme\" type=\"button\" title=\"Toggle light/dark\">light / dark</button>")
    w("</header>\n")

    # reboot banner
    if cur.get("reboot_needed"):
        b = cur["booted"] or {}
        bk = os.path.basename(b.get("kernel") or "").replace("-linux-", " ") or "?"
        ck = os.path.basename(cur.get("kernel") or "").replace("-linux-", " ") or "?"
        w(f"<div class=\"banner\"><b>Reboot needed.</b> Booted generation {esc(b.get('generation') or '?')} "
          f"runs kernel <span class=\"mono\">{esc(bk)}</span>; the active generation {esc(gen)} ships "
          f"<span class=\"mono\">{esc(ck)}</span> (or a different initrd).</div>\n")

    # versions + sizes side by side
    w("<div class=\"cols\">\n<div>\n<h2>Versions</h2>\n<table class=\"kv\"><tbody>")
    ps = prev["manifest"]["system"] if prev else None

    def vrow(label: str, val: str, pval: str | None = None, extra: str = "") -> None:
        prevtxt = f'<span class="prev">{esc(pval)} → </span>' if pval is not None and pval != val else ""
        w(f"<tr><td>{esc(label)}</td><td>{prevtxt}{esc(val)}{extra}</td></tr>")

    nx = sysinfo["nixos"]
    vrow("NixOS", f"{nx['version']}" + (f" ({nx['codeName']})" if nx.get("codeName") else ""),
         ps["nixos"]["version"] if ps else None)
    nixpkgs_in = next((i for i in inputs if i["name"] == "nixpkgs"), None)
    npr = nx.get("nixpkgsRevision")
    extra = ""
    if nixpkgs_in and nixpkgs_in["moved"] and nixpkgs_in["compare"]:
        extra = f' <a class="small" href="{esc(nixpkgs_in["compare"])}">compare</a>'
    vrow("nixpkgs", short(npr, 12) if npr else "–",
         short(ps["nixos"].get("nixpkgsRevision"), 12) if ps and ps["nixos"].get("nixpkgsRevision") else None, extra)
    vrow("Kernel", sysinfo["kernel"]["version"], ps["kernel"]["version"] if ps else None)
    vrow("systemd", str(sysinfo.get("systemd", "–")), str(ps.get("systemd")) if ps else None)
    vrow("Nix", str(sysinfo.get("nix", "–")), str(ps.get("nix")) if ps else None)
    vrow("stateVersion", str(sysinfo.get("stateVersion", "–")))
    if flake:
        vrow("Flake revision", rev_txt, (ps.get("flake") or {}).get("shortRev") if ps else None)
    if cur.get("booted"):
        w(f"<tr><td>Booted generation</td><td>{esc(cur['booted'].get('generation') or '?')}</td></tr>")
    w("</tbody></table>\n</div>\n")

    w("<div>\n<h2>Size</h2>\n<table class=\"kv\"><tbody>")
    s, p_s = cur["sizes"], (prev["sizes"] if prev else {})

    def srow(label: str, val: int | None, pval: int | None) -> None:
        w(f"<tr><td>{esc(label)}</td><td class=\"num\" style=\"text-align:left\">{esc(human(val))}{delta_html(val, pval)}</td></tr>")

    srow("System closure (total)", s.get("total"), p_s.get("total"))
    srow("System packages", s.get("system_path"), p_s.get("system_path"))
    for u, v in s.get("users", {}).items():
        srow(f"User profile: {u}", v, p_s.get("users", {}).get(u))
    boot = s.get("boot", {})
    pboot = p_s.get("boot", {})
    bsum = sum(v for v in boot.values() if v) if any(boot.values()) else None
    pbsum = sum(v for v in pboot.values() if v) if any(pboot.values()) else None
    srow("Kernel, modules, initrd, firmware", bsum, pbsum)
    disk = cur.get("disk", {})
    if "/nix/store" in disk:
        d = disk["/nix/store"]
        w(f"<tr><td>Filesystem holding /nix/store</td><td>{esc(human(d['used']))} used of {esc(human(d['size']))}"
          f"<span class=\"muted\"> ({d['used'] * 100 // max(d['size'], 1)}%)</span></td></tr>")
    if "/boot" in disk:
        d = disk["/boot"]
        w(f"<tr><td>/boot</td><td>{esc(human(d['used']))} used of {esc(human(d['size']))}"
          f"<span class=\"muted\"> ({d['used'] * 100 // max(d['size'], 1)}%)</span></td></tr>")
    g = cur.get("generations", {})
    if g.get("count"):
        w(f"<tr><td>Generations kept</td><td>{g['count']} <span class=\"muted\">({g['oldest']}–{g['newest']})</span></td></tr>")
    w("</tbody></table>\n</div>\n</div>\n")

    # inputs
    if inputs:
        moved_n = sum(1 for i in inputs if i["moved"])
        w(f"<h2 id=\"inputs\">Flake inputs <span class=\"muted small\">{len(inputs)}"
          + (f" · {moved_n} moved" if moved_n else "") + "</span></h2>\n")
        w("<table><thead><tr><th>Input</th><th>Source</th><th>Revision</th><th>Date</th><th></th></tr></thead><tbody>")
        for i in inputs:
            url = input_url(i)
            label = esc(input_label(i))
            src = f'<a href="{esc(url)}">{label}</a>' if url else label
            if i.get("follows"):
                src += ' <span class="tag">follows</span>'
            rev_html = f'<span class="prev">{esc(short(i["prev_rev"]))} → </span>' if i["moved"] else ""
            rev_html += f'<span class="mono">{esc(short(i.get("rev")))}</span>'
            tag = ""
            if i["moved"]:
                tag = '<span class="tag moved">moved</span>'
                if i["compare"]:
                    tag += f' <a class="small" href="{esc(i["compare"])}">compare</a>'
            w(f"<tr><td>{esc(i['name'])}</td><td>{src}</td><td>{rev_html}</td><td>{esc(fmt_ts(i.get('lastModified')))}</td><td>{tag}</td></tr>")
        w("</tbody></table>\n")

    # controls
    w("<div class=\"controls\">")
    w("<label><input type=\"checkbox\" id=\"show-prev\"> show previous versions</label>")
    w("<label><input type=\"checkbox\" id=\"only-changed\"> only changed</label>")
    w("<input type=\"search\" id=\"q\" placeholder=\"filter packages\" autocomplete=\"off\">")
    w("<nav>" + "".join(f'<a href="#{esc(g["id"])}">{esc(g["title"])}</a>' for g in groups) + "</nav>")
    w("</div>\n")

    # package groups
    for g in groups:
        c = g["counts"]
        summary = f"{c['total']} packages"
        if g["compared"]:
            parts = []
            if c["changed"]:
                parts.append(f"{c['changed']} changed")
            if c["added"]:
                parts.append(f"{c['added']} added")
            if c["removed"]:
                parts.append(f"{c['removed']} removed")
            summary += " · " + (" · ".join(parts) if parts else "unchanged")
        w(f"<h2 id=\"{esc(g['id'])}\">{esc(g['title'])} <span class=\"muted small\">{esc(summary)}</span></h2>\n")
        w("<table><thead><tr><th>Package</th><th>Version</th><th>Source</th><th class=\"num\">Closure</th><th>Description</th></tr></thead><tbody>")
        for r in g["rows"]:
            key = esc(f"{r['pname']} {r['description']} {r['source']}".lower())
            tip = esc(r["position"]) if r["position"] else ""
            name = esc(r["pname"])
            if r.get("homepage"):
                name = f'<a href="{esc(r["homepage"])}">{name}</a>'
            desc = r["description"]
            if len(desc) > 110:
                desc = desc[:107].rstrip() + "…"
            w(f'<tr class="pkg {r["status"]}" data-k="{key}">'
              f'<td title="{tip}">{name}</td><td class="v">{version_cell(r)}</td>'
              f'<td class="small">{esc(r["source"])}</td><td class="num">{esc(human(r["size"]))}</td>'
              f'<td class="small">{esc(desc)}</td></tr>')
        w("</tbody></table>\n")

    # changelog
    pairs = [(records[i], records[i + 1]) for i in range(len(records) - 1)]
    if pairs:
        w(f"<h2 id=\"changelog\">Changelog <span class=\"muted small\">last {len(pairs)} switch{'es' if len(pairs) != 1 else ''}</span></h2>\n")
        for n, (a, b) in enumerate(pairs):
            e = changelog_entry(a, b)
            c = e["counts"]
            head = (f"{fmt_iso(a['generated_at'])} · generation {b['generation']} → {a['generation']}"
                    f" · {c['changed']} changed, {c['added']} added, {c['removed']} removed")
            if e["size_delta"] is not None:
                head += f" · {human(e['size_delta'], signed=True)}"
            w(f"<details{' open' if n == 0 else ''}><summary>{esc(head)}</summary>")
            items = []
            if e["nixos_changed"]:
                items.append(f"NixOS {esc(b['manifest']['system']['nixos']['version'])} → {esc(a['manifest']['system']['nixos']['version'])}")
            if e["kernel_changed"]:
                items.append(f"kernel {esc(b['manifest']['system']['kernel']['version'])} → {esc(a['manifest']['system']['kernel']['version'])}")
            for i in e["moved"]:
                link = f' <a class="small" href="{esc(i["compare"])}">compare</a>' if i["compare"] else ""
                items.append(f"input <b>{esc(i['name'])}</b> <span class=\"mono\">{esc(short(i['prev_rev']))} → {esc(short(i['rev']))}</span>{link}")
            for ch in e["changes"]:
                tag = {"added": "add", "removed": "del", "changed": "chg"}[ch["status"]]
                if ch["status"] == "changed":
                    txt = f"{esc(ch['pname'])} <span class=\"mono\">{esc(', '.join(ch['prev_versions']))} → {esc(', '.join(ch['versions']))}</span>"
                elif ch["status"] == "added":
                    txt = f"{esc(ch['pname'])} <span class=\"mono\">{esc(', '.join(ch['versions']))}</span>"
                else:
                    txt = f"{esc(ch['pname'])} <span class=\"mono\">{esc(', '.join(ch['prev_versions']))}</span>"
                items.append(f'<span class="tag {tag}">{ch["status"]}</span> {txt} <span class="muted small">{esc(ch["group"])}</span>')
            if not items:
                items.append('<span class="muted">no package or input changes</span>')
            w("<ul>" + "".join(f"<li>{i}</li>" for i in items) + "</ul></details>\n")

    w("<footer>")
    w(f"Generated {esc(fmt_iso(cur['generated_at']))} by nixos-status from <span class=\"mono\">{esc(cur['system_config'])}</span>. ")
    w("This page is regenerated on every <code>nixos-rebuild switch</code>; nothing is computed when it is opened.")
    w("</footer>\n")
    w(f"<script>{JS}</script>\n</body>\n</html>\n")
    return "".join(out)


# --------------------------------------------------------------------------
# text
# --------------------------------------------------------------------------


def render_text(records: list[dict], color: bool) -> str:
    cur = records[0]
    prev = next((r for r in records[1:] if r.get("system_config") != cur.get("system_config")), None)
    m = cur["manifest"]
    si = m["system"]
    flake = si.get("flake") or {}

    def c(code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if color else s

    lines = []
    rev = flake_rev(flake)
    head = f"{si['hostname']}  generation {cur['generation']}  activated {fmt_iso(cur['generated_at'])}"
    if rev:
        head += f"  flake {rev}"
    lines.append(c("1", head))
    if cur.get("reboot_needed"):
        b = cur.get("booted") or {}
        lines.append(c("33", f"REBOOT NEEDED: booted generation {b.get('generation') or '?'} runs a different kernel or initrd"))
    lines.append("")

    ps = prev["manifest"]["system"] if prev else None

    def kv(label: str, val, pval=None) -> None:
        val = str(val)
        if pval is not None and str(pval) != val:
            val = f"{pval} -> {val}"
        lines.append(f"  {label:<14} {val}")

    nx = si["nixos"]
    kv("NixOS", nx["version"] + (f" ({nx['codeName']})" if nx.get("codeName") else ""), ps["nixos"]["version"] if ps else None)
    kv("nixpkgs", short(nx.get("nixpkgsRevision"), 12), short(ps["nixos"].get("nixpkgsRevision"), 12) if ps and ps["nixos"].get("nixpkgsRevision") else None)
    kv("kernel", si["kernel"]["version"], ps["kernel"]["version"] if ps else None)
    kv("systemd", si.get("systemd"), ps.get("systemd") if ps else None)
    kv("nix", si.get("nix"), ps.get("nix") if ps else None)
    lines.append("")

    s, p_s = cur["sizes"], (prev["sizes"] if prev else {})

    def sz(label: str, v, pv) -> None:
        d = f"  ({human(v - pv, signed=True)})" if v is not None and pv is not None else ""
        lines.append(f"  {label:<34} {human(v):>10}{d}")

    sz("system closure (total)", s.get("total"), p_s.get("total"))
    sz("system packages", s.get("system_path"), p_s.get("system_path"))
    for u, v in s.get("users", {}).items():
        sz(f"user profile: {u}", v, p_s.get("users", {}).get(u))
    boot = s.get("boot", {})
    pboot = p_s.get("boot", {})
    sz("kernel, modules, initrd, firmware",
       sum(v for v in boot.values() if v) if any(boot.values()) else None,
       sum(v for v in pboot.values() if v) if any(pboot.values()) else None)
    disk = cur.get("disk", {})
    for mp, label in (("/nix/store", "fs holding /nix/store"), ("/boot", "/boot")):
        if mp in disk:
            d = disk[mp]
            lines.append(f"  {label:<34} {human(d['used']):>10} used of {human(d['size'])}")
    g = cur.get("generations", {})
    if g.get("count"):
        lines.append(f"  {'generations kept':<34} {g['count']:>10}  ({g['oldest']}-{g['newest']})")
    lines.append("")

    inputs = diff_inputs(cur, prev)
    moved = [i for i in inputs if i["moved"]]
    if inputs:
        lines.append(f"  flake inputs: {len(inputs)}" + (f", {len(moved)} moved since previous generation" if prev else ""))
        for i in moved:
            lines.append(f"    {i['name']:<24} {short(i['prev_rev'])} -> {short(i['rev'])}")
        lines.append("")

    groups = diff_groups(cur, prev)
    for grp in groups:
        cnt = grp["counts"]
        summary = f"{cnt['total']} packages"
        if grp["compared"]:
            summary += f", {cnt['changed']} changed, {cnt['added']} added, {cnt['removed']} removed"
        lines.append(f"  {grp['title']}: {summary}")
        for r in grp["rows"]:
            if r["status"] == "changed":
                lines.append(c("33", f"    ~ {r['pname']:<30} {', '.join(r['prev_versions'])} -> {', '.join(r['versions'])}"))
            elif r["status"] == "added":
                lines.append(c("32", f"    + {r['pname']:<30} {', '.join(r['versions'])}"))
            elif r["status"] == "removed":
                lines.append(c("31", f"    - {r['pname']:<30} {', '.join(r['prev_versions'])}"))
    if not prev:
        lines.append("")
        lines.append("  (no previous generation recorded yet; changes appear after the next switch)")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------


def cmd_generate(args: argparse.Namespace) -> int:
    state = Path(args.state)
    with open(args.manifest) as fh:
        manifest = json.load(fh)
    rec = collect_runtime(manifest, args.system_config)
    state.mkdir(parents=True, exist_ok=True)
    os.chmod(state, 0o755)
    keep = int(manifest.get("historyKeep") or 10)
    records = write_record(state, rec, keep)
    page = render_html(records)
    with tempfile.NamedTemporaryFile("w", dir=state, delete=False, encoding="utf-8") as tmp:
        tmp.write(page)
    os.chmod(tmp.name, 0o644)
    os.replace(tmp.name, state / "index.html")
    if not args.quiet:
        prev = next((r for r in records[1:] if r.get("system_config") != rec["system_config"]), None)
        n = sum(1 for g in diff_groups(rec, prev) for r in g["rows"] if r["status"] != "same") if prev else 0
        log(f"generation {rec['generation']}: {state / 'index.html'} ({len(page) // 1024} KiB, {n} package changes)"
            + (", reboot needed" if rec["reboot_needed"] else ""))
    return 0


def load_or_fail(state: Path) -> list[dict]:
    records = load_history(state)
    if not records:
        log(f"no records in {state}. Enable services.nixos-status and switch once.")
        sys.exit(1)
    return records


def cmd_text(args: argparse.Namespace) -> int:
    records = load_or_fail(Path(args.state))
    color = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
    sys.stdout.write(render_text(records, color))
    return 0


def cmd_open(args: argparse.Namespace) -> int:
    page = Path(args.state) / "index.html"
    if not page.exists():
        log(f"{page} does not exist yet")
        return 1
    opener = shutil.which("xdg-open")
    if not opener:
        log("xdg-open not found; the page is at " + str(page))
        return 1
    os.execv(opener, [opener, page.as_uri()])


def cmd_path(args: argparse.Namespace) -> int:
    print(Path(args.state) / "index.html")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="nixos-status", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--state", default=os.environ.get("NIXOS_STATUS_STATE", DEFAULT_STATE),
                    help=f"state directory (default {DEFAULT_STATE}, or $NIXOS_STATUS_STATE)")
    sub = ap.add_subparsers(dest="cmd")
    g = sub.add_parser("generate", help="record the activated system and render the page (root)")
    g.add_argument("--manifest", required=True, help="evaluation-time manifest JSON")
    g.add_argument("--system-config", required=True, help="the activated system toplevel")
    g.add_argument("--quiet", action="store_true")
    sub.add_parser("text", help="print a plain-text summary (default)")
    sub.add_parser("open", help="open the page in the default browser")
    sub.add_parser("path", help="print the path of the page")
    args = ap.parse_args(argv)
    return {"generate": cmd_generate, "text": cmd_text, "open": cmd_open, "path": cmd_path,
            None: cmd_text}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
