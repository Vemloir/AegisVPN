from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]


def test_site_caddy_sets_security_headers_without_overwriting_private_api_cache():
    caddy = (ROOT / "deploy/vps/Caddyfile").read_text()
    assert 'root_common_name "ISRG Root X1"' in caddy
    site = caddy.split("site.example.com {", 1)[1].split(
        "\n}\n\n# Optional outbound", 1
    )[0]

    assert 'Strict-Transport-Security "max-age=31536000"' in site
    assert "preload" not in site
    assert 'X-Content-Type-Options "nosniff"' in site
    assert 'Referrer-Policy "strict-origin-when-cross-origin"' in site
    assert 'Permissions-Policy "camera=(), microphone=(), geolocation=()"' in site
    assert "Content-Security-Policy" in site
    assert "frame-ancestors 'none'" in site
    assert "https://telegram.org" in site
    assert "https://oauth.telegram.org" in site
    assert "-Server" in site

    api = site.split("handle /api/* {", 1)[1].split("\n    }", 1)[0]
    assert "Cache-Control" not in api


def test_site_caddy_serves_localized_entries_and_preserves_subscription_routes():
    caddy = (ROOT / "deploy/vps/Caddyfile").read_text()

    assert "try_files {path} {path}/ /ru/index.html" in caddy
    for route in ("/sub/*", "/sub-safe/*", "/sub-fast/*"):
        assert f"handle {route}" in caddy
