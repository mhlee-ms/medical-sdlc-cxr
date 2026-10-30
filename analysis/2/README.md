# CLEAR 심층 분석: AP/PA 교란 검토 (이슈 #2)

## 논문이 보고한 것
- CLEAR는 87만 건 영상-보고서 쌍(239,391명)으로 학습한 개념 기반 기반모델이며, 미국·유럽·아시아 4개 외부 데이터셋에서 검증하고 방사선학적 교란 요인을 체계적으로 식별한다고 보고한다. 출처: https://doi.org/10.1038/s41551-026-01741-4 (초록, `recommended/2026-08-19/clear/paper.json`).

## 우리 데이터가 보여주는 것
모든 쿼리는 `llm.hospital` 대상이며 집계만 기록했다.

| 항목 | AP | PA | 전체 |
|---|---|---|---|
| 검사 수 | 88 | 184 | 272 (153명) |
| 비정상(`<> 'No Finding'`) | 45 (51%) | 82 (45%) | 127 |
| 다중 라벨(`like '%\|%'`) | 23 | 27 | 50 |
| 환자 수 | 56 | 130 | 33명은 AP·PA 모두 보유 |

```sql
select count(*), count(distinct patient_key), sum((findings_label<>'No Finding')::int), sum((findings_label like '%|%')::int) from llm.hospital;
select view_position, count(*), sum((findings_label<>'No Finding')::int), sum((findings_label like '%|%')::int) from llm.hospital group by 1;
select count(*) from (select patient_key from llm.hospital group by 1 having count(distinct view_position)>1) t;
```

라벨별 양성 수(AP/PA, 다중 라벨은 각 라벨에 중복 계산): Atelectasis 16/16, Cardiomegaly 2/8, Consolidation 4/3, Edema 3/3, Effusion 17/16, Emphysema 4/5, Fibrosis 1/12, Hernia 0/2, Infiltration 20/25, Mass 0/2, Nodule 2/10, Pleural_Thickening 3/6, Pneumonia 3/0, Pneumothorax 3/9. AP에서 5건 이상인 라벨은 3개뿐이다(`figure.svg`).

```sql
select l, sum((view_position='AP')::int) ap, sum((view_position='PA')::int) pa
from llm.hospital, unnest(array['Atelectasis','Cardiomegaly','Consolidation','Edema','Effusion','Emphysema','Fibrosis','Hernia','Infiltration','Mass','Nodule','Pleural_Thickening','Pneumonia','Pneumothorax']) l
where findings_label like '%'||l||'%' group by l order by l;
```

기관(INST01–05)별 AP/PA: 17/45, 20/36, 13/41, 21/27, 17/35. 장비(DEV01–09)별 AP 4–14건, PA 10–27건. 모든 기관과 장비에 두 뷰가 존재하므로 뷰가 기관·장비와 완전히 겹치지는 않는다. 다만 PA 비중이 기관 간 53–75%로 다르다.

```sql
select institution_code, view_position, count(*) from llm.hospital group by 1,2 order by 1,2;
select device_id, view_position, count(*) from llm.hospital group by 1,2 order by 1,2;
```

## 추론 (가설 수준)
- AP는 비정상·다중 라벨 비율이 높아 단순 AP/PA 성능 비교는 사례 구성의 영향을 받을 수 있다.
- 라벨별 민감도 비교는 표본이 너무 작아 Atelectasis·Effusion·Infiltration 외에는 불가능하다.

## 모델 부분 (CXR Lens로 위임)
성능 차이(AUROC·민감도·CI)와 Grad-CAM은 여기서 실행하지 않았다. 병합 후 `CXR Lens`가 `analysis.json`의 `cxr.runs`(AP, PA, AP+Infiltration)를 실행한다.

## Limitations
- 272건 합성 코호트이며 같은 환자의 반복 검사가 있어 독립 표본이 아니다.
- 대부분의 라벨은 한 뷰에서 5건 미만이다.
- 기관·장비·성별·나이 보정은 하지 않았다.
- CLEAR 같은 개념 모델이 없어 "어떤 개념이 차이를 설명하는가"에는 답할 수 없다. Grad-CAM은 대체재가 아니다.
- 모든 결과는 가설 생성용이다.
