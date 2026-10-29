"""Chest X-ray analysis harness.

Reads images through the masked `llm.hospital` view as `llm_reader`, runs a
pretrained torchxrayvision DenseNet, localizes each top finding with Grad-CAM,
and writes predictions, cohort-level metrics, overlays and a Korean report.

    python cxr_harness.py check
    python cxr_harness.py run [--limit N] [--view PA|AP] [--label Nodule] [--top-k 3]
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import platform
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    # torchxrayvision prints a block-character progress bar that cp949 consoles reject.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
from PIL import Image, ImageDraw, ImageFont

NIH_LABELS = [
    "Atelectasis", "Cardiomegaly", "Consolidation", "Edema", "Effusion",
    "Emphysema", "Fibrosis", "Hernia", "Infiltration", "Mass", "Nodule",
    "Pleural_Thickening", "Pneumonia", "Pneumothorax",
]
KO = {
    "Atelectasis": "무기폐", "Cardiomegaly": "심비대", "Consolidation": "경화",
    "Edema": "부종", "Effusion": "흉수", "Emphysema": "폐기종", "Fibrosis": "섬유화",
    "Hernia": "탈장", "Infiltration": "침윤", "Mass": "종괴", "Nodule": "결절",
    "Pleural_Thickening": "흉막비후", "Pneumonia": "폐렴", "Pneumothorax": "기흉",
}
MIN_POSITIVES = 5
DISPLAY = 512
CAM_EXPORT = 128
DIFFUSE_AREA_PCT = 35
SHORTCUT_PCT = 50
THORAX_MARGIN_PX = 32
REPO_ROOT = Path(__file__).resolve().parents[4]

# Output order of torchxrayvision's ChestX-Det PSPNet; masks are cached as a 14-bit bitmask in this order.
ANATOMY = [
    "Left Clavicle", "Right Clavicle", "Left Scapula", "Right Scapula", "Left Lung", "Right Lung",
    "Left Hilus Pulmonis", "Right Hilus Pulmonis", "Heart", "Aorta", "Facies Diaphragmatica",
    "Mediastinum", "Weasand", "Spine",
]
ANATOMY_KO = {
    "Left Clavicle": "좌측 쇄골", "Right Clavicle": "우측 쇄골", "Left Scapula": "좌측 견갑골",
    "Right Scapula": "우측 견갑골", "Left Lung": "좌폐", "Right Lung": "우폐",
    "Left Hilus Pulmonis": "좌측 폐문", "Right Hilus Pulmonis": "우측 폐문", "Heart": "심장",
    "Aorta": "대동맥", "Facies Diaphragmatica": "횡격막 아래(상복부)", "Mediastinum": "종격동",
    "Weasand": "기관", "Spine": "척추",
}
# The PSPNet "Facies Diaphragmatica" mask covers the whole subdiaphragmatic area, so it is not counted as thorax.
THORAX = {"Left Lung", "Right Lung", "Left Hilus Pulmonis", "Right Hilus Pulmonis", "Heart", "Aorta",
          "Mediastinum", "Weasand"}
PEAK_PRIORITY = ["Heart", "Right Hilus Pulmonis", "Left Hilus Pulmonis", "Right Lung", "Left Lung", "Aorta",
                 "Weasand", "Mediastinum", "Facies Diaphragmatica", "Right Clavicle", "Left Clavicle",
                 "Right Scapula", "Left Scapula", "Spine"]


def dsn() -> str:
    if os.environ.get("CXR_DB_DSN"):
        return os.environ["CXR_DB_DSN"]
    password = os.environ.get("LLM_DB_PASSWORD", "llm-readonly-local")
    return f"host=localhost port=5432 dbname=medical user=llm_reader password={password}"


def age_bin(age: int) -> str:
    # Same 40-year bins as db/data/README.md; finer bins break k-anonymity here.
    return "0-39" if age < 40 else "40-79" if age < 80 else "80+"


# --------------------------------------------------------------------------- data

@dataclass
class Study:
    id: str
    labels: list[str]
    view: str
    sex: str
    age_bin: str
    png: bytes = field(repr=False, default=b"")


def fetch_studies(limit: int | None, view: str | None, label: str | None) -> list[Study]:
    import psycopg

    clauses, params = [], []
    if view:
        clauses.append("view_position = %s")
        params.append(view)
    if label:
        clauses.append("%s = ANY(string_to_array(findings_label, '|'))")
        params.append(label)
    sql = "SELECT image_file, findings_label, view_position, sex, age, image_data FROM hospital"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += " ORDER BY image_file"
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    studies = []
    with psycopg.connect(dsn()) as conn, conn.cursor(name="cxr_images") as cur:
        cur.itersize = 32
        cur.execute(sql, params)
        for image_file, findings, view_pos, sex, age, data in cur:
            labels = [] if findings == "No Finding" else findings.split("|")
            studies.append(Study(image_file, labels, view_pos, sex, age_bin(age), bytes(data)))
    return studies


# -------------------------------------------------------------------------- model

class Analyzer:
    def __init__(self, weights: str):
        import torch
        import torchxrayvision as xrv

        self.torch, self.xrv = torch, xrv
        self.model = xrv.models.DenseNet(weights=weights).eval()
        self.model.requires_grad_(False)
        self.weights = weights
        self.classes = [c for c in self.model.pathologies if c in NIH_LABELS]
        self.index = {c: self.model.pathologies.index(c) for c in self.classes}

    @staticmethod
    def preprocess(png: bytes) -> tuple[np.ndarray, np.ndarray]:
        img = Image.open(io.BytesIO(png)).convert("L")
        w, h = img.size
        s = min(w, h)
        img = img.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))
        display = np.asarray(img.resize((DISPLAY, DISPLAY), Image.BILINEAR))
        model_in = np.asarray(img.resize((224, 224), Image.BILINEAR), dtype=np.float32)
        model_in = (model_in / 255.0) * 2048.0 - 1024.0  # torchxrayvision's [-1024, 1024] range
        return display, model_in

    def forward(self, model_in: np.ndarray):
        torch, F = self.torch, self.torch.nn.functional
        with torch.no_grad():
            feats = self.model.features(torch.from_numpy(model_in)[None, None])
        # Grad-CAM only needs d(logit)/d(last feature map), so the feature map is the sole leaf.
        feats.requires_grad_(True)
        logits = self.model.classifier(F.adaptive_avg_pool2d(F.relu(feats), 1).flatten(1))
        # op_norm maps each class's calibrated operating threshold to 0.5.
        probs = self.xrv.models.op_norm(torch.sigmoid(logits), self.model.op_threshs)[0]
        # Rounded once here so metrics.json and predictions.csv are computed from identical values.
        scores = {c: round(float(probs[i]), 6) for c, i in self.index.items()}
        return feats, logits, scores

    def cam(self, feats, logits, finding: str) -> np.ndarray:
        torch, F = self.torch, self.torch.nn.functional
        (grad,) = torch.autograd.grad(logits[0, self.index[finding]], feats, retain_graph=True)
        cam = F.relu((grad.mean(dim=(2, 3), keepdim=True) * feats).sum(1, keepdim=True))
        cam = F.interpolate(cam, size=(DISPLAY, DISPLAY), mode="bilinear", align_corners=False)[0, 0]
        cam = cam.detach().numpy()
        return cam / cam.max() if cam.max() > 0 else cam

    def analyze(self, model_in: np.ndarray, top_k: int) -> tuple[dict[str, float], dict[str, np.ndarray]]:
        feats, logits, scores = self.forward(model_in)
        top = sorted(scores, key=scores.get, reverse=True)[:top_k]
        return scores, {c: self.cam(feats, logits, c) for c in top}


# ---------------------------------------------------------------------- anatomy

class Anatomy:
    """Lung/heart/mediastinum masks from torchxrayvision's ChestX-Det PSPNet, cached per study."""

    def __init__(self, cache_dir: Path | None):
        self.cache_dir = cache_dir
        self._model = None
        if cache_dir:
            cache_dir.mkdir(parents=True, exist_ok=True)

    def model(self):
        if self._model is None:
            import torchxrayvision as xrv

            self._model = xrv.baseline_models.chestx_det.PSPNet().eval()
            if list(self._model.targets) != ANATOMY:
                raise RuntimeError(f"unexpected segmentation targets: {self._model.targets}")
        return self._model

    def segment(self, display: np.ndarray) -> np.ndarray:
        import torch

        x = torch.from_numpy(display.astype(np.float32) / 255.0 * 2048.0 - 1024.0)[None, None]
        with torch.no_grad():
            y = torch.sigmoid(self.model()(x))[0].numpy() > 0.5
        bits = np.zeros(y.shape[1:], np.uint16)
        for i in range(len(ANATOMY)):
            bits |= y[i].astype(np.uint16) << i
        return bits

    def masks(self, study_id: str, display: np.ndarray, persist: bool = True) -> dict[str, np.ndarray]:
        # Keyed by image content, not by masked ID: the masking secret is regenerated whenever the DB container is
        # initialised (every Actions run, every new codespace), so masked IDs are not stable cache keys.
        key = hashlib.sha1(display.tobytes()).hexdigest()[:24]
        path = self.cache_dir / f"{key}.png" if self.cache_dir and persist else None
        if path and path.exists():
            bits = np.asarray(Image.open(path)).astype(np.uint16)
        else:
            bits = self.segment(display)
            if path:
                Image.fromarray(bits).save(path, optimize=True)
        return {name: ((bits >> i) & 1).astype(bool) for i, name in enumerate(ANATOMY)}


