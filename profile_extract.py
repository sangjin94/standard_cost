"""업로드 데이터 → 물동 프로파일 추출 (docs/DESIGN.md §2 양식, §3 추출값).

원본 행은 저장하지 않고 집계만 프로파일 JSON에 남긴다:
  daily   : {날짜: {'box': .., 'orders': .., 'lines': ..}}
  product : {상품코드: 출고BOX 합}   ← 상품마스터가 나중에 와도 PLT 재환산 가능
  region  : {시도: BOX 합}
  pm      : {상품코드: PLT입수}      (상품마스터 업로드분)
  stock   : 평균재고 관련
  inbound : 입고 집계
"""
import math
import pandas as pd

# delivery_pricing 과 동일한 시도 정규화 (권역 단가표 키와 일치시킴)
SIDO_SHORT = {
    '서울특별시': '서울', '서울시': '서울', '서울': '서울',
    '부산광역시': '부산', '부산': '부산',
    '대구광역시': '대구', '대구': '대구',
    '인천광역시': '인천', '인천시': '인천', '인천': '인천',
    '광주광역시': '광주', '광주': '광주',
    '대전광역시': '대전', '대전': '대전',
    '울산광역시': '울산', '울산': '울산',
    '세종특별자치시': '세종', '세종시': '세종', '세종': '세종',
    '경기도': '경기', '경기': '경기',
    '강원특별자치도': '강원', '강원도': '강원', '강원': '강원',
    '충청북도': '충북', '충북': '충북',
    '충청남도': '충남', '충남': '충남',
    '전라북도': '전북', '전북특별자치도': '전북', '전북': '전북',
    '전라남도': '전남', '전남': '전남',
    '경상북도': '경북', '경북': '경북',
    '경상남도': '경남', '경남': '경남',
    '제주특별자치도': '제주', '제주도': '제주', '제주': '제주',
}
ALL_SIDO = ['서울', '경기', '인천', '강원', '충북', '충남', '대전', '세종',
            '전북', '전남', '광주', '경북', '경남', '대구', '울산', '부산', '제주']


def _cell(v):
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return None
    s = str(v).strip()
    return None if (not s or s.lower() == 'nan') else s


def _num_series(df, col):
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index, dtype='object')
    num = pd.to_numeric(df[col], errors='coerce')
    return num


def _date_series(df, col):
    """YYYYMMDD 숫자형과 일반 날짜 문자열 모두 인식."""
    if col not in df.columns:
        return pd.Series([None] * len(df), index=df.index, dtype='object')
    vals = [_cell(v) for v in df[col].tolist()]
    vals = [(v[:-2] if (v and v.endswith('.0')) else v) for v in vals]
    out = []
    for v in vals:
        if not v:
            out.append(None)
            continue
        try:
            if v.isdigit() and len(v) == 8:
                out.append(pd.to_datetime(v, format='%Y%m%d').date())
            else:
                out.append(pd.to_datetime(v).date())
        except Exception:
            out.append(None)
    return pd.Series(out, index=df.index, dtype='object')


def sido_of(addr):
    if not addr:
        return None
    parts = str(addr).strip().split()
    if not parts:
        return None
    return SIDO_SHORT.get(parts[0])


# ─── 출고내역 ────────────────────────────────────────────────────────────────

SHIP_COL_MAP = {
    '출고일자': 'date', '납품일자': 'date', '출고일': 'date', '일자': 'date',
    '주문번호': 'order_no',
    '배송처코드': 'store', '점포코드': 'store', '거래처코드': 'store',
    '배송처명': 'store_name', '점포명': 'store_name', '거래처명': 'store_name',
    '주소': 'address', '배송주소': 'address',
    '상품코드': 'product', '품목코드': 'product',
    '박스수': 'box', '출고수량(BOX)': 'box', '출고수량': 'box', '수량(BOX)': 'box', '수량': 'box',
    '출고수량(PLT)': 'plt',
}


