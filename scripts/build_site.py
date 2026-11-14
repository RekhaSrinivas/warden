"""Build a static copy of Warden's public pages (the story and the lab) for GitHub Pages.

    python scripts/build_site.py [out_dir]      default: site/

No server needed: the story replays the published live run (docs/live-runs/2026-11-13), labelled as
such, and the lab shows docs/lab/results.json. Links that need a running Warden (the console, the
verify page) point at the repo instead. Paths are relative, so it works under any base path
(e.g. https://<user>.github.io/warden/).
"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

REPO = "https://github.com/RekhaSrinivas/warden"
TRY_IT = f"{REPO}#try-it-in-5-minutes-no-keys"
EVIDENCE = f"{REPO}/tree/main/docs/live-runs/2026-11-13"
LINKS = [
    ('"/static/', '"static/'),
    ('href="/"', 'href="./"'),
    ('href="/lab"', 'href="lab.html"'),
    ('href="/console"', f'href="{TRY_IT}" target="_blank" rel="noopener"'),
    ('href="/verify"', f'href="{EVIDENCE}" target="_blank" rel="noopener"'),
    ('"/api/scorecard"', '"api/scorecard.json"'),
    ("your offline console starts fresh.", "run Warden yourself in five minutes from the README."),
]


def relink(text: str) -> str:
    for old, new in LINKS:
        text = text.replace(old, new)
    return text


def main(out: Path) -> int:
    from fastapi.testclient import TestClient

    from warden.web import create_app

    client = TestClient(create_app())  # offline: the story replays the published live run
    if out.exists():
        shutil.rmtree(out)
    (out / "api").mkdir(parents=True)
    shutil.copytree(ROOT / "warden" / "web" / "static", out / "static")
    for name in ("story.js", "lab.js"):
        path = out / "static" / name
        path.write_text(relink(path.read_text(encoding="utf-8")), encoding="utf-8")
    pages = {"index.html": "/", "lab.html": "/lab"}
    for name, url in pages.items():
        r = client.get(url)
        r.raise_for_status()
        (out / name).write_text(relink(r.text), encoding="utf-8")
    (out / "api" / "scorecard.json").write_text(json.dumps(client.get("/api/scorecard").json()), encoding="utf-8")
    (out / ".nojekyll").write_text("", encoding="utf-8")
    leftovers = [p.name for p in out.glob("*.html") if '"/static/' in p.read_text(encoding="utf-8")]
    if leftovers:
        print("Absolute links remain in", leftovers)
        return 1
    print(f"Built {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}: {', '.join(pages)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else ROOT / "site"))
