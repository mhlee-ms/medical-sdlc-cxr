"""CXR Lens — local web viewer for the chest X-ray analysis harness.

Serves the runs written by `cxr_harness.py run` and computes Grad-CAM on demand for any
finding. Images are read through the masked `llm.hospital` view as `llm_reader`; uploads
are analyzed in memory and never written to disk. Binds to 127.0.0.1 only.

    python cxr_web.py [--port 8765] [--runs runs/cxr]
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import re
import sys
import threading
import uuid
from collections import OrderedDict
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cxr_harness import (  # noqa: E402
    DISPLAY, KO, REPO_ROOT, Analyzer, Anatomy, age_bin, anatomy_attribution, anatomy_view, cam_region, dsn,
)

from fastapi import FastAPI, HTTPException, Request  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse, Response  # noqa: E402

ASSET = Path(__file__).resolve().parents[1] / "assets" / "viewer.html"
SAFE_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_UPLOAD = 20 * 1024 * 1024


class LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def put(self, key, value):
        self[key] = value
        self.move_to_end(key)
        while len(self) > self.size:
            self.popitem(last=False)
        return value

    def fetch(self, key):
        if key in self:
            self.move_to_end(key)
            return self[key]
        return None


class Engine:
    def __init__(self, runs_dir: Path, anatomy: bool = True):
        self.runs_dir = runs_dir
        self.analyzers: dict[str, Analyzer] = {}
        self.anatomy = Anatomy(runs_dir / "_anatomy") if anatomy else None
        self.lock = threading.Lock()
        self.images = LRU(64)    # id -> (display uint8, model_in float32)
        self.masks_cache = LRU(64)
        self.cams = LRU(256)     # (weights, id, finding) -> payload
        self.uploads = LRU(16)   # id -> (display, model_in)

    def analyzer(self, weights: str) -> Analyzer:
        if weights not in self.analyzers:
            self.analyzers[weights] = Analyzer(weights)
        return self.analyzers[weights]

    def masks(self, study_id: str):
        if self.anatomy is None:
            return None
        cached = self.masks_cache.fetch(study_id)
        if cached is not None:
            return cached
        display, _ = self.study(study_id)
        with self.lock:
            m = self.anatomy.masks(study_id, display, persist=not study_id.startswith("upload-"))
        return self.masks_cache.put(study_id, m)

    def anatomy_view(self, study_id: str) -> dict:
        m = self.masks(study_id)
        if m is None:
            return {"contours": {}, "ctr": None}
        return anatomy_view(m)

    def runs(self) -> list[dict]:
        out = []
        if self.runs_dir.exists():
            for d in sorted(self.runs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
                if (d / "run.json").exists() and (d / "findings.json").exists():
                    out.append(json.loads((d / "run.json").read_text(encoding="utf-8")))
        return out

    def run(self, name: str) -> dict:
        d = self.runs_dir / name
        if not SAFE_NAME.match(name) or not (d / "run.json").exists():
            raise HTTPException(404, "run not found")
        load = lambda f: json.loads((d / f).read_text(encoding="utf-8"))  # noqa: E731
        return {"run": load("run.json"), "metrics": load("metrics.json"), "findings": load("findings.json")}

    def study(self, study_id: str):
        if study_id.startswith("upload-"):
            item = self.uploads.fetch(study_id)
            if item is None:
                raise HTTPException(404, "upload expired")
            return item
        cached = self.images.fetch(study_id)
        if cached is not None:
            return cached
        import psycopg

        with psycopg.connect(dsn()) as conn:
            row = conn.execute("SELECT image_data FROM hospital WHERE image_file = %s", (study_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "study not found")
        return self.images.put(study_id, Analyzer.preprocess(bytes(row[0])))

    def explain(self, weights: str, study_id: str, finding: str) -> dict:
        key = (weights, study_id, finding)
        cached = self.cams.fetch(key)
        if cached is not None:
            return cached
        _, model_in = self.study(study_id)
        with self.lock:
            a = self.analyzer(weights)
            if finding not in a.index:
                raise HTTPException(400, f"{finding} is not supported by {weights}")
            feats, logits, scores = a.forward(model_in)
            cam = a.cam(feats, logits, finding)
        region = cam_region(cam)
        m = self.masks(study_id)
        if m is not None:
            region.update(anatomy_attribution(cam, m))
        png = io.BytesIO()
        Image.fromarray((cam * 255).astype(np.uint8)).save(png, format="PNG", optimize=True)
        payload = {
            "finding": finding, "score": scores[finding], "region": region,
            "cam": "data:image/png;base64," + base64.b64encode(png.getvalue()).decode(),
        }
        return self.cams.put(key, payload)

    def upload(self, weights: str, data: bytes) -> dict:
        try:
            display, model_in = Analyzer.preprocess(data)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(400, f"이미지를 읽을 수 없습니다: {e}") from e
        study_id = f"upload-{uuid.uuid4().hex[:12]}"
        self.uploads.put(study_id, (display, model_in))
        with self.lock:
            _, _, scores = self.analyzer(weights).forward(model_in)
        top = sorted(scores, key=scores.get, reverse=True)[:3]
        return {"id": study_id, "labels": None, "view": "-", "sex": "-", "age_bin": "-",
                "scores": scores, "top": top, "regions": {}, "uploaded": True}


def create_app(engine: Engine) -> FastAPI:
    app = FastAPI(title="CXR Lens", docs_url=None, redoc_url=None)

    @app.get("/")
    def index():
        return FileResponse(ASSET, media_type="text/html; charset=utf-8")

    @app.get("/api/meta")
    def meta():
        return {"ko": KO, "display": DISPLAY, "age_bins": [age_bin(0), age_bin(40), age_bin(80)]}

    @app.get("/api/runs")
    def runs():
        return engine.runs()

    @app.get("/api/runs/{name}")
    def run(name: str):
        return JSONResponse(engine.run(name))

    @app.get("/api/image/{study_id}.jpg")
    def image(study_id: str, size: int = DISPLAY):
        display, _ = engine.study(study_id)
        img = Image.fromarray(display)
        if size < DISPLAY:
            img = img.resize((max(32, size), max(32, size)), Image.BILINEAR)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=88)
        return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/cam/{study_id}/{finding}")
    def cam(study_id: str, finding: str, weights: str = "densenet121-res224-all"):
        return engine.explain(weights, study_id, finding)

    @app.get("/api/anatomy/{study_id}")
    def anatomy(study_id: str):
        return engine.anatomy_view(study_id)

    @app.post("/api/upload")
    async def upload(request: Request, weights: str = "densenet121-res224-all"):
        data = await request.body()
        if not data or len(data) > MAX_UPLOAD:
            raise HTTPException(400, "20MB 이하의 PNG/JPEG 파일을 올려 주세요")
        return engine.upload(weights, data)

    return app


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--runs", default=str(REPO_ROOT / "runs" / "cxr"))
    p.add_argument("--no-anatomy", action="store_true", help="disable lung/heart contours and anatomy check")
    args = p.parse_args()

    import uvicorn

    engine = Engine(Path(args.runs), anatomy=not args.no_anatomy)
    runs = engine.runs()
    if not runs:
        print(f"no runs under {args.runs} — run `cxr_harness.py run --name baseline` first")
        return 1
    print("loading model weights…")
    for w in dict.fromkeys(r["weights"] for r in runs):
        engine.analyzer(w)
    if engine.anatomy is not None:
        engine.anatomy.model()
    print(f"CXR Lens → http://127.0.0.1:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(create_app(engine), host="127.0.0.1", port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