def _widest_row(mask: np.ndarray) -> tuple[int, int, int, int]:
    rows = np.where(mask.any(1))[0]
    if len(rows) == 0:
        return 0, 0, 0, 0
    sub = mask[rows]
    left = sub.argmax(1)
    right = sub.shape[1] - 1 - sub[:, ::-1].argmax(1)
    i = int(np.argmax(right - left))
    return int(right[i] - left[i]), int(rows[i]), int(left[i]), int(right[i])


def ctr_measure(m: dict[str, np.ndarray]) -> dict | None:
    """Cardiothoracic ratio: widest heart span over widest span of both lungs (inner thoracic width proxy)."""
    lungs, heart = m["Left Lung"] | m["Right Lung"], m["Heart"]
    if lungs.sum() < 0.05 * lungs.size or heart.sum() < 0.01 * heart.size:
        return None
    hw, hy, hx0, hx1 = _widest_row(heart)
    tw, ty, tx0, tx1 = _widest_row(lungs)
    if tw == 0:
        return None
    return {"ctr": round(hw / tw, 3), "heart": [hx0, hy, hx1], "thorax": [tx0, ty, tx1]}


def thorax_roi(m: dict[str, np.ndarray]) -> np.ndarray:
    """Thoracic cavity: convex hull of lungs/heart/mediastinum, widened by half a Grad-CAM cell.

    Lung masks cover aerated lung only, so fluid, collapse and the costophrenic angles would otherwise fall
    outside; the margin absorbs the ~73 px (at 512) resolution of a 7x7 DenseNet feature map.
    """
    from scipy.ndimage import distance_transform_edt
    from skimage.morphology import convex_hull_image

    core = np.zeros(next(iter(m.values())).shape, bool)
    for k in THORAX:
        core |= m[k]
    if not core.any():
        return core
    return distance_transform_edt(~convex_hull_image(core)) <= THORAX_MARGIN_PX


