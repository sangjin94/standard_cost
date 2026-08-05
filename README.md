# 표준물류단가 산정 시스템 (standard_cost)

물류영업팀이 신규 화주 견적 시 **작업비 · 물류비(운송) · 보관비**를 원가 기반으로 산정하고,
일반관리비·목표이익률을 반영한 최종 표준단가를 산출하는 Flask 웹앱입니다.

## 산정 로직

모든 비용의 공통 공식: **총원가 ÷ 처리능력(생산성 · 적재효율 · CAPA) = 단위원가**

| 비용 | 공식 |
|------|------|
| 작업비 | 공정별 `시급 ÷ 시간당 생산성` 합산 × (1 + 간접배부율) |
| 물류비 | ① **배송단가 시스템 연동** (아래 참고) ② 직접입력 ③ `대당 월 운행원가 ÷ (최대적재 PLT × 적재율 × 회전수 × 운행일수)` ÷ BOX/PLT |
| 보관비 | `(임차료 + 관리비) ÷ (유효 CAPA × 목표가동률)` × 평균재고 PLT ÷ 월 출고 BOX |
| 최종단가 | 원가합 × (1 + 일반관리비율) ÷ (1 − 목표이익률) |

### 배송비 단가 시스템 연동

같은 PC의 [hanex_deliverycost](https://github.com/sangjin94/hanex_deliverycost)(delivery_pricing)가 산정해 둔
고객사별 **박스당 배송단가(직송 + 이고 + 거점 변동용차, 최소/최대)를 그대로 가져와** 물류비로 사용합니다.
월평균 출고 BOX·BOX/PLT 환산비 등 물동 실적도 함께 자동 입력됩니다.

- 연동 방식: `delivery_bridge.py`를 별도 프로세스로 실행해 delivery_pricing의 계산 로직(메인 화면과 동일한 집계 규칙)을 그대로 호출 → JSON 반환 (`delivery_link.py`가 10분 캐시)
- 경로 설정: SystemConfig `delivery_pricing_dir` (기본 `C:\Users\HanEx\Desktop\delivery_pricing`)
- 우선순위: 운송비 직접입력 > 연동 > 차량 원가 계산

- 평균 재고 PLT = 일평균 출고 PLT × 재고회전일수
- 민감도 시나리오: 생산성·적재율·보관가동률 전제를 ±10% 흔들어 최종 단가 변동 확인

## 실행

```
pip install -r requirements.txt
python app.py
```

또는 `표준단가 서버실행.bat` 더블클릭 → http://localhost:5001

## 화면 구성

- **종합 견적** (`/`) — 물동 조건 입력 → 3대 비용 산출 → 최종 견적단가 + 민감도 시나리오 + 견적 저장
- **작업비 마스터** (`/masters/work-cost`) — 공정별 생산성·시급, 간접배부율
- **물류비 마스터** (`/masters/vehicle-cost`) — 차종별 고정비·변동비·적재효율
- **보관비 마스터** (`/masters/storage`) — 센터별 임차료·관리비·유효 CAPA·목표가동률

기본 등록된 공정·차량 원가는 개략 기준값이므로 실제 원가로 수정 후 사용하세요.
DB는 SQLite(`instance/standard_pricing.db`)로 자동 생성됩니다.