def parse_shipments(df):
    """출고내역(표준 양식 또는 수주일보) → 집계 dict. (§2.1)"""
    df.columns = [str(c).strip() for c in df.columns]

    # 수주일보(WMS 영문) 자동 인식
    if 'DELIVERY_DATE' in df.columns and 'STORE_CODE' in df.columns:
        ren = {'DELIVERY_DATE': '출고일자', 'STORE_CODE': '배송처코드',
               'STORE_NAME': '배송처명', 'ITEM_CODE': '상품코드',
               'SLIP_NO': '주문번호', 'ADDRESS': '주소'}
        if 'DELIVERY_BOX' in df.columns:
            ren['DELIVERY_BOX'] = '박스수'
        elif 'ORDER_BOX' in df.columns:
            ren['ORDER_BOX'] = '박스수'
        df = df.rename(columns=ren)
        # 수주일보엔 PLT입수가 있어 상품별 입수도 함께 흡수
        if 'PALLET_ENTRY_QUANTITY' in df.columns and '상품코드' in df.columns:
            _pe = pd.to_numeric(df['PALLET_ENTRY_QUANTITY'], errors='coerce')
            pm_extra = {}
            for code, pe in zip(df['상품코드'].tolist(), _pe.tolist()):
                c = _cell(code)
                if c and pe and pe > 0:
                    pm_extra[c] = float(pe)
        else:
            pm_extra = {}
    else:
        pm_extra = {}

    df = df.rename(columns={k: v for k, v in SHIP_COL_MAP.items() if k in df.columns})
    if 'box' not in df.columns:
        raise ValueError(f"박스수 컬럼을 찾을 수 없습니다. 파일 컬럼: {list(df.columns)[:10]}")
    if 'date' not in df.columns:
        raise ValueError('출고일자 컬럼을 찾을 수 없습니다.')

    dates  = _date_series(df, 'date')
    boxes  = _num_series(df, 'box').fillna(0)
    plts   = _num_series(df, 'plt')
    orders = [_cell(v) for v in df['order_no'].tolist()] if 'order_no' in df.columns else [None] * len(df)
    stores = [_cell(v) for v in df['store'].tolist()] if 'store' in df.columns else [None] * len(df)
    snames = [_cell(v) for v in df['store_name'].tolist()] if 'store_name' in df.columns else [None] * len(df)
    prods  = [_cell(v) for v in df['product'].tolist()] if 'product' in df.columns else [None] * len(df)
    addrs  = [_cell(v) for v in df['address'].tolist()] if 'address' in df.columns else [None] * len(df)

    daily = {}       # date_str → {'box','plt','orders':set,'lines','stores':set}
    product = {}     # code → box sum
    region = {}      # sido → box sum
    storeday = {}    # (date,store) → box sum — 직송/공동배송 분리 판정용(A28)
    region_unknown = 0.0
    has_order_col = 'order_no' in df.columns
    total_box = 0.0
    total_plt_direct = 0.0
    plt_direct_box = 0.0     # PLT 직접값이 있던 행의 박스 합 (커버리지 판단)
    skipped = 0

    for i in range(len(df)):
        d = dates.iloc[i]
        b = float(boxes.iloc[i] or 0)
        if d is None or b <= 0:
            skipped += 1
            continue
        ds = str(d)
        rec = daily.setdefault(ds, {'box': 0.0, 'plt': 0.0, 'orders': set(), 'lines': 0, 'stores': set()})
        rec['box'] += b
        rec['lines'] += 1
        okey = orders[i] or (stores[i] or snames[i] or '?')   # 주문번호 없으면 (일자+배송처)=1주문 (§2.1)
        rec['orders'].add(okey)
        if stores[i] or snames[i]:
            rec['stores'].add(stores[i] or snames[i])
        total_box += b

        p = plts.iloc[i]
        if p is not None and not (isinstance(p, float) and math.isnan(p)) and p > 0:
            rec['plt'] += float(p)
            total_plt_direct += float(p)
            plt_direct_box += b

        if prods[i]:
            product[prods[i]] = product.get(prods[i], 0.0) + b

        sk = (ds, stores[i] or snames[i] or '?')
        storeday[sk] = storeday.get(sk, 0.0) + b

        sd = sido_of(addrs[i])
        if sd:
            region[sd] = region.get(sd, 0.0) + b
        else:
            region_unknown += b

    if not daily:
        raise ValueError('유효한 행이 없습니다 (출고일자·박스수 확인).')

    # (일자,점포) 물량을 정수 BOX 히스토그램으로 압축 — 직송 기준 PLT가 바뀌어도
    # 재계산할 수 있도록 {박스수: [건수, 박스합]} 형태로 보관 (§3, A28)
    sd_hist = {}
    for _, bx in storeday.items():
        key = str(int(round(bx)))
        h = sd_hist.setdefault(key, [0, 0.0])
        h[0] += 1
        h[1] += bx
    sd_hist = {k: [c, round(t, 1)] for k, (c, t) in sd_hist.items()}

    daily_out = {ds: {'box': round(v['box'], 1), 'plt': round(v['plt'], 2),
                      'orders': len(v['orders']), 'lines': v['lines'],
                      'stores': len(v['stores'])}
                 for ds, v in daily.items()}

    return {
        'daily': daily_out,
        'product': {k: round(v, 1) for k, v in product.items()},
        'region': {k: round(v, 1) for k, v in region.items()},
        'region_unknown_box': round(region_unknown, 1),
        'total_box': round(total_box, 1),
        'total_plt_direct': round(total_plt_direct, 2),
        'plt_direct_box': round(plt_direct_box, 1),
        'storeday_hist': sd_hist,
        'rows': int(len(df)), 'skipped': int(skipped),
        'has_order_col': bool(has_order_col),
        'pm_extra': pm_extra,           # 수주일보에서 얻은 PLT입수
    }