def anatomy_attribution(cam: np.ndarray, m: dict[str, np.ndarray], roi: np.ndarray | None = None) -> dict:
    thorax = thorax_roi(m) if roi is None else roi
    total = float(cam.sum())
    inside = 100.0 * float((cam * thorax).sum()) / total if total > 0 else 0.0
    py, px = np.unravel_index(int(np.argmax(cam)), cam.shape)
    hit = next((k for k in PEAK_PRIORITY if m[k][py, px]), None)
    if hit in ("Left Lung", "Right Lung"):
        ys = np.where(m[hit].any(1))[0]
        t = (py - ys[0]) / max(1, ys[-1] - ys[0])
        structure = ANATOMY_KO[hit] + (" 상부" if t < 1 / 3 else " 중부" if t < 2 / 3 else " 하부")
    else:
        structure = ANATOMY_KO[hit] if hit else "흉강 밖"
    return {"structure": structure, "in_thorax_pct": round(inside, 1),
            "shortcut": inside < SHORTCUT_PCT, "peak": [int(px), int(py)]}


def contours(m: dict[str, np.ndarray], names=("Right Lung", "Left Lung", "Heart")) -> dict[str, list]:
    from skimage.measure import approximate_polygon, find_contours

    out = {}
    for name in names:
        polys = [approximate_polygon(c, 1.5) for c in find_contours(m[name].astype(float), 0.5) if len(c) > 30]
        out[name] = [[[round(float(x), 1), round(float(y), 1)] for y, x in p] for p in polys]
    return out


