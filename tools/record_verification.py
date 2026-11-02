#!/usr/bin/env python3
"""Copy the verification verdict from the bot's review into analysis/<n>/analysis.json.

The verification agent records its three gates in two places: the `checks` array it
pushes to the pull request branch, and the checklist in its review body. The push is
the fragile half (it runs from a sandbox with no git credentials), so this script is
the fallback that the "Record verification" workflow runs afterwards:

    python3 tools/record_verification.py <analysis.json> <review-body-file>

It only writes when `checks` is still empty, and exits 0 either way.
"""

import json
import pathlib
import re
import sys

GATE_LINE = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ xX])\]\s*(?P<name>[^—\-–:]+?)\s*[—–:-]\s*(?P<detail>.*)$")
GATES = ("출처", "데이터", "이전 에이전트")


def parse(review: str) -> list[dict]:
    checks = []
    for line in review.splitlines():
        m = GATE_LINE.match(line)
        if not m or not any(g in m["name"] for g in GATES):
            continue
        ok = m["mark"].lower() == "x"
        detail = m["detail"].strip()
        detail = re.sub(r"^(통과|실패)[.。]?\s*", "", detail)
        checks.append({"ok": ok, "name": m["name"].strip(), "by": "모델", "detail": "" if ok else detail})
    return checks


def main() -> int:
    path, review_path = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
    analysis = json.loads(path.read_text(encoding="utf-8"))
    if analysis.get("checks"):
        print(f"{path}: checks already recorded ({len(analysis['checks'])}); nothing to do")
        return 0
    checks = parse(review_path.read_text(encoding="utf-8"))
    if not checks:
        print("no gate checklist found in the review body; nothing to do")
        return 0
    analysis["checks"] = checks
    path.write_text(json.dumps(analysis, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{path}: recorded {len(checks)} checks — " + ", ".join(f"{c['name']}={'ok' if c['ok'] else 'fail'}" for c in checks))
    return 0


def demo() -> None:
    body = (
        "검증 결과: request changes\n\n"
        "- [ ] 출처 실재 — 실패. DOI 원문을 해소하지 못했습니다.\n"
        "- [x] 데이터 분석 — 통과. 272/153 재현.\n"
        "- [ ] 이전 에이전트 — 실패. 표 생략.\n"
        "- [x] 다른 체크박스 — 무시돼야 함\n"
    )
    checks = parse(body)
    assert [c["name"] for c in checks] == ["출처 실재", "데이터 분석", "이전 에이전트"], checks
    assert [c["ok"] for c in checks] == [False, True, False], checks
    assert checks[0]["detail"] == "DOI 원문을 해소하지 못했습니다.", checks[0]
    assert checks[1]["detail"] == "", checks[1]
    print("ok")


if __name__ == "__main__":
    if "--check" in sys.argv:
        demo()
    else:
        sys.exit(main())
