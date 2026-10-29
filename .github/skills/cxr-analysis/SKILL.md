---
name: cxr-analysis
description: 'Run a pretrained chest X-ray model (torchxrayvision DenseNet + Grad-CAM) on the masked hospital DB and produce findings classification, heatmap overlays marking the region behind each finding, cohort metrics (AUROC, sensitivity, specificity, subgroups), a Korean report and a local web viewer (CXR Lens). Use when asked to classify chest X-rays, mark/localize findings, visualize Grad-CAM, evaluate a model on our cohort, compare AP vs PA or subgroups, review missed nodules/false negatives, compare model weights, prepare a physician demo, or run a 흉부 X선 분석/진단 분류/병변 표시/웹 데모 실습.'
argument-hint: '분석 질문 (예: "AP와 PA에서 성능 차이가 있나?", "놓친 결절 검토")'
---

# 흉부 X선 분석 하네스

마스킹된 `llm.hospital` 뷰의 흉부 X선 272장에 **사전학습 모델을 그대로** 돌려,
어떤 소견이 있는지 분류하고(Grad-CAM으로 근거 부위 표시) 코호트 수준에서 얼마나 맞는지
분석합니다. 학습은 하지 않습니다. 스크립트는 숫자와 그림을 만들고, 판단과 서술은 에이전트가
하며, 결론은 사람이 승인합니다 — 저장소의 다른 워크플로와 같은 원칙입니다.

## 언제 쓰나

- "이 X선들에서 무슨 소견이 나오는지 분류하고 어디를 보고 판단했는지 보여줘"
- "이 모델이 우리 코호트에서 얼마나 맞아? AP/PA, 성별, 연령대별로는?"
- "놓친 결절(거짓음성) 증례를 보여줘"
- 실습 참가자가 분석 질문을 하나 정해 **기획 → 실행 → 검증 → 보고**를 돌려 볼 때

## 준비 (최초 1회)

**Codespaces라면 할 일이 없습니다.** `.devcontainer` 가 Python 환경, 가중치, DB를 자동으로 준비합니다.
아래에서 `PY` 는 `.venv/bin/python`(Codespaces · Linux · macOS) 또는 `.\.venv\Scripts\python`(Windows)입니다.

로컬에서는 저장소 루트에서:

```bash
docker compose up -d                                  # 영상이 들어 있는 DB
python -m venv .venv
PY -m pip install -r .github/skills/cxr-analysis/requirements.txt
PY .github/skills/cxr-analysis/scripts/cxr_harness.py check
```

