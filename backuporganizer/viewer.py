"""Manifest visualization: the --tree terminal view and the --browse HTML page.

Both are built purely from the manifest, so exploring the whole backup costs
zero downloads.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .config import Config
from .manifest import Manifest
from .util import human_size


def build_tree(files: dict[str, dict]) -> dict:
    """Fold manifest arcnames into a nested tree with aggregated sizes.

    Node shape: {"dirs": {name: node}, "files": [(name, entry)], "size", "count"}
    """
    root = {"dirs": {}, "files": [], "size": 0, "count": 0}
    for arc in sorted(files):
        entry = files[arc]
        parts = arc.split("/")
        node = root
        node["size"] += entry["size"]
        node["count"] += 1
        for part in parts[:-1]:
            node = node["dirs"].setdefault(
                part, {"dirs": {}, "files": [], "size": 0, "count": 0}
            )
            node["size"] += entry["size"]
            node["count"] += 1
        node["files"].append((parts[-1], entry))
    return root


def cmd_tree(cfg: Config, prefix: str) -> int:
    """Print the backed-up file tree (optionally limited to a path prefix)."""
    manifest = Manifest.load(cfg.manifest_path)
    files = {
        arc: e for arc, e in manifest.files.items()
        if not prefix or arc.lower().startswith(prefix.lower())
    }
    if not files:
        print("Nothing backed up" + (f" under {prefix!r}." if prefix else " yet."))
        return 1
    root = build_tree(files)

    def render(node: dict, indent: str) -> None:
        dirs = sorted(node["dirs"].items())
        entries: list[tuple[str, str]] = [
            (f"{name}/", f"[{human_size(sub['size'])}, {sub['count']} file(s)]")
            for name, sub in dirs
        ] + [
            (name, f"({human_size(e['size'])}, "
                   f"{e['chunk'] if e['source'] == 'dropzone' else 'synced'})")
            for name, e in node["files"]
        ]
        for i, (label, info) in enumerate(entries):
            last = i == len(entries) - 1
            print(f"{indent}{'└── ' if last else '├── '}{label}  {info}")
            if label.endswith("/"):
                render(node["dirs"][label[:-1]], indent + ("    " if last else "│   "))

    print(f".  [{human_size(root['size'])}, {root['count']} file(s)]")
    render(root, "")
    return 0


# Self-contained HTML template for --browse. Placeholders: __TITLE__,
# __META__, __DATA__ (a JSON array of [path, size, chunk, uploaded, source]).
BROWSER_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BackupOrganizer — __TITLE__</title>
<style>
  :root { color-scheme: light dark;
    --fg: #1d1d1f; --muted: #6e6e73; --bg: #ffffff; --hover: #0000000d;
    --sync: #0a84ff22; --sync-fg: #0060c0; --arch: #30d15822; --arch-fg: #1a7a35;
    --pending: #ff9f0a22; --pending-fg: #b06000; }
  @media (prefers-color-scheme: dark) { :root {
    --fg: #f5f5f7; --muted: #98989d; --bg: #1c1c1e; --hover: #ffffff14;
    --sync-fg: #64b5ff; --arch-fg: #5fdb7d; --pending-fg: #ffb340; } }
  body { font: 14px/1.5 -apple-system, BlinkMacSystemFont, sans-serif;
         color: var(--fg); background: var(--bg); margin: 0; padding: 1.2em 1.6em 3em; }
  h1 { font-size: 1.25em; margin: 0 0 .1em; }
  .meta { color: var(--muted); margin-bottom: 1em; }
  #q { font: inherit; width: min(28em, 100%); padding: .45em .7em; margin-bottom: 1em;
       border: 1px solid var(--muted); border-radius: 8px; background: transparent; color: var(--fg); }
  details { padding-left: 1.1em; }
  summary { cursor: pointer; border-radius: 6px; padding: .1em .3em; list-style-position: outside; }
  summary:hover, .file:hover { background: var(--hover); }
  .file { padding: .1em .3em .1em 2.15em; border-radius: 6px; }
  .sz { color: var(--muted); font-variant-numeric: tabular-nums; margin-left: .6em; }
  .badge { font-size: .78em; border-radius: 5px; padding: .08em .45em; margin-left: .6em;
           font-variant-numeric: tabular-nums; }
  .b-sync { background: var(--sync); color: var(--sync-fg); }
  .b-arch { background: var(--arch); color: var(--arch-fg); }
  .b-pend { background: var(--pending); color: var(--pending-fg); }
  .hide { display: none; }
  .dim { color: var(--muted); }
  .dl { visibility: hidden; font: inherit; font-size: .78em; cursor: pointer;
        border: 1px solid var(--muted); border-radius: 5px; background: transparent;
        color: var(--fg); padding: .02em .5em; margin-left: .6em; }
  summary:hover .dl, .file:hover .dl, .dl.ok { visibility: visible; }
  .hint { color: var(--muted); font-size: .85em; margin: -.5em 0 1em; }
  .hint code { font-family: ui-monospace, monospace; }
</style></head><body>
<h1>Backup contents</h1>
<div class="meta">__META__</div>
<p class="hint">⬇ copies a restore command — paste it in Terminal to download that
file or folder from Proton Drive. Entire backup:
<code>backup-organizer --restore-all --dest ~/some/folder</code></p>
<input id="q" type="search" placeholder="Search files… (name or path)" autofocus>
<div id="tree"></div>
<script>
const FILES = __DATA__;   // [path, size, chunk, uploaded, source]
const human = n => { for (const u of ["B","KB","MB","GB","TB"]) {
  if (n < 1024 || u === "TB") return u === "B" ? n + " B" : n.toFixed(1) + " " + u; n /= 1024; } };

const root = { dirs: new Map(), files: [], size: 0 };
for (const f of FILES) {
  const parts = f[0].split("/");
  let node = root; node.size += f[1];
  for (const p of parts.slice(0, -1)) {
    if (!node.dirs.has(p)) node.dirs.set(p, { dirs: new Map(), files: [], size: 0 });
    node = node.dirs.get(p); node.size += f[1];
  }
  node.files.push(f);
}
const fileEls = [];
function copyText(t) {
  if (navigator.clipboard) return navigator.clipboard.writeText(t);
  const ta = document.createElement("textarea"); ta.value = t;
  document.body.append(ta); ta.select(); document.execCommand("copy"); ta.remove();
  return Promise.resolve();
}
function dlButton(path, isDir) {
  const b = document.createElement("button"); b.className = "dl"; b.textContent = "⬇";
  b.title = "Copy the Terminal command that downloads " + (isDir ? "this folder" : "this file");
  b.onclick = ev => {
    ev.preventDefault(); ev.stopPropagation();
    const quoted = "'" + (path + (isDir ? "/" : "")).replaceAll("'", "'\\\\''") + "'";
    copyText("backup-organizer --restore " + quoted);
    b.textContent = "✓ copied — paste in Terminal"; b.classList.add("ok");
    setTimeout(() => { b.textContent = "⬇"; b.classList.remove("ok"); }, 1800);
  };
  return b;
}
function render(node, parent, depth, prefix) {
  for (const [name, sub] of [...node.dirs].sort((a, b) => a[0].localeCompare(b[0]))) {
    const d = document.createElement("details"); if (depth === 0) d.open = true;
    const s = document.createElement("summary");
    s.append("📁 " + name, Object.assign(document.createElement("span"),
      { className: "sz", textContent: human(sub.size) }), dlButton(prefix + name, true));
    d.append(s); parent.append(d); render(sub, d, depth + 1, prefix + name + "/");
  }
  for (const [path, size, chunk, up, src] of node.files) {
    const el = document.createElement("div"); el.className = "file"; el.dataset.p = path.toLowerCase();
    const badge = Object.assign(document.createElement("span"), {
      className: "badge " + (up ? (src === "sync" ? "b-sync" : "b-arch") : "b-pend"),
      textContent: chunk + (up ? "" : " · not uploaded"),
      title: up ? "Uploaded " + up : "Awaiting upload" });
    el.append("📄 " + path.split("/").pop(),
      Object.assign(document.createElement("span"), { className: "sz", textContent: human(size) }),
      badge, dlButton(path, false));
    parent.append(el); fileEls.push(el);
  }
}
render(root, document.getElementById("tree"), 0, "");
document.getElementById("q").addEventListener("input", e => {
  const q = e.target.value.trim().toLowerCase();
  for (const el of fileEls) el.classList.toggle("hide", q !== "" && !el.dataset.p.includes(q));
  for (const d of document.querySelectorAll("details")) {
    const any = [...d.querySelectorAll(".file")].some(f => !f.classList.contains("hide"));
    d.classList.toggle("hide", !any);
    if (q !== "" && any) d.open = true;
  }
});
</script></body></html>
"""


