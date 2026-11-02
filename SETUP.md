# 고객사 배포 체크리스트

이 템플릿으로 새 저장소를 만들어, 논문 수집 → 탐색 → 딥다이브 → 독립 검증 → 흉부 X선 AI 분석 →
웹 게시까지 전부 GitHub 안에서 돌리는 데 필요한 조건과 순서입니다. 고객사 PC 에 Docker·Python 을
설치할 필요는 없습니다. 모든 작업은 GitHub 가 제공하는 Actions 러너(ubuntu-latest) 안에서 돕니다.

## 1. 사전 조건

| 항목 | 필요 조건 | 확인 방법 |
|---|---|---|
| GitHub 계정/조직 | 저장소를 만들 수 있는 권한 | — |
| GitHub Copilot | 워크플로를 실행하는 계정에 Copilot 라이선스, 조직이면 Copilot 정책에서 사용 허용 | 에이전트 워크플로(수집·탐색·딥다이브·검증)가 Copilot 요청을 씁니다 |
| GitHub Actions | GitHub-hosted 러너 사용 가능 | 조직 정책에서 Actions 가 꺼져 있으면 실행되지 않습니다 |
| 허용 액션 | 조직이 액션을 제한한다면 `actions/*`, `github/gh-aw-actions/*` 허용 | Settings → Actions → General |
| GitHub Pages | public 저장소, 또는 유료 플랜의 private/internal 저장소 | Settings → Pages |
| 외부 접속 | 러너에서 `ghcr.io`, `download.pytorch.org`, `pypi.org`, `files.pythonhosted.org`, `github.com`(모델 가중치), `api.openalex.org`, 출판사 도메인 | GitHub-hosted 러너는 기본으로 열려 있습니다 |

Self-hosted 러너를 쓴다면 Linux + Docker + 위 외부 접속이 필요합니다(서비스 컨테이너로 DB 를 띄웁니다).

## 2. 저장소 설정 (5분)

1. 템플릿에서 **Use this template → Create a new repository** (Public 권장: Pages 무료).
2. **Settings → Actions → General → Workflow permissions**
   → **Allow GitHub Actions to create and approve pull requests** 체크 → Save.
   빠뜨리면 에이전트가 결과 PR 을 만들지 못합니다.
3. **Settings → Pages → Source = GitHub Actions**.
4. (선택) **Settings → Secrets and variables → Actions** 에 `LLM_DB_PASSWORD`.
   없으면 기본값을 씁니다. DB 는 매 실행마다 러너 안에서 생겼다가 사라지는 임시 컨테이너입니다.
5. **Actions** 탭에서 워크플로를 활성화하고 **Setup check → Run workflow**. 요약이 모두 ✅ 인지 확인합니다.

## 3. 첫 실행 순서

| 단계 | 하는 일 | 사람이 할 일 | 소요(실측) |
|---|---|---|---|
| ① CXR Lens | 272장 AI 분석 → `/cxr/` 게시 | Actions → CXR Lens → Run workflow | 약 15–20분 (가중치 캐시 후 단축) |
| ② 논문 수집 | 15개 매체 검색 → `recommended/` PR, 자동 병합 → 대시보드 | Actions → Daily paper recommendations → Run workflow (`date` 비우면 어제) | 약 10–15분 |
| ③ 탐색 | 이슈의 논문을 우리 데이터로 재현 가능한지 조사 → `Probing:` PR | Issues → New issue → **논문 분석 요청** 템플릿에 논문 경로 | 약 10분 |
| ④ 딥다이브 | 탐색 PR 병합 시 시작 → 분석 + CXR 재분석 요청 → `Analysis:` PR | 탐색 PR 을 읽고 Merge | 약 15–25분 |
| ⑤ 독립 검증 | 딥다이브 PR 을 다른 에이전트가 재현 검증 → 리뷰 + `checks` 기록 | 자동 (`Record verification` 이 판정을 analysis.json 에 보강) | 약 10–15분 |
| ⑥ CXR 재분석 | 분석 PR 병합 시 딥다이브가 요청한 조건으로 다시 분석 → `/cxr/` 갱신 | 검증 리뷰를 읽고 분석 PR Merge | 약 15–20분 |