def anatomy_view(m: dict[str, np.ndarray]) -> dict:
    """What the viewer draws: lung/heart/thoracic-cavity outlines and the cardiothoracic ratio."""
    cs = contours(m)
    cs.update(contours({"Thorax": thorax_roi(m)}, names=("Thorax",)))
    return {"contours": cs, "ctr": ctr_measure(m),
            "names": {**{k: ANATOMY_KO[k] for k in ("Right Lung", "Left Lung", "Heart")}, "Thorax": "흉강 기준 영역"}}


# ---------------------------------------------------------------- localization

def cam_region(cam: np.ndarray, threshold: float = 0.5) -> dict:
    from skimage.measure import label, regionprops

    regions = regionprops(label(cam >= threshold), intensity_image=cam)
    if not regions:
        return {}
    r = max(regions, key=lambda r: r.intensity_image.sum())
    y0, x0, y1, x1 = r.bbox
    cy, cx = r.centroid
    area_pct = round(100 * r.area / cam.size, 1)
    side = "영상 좌측(환자 우측)" if cx < DISPLAY * 0.45 else "영상 우측(환자 좌측)" if cx > DISPLAY * 0.55 else "중앙"
    zone = "상부" if cy < DISPLAY / 3 else "중부" if cy < DISPLAY * 2 / 3 else "하부"
    return {
        "bbox": [int(x0), int(y0), int(x1), int(y1)],
        "area_pct": area_pct,
        "location": "광범위(비특이적)" if area_pct > DIFFUSE_AREA_PCT else f"{side} {zone}",
    }


def colormap(v: np.ndarray) -> np.ndarray:
    stops = np.array([[0, 0, 4], [87, 16, 110], [188, 55, 84], [249, 142, 9], [252, 255, 164]], float)
    pos = np.clip(v, 0, 1) * (len(stops) - 1)
    lo = np.floor(pos).astype(int).clip(0, len(stops) - 2)
    t = (pos - lo)[..., None]
    return ((1 - t) * stops[lo] + t * stops[lo + 1]).astype(np.uint8)


def font(size: int):
    for name in ("malgun.ttf", "AppleGothic.ttf", "NanumGothic.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_panel(display: np.ndarray, study: Study, scores: dict, cams: dict, regions: dict,
                 masks: dict | None = None) -> Image.Image:
    original = Image.fromarray(display).convert("RGB")
    if masks:
        d = ImageDraw.Draw(original)
        for name, polys in contours(masks).items():
            color = (245, 165, 36) if name == "Heart" else (45, 212, 191)
            for p in polys:
                d.line([tuple(pt) for pt in p] + [tuple(p[0])], fill=color, width=2)
    tiles = [("원본 · 정답: " + (", ".join(study.labels) or "No Finding"), original)]
    base = np.stack([display] * 3, -1).astype(float)
    for c, cam in cams.items():
        heat = colormap(cam).astype(float)
        alpha = (0.55 * np.clip(cam, 0, 1))[..., None]
        tile = Image.fromarray((base * (1 - alpha) + heat * alpha).astype(np.uint8))
        rg = regions.get(c) or {}
        if rg.get("bbox"):
            color = (242, 85, 90) if rg.get("shortcut") else (0, 255, 170)
            ImageDraw.Draw(tile).rectangle(rg["bbox"], outline=color, width=3)
        hit = " [정답]" if c in study.labels else ""
        tiles.append((f"{c} ({KO[c]}) {scores[c]:.3f}{hit}", tile))

    header, pad = 34, 6
    panel = Image.new("RGB", (len(tiles) * (DISPLAY + pad) + pad, DISPLAY + header + pad), (17, 20, 26))
    draw, f = ImageDraw.Draw(panel), font(17)
    for i, (caption, tile) in enumerate(tiles):
        x = pad + i * (DISPLAY + pad)
        draw.text((x + 4, 8), caption, fill=(230, 235, 240), font=f)
        panel.paste(tile, (x, header))
    return panel


# ---------------------------------------------------------------------- metrics

def auroc(y: np.ndarray, s: np.ndarray) -> float | None:
    pos, neg = s[y == 1], s[y == 0]
    if len(pos) == 0 or len(neg) == 0:
        return None
    greater = (pos[:, None] > neg[None, :]).sum()
    ties = (pos[:, None] == neg[None, :]).sum()
    return float((greater + 0.5 * ties) / (len(pos) * len(neg)))


