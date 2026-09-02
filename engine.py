"""표준물류단가 산정 엔진 (docs/DESIGN.md §4 가정, §5 수식).

compute(summary, params, processes, region_rates, delivery) → 결과 dict
모든 단계의 중간값을 결과에 남겨 화면·견적서에서 "왜 이 단가인가"를 추적할 수 있게 한다.
"""
import math

# ─── 파라미터 기본값 (가정 A1~A25 — 전부 개략값, 마스터에서 수정) ─────────────

PARAM_DEFS = [
    # key, value, label, unit, group, assumption, description
    ('daily_wage',        13000, '일용직 지급 시급', '원/시', '인건비', 'A1', '상하차·피킹·검수 일용 인력'),
    ('wage_burden',        1.15, '인건비 부담률', '배', '인건비', 'A1', '주휴·4대보험 등 간접부담. 도급 계약이면 1.0'),
    ('forklift_wage',     17000, '지게차 기사 지급 시급', '원/시', '인건비', 'A2', ''),
    ('mgr_month_cost',  5000000, '소장 월 실부담', '원/월', '인건비', 'A3', '4대보험·퇴직충당 포함'),
    ('lead_month_cost', 3600000, '반장 월 실부담', '원/월', '인건비', 'A3', ''),
    ('office_month_cost', 3200000, '사무 월 실부담', '원/월', '인건비', 'A3', ''),
    ('work_hours_month',    174, '월 작업가용시간', '시간', '인건비', 'A4', '8h × 21.7일'),
    ('biz_days_per_month', 21.7, '월 영업일수', '일', '물동', 'A10', '주6일 화주는 26으로'),
    ('default_boxes_per_plt', 60, '대표 PLT입수(미상 상품)', 'BOX/PLT', '물동', 'A7', '상품마스터 매칭 0%일 때만 사용'),
    ('stock_turn_days',      15, '재고회전일수(재고 미업로드 시)', '일', '물동', 'A8', ''),
    ('rent_per_py',       40000, '창고 임차료', '원/평·월', '보관', 'A12', '수도권 상온 개략'),
    ('mgmt_per_py',        7000, '창고 관리비·수도광열', '원/평·월', '보관', 'A13', ''),
    ('plt_per_py',          3.5, '평당 유효 적재', 'PLT/평', '보관', 'A14', '랙 4단 기준. 평치는 1.5'),
    ('occupancy',          0.85, '목표 가동률', '', '보관', 'A15', '빈 자리도 원가다 — 만실 기준 금지'),
    ('forklift_month',  1200000, '지게차 월비용(대당)', '원/월', '보관', 'A16', '리스+유지+충전'),
    ('plt_per_forklift',   3000, '지게차 1대당 담당 PLT', 'PLT', '보관', 'A16', '월 처리 PLT 기준'),
    ('pallet_month',        900, '파렛트 렌탈', '원/PLT·월', '보관', 'A25', '화주 사급이면 0'),
    ('wrap_per_plt',        500, '랩핑 소모품', '원/PLT', '간접', 'A22', '화주 사급이면 0'),
    ('label_per_box',        30, '출고 라벨·테이프', '원/BOX', '간접', 'A22', ''),
    ('overhead_rate',      0.18, '간접배부율', '', '간접', 'A21', '관리인력·전산 배부. 고정인력 별도 계상 시 낮출 것'),
    ('admin_rate',         0.07, '일반관리비율', '', '간접', 'A23', '본사 배부'),
    ('margin_rate',        0.10, '목표이익률', '', '간접', 'A24', '단가 = 원가 ÷ (1−이익률)'),
]

PROCESS_DEFS = [
    # name, flow, unit, productivity, worker_type, memo (가정 A6)
    ('하차(파렛트)',   '입고', 'PLT', 25,  '지게차', '카운트 포함 — A6a'),
    ('적치(랙 입고)',  '입고', 'PLT', 20,  '지게차', 'A6c'),
    ('피킹(케이스)',   '출고', 'BOX', 90,  '일용',   '오더 밀도 보통 — A6d'),
    ('검수·포장',      '출고', 'BOX', 120, '일용',   '바코드 검수+간이포장 — A6e'),
    ('상차(파렛트)',   '출고', 'PLT', 25,  '지게차', 'A6f'),
]