`check`는 패키지, DB 접속(`llm_reader`), 모델 가중치(최초 1회 약 30MB), 폐·심장 분할 가중치(최초 1회 약 260MB)를
확인합니다. 실패하면 [문제 해결](./references/interpretation.md#문제-해결).

## 절차

1. **질문을 한 문장으로 고정한다.** 사용자의 요청을 [분석 과제 예시](./references/analysis-tasks.md)
   중 하나에 대응시키거나, 새 질문이면 같은 형식(질문 · 실행 · 볼 지표 · 결론 낼 수 없는 것)으로
   짧은 기획을 먼저 보여 주고 확인받는다. 표본 수가 결론을 받쳐 주는지 미리 말한다.
2. **실행한다.** [cxr_harness.py](./scripts/cxr_harness.py)를 질문에 맞는 필터로 실행한다.

   ```bash
   PY .github/skills/cxr-analysis/scripts/cxr_harness.py run --name <짧은-이름> [--view PA|AP] [--label Nodule] [--limit N] [--top-k 3] [--weights densenet121-res224-all] [--no-anatomy]
   ```

   전체 272장은 CPU에서 첫 실행 약 10분(폐·심장 분할 포함), 이후 분할 캐시(`runs/cxr/_anatomy/`)를
   재사용해 약 1.5분. 빠른 확인은 `--limit 12`. 웹 뷰어가 떠 있으면 새 런은 상단 런 목록에 자동으로 나타난다.

   논문 딥다이브가 `analysis/<이슈>/analysis.json` 의 `cxr.runs` 로 요청한 조건은
   `PY .github/skills/cxr-analysis/scripts/cxr_harness.py requests` 가 한 번에 돌린다(런 이름 `i<이슈>-<name>`).
   Actions 에서는 그 결과가 병합될 때 CXR Lens 워크플로가 자동으로 실행한다.
3. **산출물을 읽는다.** `runs/cxr/<이름>/` 아래:

   | 파일 | 내용 |
   |---|---|
   | `report.md` | 한국어 요약 리포트 (성능표, 하위군, 검토할 증례, 해부학적 근거 점검, 심흉비, 주의사항) |
   | `metrics.json` | 이상 소견 유무 · 소견별 · 하위군 AUROC(95% CI), 민감도, 특이도, PPV, 혼동행렬, `anatomy`(흉강 밖 근거, CTR) |
   | `predictions.csv` | 영상별 14개 소견 점수, 1순위 근거 위치, CTR (ID는 마스킹 해시) |
   | `findings.json` | 영상별 상위 소견과 Grad-CAM 영역(bbox, 해부학적 위치, 흉강 안 비율, 지름길 의심 여부), CTR |
   | `overlays/<id>.jpg` | 원본(폐·심장 윤곽) + 상위 소견별 히트맵·박스 패널 (흉강 밖 근거는 빨간 박스) |
   | `cams/<id>__<소견>.png` | 상위 소견 히트맵(128px 흑백) — 정적 웹 뷰어용 |
   | `run.json` | 가중치, 필터, 소요 시간 — 재현용 |

4. **검증한다.** 보고할 핵심 수치 하나 이상을 `predictions.csv`에서 **직접 다시 계산**해
   `metrics.json`과 일치하는지 확인한다 ([재계산 예시](./references/analysis-tasks.md#검증-수치-재계산)).
   과장되거나 오해를 부르는 표현이 없는지, ⚠️(표본 부족) 항목을 결론처럼 쓰지 않았는지 점검한다.
5. **보고한다.** 질문에 대한 답을 먼저, 근거 수치(분모 포함)와 패널 2~3장을 링크로, 그리고
   "이 데이터로는 결론 낼 수 없는 것"을 반드시 함께 쓴다. 해석 원칙은
   [해석 가이드](./references/interpretation.md)를 따른다.

## 웹 뷰어 (CXR Lens)

두 가지 형태가 있습니다. 화면은 같습니다.

| 형태 | 만드는 법 | 차이 |
|---|---|---|
| **GitHub Pages (정적)** | Actions → **CXR Lens** → Run workflow ([cxr-lens.yml](../../workflows/cxr-lens.yml)). 로컬에서는 `PY scripts/cxr_site.py` → `site/cxr/` | 서버 없음. 영상마다 상위 `--top-k` 소견의 히트맵만, 업로드 없음. 주소 `https://<owner>.github.io/<repo>/cxr/` |
| **실시간 서버** | `PY scripts/cxr_web.py` | 모든 소견의 히트맵을 그 자리에서 계산, 영상 업로드, 새 런 자동 표시 |

정적 사이트는 `fetch` 로 상대 경로의 JSON·이미지를 읽으므로 파일을 더블클릭하지 말고 HTTP 로 엽니다
(로컬 확인: `python -m http.server -d site 8780` → `http://127.0.0.1:8780/cxr/`).
Actions 는 매 실행마다 DB 서비스 컨테이너를 새로 띄워 마스킹 ID가 바뀌므로 **분석과 사이트 빌드를 같은 job에서** 합니다.
폐·심장 분할 캐시는 영상 내용 해시로 저장되어 실행 간 재사용됩니다.

실시간 서버:

```bash
PY .github/skills/cxr-analysis/scripts/cxr_web.py   # → http://127.0.0.1:8765 (Codespaces는 포트 탭에서 자동으로 열림)
```

서버는 실행한 터미널을 점유하므로, 에이전트가 띄울 때는 백그라운드로 실행하고 `/api/runs` 응답으로 준비를 확인한다.

| 탭 | 내용 |
|---|---|
| 증례 (첫 화면) | 목록(놓침·과잉·적중·흉강 밖 근거 필터) · 히트맵/박스/폐·심장 윤곽/심흉비 측정선/밝기·대비 · 근거의 해부학적 위치와 흉강 안 비율 · 14개 소견 점수 · 업로드 영상 분석 |
| 코호트 성능 | 소견별·하위군 AUROC 포레스트 플롯, 1순위 적중률, 광범위 근거 비율 |
| 임계값 | 슬라이더로 민감도·특이도·PPV·판독 대상 수, ROC 곡선, 점수 분포 |
| 모델 비교 | 두 런(예: `baseline` vs `padchest`)의 소견별 AUROC 차이와 흉강 밖 근거 비율 |
| 검증 리포트 | 판정 배너 · 8개 자동 점검 항목(신호등) · 보고 지침(TRIPOD+AI·CLAIM·DECIDE-AI) 대비 체크리스트 · 자동 CTR vs 모델 산점도 |

- 채팅에서 새로 실행한 런은 페이지를 새로고침하지 않아도 상단 런 목록에 나타납니다(10초 간격, 목록을 열 때 즉시).

- `127.0.0.1` 에만 바인딩합니다(Codespaces 포트 전달은 기본 비공개라 본인만 접속). 업로드 영상은 메모리에서만 처리하고 저장하지 않습니다.
- URL 해시로 화면을 바로 열 수 있습니다. 마스킹 ID는 DB를 새로 만들 때마다 바뀌므로, 다른 환경에서도 통하는 링크는
  **규칙으로 고릅니다**: `#basis=Pneumonia&label=__none&pick=1`(No Finding 중 폐렴 점수 1위), `#basis=Nodule&status=FN&pick=2&finding=Fibrosis`,
  `#status=SC&label=__none&pick=1`(흉강 밖 근거), `#tab=thresh&basis=Effusion&thr=0.4`, `#run=padchest`.
  같은 DB 안에서는 `#case=<ID 끝 8자리>&finding=Nodule` 도 됩니다.
- 의사 대상 시연은 [데모 대본](./references/demo-script.md)을, 검증 원칙은 [해석 가이드](./references/interpretation.md#검증-베스트-프랙티스--이-하네스가-다루는-것과-못-다루는-것)를 따릅니다.

## 지켜야 할 것

- DB는 **`llm_reader`로 `llm.hospital` 뷰만** 읽는다. 관리자 계정이나 `public.hospital`을 쓰지 않는다.
- 보고에는 **집계 수치만** 쓴다. 나이는 `0-39 / 40-79 / 80+` 구간으로만, 환자 단위 메타데이터를 나열하지 않는다.
  증례를 가리킬 때는 마스킹 ID 끝 8자리와 패널 링크만 쓴다.
- `runs/`와 `.venv/`는 커밋하지 않는다 (`.gitignore`에 포함).
- Grad-CAM 박스를 "병변 위치"라고 쓰지 않는다. **"모델이 판단에 주로 쓴 영역"** 이다. `흉강 밖 근거`도
  확정이 아니라 **검토 목록**으로 쓴다(기저부 소견은 경계를 넘기 쉽다).
- 모든 결과는 연구·교육용이며 임상 판단 전에 의사의 검토가 필요하다고 명시한다.
