# 분석 과제 예시

각 과제는 **질문 · 실행 · 볼 지표 · 결론 낼 수 없는 것** 으로 구성됩니다. 실습에서는 하나를 골라
기획을 먼저 확인받은 뒤 실행합니다. 명령은 저장소 루트 기준이며 `$H`는 다음을 뜻합니다.

```powershell
$H = ".\.venv\Scripts\python .github/skills/cxr-analysis/scripts/cxr_harness.py"
```

---

## 1. 기준 성능 — 이 모델은 우리 코호트에서 얼마나 맞나

- **실행**: `$H run --name baseline`
- **볼 지표**: `metrics.json` → `any_finding` 의 AUROC(95% CI), 민감도, 특이도. `per_class` 에서
  양성 수가 많은 소견(침윤, 무기폐)부터.
- **결론 낼 수 없는 것**: 다른 기관 데이터에서의 성능. 영상이 NIH 공개 영상이고 가중치가 NIH를 포함해
  학습됐으므로 과대평가 가능성이 있다 (과제 5로 확인).

## 2. 촬영 자세 — AP와 PA에서 성능이 다른가

- **실행**: 하위군 표가 기본으로 들어 있으므로 `baseline` 의 `subgroups["view=AP"]`, `["view=PA"]` 를 본다.
  소견별로 깊게 보려면 `$H run --name ap --view AP`, `$H run --name pa --view PA`.
- **볼 지표**: 두 하위군의 AUROC와 CI가 겹치는지. AP는 대개 와상·중증 환자 → 유병률도 같이 비교.
- **결론 낼 수 없는 것**: 차이의 원인(자세 자체인지 환자 중증도인지). 교란을 분리할 정보가 없다.

## 3. 놓친 결절 — 거짓음성은 어떤 모습인가

- **실행**: `$H run --name nodule --label Nodule --top-k 3`
- **볼 지표**: `per_class.Nodule.sensitivity` 와 `fn`. 각 패널에서 결절 점수와, 모델이 대신 주목한 소견·영역.
  `baseline` 의 `report.md` "놓친 증례 후보" 표도 함께.
- **결론 낼 수 없는 것**: 결절 크기·위치별 민감도. DB에 병변 위치 정답이 없고 결절은 7~10건뿐이다 (⚠️).

## 4. 임계값 — 민감도를 올리면 무엇을 잃나

- **실행**: `baseline` 의 `predictions.csv` 로 임계값을 바꿔 가며 계산한다.

  ```python
  import csv
  rows = list(csv.DictReader(open("runs/cxr/baseline/predictions.csv", encoding="utf-8")))
  c = "Effusion"
  y = [c in r["labels"].split("|") for r in rows]
  for t in (0.3, 0.4, 0.5, 0.6, 0.7):
      p = [float(r[c]) >= t for r in rows]
      tp = sum(a and b for a, b in zip(p, y)); fn = sum(b and not a for a, b in zip(p, y))
      tn = sum(not a and not b for a, b in zip(p, y)); fp = sum(a and not b for a, b in zip(p, y))
      print(t, f"sens {tp/(tp+fn):.2f}", f"spec {tn/(tn+fp):.2f}", f"flagged {tp+fp}/{len(rows)}")
  ```

- **볼 지표**: 민감도/특이도와 **판독 대상으로 올라가는 건수**. 임상에선 "몇 장을 더 봐야 하나"가 실감 나는 단위다.
- **결론 낼 수 없는 것**: 이 코호트에 맞춘 임계값이 다른 코호트에서도 최적인지.

## 5. 데이터 누출 점검 — NIH를 안 본 모델로 바꾸면

- **실행**: `$H run --name padchest --weights densenet121-res224-pc`
  (스페인 PadChest로만 학습된 가중치. NIH 영상을 학습에 쓰지 않았다.)
- **볼 지표**: 두 런에서 **공통으로 평가된 소견**의 AUROC 차이. 크게 떨어지면 기준 성능이 누출로
  부풀었을 가능성. `run.json` 의 `classes` 로 공통 소견을 확인한다 (가중치마다 지원 소견이 다르다).
- **결론 낼 수 없는 것**: 차이가 누출 때문인지 학습 데이터 분포 차이 때문인지는 분리되지 않는다.

## 6. 근거 부위의 타당성 — 모델은 맞는 곳을 보나

- **실행**: `baseline` 그대로. 리포트의 "해부학적 근거 점검" 절과 `metrics.json` 의 `anatomy`,
  웹 뷰어 증례 탭의 **흉강 밖 근거** 칩.
- **볼 지표**: 1순위 근거의 흉강 안 비율 중앙값, 흉강 밖 근거(지름길 의심) 장수, 소견별 근거 위치
  (`심장` / `우폐 하부` / `횡격막 아래(상복부)` / `흉강 밖`). 다른 가중치 런과 나란히 보면, 성능이 높은 쪽이
  흉강 밖을 더 많이 보는지 확인할 수 있다.
- **결론 낼 수 없는 것**: 병변 위치 정확도. 위치 정답이 없으므로 **해부학적 타당성**까지만 점검한다.
  기저부 소견은 Grad-CAM 해상도 때문에 과대 표시될 수 있다.

## 7. 심흉비 — 단순 지표 vs 딥러닝

- **실행**: `baseline` 그대로. 리포트의 "자동 심흉비" 절, 웹 뷰어 검증 리포트 탭의 CTR 산점도.
- **볼 지표**: CTR과 모델 심비대 점수의 순위 상관, 심비대 라벨 판별 AUROC(CTR vs 모델, 전체와 PA만),
  AP와 PA의 CTR 분포 차이.
- **결론 낼 수 없는 것**: 심비대 양성은 10장뿐이다. CTR의 흉곽 폭은 폐 경계로 근사했다.

---

## 검증: 수치 재계산

보고 전에 핵심 수치를 `predictions.csv` 에서 독립적으로 다시 계산해 `metrics.json` 과 대조합니다.

```python
import csv, json
run = "runs/cxr/baseline"
rows = list(csv.DictReader(open(f"{run}/predictions.csv", encoding="utf-8")))
m = json.load(open(f"{run}/metrics.json", encoding="utf-8"))
for c, reported in m["per_class"].items():
    pos = [float(r[c]) for r in rows if c in r["labels"].split("|")]
    neg = [float(r[c]) for r in rows if c not in r["labels"].split("|")]
    if not pos or not neg:
        continue
    auc = sum((p > n) + 0.5 * (p == n) for p in pos for n in neg) / (len(pos) * len(neg))
    status = "ok" if round(auc, 3) == reported["auroc"] else "MISMATCH"
    print(f"{status:8} {c:20} recomputed {auc:.3f} reported {reported['auroc']}")
```

일치하지 않으면 보고하지 말고 원인을 찾습니다.