def auroc_ci(y: np.ndarray, s: np.ndarray, n_boot: int = 1000, seed: int = 0) -> list[float] | None:
    rng = np.random.default_rng(seed)
    vals = []
    for _ in range(n_boot):
        idx = rng.integers(0, len(y), len(y))
        v = auroc(y[idx], s[idx])
        if v is not None:
            vals.append(v)
    return [round(float(np.percentile(vals, 2.5)), 3), round(float(np.percentile(vals, 97.5)), 3)] if vals else None


def binary_metrics(y: np.ndarray, s: np.ndarray, threshold: float = 0.5) -> dict:
    p = s >= threshold
    tp, fp = int((p & (y == 1)).sum()), int((p & (y == 0)).sum())
    fn, tn = int((~p & (y == 1)).sum()), int((~p & (y == 0)).sum())

    def ratio(a, b):
        return round(a / (a + b), 3) if a + b else None

    a = auroc(y, s)
    return {
        "n": int(len(y)), "positives": int(y.sum()),
        "auroc": round(a, 3) if a is not None else None,
        "auroc_95ci": auroc_ci(y, s) if a is not None else None,
        "sensitivity": ratio(tp, fn), "specificity": ratio(tn, fp), "ppv": ratio(tp, fp),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "underpowered": bool(y.sum() < MIN_POSITIVES or (len(y) - y.sum()) < MIN_POSITIVES),
    }


def spearman(a: np.ndarray, b: np.ndarray) -> float | None:
    if len(a) < 3:
        return None
    ra, rb = np.argsort(np.argsort(a)), np.argsort(np.argsort(b))
    return round(float(np.corrcoef(ra, rb)[0, 1]), 3)


def anatomy_metrics(records: list[dict]) -> dict | None:
    top1 = [(r["top"][0], r["regions"].get(r["top"][0], {})) for r in records]
    top1 = [(c, rg) for c, rg in top1 if "in_thorax_pct" in rg]
    if not top1:
        return None
    by_class = {}
    for c in sorted({c for c, _ in top1}):
        vals = [rg for cc, rg in top1 if cc == c]
        by_class[c] = {"n": len(vals), "median_in_thorax_pct": round(float(np.median([v["in_thorax_pct"] for v in vals])), 1),
                       "shortcut": sum(v["shortcut"] for v in vals)}
    out = {
        "top1_n": len(top1),
        "top1_median_in_thorax_pct": round(float(np.median([rg["in_thorax_pct"] for _, rg in top1])), 1),
        "top1_shortcut": sum(rg["shortcut"] for _, rg in top1),
        "shortcut_threshold_pct": SHORTCUT_PCT,
        "by_class": by_class,
    }

    ctr = [r for r in records if r.get("ctr") and "Cardiomegaly" in r["scores"]]
    if ctr:
        ratio = np.array([r["ctr"]["ctr"] for r in ctr])
        score = np.array([r["scores"]["Cardiomegaly"] for r in ctr])
        label = np.array([("Cardiomegaly" in r["labels"]) for r in ctr], int)
        pa = np.array([r["view"] == "PA" for r in ctr])
        cm = {"n": len(ctr), "spearman_ctr_vs_model": spearman(ratio, score),
              "ctr_gt_0_5": int((ratio > 0.5).sum()),
              "ctr_vs_label": binary_metrics(label, ratio),
              "model_vs_label_same_set": binary_metrics(label, score)}
        if pa.sum() >= 10:
            cm["ctr_vs_label_pa"] = binary_metrics(label[pa], ratio[pa])
            cm["model_vs_label_pa"] = binary_metrics(label[pa], score[pa])
        out["ctr"] = cm
    return out


def compute_metrics(records: list[dict], classes: list[str]) -> dict:
    per_class = {}
    for c in classes:
        y = np.array([c in r["labels"] for r in records], int)
        s = np.array([r["scores"][c] for r in records])
        per_class[c] = binary_metrics(y, s)

    y_any = np.array([bool(r["labels"]) for r in records], int)
    s_any = np.array([max(r["scores"].values()) for r in records])
    subgroups = {}
    for key in ("view", "sex", "age_bin"):
        for value in sorted({r[key] for r in records}):
            m = np.array([r[key] == value for r in records])
            subgroups[f"{key}={value}"] = binary_metrics(y_any[m], s_any[m])
    metrics = {"any_finding": binary_metrics(y_any, s_any), "per_class": per_class, "subgroups": subgroups}
    anatomy = anatomy_metrics(records)
    if anatomy:
        metrics["anatomy"] = anatomy
    return metrics


# ------------------------------------------------------------------------ report

