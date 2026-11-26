"""Control-host-only, schema-compatible releases. Run directly on the host."""

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

if __package__:
    from .bundle import PAGES, digest, require, scope, verify
else:
    from bundle import PAGES, digest, require, scope, verify

SERVICES = ("bot", "siteapi")
ROOT = Path("/root/aegis")


def cmd(*args, cwd=None, timeout=300, stdin=None):
    # No stderr/response payloads in errors (may include secrets).
    p = subprocess.run(args, cwd=cwd, input=stdin, capture_output=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError(f"{args[0]} {args[1]} failed (exit {p.returncode})")
    return p.stdout


def atomic(path, data, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fchmod(fd, mode)
            os.fsync(fd)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def save(path, obj):
    atomic(path, json.dumps(obj, sort_keys=True).encode() + b"\n", 0o600)


def local_file(root, name):
    path = root / name
    require(
        path.resolve() == root.resolve() / name and (not path.exists() or path.is_file()), f"unsafe live file {name}"
    )
    return path


def file_hash(path):
    return digest(path) if path.exists() else None


class Control:
    def __init__(self, root, domain):
        require(bool(re.fullmatch(r"[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*", domain)), "invalid public site hostname")
        self.domain = domain
        self.compose = root / "deploy/vps"
        self.web = self.compose / "web"
        self.backend = root / "bot"
        self.store = root / "control-releases"

    def inspect(self, svc):
        data = json.loads(cmd("docker", "inspect", "aegis-" + svc))[0]
        require(
            data["State"]["Running"] and data["Config"]["Labels"]["com.docker.compose.service"] == svc,
            "not a running compose service: " + svc,
        )
        require(
            cmd("docker", "compose", "ps", "-q", svc, cwd=self.compose).decode().strip() == data["Id"],
            "compose identity mismatch",
        )
        return data

    def service_hash(self, svc):
        # Compose recreates from current files. Never silently deploy a pending
        # env/mount/command edit alongside a code-only release.
        # Compose 5.3.1's `config --hash` skips env_file resolution, unlike
        # `up`. Hash Compose's own resolved serialization (already escapes $).
        # It can contain credentials: stdin only, never argv, logs or disk.
        model = cmd("docker", "compose", "config", "--format", "json", cwd=self.compose)
        project = json.loads(model)["name"]
        fields = (
            cmd(
                "docker",
                "compose",
                "-p",
                project,
                "-f",
                "-",
                "config",
                "--hash",
                svc,
                cwd=self.compose,
                stdin=model,
            )
            .decode()
            .split()
        )
        require(
            len(fields) == 2 and fields[0] == svc and re.fullmatch("[0-9a-f]{64}", fields[1]),
            "unsupported compose hash output",
        )
        return fields[1]

    def check_web_mount(self):
        mounts = self.inspect("caddy")["Mounts"]
        require(
            any(
                m["Destination"] == "/srv/web"
                and m["Type"] == "bind"
                and not m["RW"]
                and Path(m["Source"]).resolve() == self.web.resolve()
                for m in mounts
            ),
            "unexpected Caddy web mount",
        )

    def health(self):
        http = build_opener(ProxyHandler({}))
        for url in ("http://127.0.0.1:8080/health", "http://127.0.0.1:8000/api/locations"):
            with http.open(url, timeout=3) as resp:
                body = json.load(resp)
            require(
                (isinstance(body, dict) and body.get("status") == "ok")
                if url.endswith("/health")
                else isinstance(body, list),
                "unhealthy " + url,
            )

    def check_site(self):
        paths = set(PAGES)
        for p in PAGES:
            paths.update(re.findall(r'(?:src|href)="/(assets/[^"?]+)"', (self.web / p).read_text()))
        for p in sorted(paths):
            url = {"index.html": "/", "ru/index.html": "/ru/", "en/index.html": "/en/"}.get(p, "/" + p)
            data = cmd(
                "curl",
                "--silent",
                "--fail",
                "--noproxy",
                "*",
                "--resolve",
                f"{self.domain}:443:127.0.0.1",
                "--max-time",
                "6",
                f"https://{self.domain}{url}",
            )
            require(hashlib.sha256(data).hexdigest() == digest(self.web / p), f"wrong public bytes: {p}")

    def ready(self):
        deadline = time.monotonic() + 45
        while True:
            try:
                self.health()
                return
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1)

    def stage(self, bundle, release, m, work):
        self.health()
        self.check_web_mount()
        self.check_site()
        state = {"phase": "prepared", "domain": self.domain, "services": {}, "web": {}, "release": release}
        for name, item in m["backend"].items():
            path = local_file(self.backend, name)
            require(file_hash(path) == item["before"], f"backend source drift: {name}")
            if path.exists():
                backup = work / "before/backend" / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, backup)
        for name, sha in m["web"].items():
            path = local_file(self.web, name)
            before = file_hash(path)
            if name.startswith("assets/"):
                require(before in (sha, None), f"immutable asset collision: {name}")
            else:
                state["web"][name] = before
                if before:
                    backup = work / "before/web" / name
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(path, backup)
        for svc in SERVICES:
            data = self.inspect(svc)
            config_hash = self.service_hash(svc)
            require(
                data["Config"]["Labels"].get("com.docker.compose.config-hash") == config_hash,
                f"{svc} live Compose definition differs; review separately",
            )
            old = data["Image"]
            tag = data["Config"]["Image"]
            require(cmd("docker", "image", "inspect", "-f", "{{.Id}}", tag).decode().strip() == old, "live tag drift")
            require(not {v["tag"] for v in state["services"].values()} & {tag}, "shared mutable service tag")
            check = "import pathlib,hashlib,json,sys;p=pathlib.Path('/app');print(json.dumps({n:hashlib.sha256((p/n).read_bytes()).hexdigest() if (p/n).exists() else None for n in sys.argv[1:]}))"
            actual = json.loads(cmd("docker", "exec", "aegis-" + svc, "python", "-c", check, *m["backend"]))
            selected = scope(m)[svc]
            require(
                set(actual) == set(m["backend"]) and all(actual[n] == m["backend"][n]["before"] for n in selected),
                f"{svc} runtime source drift",
            )
            expected = {**actual, **{n: m["backend"][n]["after"] for n in selected}}
            base = f"aegis-release-{svc}:base-{release[:20]}"
            new = f"aegis-release-{svc}:new-{release[:20]}"
            cmd("docker", "tag", old, base)
            ctx = work / ("build-" + svc)
            ctx.mkdir()
            for n in selected:
                target = ctx / n
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(bundle / "backend" / n, target)
            (ctx / "Dockerfile").write_text(f"FROM {base}\nCOPY src/ /app/src/\nRUN python -m compileall -q /app/src\n")
            cmd("docker", "build", "--pull=false", "--network=none", "-t", new, str(ctx))
            candidate = cmd("docker", "image", "inspect", "-f", "{{.Id}}", new).decode().strip()
            require(
                json.loads(
                    cmd(
                        "docker",
                        "run",
                        "--rm",
                        "--network",
                        "none",
                        "--entrypoint",
                        "python",
                        candidate,
                        "-c",
                        check,
                        *m["backend"],
                    )
                )
                == expected,
                "candidate checksum mismatch",
            )
            # Catch invalid imports without using live env, database or network.
            module = "src.main" if svc == "bot" else "src.api.main"
            cmd(
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-e",
                "BOT_TOKEN=123:test",
                "-e",
                "ADMIN_IDS=[1]",
                "-e",
                "DATABASE_URL=sqlite+aiosqlite:///:memory:",
                "--entrypoint",
                "python",
                candidate,
                "-c",
                f"import {module}",
            )
            state["services"][svc] = {"old": old, "new": candidate, "tag": tag, "config_hash": config_hash}
        return state

    def switch(self, state, side):
        for images in state["services"].values():
            cmd("docker", "tag", images[side], images["tag"])
        cmd(
            "docker",
            "compose",
            "up",
            "-d",
            "--force-recreate",
            "--no-deps",
            "--no-build",
            "--pull",
            "never",
            *SERVICES,
            cwd=self.compose,
        )
        for svc, images in state["services"].items():
            require(self.inspect(svc)["Image"] == images[side], "wrong image: " + svc)
        self.ready()

    def check_drift(self, m, state, side):
        self.check_web_mount()
        for svc, images in state["services"].items():
            require(
                self.service_hash(svc) == images["config_hash"]
                and self.inspect(svc)["Image"] == images[side]
                and cmd("docker", "image", "inspect", "-f", "{{.Id}}", images["tag"]).decode().strip() == images[side],
                f"{svc} changed since staging",
            )
        for n, v in m["backend"].items():
            require(
                file_hash(local_file(self.backend, n)) == v["before" if side == "old" else "after"],
                f"source changed: {n}",
            )
        for n, old in state["web"].items():
            require(file_hash(local_file(self.web, n)) == (old if side == "old" else m["web"][n]), f"site changed: {n}")

    def install(self, bundle, m, state):
        for name in m["backend"]:
            atomic(local_file(self.backend, name), (bundle / "backend" / name).read_bytes(), 0o644)
        self.switch(state, "new")
        # The directory itself is bind-mounted: don't rename it! Assets first;
        # cached pages keep their immutable assets even across rollback.
        for name in sorted(m["web"], key=lambda n: n in PAGES):
            path = local_file(self.web, name)
            if name.startswith("assets/") and path.exists():
                require(digest(path) == m["web"][name], "asset collision")
                continue
            atomic(path, (bundle / "web" / name).read_bytes(), 0o644)
        self.check_site()

    def restore(self, work, m, state):
        for root, folder, files in [
            (self.backend, "backend", {n: v["before"] for n, v in m["backend"].items()}),
            (self.web, "web", state["web"]),
        ]:
            for name, sha in files.items():
                path = local_file(root, name)
                if sha is None:
                    path.unlink(missing_ok=True)
                else:
                    old = work / "before" / folder / name
                    require(digest(old) == sha, "backup mismatch")
                    atomic(path, old.read_bytes(), old.stat().st_mode & 0o777)
        self.switch(state, "old")
        self.check_site()


