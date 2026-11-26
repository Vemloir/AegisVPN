"""Build a reviewed web + Python overlay bundle. No SSH, Docker or live state."""

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

PAGES = {"index.html", "en/index.html", "ru/index.html"}
PUBLIC = PAGES | {"robots.txt", "sitemap.xml", "colortest.html", "favicon.svg"}


def require(condition, reason):
    if not condition:
        raise ValueError(reason)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def safe_file(root, name):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_./-]+", name), "invalid filename")
    require(all(p not in {"", ".", ".."} for p in name.split("/")), "unsafe path")
    p = root / name
    require(p.resolve() == root.resolve() / name and p.is_file(), f"missing or linked: {name}")
    return p


def python_path(name):
    return (
        bool(re.fullmatch(r"src/(?:[A-Za-z_]\w*/)*[A-Za-z_]\w*\.py", name))
        and name != "src/core/migrations.py"
        and not name.startswith("src/models/")
    )


def public_path(name):
    return name in PUBLIC or bool(re.fullmatch(r"assets/[\w.-]+\.(?:js|css|woff2?|svg|png|webp)", name))


def inventory(root):
    files = {}
    for p in root.rglob("*"):
        require(not p.is_symlink(), "linked bundle entry")
        if p.is_file():
            files[p.relative_to(root).as_posix()] = digest(p)
    return files


def scope(m):
    if m["version"] == 1 and "service_files" not in m:
        return {s: sorted(m["backend"]) for s in ("bot", "siteapi")}
    targets = m.get("service_files")
    require(
        m["version"] == 2 and isinstance(targets, dict) and set(targets) == {"bot", "siteapi"}, "invalid service scope"
    )
    for files in targets.values():
        require(
            isinstance(files, list)
            and files
            and all(isinstance(n, str) and n in m["backend"] for n in files)
            and len(set(files)) == len(files),
            "invalid service files",
        )
    require(set().union(*targets.values()) == set(m["backend"]), "unassigned release file")
    return targets


def verify(bundle, expected):
    manifest = safe_file(bundle, "manifest.json")
    require(digest(manifest) == expected, "bundle ID mismatch")
    m = json.loads(manifest.read_text())
    scope(m)
    require(m["backend"] and PAGES <= m["web"].keys(), "incomplete release")
    require(
        all(python_path(n) for n in m["backend"]) and all(public_path(n) for n in m["web"]), "unsupported release scope"
    )
    require(
        inventory(bundle / "backend") == {n: v["after"] for n, v in m["backend"].items()}, "backend checksum mismatch"
    )
    require(inventory(bundle / "web") == m["web"], "web checksum mismatch")
    for n in PAGES:
        refs = re.findall(r'(?:src|href)="/(assets/[^"?]+)"', (bundle / "web" / n).read_text())
        require(any(x.endswith(".js") for x in refs) and all(x in m["web"] for x in refs), f"incomplete page {n}")
    return m


def build(repo, spec_path, dest):
    spec = json.loads(spec_path.read_text())
    require(not dest.exists(), "output already exists")
    require(all(python_path(n) for n in spec["backend"]), "invalid backend scope")
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        stage = Path(tmp)
        result = stage / "bundle"
        source = stage / "web"
        for folder, files, origin in [
            (result / "backend", {n: v["after"] for n, v in spec["backend"].items()}, repo / "bot"),
            (source, spec["web_inputs"], repo / "web"),
        ]:
            for name, sha in files.items():
                p = safe_file(origin, name)
                require(digest(p) == sha, f"unreviewed input: {name}")
                out = folder / name
                out.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, out)
        # Fresh installation from the reviewed lock, no ambient .env / VITE_ settings.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
        for args in [
            ["npm", "ci", "--no-audit", "--no-fund"],
            ["npm", "test", "--", "--run"],
            ["node", "--test", "scripts/localize-dist.test.mjs"],
            ["npm", "run", "build"],
        ]:
            subprocess.run(args, cwd=source, env=env, check=True)
        shutil.move(source / "dist", result / "web")
        m = {
            "version": 2,
            "service_files": spec["service_files"],
            "backend": spec["backend"],
            "web": inventory(result / "web"),
            "spec_sha256": digest(spec_path),
        }
        (result / "manifest.json").write_text(json.dumps(m, sort_keys=True, indent=2) + "\n")
        release = digest(result / "manifest.json")
        verify(result, release)
        result.rename(dest)
    return release


def rescope_v1(spec_path, dest, base, base_id):
    """Change assignments only. Preserve every approved v1 payload byte."""
    spec = json.loads(spec_path.read_text())
    require(set(spec) == {"backend", "web_inputs", "service_files"}, "unexpected scope-only spec changes")
    require(not dest.exists(), "output already exists")
    with tempfile.TemporaryDirectory(dir=dest.parent) as tmp:
        result = Path(tmp) / "bundle"
        shutil.copytree(base, result, symlinks=True)
        old = verify(result, base_id)
        require(old["version"] == 1 and spec["backend"] == old["backend"], "expected unchanged v1 backend")
        # v1 cabinet spec was canonical sorted, indented JSON, newline-terminated.
        inputs = {k: spec[k] for k in ("backend", "web_inputs")}
        encoded = (json.dumps(inputs, sort_keys=True, indent=2) + "\n").encode()
        require(hashlib.sha256(encoded).hexdigest() == old["spec_sha256"], "reviewed inputs differ from base spec")
        manifest = {
            **old,
            "version": 2,
            "service_files": spec["service_files"],
            "spec_sha256": digest(spec_path),
            "base_release": base_id,
        }
        (result / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
        release = digest(result / "manifest.json")
        verify(result, release)
        result.rename(dest)
    return release


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--reuse-v1", type=Path, help="Base bundle for an assignments-only release")
    parser.add_argument("--base-id", help="Independently approved SHA-256 of the v1 manifest")
    args = parser.parse_args()
    if (args.reuse_v1 is None) != (args.base_id is None):
        parser.error("--reuse-v1 and --base-id must be supplied together")
    print(
        rescope_v1(args.spec, args.output, args.reuse_v1, args.base_id)
        if args.reuse_v1
        else build(Path(__file__).resolve().parents[3], args.spec, args.output)
    )