def fmt(v, digits=3):
    return "–" if v is None else f"{v:.{digits}f}" if isinstance(v, float) else str(v)


def write_report(out: Path, run: dict, metrics: dict, records: list[dict]) -> None:
    def row(name, m):
        ci = f"{m['auroc_95ci'][0]:.2f}–{m['auroc_95ci'][1]:.2f}" if m["auroc_95ci"] else "–"
        flag = " ⚠️" if m["underpowered"] else ""
        return (f"| {name}{flag} | {m['positives']}/{m['n']} | {fmt(m['auroc'])} | {ci} | "
                f"{fmt(m['sensitivity'])} | {fmt(m['specificity'])} | {fmt(m['ppv'])} |")

    head = "| 대상 | 양성/전체 | AUROC | 95% CI | 민감도 | 특이도 | PPV |\n|---|---|---|---|---|---|---|"
    a = metrics["any_finding"]
    lines = [
        f"# 흉부 X선 분석 리포트 — {run['run_id']}",
        "",
        f"- 모델: `torchxrayvision` DenseNet121 (`{run['weights']}`), 학습 없이 사전학습 가중치만 사용",
        f"- 데이터: `llm.hospital` 마스킹 뷰, {run['n_studies']}건 (필터: {run['filters'] or '없음'})",
        "- 판정 기준: 모델의 보정 임계값(op_threshs)을 0.5로 정규화한 점수 ≥ 0.5",
        f"- 소요 시간: {run['seconds']}초 · 장치: CPU",
        "",
        "## 1. 이상 소견 유무 (No Finding vs 그 외)",
        "",
        f"점수 = 14개 소견 점수 중 최댓값. AUROC **{fmt(a['auroc'])}**"
        + (f" (95% CI {a['auroc_95ci'][0]:.2f}–{a['auroc_95ci'][1]:.2f})" if a["auroc_95ci"] else "")
        + f", 민감도 {fmt(a['sensitivity'])}, 특이도 {fmt(a['specificity'])}.",
        "",
        "## 2. 소견별 성능",
        "",
        head,
        *[row(f"{c} ({KO[c]})", m) for c, m in sorted(metrics["per_class"].items(), key=lambda kv: -kv[1]["positives"])],
        "",
        f"⚠️ 양성 또는 음성이 {MIN_POSITIVES}건 미만 — 수치를 결론으로 쓰지 마세요.",
        "",
        "## 3. 하위군 (이상 소견 유무 기준)",
        "",
        head,
        *[row(k, m) for k, m in metrics["subgroups"].items()],
        "",
        "## 4. 검토가 필요한 증례",
        "",
    ]

    def case_rows(title, items):
        lines.extend([f"### {title}", "", "| ID | 정답 | 모델 상위 소견 | 근거 위치 | 패널 |", "|---|---|---|---|---|"])
        for r in items:
            top = ", ".join(f"{c} {r['scores'][c]:.3f}" for c in r["top"])
            rg = r["regions"].get(r["top"][0], {})
            loc = rg.get("structure", rg.get("location", "–"))
            if "in_thorax_pct" in rg:
                loc += f" (흉강 안 {rg['in_thorax_pct']:.0f}%{' ⚠️' if rg['shortcut'] else ''})"
            lines.append(f"| `{r['id'][-8:]}` | {', '.join(r['labels']) or 'No Finding'} | {top} | {loc} | "
                         f"[보기](overlays/{r['id']}.jpg) |")
        lines.append("")

    def any_score(r):
        return max(r["scores"].values())

    fn = sorted([r for r in records if r["labels"]], key=any_score)[:5]
    fp = sorted([r for r in records if not r["labels"]], key=any_score, reverse=True)[:5]
    tp = sorted([r for r in records if r["labels"] and r["top"][0] in r["labels"]],
                key=lambda r: -r["scores"][r["top"][0]])[:5]
    case_rows("놓친 증례 후보 — 이상 소견이 있는데 점수가 가장 낮음", fn)
    case_rows("과잉 판정 후보 — No Finding인데 점수가 가장 높음", fp)
    case_rows("잘 맞힌 증례 — 1순위 소견이 정답과 일치", tp)

    an = metrics.get("anatomy")
    if an:
        lines += [
            "## 5. 해부학적 근거 점검",
            "",
            "폐·심장·종격동 분할(torchxrayvision ChestX-Det PSPNet)로 흉강(세 구조의 볼록 껍질 + "
            f"{THORAX_MARGIN_PX}px 여유)을 만들고, Grad-CAM 근거가 그 안에 있는지 봤습니다.",
            "",
            f"- 1순위 소견 근거의 흉강 안 비율 중앙값 **{an['top1_median_in_thorax_pct']:.0f}%**",
            f"- 근거의 {an['shortcut_threshold_pct']}% 이상이 흉강 밖인 판정(지름길 의심) **{an['top1_shortcut']}/{an['top1_n']}건**",
            "",
            "| 1순위 소견 | 건수 | 흉강 안 비율 중앙값 | 지름길 의심 |",
            "|---|---|---|---|",
            *[f"| {c} ({KO[c]}) | {v['n']} | {v['median_in_thorax_pct']:.0f}% | {v['shortcut']} |"
              for c, v in sorted(an["by_class"].items(), key=lambda kv: -kv[1]["n"])],
            "",
        ]
        cm = an.get("ctr")
        if cm:
            lines += [
                "### 자동 심흉비(CTR)와 심비대",
                "",
                f"- 측정 가능 {cm['n']}건, CTR > 0.5 **{cm['ctr_gt_0_5']}건**. 모델 심비대 점수와의 순위 상관 "
                f"(Spearman) **{fmt(cm['spearman_ctr_vs_model'])}**.",
                f"- 심비대 라벨 판별 AUROC — CTR {fmt(cm['ctr_vs_label']['auroc'])} vs 모델 "
                f"{fmt(cm['model_vs_label_same_set']['auroc'])} (양성 {cm['ctr_vs_label']['positives']}건"
                f"{', ⚠️ 표본 부족' if cm['ctr_vs_label']['underpowered'] else ''}).",
                "- AP 영상은 심장이 확대되어 CTR이 과대 측정됩니다. CTR은 분할 모델의 폐 폭을 흉곽 내경의 근사로 씁니다.",
                "",
            ]

    lines += [
        f"## {6 if an else 5}. 해석 시 주의",
        "",
        "- **데이터 누출 가능성**: 영상은 NIH ChestX-ray14 공개 영상이고 이 가중치는 NIH를 포함해 학습됐습니다. "
        "같은 영상을 학습에 썼을 수 있어 성능이 과대평가될 수 있습니다.",
        "- **Grad-CAM은 병변 위치가 아닙니다.** 모델이 점수를 낼 때 기여한 영역입니다. 이 데이터에는 병변 위치 정답이 "
        "없어 위치의 정확도는 평가하지 않았습니다.",
        "- **라벨은 판독문에서 자동 추출된 NIH 라벨**이며 잡음이 있습니다.",
        "- 모든 결과는 연구·교육용이며 임상 판단에 쓰기 전에 의사의 검토가 필요합니다.",
        "",
    ]
    (out / "report.md").write_text("\n".join(lines), encoding="utf-8")