def execute(host, action, bundle, release):
    require(bool(re.fullmatch("[a-f0-9]{64}", release)), "expected full bundle ID")
    host.store.mkdir(exist_ok=True, mode=0o700)
    work = host.store / release
    state_file = work / "state.json"
    with (host.store / "lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for record in host.store.glob("*/state.json"):
            if json.loads(record.read_text())["phase"] == "switching":
                require(
                    action == "rollback" and record == state_file, "interrupted release; recover it before continuing"
                )
        if action == "prepare":
            require(not work.exists(), "release already staged")
            with tempfile.TemporaryDirectory(dir=host.store) as temp:
                stage = Path(temp) / "release"
                stage.mkdir()
                shutil.copytree(bundle, stage / "bundle", symlinks=True)
                m = verify(stage / "bundle", release)
                state = host.stage(stage / "bundle", release, m, stage)
                save(stage / "state.json", state)
                stage.rename(work)
            return
        m = verify(work / "bundle", release)
        state = json.loads(state_file.read_text())
        require(host.domain == state["domain"], "wrong site domain")
        if action == "apply":
            require(state["phase"] == "prepared", "not prepared")
            host.check_drift(m, state, "old")
            state["phase"] = "switching"
            save(state_file, state)
            try:
                host.install(work / "bundle", m, state)
            except BaseException:
                host.restore(work, m, state)
                state["phase"] = "rolled-back"
                save(state_file, state)
                raise
            state["phase"] = "active"
            save(state_file, state)
        else:
            require(state["phase"] in {"active", "switching"}, "release not active/interrupted")
            if state["phase"] == "active":
                host.check_drift(m, state, "new")
            state["phase"] = "switching"
            save(state_file, state)
            host.restore(work, m, state)
            state["phase"] = "rolled-back"
            save(state_file, state)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "apply", "rollback"])
    parser.add_argument("release")
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--domain", required=True)
    args = parser.parse_args()
    require(args.action != "prepare" or args.bundle is not None, "prepare needs --bundle")
    execute(Control(ROOT, args.domain), args.action, args.bundle, args.release)
    print(args.action + " OK: " + args.release)