def cmd_browse(cfg: Config, out: Path | None) -> int:
    """Generate the interactive HTML backup browser and open it."""
    manifest = Manifest.load(cfg.manifest_path)
    if not manifest.files:
        print("Nothing backed up yet.")
        return 1
    def chunk_and_uploaded(e: dict) -> tuple[str, str]:
        if e["source"] == "dropzone":
            chunk = e.get("chunk", "?")
            return chunk, manifest.chunks.get(chunk, {}).get("uploaded", "")
        return "synced", e.get("uploaded", "")

    data = [
        [arc, e["size"], *chunk_and_uploaded(e), e["source"]]
        for arc, e in sorted(manifest.files.items())
    ]
    total = sum(e["size"] for e in manifest.files.values())
    meta = (
        f"{len(manifest.files)} files · {human_size(total)} · "
        f"{len(manifest.chunks)} chunks on {cfg.remote_folder} · "
        f"last backup {manifest.last_backup or 'never'}"
    )
    html = (
        BROWSER_HTML
        .replace("__TITLE__", cfg.remote_folder)
        .replace("__META__", meta)
        .replace("__DATA__", json.dumps(data, ensure_ascii=True).replace("</", "<\\/"))
    )
    out = (out or cfg.backup_dir / "backup_browser.html").expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html)
    print(f"Wrote {out} ({human_size(out.stat().st_size)})")
    subprocess.run(["/usr/bin/open", str(out)], capture_output=True)
    return 0