# ─── 상품마스터 ──────────────────────────────────────────────────────────────

def parse_products(df):
    """상품마스터 → {상품코드: PLT입수}. (§2.2)"""
    df.columns = [str(c).strip() for c in df.columns]
    ren = {'상품코드': 'code', '품목코드': 'code', 'ITEM_CODE': 'code',
           'PLT입수': 'ppb', 'PLT입수(BOX/PLT)': 'ppb', 'PLT당박스': 'ppb',
           'PALLET_ENTRY_QUANTITY': 'ppb', '팔레트입수': 'ppb'}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
    if 'code' not in df.columns or 'ppb' not in df.columns:
        raise ValueError(f"상품코드 / PLT입수 컬럼을 찾을 수 없습니다. 파일 컬럼: {list(df.columns)[:10]}")
    out = {}
    ppbs = pd.to_numeric(df['ppb'], errors='coerce')
    for code, ppb in zip(df['code'].tolist(), ppbs.tolist()):
        c = _cell(code)
        if c and ppb and ppb > 0:
            out[c] = float(ppb)
    if not out:
        raise ValueError('유효한 상품 행이 없습니다.')
    return out


# ─── 재고 ────────────────────────────────────────────────────────────────────

def parse_stock(df, pm, default_ppb):
    """재고 스냅샷 → 일별 재고 PLT 평균. (§2.3)
    재고PLT 직접값 우선, 없으면 재고박스 ÷ PLT입수."""
    df.columns = [str(c).strip() for c in df.columns]
    ren = {'기준일자': 'date', '일자': 'date', '재고일자': 'date',
           '상품코드': 'code', '품목코드': 'code',
           '재고박스': 'box', '재고수량(BOX)': 'box', '재고수량': 'box',
           '재고PLT': 'plt', '재고수량(PLT)': 'plt'}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
    if 'date' not in df.columns:
        raise ValueError('기준일자 컬럼을 찾을 수 없습니다.')
    dates = _date_series(df, 'date')
    boxes = _num_series(df, 'box')
    plts  = _num_series(df, 'plt')
    codes = [_cell(v) for v in df['code'].tolist()] if 'code' in df.columns else [None] * len(df)

    per_day = {}
    box_days = 0.0          # 재고 박스·일 합 (박스·일당 청구 단위용, A17)
    unmatched_box = 0.0
    for i in range(len(df)):
        d = dates.iloc[i]
        if d is None:
            continue
        ds = str(d)
        b = boxes.iloc[i]
        bv = float(b) if (b is not None and not (isinstance(b, float) and math.isnan(b)) and b > 0) else 0.0
        box_days += bv
        p = plts.iloc[i]
        if p is not None and not (isinstance(p, float) and math.isnan(p)) and p > 0:
            per_day[ds] = per_day.get(ds, 0.0) + float(p)
            continue
        if bv <= 0:
            continue
        ppb = pm.get(codes[i]) if codes[i] else None
        if not ppb:
            ppb = default_ppb
            unmatched_box += bv
        per_day[ds] = per_day.get(ds, 0.0) + bv / ppb

    if not per_day:
        raise ValueError('유효한 재고 행이 없습니다.')
    avg_plt = sum(per_day.values()) / len(per_day)
    return {'days': len(per_day), 'avg_stock_plt': round(avg_plt, 1),
            'box_days': round(box_days, 1),
            'daily': {k: round(v, 1) for k, v in sorted(per_day.items())},
            'unmatched_box': round(unmatched_box, 1)}


# ─── 입고내역 ────────────────────────────────────────────────────────────────