결과 주소: 대시보드 `https://<owner>.github.io/<repo>/`, CXR Lens `https://<owner>.github.io/<repo>/cxr/`.

### 매일 자동 수집 켜기 (기본은 꺼짐)

템플릿에서는 비용 통제를 위해 일정 실행을 껐습니다. 켜려면
`.github/workflows/daily-paper-recommendations.md` 의 `on:` 아래에

```yaml
  schedule:
    - cron: "0 0 * * *"   # 매일 09:00 KST
```

를 넣고 `gh aw compile daily-paper-recommendations` 로 `.lock.yml` 을 다시 만들어 커밋합니다
(gh-aw v0.86.2, `gh extension install github/gh-aw`).

## 4. 비용과 사용량

- Actions 분(minutes): public 저장소는 무료. private 이면 계정 플랜의 포함 분에서 차감됩니다.
  CXR Lens 는 CPU 만 쓰며 1회 약 15–20분.
- Copilot 요청: 에이전트 워크플로 1회에 프리미엄 요청을 수십 건 단위로 씁니다(탐색 1회 실측 약 20건대).
  매일 수집을 켜면 매일 1회분이 추가됩니다.
- 외부 유료 서비스(Azure 등)는 쓰지 않습니다. 모델은 공개 가중치(torchxrayvision)를 러너에서 내려받습니다.

## 5. 재현성과 알려진 제약

- **고정된 것**: DB 이미지(다이제스트 고정), Python 패키지(`requirements.lock`), 모델 가중치(torchxrayvision 1.5.7).
  같은 조건이면 CXR Lens 의 수치(AUROC 등)는 매번 같습니다.
- **매번 달라지는 것**: 에이전트(LLM)가 쓰는 글·쿼리·선택. 수집되는 논문, 딥다이브의 결론과 CXR 재분석 요청
  조건은 실행마다 다를 수 있습니다. 검증 에이전트와 사람의 병합 단계가 이를 거르는 장치입니다.
- **마스킹 ID**: DB 컨테이너가 뜰 때마다 무작위로 다시 마스킹됩니다. 그래서 분석과 웹 생성은 한 실행 안에서
  끝내고, 링크는 ID 대신 조건(`#basis=…&view=…`)으로 겁니다.
- **출판사 페이지**: 일부 출판사는 자동 접속을 막아 원문 확인이 안 될 수 있습니다. 검증 에이전트는
  Crossref 로 서지 정보를 확인하고, 확인하지 못한 항목은 실패로 표시합니다.
- **자기 PR 리뷰**: 에이전트가 자기 저장소 봇이 만든 PR 에 "변경 요청"을 걸 수 없어 일반 코멘트로 남는 경우가
  있습니다. 판정 자체는 리뷰 본문과 `checks` 에 그대로 남습니다.
- **Pages 는 공개 웹**입니다. 올라가는 영상은 NIH 공개 영상, 병원 정보는 합성, 식별자는 마스킹 해시입니다.
  실제 병원 데이터로 바꿀 때는 private 저장소 + 접근 제한 Pages(Enterprise) 또는 내부 호스팅을 쓰세요.
- **의료기기 아님**: 연구·교육 목적 데모입니다. 진단에 쓰지 않습니다.

## 6. 문제 해결

| 증상 | 조치 |
|---|---|
| 에이전트 실행은 성공했는데 PR 이 안 생김 | 2-2 (PR 생성 허용) 확인 후 재실행 |
| deploy 가 "Pages not enabled" | 2-3 확인 후 재실행 |
| DB 단계 실패 (`password authentication failed`) | `LLM_DB_PASSWORD` 시크릿을 지우거나 다시 저장 후 재실행 |
| ghcr.io 이미지 pull 실패 | 조직 방화벽/프록시, self-hosted 러너의 외부 접속 확인 |
| 에이전트 워크플로가 Copilot 권한 오류 | 실행 계정의 Copilot 라이선스와 조직 Copilot 정책 확인 |
| 검증 결과(checks)가 대시보드에 비어 있음 | Actions → **Record verification** 을 PR 번호로 수동 실행 |
