import json
from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[1] / "app" / "static"


def test_pwa_shell_has_manifest_local_icons_and_safe_worker_contract():
    manifest = json.loads((STATIC_DIR / "manifest.webmanifest").read_text(encoding="utf-8"))
    index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    worker = (STATIC_DIR / "service-worker.js").read_text(encoding="utf-8")

    assert manifest["start_url"] == "/"
    assert manifest["display"] == "standalone"
    assert len(manifest["icons"]) >= 2
    for icon in manifest["icons"]:
        assert (STATIC_DIR / icon["src"].removeprefix("/static/")).exists()
    assert 'href="/static/manifest.webmanifest"' in index
    assert 'src="/static/vendor/lucide.js"' in index
    assert 'src="/static/pwa-register.js"' in index
    assert 'src="/static/firebase-auth.js"' in index
    assert 'id="install-app"' in index
    assert 'url.pathname.startsWith("/api/")' in worker
    assert '"/static/vendor/lucide.js"' in worker
    assert '"/static/firebase-auth.js"' in worker
