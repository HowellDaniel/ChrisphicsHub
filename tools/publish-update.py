#!/usr/bin/env python3
"""Publish this tree as the build the shop's counters can take in.

The weekly check reads one number, VERSION. This tool writes the *build* beside that number: the
files the app is made of (the engine, the schema, the version, the screens) with a sha256 for each,
in the one place the engine is allowed to fetch from — `update/` on the repository's main branch.

    python3 tools/publish-update.py                 # write update/ from this tree
    python3 tools/publish-update.py --check         # say what would be written, write nothing

Nothing here talks to the network: committing and pushing update/ is the shop's release, done with
git like every other change. Nothing here reads the book, and nothing here builds a bundle — a copy
that takes this in keeps its own shell, its own .app and its own records.

The file list is the whole trust boundary: server.py is what the app runs, and a package that
carried anything else would be a way to write a file nobody asked for. So the rule below is the
same one server.py applies when it stages a download (part_target), and the check tool holds the
two together.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTPUT = os.path.join(ROOT, "update")


def part_target(name):
    clean = str(name or "").replace("\\", "/").lstrip("./")
    if clean in ("server.py", "schema.sql", "VERSION"):
        return clean
    if clean.startswith("public/") and not clean.endswith("/"):
        depth = clean.count("/")
        if 1 <= depth <= 3 and all(part not in ("", "..", ".") for part in clean.split("/")):
            return clean
    return None


def files_in(root):
    """Every part of the app, in a stable order, and nothing else: no tools, no build output, no
    book, no installer, no dot-file."""
    found = []
    for base, folders, names in os.walk(os.path.join(root, "public")):
        folders[:] = sorted(f for f in folders if not f.startswith("."))
        for name in sorted(names):
            if name.startswith("."):
                continue
            found.append(os.path.join(base, name))
    for name in ("server.py", "schema.sql", "VERSION"):
        found.append(os.path.join(root, name))
    return sorted({part_target(os.path.relpath(path, root)) for path in found} - {None})


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def build(root=ROOT, output=OUTPUT, notes=""):
    version = open(os.path.join(root, "VERSION"), encoding="utf-8").read().strip()
    if not version:
        raise SystemExit("VERSION is empty, so there is no build to publish")
    folder = "chrisphics-hub-" + version
    staging = os.path.join(output, folder)
    parts = []
    for name in files_in(root):
        source = os.path.join(root, name)
        size = os.path.getsize(source)
        parts.append({"name": name, "size": size, "sha256": sha256_of(source)})
    manifest = {"version": version, "folder": folder, "base": "main/update/" + folder,
                "notes": notes.strip(), "files": parts,
                "size": sum(p["size"] for p in parts)}
    return staging, parts, manifest


def write(root=ROOT, output=OUTPUT, notes=""):
    staging, parts, manifest = build(root, output, notes)
    if os.path.isdir(staging):
        shutil.rmtree(staging)
    for part in parts:
        landing = os.path.join(staging, part["name"])
        os.makedirs(os.path.dirname(landing), exist_ok=True)
        shutil.copy2(os.path.join(root, part["name"]), landing)
    os.makedirs(output, exist_ok=True)
    with open(os.path.join(output, "current.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=1, sort_keys=True)
        handle.write("\n")
    return staging, manifest


def main(argv):
    checking = "--check" in argv
    notes = ""
    for arg in argv:
        if arg.startswith("--notes="):
            notes = arg.split("=", 1)[1]
    staging, parts, manifest = build(notes=notes)
    words = "%s: %d files, %d bytes" % (manifest["version"], len(parts), manifest["size"])
    if checking:
        print("would publish " + words)
        for part in parts:
            print("  %-40s %8d  %s" % (part["name"], part["size"], part["sha256"][:12]))
        return 0
    staging, manifest = write(notes=notes)
    print("published " + words)
    print("  %s" % os.path.relpath(staging, ROOT))
    print("  %s" % os.path.relpath(os.path.join(OUTPUT, "current.json"), ROOT))
    print("commit and push update/ to make it the shop's newest build")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
