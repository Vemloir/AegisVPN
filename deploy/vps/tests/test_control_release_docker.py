"""Opt-in real Docker rehearsal. No production services, external network, or VPN."""

import json
import os
import shutil
import uuid

import pytest
from deploy.vps.control_release import bundle, host

pytestmark = pytest.mark.skipif(
    os.getenv("AEGIS_RELEASE_DOCKER_TEST") != "1", reason="isolated docker rehearsal opt-in"
)


def test_build_failure_then_apply_and_health_rollback(tmp_path, monkeypatch):
    name = "aegiscitest" + uuid.uuid4().hex[:12]
    live = host.Control(tmp_path, "example.org")
    web = live.web
    ctx = tmp_path / "context"
    src = ctx / "src"
    (src / "api").mkdir(parents=True)
    (src / "main.py").write_text("version=0\n")
    (src / "api/main.py").write_text("version=0\n")
    shutil.copytree(src, live.backend / "src")
    (ctx / "Dockerfile").write_text("FROM python:3.12-slim\nWORKDIR /app\nCOPY src/ src/\n")
    web.mkdir(parents=True)
    (web / "assets").mkdir()
    (web / "assets/old.js").write_text("old")
    for page in bundle.PAGES:
        (web / page).parent.mkdir(exist_ok=True)
        (web / page).write_text('<script src="/assets/old.js"></script>')
    raw = host.cmd
    services = {
        k: {
            "container_name": name + "-" + k,
            "image": name + ":" + k,
            "network_mode": "none",
            "command": ["python", "-m", "http.server", "8080", "--bind", "127.0.0.1"],
        }
        for k in [*host.SERVICES, "caddy"]
    }
    env_file = live.compose / "bot.env"
    baseline = "RELEASE_TEST='initial$HOME/${USER}/$$'\n"
    env_file.write_text(baseline)
    services["bot"]["env_file"] = ["bot.env"]
    services["caddy"]["volumes"] = [str(web) + ":/srv/web:ro"]
    services["caddy"]["working_dir"] = "/srv/web"
    (live.compose / "compose.yaml").write_text(json.dumps({"name": name, "services": services}))

    def local(*args, **kw):
        return raw(
            *(name + "-" + a[6:] if a in ("aegis-bot", "aegis-siteapi", "aegis-caddy") else a for a in args), **kw
        )

    monkeypatch.setattr(host, "cmd", local)

    def http(service, path):
        return local(
            "docker",
            "exec",
            name + "-" + service,
            "python",
            "-c",
            "import urllib.request,sys;sys.stdout.buffer.write(urllib.request.urlopen('http://127.0.0.1:8080'+sys.argv[1],timeout=2).read())",
            path,
        )

    def health():
        for svc in host.SERVICES:
            entry = "src/main.py" if svc == "bot" else "src/api/main.py"
            assert http(svc, "/" + entry) == (live.backend / entry).read_bytes()
        assert http("siteapi", "/src/main.py") == b"version=-1\n"

    def site():
        for page in bundle.PAGES:
            assert http("caddy", "/" + page) == (web / page).read_bytes()

    monkeypatch.setattr(live, "health", health)
    monkeypatch.setattr(live, "check_site", site)

    def make(label, code):
        b = tmp_path / label
        (b / "backend/src").mkdir(parents=True)
        (b / "backend/src/main.py").write_text(code)
        (b / "backend/src/api").mkdir()
        (b / "backend/src/api/main.py").write_text("version=2\n")
        shutil.copytree(web, b / "web")
        (b / "web/assets/new.js").write_text(name)
        for page in bundle.PAGES:
            (b / "web" / page).write_text('<script src="/assets/new.js"></script>')
        m = {
            "version": 2,
            "service_files": {"bot": ["src/main.py"], "siteapi": ["src/api/main.py"]},
            "web": bundle.inventory(b / "web"),
            "backend": {
                "src/api/main.py": {
                    "before": bundle.digest(src / "api/main.py"),
                    "after": bundle.digest(b / "backend/src/api/main.py"),
                },
                "src/main.py": {
                    "before": bundle.digest(src / "main.py"),
                    "after": bundle.digest(b / "backend/src/main.py"),
                },
            },
        }
        (b / "manifest.json").write_text(json.dumps(m))
        return b, bundle.digest(b / "manifest.json")

    tags = []
    try:
        raw("docker", "build", "--pull=false", "--network=none", "-t", name + ":bot", str(ctx))
        raw("docker", "tag", name + ":bot", name + ":caddy")
        (src / "main.py").write_text("version=-1\n")
        raw("docker", "build", "--pull=false", "--network=none", "-t", name + ":siteapi", str(ctx))
        (src / "main.py").write_text("version=0\n")
        raw("docker", "compose", "up", "-d", "--no-build", "--pull", "never", cwd=live.compose)
        initial = {s: live.inspect(s)["Id"] for s in services}
        bad, bad_id = make("invalid", "syntax error!\n")
        tags.append(bad_id)
        with pytest.raises(RuntimeError, match="build failed"):
            host.execute(live, "prepare", bad, bad_id)
        assert initial == {s: live.inspect(s)["Id"] for s in services}
        health()
        site()
        good, release = make("good", "version=1\n")
        tags.append(release)
        env_file.write_text("RELEASE_TEST='edited'\n")
        with pytest.raises(ValueError, match="bot live Compose definition differs"):
            host.execute(live, "prepare", good, release)
        assert initial == {s: live.inspect(s)["Id"] for s in services}
        env_file.write_text(baseline)
        # Divergence in an assigned file is a real conflict, unlike the old
        # bot entrypoint in the siteapi base. Never fix it by overwriting it.
        raw(
            "docker",
            "exec",
            name + "-siteapi",
            "python",
            "-c",
            "from pathlib import Path;Path('src/api/main.py').write_text('local edit')",
        )
        with monkeypatch.context() as patch:
            patch.setattr(live, 'health', lambda: http('siteapi', '/src/api/main.py'))
            with pytest.raises(ValueError, match="siteapi runtime source drift"):
                host.execute(live, "prepare", good, release)
        raw(
            "docker",
            "exec",
            name + "-siteapi",
            "python",
            "-c",
            "from pathlib import Path;Path('src/api/main.py').write_text('version=0\\n')",
        )
        host.execute(live, "prepare", good, release)
        env_file.write_text("RELEASE_TEST='edited after prepare'\n")
        with pytest.raises(ValueError, match="bot changed since staging"):
            host.execute(live, "apply", None, release)
        env_file.write_text(baseline)
        assert initial == {s: live.inspect(s)["Id"] for s in services}
        db = live.compose / "data/bot/db-sentinel"
        db.parent.mkdir(parents=True)
        db.write_text("previous")

        def site_gate():
            site()
            if "new.js" in (web / "index.html").read_text():
                db.write_text("preserve later transaction")
                raise ValueError("simulated post-switch site failure")

        monkeypatch.setattr(live, "check_site", site_gate)
        with pytest.raises(ValueError, match="post-switch"):
            host.execute(live, "apply", None, release)
        health()
        site()
        assert db.read_text() == "preserve later transaction"
        assert live.inspect("caddy")["Id"] == initial["caddy"] and "old.js" in (web / "ru/index.html").read_text()
        assert json.loads((live.store / release / "state.json").read_text())["phase"] == "rolled-back"
    finally:
        raw("docker", "compose", "down", cwd=live.compose)
        for tag in [name + ":" + s for s in services] + [
            f"aegis-release-{s}:{kind}-{r[:20]}" for r in tags for s in host.SERVICES for kind in ("base", "new")
        ]:
            try:
                raw("docker", "image", "rm", tag)
            except RuntimeError:
                pass