def parse_inbound(df, pm, default_ppb):
    """입고내역 → 월평균 입고 PLT/BOX. (§2.4)"""
    df.columns = [str(c).strip() for c in df.columns]
    ren = {'입고일자': 'date', '일자': 'date',
           '상품코드': 'code', '품목코드': 'code',
           '박스수': 'box', '입고박스': 'box', '입고수량(BOX)': 'box', '입고수량': 'box',
           'PLT수': 'plt', '입고PLT': 'plt', '입고수량(PLT)': 'plt'}
    df = df.rename(columns={k: v for k, v in ren.items() if k in df.columns})
    if 'date' not in df.columns:
        raise ValueError('입고일자 컬럼을 찾을 수 없습니다.')
    dates = _date_series(df, 'date')
    boxes = _num_series(df, 'box')
    plts  = _num_series(df, 'plt')
    codes = [_cell(v) for v in df['code'].tolist()] if 'code' in df.columns else [None] * len(df)

    days = set()
    tot_box = 0.0
    tot_plt = 0.0
    for i in range(len(df)):
        d = dates.iloc[i]
        if d is None:
            continue
        days.add(str(d))
        p = plts.iloc[i]
        b = boxes.iloc[i]
        bv = float(b) if (b is not None and not (isinstance(b, float) and math.isnan(b)) and b > 0) else 0.0
        tot_box += bv
        if p is not None and not (isinstance(p, float) and math.isnan(p)) and p > 0:
            tot_plt += float(p)
        elif bv > 0:
            ppb = (pm.get(codes[i]) if codes[i] else None) or default_ppb
            tot_plt += bv / ppb
    if not days:
        raise ValueError('유효한 입고 행이 없습니다.')
    return {'days': len(days), 'total_box': round(tot_box, 1), 'total_plt': round(tot_plt, 1)}


# ─── 프로파일 요약 (§3 표) ───────────────────────────────────────────────────

def percentile(sorted_vals, p):
    if not sorted_vals:
        return 0.0
    k = (len(sorted_vals) - 1) * p
    f = math.floor(k)
    c = math.ceil(k)
    if f == c:
        return sorted_vals[int(k)]
    return sorted_vals[f] * (c - k) + sorted_vals[c] * (k - f)


