"""Build a static CXR Lens site for GitHub Pages from harness runs.

    python cxr_site.py [--runs runs/cxr] [--out site/cxr] [--only baseline,padchest]

The site is plain files with relative paths (no server), so it works under any Pages sub-path. Study images are
read through the masked llm.hospital view as llm_reader, from the same DB instance the runs were made against:
the masking secret is regenerated whenever the DB is initialised, so masked IDs only match within one instance.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cxr_harness import DISPLAY, KO, REPO_ROOT, Analyzer, Anatomy, anatomy_view, dsn  # noqa: E402

from PIL import Image  # noqa: E402

ASSET = Path(__file__).resolve().parents[1] / "assets" / "viewer.html"
THUMB = 96


def load_runs(runs_dir: Path, only: set[str] | None) -> list[tuple[Path, dict]]:
    runs = []
    for d in sorted(runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True) if runs_dir.exists() else []:
        if d.name.startswith("_") or (only and d.name not in only):
            continue
        if all((d / f).exists() for f in ("run.json", "metrics.json", "findings.json")):
            runs.append((d, json.loads((d / "run.json").read_text(encoding="utf-8"))))
    return runs


def write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def fetch_images(ids: list[str]) -> dict[str, bytes]:
    import psycopg

    with psycopg.connect(dsn()) as conn:
        rows = conn.execute("SELECT image_file, image_data FROM hospital WHERE image_file = ANY(%s)", (ids,)).fetchall()
    return {i: bytes(d) for i, d in rows}


def build(runs_dir: Path, out: Path, only: set[str] | None) -> int:
    runs = load_runs(runs_dir, only)
    if not runs:
        print(f"no runs under {runs_dir} — run `cxr_harness.py run --name baseline` first")
        return 1
    if out.exists():
        shutil.rmtree(out)
    (out / "img" / "thumb").mkdir(parents=True)

    html = ASSET.read_text(encoding="utf-8")
    html = html.replace("<script>", "<script>window.CXR_STATIC = true;</script>\n<script>", 1)
    (out / "index.html").write_text(html, encoding="utf-8")
    write_json(out / "data" / "meta.json", {"ko": KO, "display": DISPLAY})
    write_json(out / "data" / "runs.json", [run for _, run in runs])

    ids: dict[str, None] = {}
    for d, run in runs:
        findings = json.loads((d / "findings.json").read_text(encoding="utf-8"))
        write_json(out / "data" / "runs" / f"{run['run_id']}.json", {
            "run": run,
            "metrics": json.loads((d / "metrics.json").read_text(encoding="utf-8")),
            "findings": findings,
        })
        if (d / "cams").is_dir():
            shutil.copytree(d / "cams", out / "cams" / run["run_id"])
        else:
            print(f"warning: {d.name} has no cams/ (made by an older harness) — heatmaps will be missing; rerun it")
        ids.update(dict.fromkeys(f["id"] for f in findings))

    images = fetch_images(list(ids))
    missing = [i for i in ids if i not in images]
    if missing:
        print(f"error: {len(missing)} studies are not in this DB instance (masked IDs change when the DB is "
              "re-initialised). Rerun the harness against the current DB, then build the site.")
        return 1

    anatomy = Anatomy(runs_dir / "_anatomy") if any(run.get("anatomy") for _, run in runs) else None
    views = {}
    for n, (study_id, data) in enumerate(images.items(), 1):
        display, _ = Analyzer.preprocess(data)
        img = Image.fromarray(display)
        img.save(out / "img" / f"{study_id}.jpg", quality=88)
        img.resize((THUMB, THUMB), Image.BILINEAR).save(out / "img" / "thumb" / f"{study_id}.jpg", quality=80)
        if anatomy is not None:
            views[study_id] = anatomy_view(anatomy.masks(study_id, display))
        if n % 50 == 0 or n == len(images):
            print(f"  images {n}/{len(images)}")
    if views:
        write_json(out / "data" / "anatomy.json", views)

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"site: {out} · {len(runs)} runs · {len(images)} studies · {size / 1e6:.1f} MB")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--runs", default=str(REPO_ROOT / "runs" / "cxr"))
    p.add_argument("--out", default=str(REPO_ROOT / "site" / "cxr"))
    p.add_argument("--only", help="comma-separated run names to include (default: all)")
    args = p.parse_args()
    only = set(args.only.split(",")) if args.only else None
    return build(Path(args.runs), Path(args.out), only)


if __name__ == "__main__":
    sys.exit(main())
