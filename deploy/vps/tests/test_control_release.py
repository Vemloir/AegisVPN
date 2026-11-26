"""Exercise the release transaction on real files; replace Docker and HTTPS only."""

import json
import subprocess

import pytest
from deploy.vps.control_release import bundle, host


def put(p, data):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(data)
    return bundle.digest(p)


@pytest.fixture
def env(tmp_path, monkeypatch):
    live = host.Control(tmp_path, "example.org")
    incoming = tmp_path / "incoming"
    entry = "src/main.py"
    before = put(live.backend / entry, "old")
    after = put(incoming / "backend" / entry, "new")
    old_page = '<script src="/assets/old.js"></script>'
    page = '<script src="/assets/new.js"></script>'
    for name in bundle.PAGES:
        put(live.web / name, old_page)
        put(incoming / "web" / name, page)
    put(live.web / "assets/old.js", "old asset")
    put(incoming / "web/assets/new.js", "new asset")
    m = {
        "version": 1,
        "backend": {entry: {"after": after, "before": before}},
        "web": bundle.inventory(incoming / "web"),
    }
    release = put(incoming / "manifest.json", json.dumps(m))
    old_images = {svc: "old-" + svc for svc in host.SERVICES}
    tagged = old_images.copy()
    running = old_images.copy()
    calls = []

    def inspect(svc):
        return {"Image": running[svc], "Config": {"Image": svc, "Labels": {"com.docker.compose.config-hash": "c" * 64}}}

    def docker(*args, cwd=None, timeout=300):
        calls.append(args)
        assert args[0] == "docker"
        if args[1:3] == ("image", "inspect"):
            image = args[-1]
            return tagged.get(image, image).encode()
        if args[1] == "exec":
            return json.dumps({entry: before}).encode()
        if args[1] == "run":
            return json.dumps({entry: after}).encode()
        if args[1] == "build":
            tagged[args[-2]] = "built-" + args[-2]
            return b""
        if args[1] == "tag":
            tagged[args[3]] = args[2]
            return b""
        if args[1:3] == ("compose", "up"):
            assert "--no-deps" in args and "--no-build" in args and args[-2:] == host.SERVICES
            running.update({s: tagged[s] for s in host.SERVICES})
            return b""
        pytest.fail(f"unexpected command {args}")

    monkeypatch.setattr(live, "inspect", inspect)
    monkeypatch.setattr(live, "service_hash", lambda _: "c" * 64)
    monkeypatch.setattr(host, "cmd", docker)
    for fn in ["health", "ready", "check_site", "check_web_mount"]:
        monkeypatch.setattr(live, fn, lambda: None)
    return live, incoming, release, running, calls


def test_broken_second_build_leaves_running_services_and_site_untouched(env, monkeypatch):
    live, src, release, running, calls = env
    original = host.cmd

    def broken(*args, **kwargs):
        if args[1] == "build" and "siteapi" in args[-2]:
            raise subprocess.CalledProcessError(1, "docker build")
        return original(*args, **kwargs)

    monkeypatch.setattr(host, "cmd", broken)
    with pytest.raises(subprocess.CalledProcessError):
        host.execute(live, "prepare", src, release)
    assert not (live.store / release).exists() and not any(a[1:3] == ("compose", "up") for a in calls)
    assert running == {"bot": "old-bot", "siteapi": "old-siteapi"} and "old.js" in (live.web / "index.html").read_text()
    assert (live.backend / "src/main.py").read_text() == "old"


@pytest.mark.parametrize("failure", [False, True])
def test_deploy_and_rollback_preserve_db_and_old_asset(env, monkeypatch, failure):
    live, src, release, running, calls = env
    db = live.compose / "data/bot/test.db"
    put(db, "before")
    host.execute(live, "prepare", src, release)

    def probe():
        if "new.js" in (live.web / "en/index.html").read_text():
            db.write_text("new transaction")
            if failure:
                raise RuntimeError("site probe failed")

    monkeypatch.setattr(live, "check_site", probe)
    if failure:
        with pytest.raises(RuntimeError, match="site probe"):
            host.execute(live, "apply", None, release)
    else:
        host.execute(live, "apply", None, release)
        assert all(i.startswith("built-") for i in running.values())
        host.execute(live, "rollback", None, release)
    assert running == {"bot": "old-bot", "siteapi": "old-siteapi"}
    assert (live.store / release / "state.json").is_file() and json.loads(
        (live.store / release / "state.json").read_text()
    )["phase"] == "rolled-back"
    assert (live.web / "assets/old.js").is_file() and "old.js" in (live.web / "en/index.html").read_text()
    assert (live.backend / "src/main.py").read_text() == "old" and db.read_text() == "new transaction"


def test_drift_and_tampering_refused_before_switch(env):
    live, src, release, running, calls = env
    host.execute(live, "prepare", src, release)
    f = live.backend / "src/main.py"
    f.write_text("hotfix")
    with pytest.raises(ValueError, match="source changed"):
        host.execute(live, "apply", None, release)
    assert f.read_text() == "hotfix"
    f.write_text("old")
    put(live.store / release / "bundle/web/assets/new.js", "oops")
    with pytest.raises(ValueError, match="checksum"):
        host.execute(live, "apply", None, release)
    assert not any(a[1:3] == ("compose", "up") for a in calls)


def test_rescope_reuses_approved_bytes_only(env, tmp_path):
    _, base, _, _, _ = env
    mpath = base / "manifest.json"
    old = json.loads(mpath.read_text())
    inputs = {"backend": old["backend"], "web_inputs": {"src/App.jsx": "a" * 64}}
    spec = tmp_path / "spec.json"
    old["spec_sha256"] = put(spec, json.dumps(inputs, sort_keys=True, indent=2) + "\n")
    approved = put(mpath, json.dumps(old))
    new_spec = {**inputs, "service_files": {s: list(old["backend"]) for s in host.SERVICES}}
    put(spec, json.dumps(new_spec))
    dest = tmp_path / "scoped"
    release = bundle.rescope_v1(spec, dest, base, approved)
    new = bundle.verify(dest, release)
    assert new["base_release"] == approved != release and bundle.digest(mpath) == approved
    assert new["web"] == old["web"] and new["backend"] == old["backend"]
    other = tmp_path / "failed"
    put(spec, json.dumps({**new_spec, "web_inputs": {}}))
    with pytest.raises(ValueError, match="inputs differ"):
        bundle.rescope_v1(spec, other, base, approved)
    put(spec, json.dumps(new_spec))
    put(base / "web/assets/new.js", "different bytes")
    with pytest.raises(ValueError, match="web checksum"):
        bundle.rescope_v1(spec, other, base, approved)
    assert not other.exists()


def test_unsafe_paths_refused():
    for name in ["src/../deploy.py", "src/models/server.py", "src/core/migrations.py", ".env"]:
        assert not bundle.python_path(name)
    for name in ["assets/../private.js", "private.pem", "assets/evil.html"]:
        assert not bundle.public_path(name)