def summarize(profile, params):
    """프로파일 JSON + 파라미터 → 견적 계산에 쓰는 요약값(§3). engine.compute 의 입력.

    반환값의 모든 키는 quote.overrides_json 의 'p:<key>' 로 수동 덮어쓰기 가능.
    """
    ship = profile.get('ship')
    if not ship:
        return None
    daily = ship['daily']
    dates = sorted(daily.keys())
    n_days = len(dates)
    span_months = max(n_days / params['biz_days_per_month'], 0.25)   # A10

    day_boxes = sorted(v['box'] for v in daily.values())
    total_box = ship['total_box']
    avg_box = total_box / n_days
    p95_box = percentile(day_boxes, 0.95)                            # A11

    # PLT 환산: ① 행 PLT 직접값 ② 상품마스터 조인 ③ 대표 PLT입수(A7)
    pm = dict(ship.get('pm_extra') or {})
    pm.update(profile.get('pm') or {})
    matched_box = 0.0
    matched_plt = 0.0
    for code, box in ship['product'].items():
        if code in pm:
            matched_box += box
            matched_plt += box / pm[code]
    coverage = matched_box / total_box if total_box else 0.0
    if matched_plt > 0:
        rep_ppb = matched_box / matched_plt          # 매칭분 가중평균 (A7)
    else:
        rep_ppb = params['default_boxes_per_plt']
    est_total_plt = matched_plt + (total_box - matched_box) / rep_ppb
    # 행 단위 PLT 직접값이 전체를 커버하면 그것을 우선
    if ship['plt_direct_box'] >= total_box * 0.999 and ship['total_plt_direct'] > 0:
        est_total_plt = ship['total_plt_direct']
        coverage = 1.0
        rep_ppb = total_box / est_total_plt

    avg_plt = est_total_plt / n_days
    boxes_per_plt = total_box / est_total_plt if est_total_plt > 0 else rep_ppb

    orders = sum(v['orders'] for v in daily.values())
    lines = sum(v['lines'] for v in daily.values())

    stock = profile.get('stock')
    if stock:
        avg_stock_plt = stock['avg_stock_plt']
        stock_source = f"재고 스냅샷 {stock['days']}일 평균"
        # 일평균 재고 박스 (박스·일 청구용). 구버전 프로파일엔 box_days가 없어 입수로 추정
        if stock.get('box_days'):
            avg_stock_box = stock['box_days'] / stock['days']
            stock_box_src = '재고 파일 박스 집계'
        else:
            avg_stock_box = None
            stock_box_src = None
    else:
        avg_stock_plt = avg_plt * params['stock_turn_days']          # A8
        stock_source = f"추정: 일평균 출고 {avg_plt:.1f}PLT × 회전일수 {params['stock_turn_days']:.0f}일 (A8)"
        avg_stock_box = None
        stock_box_src = None

    inbound = profile.get('inbound')
    if inbound:
        in_plt_per_day = inbound['total_plt'] / inbound['days']
        in_box_per_day = inbound['total_box'] / inbound['days'] if inbound['total_box'] else avg_box
        inbound_source = f"입고내역 {inbound['days']}일 실적"
    else:
        in_plt_per_day = avg_plt                                     # A9
        in_box_per_day = avg_box
        inbound_source = '추정: 입고량 = 출고량 (A9)'

    region = ship.get('region') or {}
    region_box = sum(region.values())

    # 직송/택배/공동배송 분리 (A28·A29): 점포·일 물량 기준 3분류
    #   PLT ≥ 직송기준 → 직송 / BOX ≤ 택배기준 → 택배 / 나머지 → 이고+공동배송
    sd_hist = ship.get('storeday_hist') or {}
    direct_share_pct = None
    parcel_share_pct = 0.0
    if sd_hist:
        thr_box = params.get('direct_plt_threshold', 3.0) * (total_box / est_total_plt
                                                             if est_total_plt > 0 else rep_ppb)
        direct_box = sum(t for k, (c, t) in sd_hist.items() if float(k) >= thr_box)
        direct_share_pct = round(direct_box / total_box * 100, 1) if total_box else 0.0
        pb_thr = params.get('parcel_box_threshold', 0) or 0
        if pb_thr > 0 and total_box:
            parcel_box = sum(t for k, (c, t) in sd_hist.items()
                             if float(k) <= pb_thr and float(k) < thr_box)
            parcel_share_pct = round(parcel_box / total_box * 100, 1)

    return {
        'period_from': dates[0], 'period_to': dates[-1],
        'biz_days': n_days, 'span_months': round(span_months, 2),
        'monthly_box': round(total_box / span_months),
        'monthly_plt': round(est_total_plt / span_months, 1),
        'avg_day_box': round(avg_box, 1), 'avg_day_plt': round(avg_plt, 2),
        'p95_day_box': round(p95_box, 1),
        'peak_ratio': round(p95_box / avg_box, 2) if avg_box else 1.0,
        'orders_per_day': round(orders / n_days, 1),
        'box_per_order': round(total_box / orders, 1) if orders else 0,
        'lines_per_order': round(lines / orders, 2) if orders else 0,
        'boxes_per_plt': round(boxes_per_plt, 1),
        'ppb_coverage': round(coverage * 100, 1),
        'avg_stock_plt': round(avg_stock_plt, 1),
        'avg_stock_box': round(avg_stock_box, 1) if avg_stock_box is not None else round(avg_stock_plt * boxes_per_plt, 1),
        'stock_box_src': stock_box_src or f'추정: 재고 {avg_stock_plt:.0f}PLT × 입수 {boxes_per_plt:.1f} (재고 파일 재업로드 시 실측)',
        'stock_source': stock_source,
        'in_plt_per_day': round(in_plt_per_day, 2),
        'in_box_per_day': round(in_box_per_day, 1),
        'inbound_source': inbound_source,
        'direct_share_pct': direct_share_pct if direct_share_pct is not None else 0.0,
        'parcel_share_pct': parcel_share_pct,
        'has_storeday': bool(sd_hist),
        'region_share': {k: round(v / region_box * 100, 1) for k, v in
                         sorted(region.items(), key=lambda kv: -kv[1])} if region_box else {},
        'region_box_pct': round(region_box / total_box * 100, 1) if total_box else 0,
        'has_order_col': ship.get('has_order_col', False),
        'warnings': _warnings(n_days, span_months, coverage, ship, region_box, total_box),
    }


def _warnings(n_days, span_months, coverage, ship, region_box, total_box):
    """§7 필요조건 위반 경고."""
    w = []
    if span_months < 1.0:
        w.append(f'분석기간이 {n_days}영업일({span_months:.1f}개월)로 짧습니다 — 최소 1개월 (§7)')
    if coverage < 0.8:
        w.append(f'PLT입수 커버리지 {coverage*100:.0f}% — 80% 미만이라 보관·상하차비 오차 위험 (한계 §6.3)')
    if not ship.get('has_order_col'):
        w.append('주문번호 컬럼 없음 — (일자+배송처)를 1주문으로 간주해 주문 수가 과소집계될 수 있음')
    if total_box and region_box / total_box < 0.5:
        w.append(f'주소 있는 물량이 {region_box/total_box*100:.0f}%뿐 — 배송비 모드②(권역 단가표) 신뢰도 낮음')
    return w