# ------------------------------------------------------------------- commands

def cmd_check(_args) -> int:
    print(f"python {platform.python_version()}")
    try:
        import psycopg
        import skimage  # noqa: F401
        import torch
        import torchxrayvision
        print(f"packages ok: torch {torch.__version__}, torchxrayvision {torchxrayvision.__version__}")
    except ImportError as e:
        print(f"FAIL missing package: {e.name} → pip install -r .github/skills/cxr-analysis/requirements.txt")
        return 1

    ok = True
    try:
        with psycopg.connect(dsn(), connect_timeout=5) as conn:
            n, combos = conn.execute("SELECT count(*), count(DISTINCT findings_label) FROM hospital").fetchone()
            print(f"db ok: {n} studies, {combos} label combinations (as llm_reader)")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"FAIL db: {e}\n  → docker compose up -d  (repo root)")
    try:
        print(f"model ok: {len(Analyzer('densenet121-res224-all').classes)} NIH classes")
    except Exception as e:  # noqa: BLE001
        ok = False
        print(f"FAIL model weights: {e}")
    try:
        Anatomy(None).model()
        print(f"anatomy ok: {len(ANATOMY)} structures (lungs, heart, mediastinum …)")
    except Exception as e:  # noqa: BLE001
        print(f"WARN anatomy model: {e}\n  → run with --no-anatomy, or download the weights from the URL above")
    return 0 if ok else 1