REGION_DEFS = [
    # 시도, 박스당 단가 (A19 — 개략 seed, 실계약 단가로 교체)
    ('서울', 420), ('경기', 400), ('인천', 420), ('강원', 750),
    ('충북', 600), ('충남', 600), ('대전', 580), ('세종', 580),
    ('전북', 700), ('전남', 800), ('광주', 700),
    ('경북', 750), ('경남', 750), ('대구', 700), ('울산', 750), ('부산', 700),
    ('제주', 1200),
]


def hourly_cost(params, worker_type):
    """실부담 시급 (A1·A2)."""
    if worker_type == '지게차':
        return params['forklift_wage'] * params['wage_burden']
    return params['daily_wage'] * params['wage_burden']


def compute(s, params, processes, region_rates, delivery):
    """s: profile_extract.summarize() 출력(수동 보정 반영 후), params: dict,
    processes: [{name,flow,unit,productivity,worker_type}], region_rates: {시도: 원/BOX},
    delivery: {'mode': 'manual'|'region'|'link', 'manual_min','manual_max', 'link_min','link_max'}
    """
    bpp = s['boxes_per_plt'] or params['default_boxes_per_plt']
    monthly_box = s['monthly_box']
    monthly_plt = s['monthly_plt']
    if not monthly_box:
        raise ValueError('월평균 출고 BOX가 0입니다.')

    # ── 작업비 (§5.1) ───────────────────────────────────────────────────────
    proc_rows = []
    direct_cpb = 0.0            # 박스당 직접작업비
    manhours_avg = {'입고': 0.0, '출고': 0.0}
    manhours_p95 = {'입고': 0.0, '출고': 0.0}
    for p in processes:
        if not p['productivity'] or p['productivity'] <= 0:
            continue
        wage = hourly_cost(params, p['worker_type'])
        unit_cost = wage / p['productivity']                       # 원/단위
        cpb = unit_cost / bpp if p['unit'] == 'PLT' else unit_cost # 원/BOX 환산

        # 물동 기준: 입고 공정은 입고량(A9), 출고 공정은 출고량
        if p['flow'] == '입고':
            day_units = s['in_plt_per_day'] if p['unit'] == 'PLT' else s['in_box_per_day']
            p95_units = day_units * (s['peak_ratio'] or 1.0)
        else:
            day_units = s['avg_day_plt'] if p['unit'] == 'PLT' else s['avg_day_box']
            p95_units = (s['p95_day_box'] / bpp) if p['unit'] == 'PLT' else s['p95_day_box']
        mh = day_units / p['productivity']
        manhours_avg[p['flow']] += mh
        manhours_p95[p['flow']] += p95_units / p['productivity']

        proc_rows.append({
            'name': p['name'], 'flow': p['flow'], 'unit': p['unit'],
            'worker_type': p['worker_type'], 'productivity': p['productivity'],
            'wage': round(wage), 'unit_cost': round(unit_cost, 1),
            'cost_per_box': round(cpb, 2), 'day_manhours': round(mh, 1),
        })
        direct_cpb += cpb

    supplies_cpb = params['wrap_per_plt'] / bpp + params['label_per_box']       # A22
    work_cpb = direct_cpb * (1 + params['overhead_rate']) + supplies_cpb        # §5.1

    # 인력 소요 (참고 표시): 평시/피크 (A5·A11)
    mh_total_avg = manhours_avg['입고'] + manhours_avg['출고']
    mh_total_p95 = manhours_p95['입고'] + manhours_p95['출고']
    heads_avg = mh_total_avg / 8.0
    heads_p95 = mh_total_p95 / 8.0
    manpower = {
        'day_manhours_avg': round(mh_total_avg, 1),
        'heads_avg': round(heads_avg, 1),
        'heads_p95': round(heads_p95, 1),
        'heads_temp': round(max(heads_p95 - heads_avg, 0), 1),   # 피크 초과분 = 일용 (A5)
        'note': ('피크배율 ≤ 1.3 → 전원 고정 배치 가능 (A5)'
                 if (s['peak_ratio'] or 1) <= 1.3 else
                 f"평시 {heads_avg:.1f}명 고정 + 피크일 {max(heads_p95-heads_avg,0):.1f}명 일용 (A5·A11)"),
    }

    # ── 보관비 (§5.2) ───────────────────────────────────────────────────────
    avg_stock = s['avg_stock_plt']
    eff_plt_per_py = params['plt_per_py'] * params['occupancy']
    need_py = avg_stock / eff_plt_per_py if eff_plt_per_py else 0
    rate_space = (params['rent_per_py'] + params['mgmt_per_py']) / eff_plt_per_py if eff_plt_per_py else 0
    forklift_alloc = (params['forklift_month'] / params['plt_per_forklift']) if params['plt_per_forklift'] else 0
    plt_month_rate = rate_space + params['pallet_month'] + forklift_alloc
    monthly_storage = plt_month_rate * avg_stock
    storage_cpb = monthly_storage / monthly_box
    storage = {
        'avg_stock_plt': avg_stock, 'need_py': round(need_py, 1),
        'eff_plt_per_py': round(eff_plt_per_py, 2),
        'space_rate': round(rate_space), 'forklift_alloc': round(forklift_alloc),
        'pallet_month': params['pallet_month'],
        'plt_month_rate': round(plt_month_rate),
        'plt_day_rate': round(plt_month_rate / 30.4, 1),
        'monthly_cost': round(monthly_storage),
        'cost_per_box': round(storage_cpb, 2),
        'stock_source': s['stock_source'],
    }

    # ── 배송비 (§5.3) ───────────────────────────────────────────────────────
    mode = delivery.get('mode') or 'manual'
    dmin = dmax = 0.0
    dnote = ''
    if mode == 'region':
        share = s.get('region_share') or {}
        if not share:
            mode = 'manual'
            dnote = '주소 데이터가 없어 권역 모드를 쓸 수 없음 → 직접입력으로 전환'
        else:
            wsum = 0.0
            missing = []
            for sido, pct in share.items():
                r = region_rates.get(sido)
                if r is None:
                    missing.append(sido)
                    continue
                wsum += pct / 100.0 * r
            covered = 100.0 - sum(share[m] for m in missing)
            if covered <= 0:
                raise ValueError('권역 단가표에 일치하는 시도가 없습니다.')
            dmin = dmax = wsum / (covered / 100.0)      # 미상·미등록 권역은 분포 안분 (A20)
            dnote = f"권역 분포 가중평균 (주소 커버 {s['region_box_pct']:.0f}%"
            dnote += f", 단가 미등록: {', '.join(missing)})" if missing else ')'
    if mode == 'manual':
        dmin = float(delivery.get('manual_min') or 0)
        dmax = float(delivery.get('manual_max') or dmin)
        dnote = dnote or '박스당 단가 직접입력'
    elif mode == 'link':
        dmin = float(delivery.get('link_min') or 0)
        dmax = float(delivery.get('link_max') or dmin)
        dnote = '배송비단가시스템(delivery_pricing) 연동값'
    deliv = {'mode': mode, 'min': round(dmin, 1), 'max': round(dmax, 1), 'note': dnote}

    # ── 최종 단가 (§5.4) ────────────────────────────────────────────────────
    markup = (1 + params['admin_rate']) / (1 - params['margin_rate'])
    def _final(cpb):
        return cpb * markup

    cost_min = work_cpb + storage_cpb + dmin
    cost_max = work_cpb + storage_cpb + dmax
    final = {
        'markup': round(markup, 4),
        'cost_cpb_min': round(cost_min, 1), 'cost_cpb_max': round(cost_max, 1),
        'price_cpb_min': round(_final(cost_min), 1),
        'price_cpb_max': round(_final(cost_max), 1),
        'monthly_revenue_min': round(_final(cost_min) * monthly_box),
        'monthly_revenue_max': round(_final(cost_max) * monthly_box),
    }

    # 항목별 견적 단가표 (청구단위 분해 — §5.4)
    in_procs  = [r for r in proc_rows if r['flow'] == '입고']
    out_procs = [r for r in proc_rows if r['flow'] == '출고']
    inbound_cost_plt = sum(r['unit_cost'] if r['unit'] == 'PLT' else r['unit_cost'] * bpp
                           for r in in_procs) * (1 + params['overhead_rate'])
    outbound_cost_box = (sum(r['cost_per_box'] for r in out_procs)
                         * (1 + params['overhead_rate']) + supplies_cpb)
    tariff = [
        {'item': '입고비',  'unit': '원/PLT',   'cost': round(inbound_cost_plt),
         'price': round(_final(inbound_cost_plt))},
        {'item': '출고비',  'unit': '원/BOX',   'cost': round(outbound_cost_box, 1),
         'price': round(_final(outbound_cost_box), 1)},
        {'item': '보관비',  'unit': '원/PLT·월', 'cost': storage['plt_month_rate'],
         'price': round(_final(storage['plt_month_rate']))},
        {'item': '배송비',  'unit': '원/BOX',
         'cost': (f"{deliv['min']:,.0f}~{deliv['max']:,.0f}" if deliv['max'] > deliv['min'] else round(deliv['min'], 1)),
         'price': (f"{_final(deliv['min']):,.0f}~{_final(deliv['max']):,.0f}"
                   if deliv['max'] > deliv['min'] else round(_final(deliv['min']), 1))},
    ]

    # ── 민감도 (§5.5) ───────────────────────────────────────────────────────
    sens = []
    for label, f in [
        ('물동 −20%', {'volume': 0.8}), ('물동 +20%', {'volume': 1.2}),
        ('생산성 −10%', {'prod': 0.9}), ('재고 +30%', {'stock': 1.3}),
        ('가동률 70%', {'occ': 0.70}),
    ]:
        v  = f.get('volume', 1.0)
        pr = f.get('prod', 1.0)
        st = f.get('stock', 1.0)
        oc = f.get('occ')
        w2 = direct_cpb / pr * (1 + params['overhead_rate']) + supplies_cpb
        eff2 = params['plt_per_py'] * (oc if oc is not None else params['occupancy'])
        rate2 = ((params['rent_per_py'] + params['mgmt_per_py']) / eff2
                 + params['pallet_month'] + forklift_alloc) if eff2 else 0
        # 물동이 변해도 재고는 그대로면 박스당 보관 배부가 변한다 (§6.6 순환성)
        st2 = rate2 * (avg_stock * st) / (monthly_box * v)
        c2 = w2 + st2 + (dmin + dmax) / 2
        sens.append({'label': label,
                     'cost_cpb': round(c2, 1),
                     'price_cpb': round(_final(c2), 1),
                     'delta_pct': round((c2 / ((cost_min + cost_max) / 2) - 1) * 100, 1)})

    return {
        'work': {'processes': proc_rows, 'direct_cpb': round(direct_cpb, 2),
                 'overhead_rate': params['overhead_rate'],
                 'supplies_cpb': round(supplies_cpb, 2),
                 'cost_per_box': round(work_cpb, 2)},
        'manpower': manpower,
        'storage': storage,
        'delivery': deliv,
        'final': final,
        'tariff': tariff,
        'sensitivity': sens,
        'inputs': {'monthly_box': monthly_box, 'monthly_plt': monthly_plt,
                   'boxes_per_plt': bpp, 'markup': round(markup, 4)},
    }