def cmd_run(args) -> int:
    t0 = time.time()
    run_id = args.name or datetime.now().strftime("%Y%m%d-%H%M%S")
    out = Path(args.out) / run_id
    (out / "overlays").mkdir(parents=True, exist_ok=True)
    (out / "cams").mkdir(exist_ok=True)

    studies = fetch_studies(args.limit, args.view, args.label)
    if not studies:
        print("no studies matched the filters")
        return 1
    analyzer = Analyzer(args.weights)
    anatomy = None if args.no_anatomy else Anatomy(Path(args.out) / "_anatomy")
    print(f"{len(studies)} studies · {len(analyzer.classes)} classes · anatomy {'off' if anatomy is None else 'on'} · out={out}")

    records = []
    for i, st in enumerate(studies, 1):
        display, model_in = analyzer.preprocess(st.png)
        scores, cams = analyzer.analyze(model_in, args.top_k)
        regions = {c: cam_region(cam) for c, cam in cams.items()}
        masks, ctr = None, None
        if anatomy is not None:
            masks = anatomy.masks(st.id, display)
            ctr = ctr_measure(masks)
            roi = thorax_roi(masks)
            for c, cam in cams.items():
                regions[c].update(anatomy_attribution(cam, masks, roi))
        render_panel(display, st, scores, cams, regions, masks).save(out / "overlays" / f"{st.id}.jpg", quality=85)
        for c, cam in cams.items():
            # Small grayscale CAMs for the static (GitHub Pages) viewer; it upsamples and colours them in the browser.
            Image.fromarray((np.clip(cam, 0, 1) * 255).astype(np.uint8)).resize((CAM_EXPORT, CAM_EXPORT), Image.BILINEAR) \
                .save(out / "cams" / f"{st.id}__{c}.png", optimize=True)
        records.append({"id": st.id, "labels": st.labels, "view": st.view, "sex": st.sex,
                        "age_bin": st.age_bin, "scores": scores, "top": list(cams), "regions": regions, "ctr": ctr})
        if i % 20 == 0 or i == len(studies):
            print(f"  {i}/{len(studies)}")

    metrics = compute_metrics(records, analyzer.classes)
    filters = ", ".join(f"{k}={v}" for k, v in (("limit", args.limit), ("view", args.view), ("label", args.label)) if v)
    run = {
        "run_id": run_id, "weights": args.weights, "top_k": args.top_k, "filters": filters,
        "n_studies": len(records), "classes": analyzer.classes, "seconds": round(time.time() - t0, 1),
        "anatomy": anatomy is not None,
        "source": "llm.hospital (masked view, role llm_reader)",
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }

    with open(out / "predictions.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["id", "labels", "view", "sex", "age_bin", *analyzer.classes, "top", "top_location", "ctr"])
        for r in records:
            rg = r["regions"].get(r["top"][0], {})
            w.writerow([r["id"], "|".join(r["labels"]) or "No Finding", r["view"], r["sex"], r["age_bin"],
                        *[f"{r['scores'][c]:.6f}" for c in analyzer.classes], "|".join(r["top"]),
                        rg.get("structure", rg.get("location", "")), r["ctr"]["ctr"] if r["ctr"] else ""])
    (out / "findings.json").write_text(json.dumps(records, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "run.json").write_text(json.dumps(run, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out, run, metrics, records)

    a = metrics["any_finding"]
    print(f"done in {run['seconds']}s · any-finding AUROC {fmt(a['auroc'])} "
          f"(sens {fmt(a['sensitivity'])}, spec {fmt(a['specificity'])})")
    print(f"report: {out / 'report.md'}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="verify packages, DB access and model weights")
    r = sub.add_parser("run", help="analyze studies and write a report")
    r.add_argument("--limit", type=int, help="analyze only the first N studies")
    r.add_argument("--view", choices=["PA", "AP"], help="restrict to one view position")
    r.add_argument("--label", choices=NIH_LABELS, help="restrict to studies carrying this label")
    r.add_argument("--top-k", type=int, default=3, help="findings to localize per study (default 3)")
    r.add_argument("--weights", default="densenet121-res224-all", help="torchxrayvision weights")
    r.add_argument("--no-anatomy", action="store_true",
                   help="skip lung/heart segmentation (anatomy check and cardiothoracic ratio)")
    r.add_argument("--out", default=str(REPO_ROOT / "runs" / "cxr"), help="output root (default runs/cxr)")
    r.add_argument("--name", help="run directory name (default timestamp)")
    args = p.parse_args()
    return cmd_check(args) if args.cmd == "check" else cmd_run(args)


if __name__ == "__main__":
    sys.exit(main())
