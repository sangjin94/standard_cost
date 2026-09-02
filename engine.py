"""표준물류단가 산정 엔진 (docs/DESIGN.md §4 가정, §5 수식).

compute(summary, params, processes, region_rates, delivery, stages) → 결과 dict

견적 산출물은 종합단가가 아니라 물류 흐름 순서의 4개 항목 단가다 (§5.4):
  입고 작업비(원/PLT) → 보관비(원/PLT·월) → 출고 작업비(원/BOX) → 배송비[이고+배송](원/BOX)
각 항목(스테이지)에는 숫자가 대입된 계산 추적(trace)이 붙어 "왜 이 단가인가"를
누구나 화면에서 확인할 수 있다.
"""
import math

# ─── 파라미터 기본값 (가정 A1~A28 — 전부 개략값, 마스터에서 수정) ─────────────

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
    ('direct_plt_threshold', 3.0, '직송 기준 PLT (점포·일)', 'PLT', '배송', 'A28', '점포·일 물량이 이 PLT 이상이면 직송, 미만이면 이고+공동배송'),
    ('parcel_box_threshold',   3, '택배 기준 BOX (점포·일)', 'BOX', '배송', 'A29', '택배 사용 화주만: 점포·일 물량이 이 박스수 이하면 택배'),
    ('overhead_rate',      0.18, '간접배부율', '', '간접', 'A21', '관리인력·전산 배부. 고정인력 별도 계상 시 낮출 것'),
    ('admin_rate',         0.07, '일반관리비율', '', '간접', 'A23', '본사 배부'),
    ('margin_rate',        0.10, '목표이익률', '', '간접', 'A24', '단가 = 원가 ÷ (1−이익률)'),
]

PROCESS_DEFS = [
    # name, flow, unit, productivity, worker_type, memo (가정 A6)
    ('하차(파렛트)',   '입고', 'PLT', 25,  '지게차', '카운트 포함 — A6a'),
    ('적치(랙 입고)',  '입고', 'PLT', 20,  '지게차', 'A6c — 보관 미사용(크로스도킹) 화주는 견적에서 제외'),
    ('피킹(케이스)',   '출고', 'BOX', 90,  '일용',   '오더 밀도 보통 — A6d'),
    ('검수·포장',      '출고', 'BOX', 120, '일용',   'A6e'),
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

DEFAULT_STAGES = {'storage': True, 'transfer': False, 'transfer_per_plt': 0.0,
                  'parcel': False, 'parcel_cost': 0.0}


def hourly_cost(params, worker_type):
    """실부담 시급 (A1·A2)."""
    if worker_type == '지게차':
        return params['forklift_wage'] * params['wage_burden']
    return params['daily_wage'] * params['wage_burden']


def _w(v, dec=0):
    """천단위 콤마 숫자 문자열."""
    if dec:
        return f'{v:,.{dec}f}'
    return f'{round(v):,}'


def compute(s, params, processes, region_rates, delivery, stages=None):
    """s: profile_extract.summarize() 출력(수동 보정 반영 후), params: dict,
    processes: 견적별 제외 반영 후 공정 목록, region_rates: {시도: 원/BOX},
    delivery: 배송 설정, stages: {'storage','transfer','transfer_per_plt'} (A26·A27)
    """
    st = dict(DEFAULT_STAGES)
    st.update(stages or {})

    bpp = s['boxes_per_plt'] or params['default_boxes_per_plt']
    monthly_box = s['monthly_box']
    monthly_plt = s['monthly_plt']
    if not monthly_box:
        raise ValueError('월평균 출고 BOX가 0입니다.')

    oh = params['overhead_rate']
    admin = params['admin_rate']
    margin = params['margin_rate']
    markup = (1 + admin) / (1 - margin)
    markup_trace = {'label': '견적 배율 (A23·A24)',
                    'expr': f'(1 + 일반관리비 {admin:.0%}) ÷ (1 − 목표이익률 {margin:.0%})',
                    'val': f'× {markup:.4f}'}

    def _final(cost):
        return cost * markup

    # ── 작업 공정 계산 (§5.1) ────────────────────────────────────────────────
    proc_rows = []
    manhours_avg = {'입고': 0.0, '출고': 0.0}
    manhours_p95 = {'입고': 0.0, '출고': 0.0}
    for p in processes:
        if not p['productivity'] or p['productivity'] <= 0:
            continue
        wage = hourly_cost(params, p['worker_type'])
        unit_cost = wage / p['productivity']                       # 원/단위
        cpb = unit_cost / bpp if p['unit'] == 'PLT' else unit_cost # 원/BOX 환산

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
            'wage': round(wage), 'wage_base': (params['forklift_wage'] if p['worker_type'] == '지게차'
                                              else params['daily_wage']),
            'unit_cost': round(unit_cost, 1),
            'cost_per_box': round(cpb, 2), 'day_manhours': round(mh, 1),
        })

    in_procs  = [r for r in proc_rows if r['flow'] == '입고']
    out_procs = [r for r in proc_rows if r['flow'] == '출고']
    supplies_cpb = params['wrap_per_plt'] / bpp + params['label_per_box']       # A22

    # ── ① 입고 작업비 (원/PLT) ──────────────────────────────────────────────
    in_trace = []
    inbound_plt_direct = 0.0
    for r in in_procs:
        in_trace.append({'label': f"{r['name']} 실부담시급 (A1·A2)",
                         'expr': f"지급 {_w(r['wage_base'])}원 × 부담률 {params['wage_burden']}",
                         'val': f"{_w(r['wage'])}원/시"})
        if r['unit'] == 'PLT':
            per_plt = r['unit_cost']
            in_trace.append({'label': f"{r['name']} 단가",
                             'expr': f"{_w(r['wage'])}원 ÷ 생산성 {r['productivity']}PLT/시",
                             'val': f"{_w(per_plt)}원/PLT"})
        else:
            per_plt = r['unit_cost'] * bpp
            in_trace.append({'label': f"{r['name']} 단가 (BOX→PLT 환산)",
                             'expr': f"{_w(r['wage'])}원 ÷ {r['productivity']}BOX/시 × PLT입수 {bpp}",
                             'val': f"{_w(per_plt)}원/PLT"})
        inbound_plt_direct += per_plt
    inbound_plt_rate = inbound_plt_direct * (1 + oh)
    if in_procs:
        in_trace.append({'label': '직접비 합계', 'expr': ' + '.join(r['name'] for r in in_procs),
                         'val': f"{_w(inbound_plt_direct)}원/PLT"})
        in_trace.append({'label': '간접배부 가산 (A21)',
                         'expr': f"{_w(inbound_plt_direct)} × (1 + {oh:.0%})",
                         'val': f"{_w(inbound_plt_rate)}원/PLT"})
        in_trace.append(markup_trace)
        in_trace.append({'label': '입고 작업비 견적단가',
                         'expr': f"{_w(inbound_plt_rate)} × {markup:.4f}",
                         'val': f"{_w(_final(inbound_plt_rate))}원/PLT"})
    inbound_cpb = inbound_plt_rate / bpp if bpp else 0.0

    # ── ② 보관비 (원/PLT·월) — 스테이지 토글 A26 ────────────────────────────
    avg_stock = s['avg_stock_plt']
    eff_plt_per_py = params['plt_per_py'] * params['occupancy']
    need_py = avg_stock / eff_plt_per_py if eff_plt_per_py else 0
    rate_space = (params['rent_per_py'] + params['mgmt_per_py']) / eff_plt_per_py if eff_plt_per_py else 0
    forklift_alloc = (params['forklift_month'] / params['plt_per_forklift']) if params['plt_per_forklift'] else 0
    plt_month_rate = rate_space + params['pallet_month'] + forklift_alloc
    monthly_storage = plt_month_rate * avg_stock if st['storage'] else 0.0
    storage_cpb = (monthly_storage / monthly_box) if st['storage'] else 0.0
    stor_trace = [
        {'label': '평당 유효 적재 (A14·A15)',
         'expr': f"{params['plt_per_py']}PLT/평 × 가동률 {params['occupancy']:.0%}",
         'val': f"{eff_plt_per_py:.2f}PLT/평"},
        {'label': '공간 단가 (A12·A13)',
         'expr': f"(임차 {_w(params['rent_per_py'])} + 관리 {_w(params['mgmt_per_py'])})원/평 ÷ {eff_plt_per_py:.2f}PLT/평",
         'val': f"{_w(rate_space)}원/PLT·월"},
        {'label': '파렛트 렌탈 (A25)', 'expr': '', 'val': f"{_w(params['pallet_month'])}원/PLT·월"},
        {'label': '지게차 배부 (A16)',
         'expr': f"{_w(params['forklift_month'])}원 ÷ {_w(params['plt_per_forklift'])}PLT",
         'val': f"{_w(forklift_alloc)}원/PLT·월"},
        {'label': '보관비 원가',
         'expr': f"{_w(rate_space)} + {_w(params['pallet_month'])} + {_w(forklift_alloc)}",
         'val': f"{_w(plt_month_rate)}원/PLT·월"},
        markup_trace,
        {'label': '보관비 견적단가',
         'expr': f"{_w(plt_month_rate)} × {markup:.4f}",
         'val': f"{_w(_final(plt_month_rate))}원/PLT·월"},
        {'label': '참고: 월 보관비',
         'expr': f"평균재고 {_w(avg_stock)}PLT ({s['stock_source']}) · 필요 {_w(need_py)}평",
         'val': f"{_w(monthly_storage)}원/월"},
    ]
    storage = {
        'enabled': bool(st['storage']),
        'avg_stock_plt': avg_stock, 'need_py': round(need_py, 1),
        'eff_plt_per_py': round(eff_plt_per_py, 2),
        'space_rate': round(rate_space), 'forklift_alloc': round(forklift_alloc),
        'pallet_month': params['pallet_month'],
        'plt_month_rate': round(plt_month_rate),
        'plt_day_rate': round(plt_month_rate / 30.4, 1),
        'monthly_cost': round(monthly_storage),
        'cost_per_box': round(storage_cpb, 2),
        'stock_source': s['stock_source'] if st['storage'] else '보관 미사용 화주 (크로스도킹/통과형) — A26',
    }

    # ── ③ 출고 작업비 (원/BOX) ──────────────────────────────────────────────
    out_trace = []
    outbound_direct_cpb = 0.0
    for r in out_procs:
        out_trace.append({'label': f"{r['name']} 실부담시급 (A1·A2)",
                          'expr': f"지급 {_w(r['wage_base'])}원 × 부담률 {params['wage_burden']}",
                          'val': f"{_w(r['wage'])}원/시"})
        if r['unit'] == 'BOX':
            out_trace.append({'label': f"{r['name']} 단가",
                              'expr': f"{_w(r['wage'])}원 ÷ 생산성 {r['productivity']}BOX/시",
                              'val': f"{r['cost_per_box']:.1f}원/BOX"})
        else:
            out_trace.append({'label': f"{r['name']} 단가 (PLT→BOX 환산)",
                              'expr': f"{_w(r['wage'])}원 ÷ {r['productivity']}PLT/시 ÷ PLT입수 {bpp}",
                              'val': f"{r['cost_per_box']:.1f}원/BOX"})
        outbound_direct_cpb += r['cost_per_box']
    outbound_cpb = outbound_direct_cpb * (1 + oh) + supplies_cpb
    if out_procs:
        out_trace.append({'label': '직접비 합계', 'expr': ' + '.join(r['name'] for r in out_procs),
                          'val': f"{outbound_direct_cpb:.1f}원/BOX"})
        out_trace.append({'label': '간접배부 가산 (A21)',
                          'expr': f"{outbound_direct_cpb:.1f} × (1 + {oh:.0%})",
                          'val': f"{outbound_direct_cpb * (1 + oh):.1f}원/BOX"})
        out_trace.append({'label': '소모품 (A22)',
                          'expr': f"랩핑 {_w(params['wrap_per_plt'])}원/PLT ÷ {bpp} + 라벨 {_w(params['label_per_box'])}원",
                          'val': f"{supplies_cpb:.1f}원/BOX"})
        out_trace.append(markup_trace)
        out_trace.append({'label': '출고 작업비 견적단가',
                          'expr': f"{outbound_cpb:.1f} × {markup:.4f}",
                          'val': f"{_final(outbound_cpb):.1f}원/BOX"})

    # 인력 소요 (참고 표시): 평시/피크 (A5·A11)
    mh_total_avg = manhours_avg['입고'] + manhours_avg['출고']
    heads_avg = mh_total_avg / 8.0
    heads_p95 = (manhours_p95['입고'] + manhours_p95['출고']) / 8.0
    manpower = {
        'day_manhours_avg': round(mh_total_avg, 1),
        'heads_avg': round(heads_avg, 1),
        'heads_p95': round(heads_p95, 1),
        'heads_temp': round(max(heads_p95 - heads_avg, 0), 1),
        'note': ('피크배율 ≤ 1.3 → 전원 고정 배치 가능 (A5)'
                 if (s['peak_ratio'] or 1) <= 1.3 else
                 f"평시 {heads_avg:.1f}명 고정 + 피크일 {max(heads_p95-heads_avg,0):.1f}명 일용 (A5·A11)"),
    }

    # ── ④ 배송비 [이고비 + 배송비] (원/BOX) ─────────────────────────────────
    mode = delivery.get('mode') or 'manual'
    dmin = dmax = 0.0
    dnote = ''
    dv_trace = []
    direct_pct = min(max(float(s.get('direct_share_pct') or 0), 0.0), 100.0)
    # 택배 (A29): 사용 화주만, 점포·일 소량 물량을 외부 택배사 원가로 처리
    parcel_pct = 0.0
    parcel_cost = float(st.get('parcel_cost') or 0)
    if st.get('parcel'):
        parcel_pct = min(max(float(s.get('parcel_share_pct') or 0), 0.0), 100.0 - direct_pct)
    joint_share = max(1.0 - direct_pct / 100.0 - parcel_pct / 100.0, 0.0)
    d_split = None
    if mode == 'split':
        dr_min = float(delivery.get('direct_min') or 0)
        dr_max = float(delivery.get('direct_max') or dr_min)
        jt_min = float(delivery.get('joint_min') or 0)
        jt_max = float(delivery.get('joint_max') or jt_min)
        pc = parcel_pct / 100.0 * parcel_cost
        dmin = direct_pct / 100.0 * dr_min + joint_share * jt_min + pc
        dmax = direct_pct / 100.0 * dr_max + joint_share * jt_max + pc
        dnote = (f"직송 {direct_pct:.0f}% × {dr_min:,.0f}~{dr_max:,.0f}원 "
                 + (f"+ 택배 {parcel_pct:.0f}% × {parcel_cost:,.0f}원 " if st.get('parcel') else '')
                 + f"+ 공동배송 {joint_share*100:.0f}% × {jt_min:,.0f}~{jt_max:,.0f}원 (A28·A29)")
        d_split = {'direct_pct': round(direct_pct, 1),
                   'direct_min': dr_min, 'direct_max': dr_max,
                   'joint_min': jt_min, 'joint_max': jt_max,
                   'parcel_pct': round(parcel_pct, 1), 'parcel_cost': parcel_cost,
                   'joint_pct': round(joint_share * 100, 1)}
        _split_lbl = f"직송 {direct_pct:.1f}%"
        if st.get('parcel'):
            _split_lbl += f" / 택배 {parcel_pct:.1f}%"
        _split_lbl += f" / 공동배송 {joint_share*100:.1f}%"
        dv_trace.append({'label': '물량 분류 (A28·A29)',
                         'expr': f"점포·일 ≥ {params['direct_plt_threshold']}PLT → 직송"
                                 + (f" · ≤ {params['parcel_box_threshold']:.0f}BOX → 택배" if st.get('parcel') else '')
                                 + " · 나머지 → 이고+공동배송 (출고내역에서 추출)",
                         'val': _split_lbl})
        if st.get('parcel'):
            dv_trace.append({'label': '택배 원가 (A29)',
                             'expr': f"외부 택배사 계약원가 {parcel_cost:,.0f}원/BOX × 택배 {parcel_pct:.0f}%",
                             'val': f"{pc:,.1f}원/BOX"})
        dv_trace.append({'label': '배송비 가중평균',
                         'expr': (f"{direct_pct:.0f}% × 직송 {dr_min:,.0f}~{dr_max:,.0f}원"
                                  + (f" + {parcel_pct:.0f}% × 택배 {parcel_cost:,.0f}원" if st.get('parcel') else '')
                                  + f" + {joint_share*100:.0f}% × 공동 {jt_min:,.0f}~{jt_max:,.0f}원"),
                         'val': f"{dmin:,.1f}~{dmax:,.1f}원/BOX"})
    elif mode == 'region':
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
            dmin = dmax = wsum / (covered / 100.0)      # A20
            dnote = f"권역 분포 가중평균 (주소 커버 {s['region_box_pct']:.0f}%"
            dnote += f", 단가 미등록: {', '.join(missing)})" if missing else ')'
            top = list(share.items())[:4]
            dv_trace.append({'label': '권역 단가표 가중평균 (A19·A20)',
                             'expr': ' + '.join(f"{sido} {pct}%×{region_rates.get(sido, 0):,.0f}원"
                                                for sido, pct in top) + (' + …' if len(share) > 4 else ''),
                             'val': f"{dmin:,.1f}원/BOX"})
    if mode == 'manual':
        dmin = float(delivery.get('manual_min') or 0)
        dmax = float(delivery.get('manual_max') or dmin)
        dnote = dnote or '박스당 단가 직접입력 (전 물량 단일)'
        dv_trace.append({'label': '박스당 배송단가 (직접입력)', 'expr': '전 물량 단일 단가',
                         'val': f"{dmin:,.0f}~{dmax:,.0f}원/BOX"})
    elif mode == 'link':
        dmin = float(delivery.get('link_min') or 0)
        dmax = float(delivery.get('link_max') or dmin)
        dnote = '배송비단가시스템(delivery_pricing) 연동값 (직송+이고+용차 포함)'
        dv_trace.append({'label': '배송비단가시스템 연동', 'expr': '직송+이고+용차 포함 실적 기반 단가',
                         'val': f"{dmin:,.0f}~{dmax:,.0f}원/BOX"})

    # 이고비 (A27): split 모드에서는 공동배송 물량에만 배부
    transfer_per_plt = float(st.get('transfer_per_plt') or 0)
    tr_share = joint_share if mode == 'split' else 1.0
    transfer_cpb = (transfer_per_plt / bpp * tr_share) if st['transfer'] else 0.0
    if st['transfer']:
        dv_trace.append({'label': '이고비 (A27)',
                         'expr': f"{_w(transfer_per_plt)}원/PLT ÷ PLT입수 {bpp}"
                                 + (f" × 공동배송 {tr_share*100:.0f}%" if mode == 'split' else ' (전 물량)'),
                         'val': f"{transfer_cpb:,.1f}원/BOX"})
    transfer = {
        'enabled': bool(st['transfer']),
        'per_plt': round(transfer_per_plt, 1),
        'share_pct': round(tr_share * 100, 1),
        'cost_per_box': round(transfer_cpb, 2),
        'note': ('공동배송 물량에만 적용 (직송은 이고 없음)' if mode == 'split' else '전 물량 기준')
                if st['transfer'] else '이고 없음 (단일 센터 직배)',
    }

    dv_cost_min = dmin + transfer_cpb
    dv_cost_max = dmax + transfer_cpb
    if st['transfer'] or dmin or dmax:
        dv_trace.append({'label': '배송비 원가 [이고+배송]',
                         'expr': f"배송 {dmin:,.1f}~{dmax:,.1f} + 이고 {transfer_cpb:,.1f}",
                         'val': f"{dv_cost_min:,.1f}~{dv_cost_max:,.1f}원/BOX"})
        dv_trace.append(markup_trace)
        dv_trace.append({'label': '배송비 견적단가',
                         'expr': f"{dv_cost_min:,.1f}~{dv_cost_max:,.1f} × {markup:.4f}",
                         'val': f"{_final(dv_cost_min):,.1f}~{_final(dv_cost_max):,.1f}원/BOX"})
    deliv = {'mode': mode, 'min': round(dmin, 1), 'max': round(dmax, 1),
             'note': dnote, 'split': d_split,
             'total_min': round(dv_cost_min, 1), 'total_max': round(dv_cost_max, 1)}

    # ── 월 물동 (청구량) ─────────────────────────────────────────────────────
    in_plt_month = s['in_plt_per_day'] * params['biz_days_per_month']

    # ── 스테이지 (견적 4항목 + 계산 추적) ────────────────────────────────────
    def _rng(a, b, dec=1):
        return f"{a:,.{dec}f}~{b:,.{dec}f}" if abs(b - a) > 1e-9 else f"{a:,.{dec}f}"

    stages_out = [
        {'key': 'inbound', 'name': '입고 작업비', 'icon': 'bi-box-arrow-in-down',
         'enabled': bool(in_procs), 'unit': '원/PLT',
         'cost': round(inbound_plt_rate), 'price': round(_final(inbound_plt_rate)),
         'cpb': round(inbound_cpb, 2),
         'volume_label': f"월 입고 {_w(in_plt_month)}PLT",
         'monthly_min': round(_final(inbound_plt_rate) * in_plt_month),
         'monthly_max': round(_final(inbound_plt_rate) * in_plt_month),
         'items': [{'label': f"{r['name']} ({r['productivity']}{r['unit']}/시)",
                    'value': f"{r['unit_cost']:,.0f}원/{r['unit']}"} for r in in_procs]
                  + [{'label': f'간접배부 {oh:.0%}', 'value': ''}],
         'trace': in_trace, 'off_reason': '입고 공정 없음'},
        {'key': 'storage', 'name': '보관비', 'icon': 'bi-building',
         'enabled': storage['enabled'], 'unit': '원/PLT·월',
         'cost': storage['plt_month_rate'], 'price': round(_final(storage['plt_month_rate'])),
         'cpb': storage['cost_per_box'],
         'volume_label': f"평균재고 {_w(avg_stock)}PLT",
         'monthly_min': round(_final(monthly_storage)),
         'monthly_max': round(_final(monthly_storage)),
         'items': [{'label': f"공간 (유효 {storage['eff_plt_per_py']}PLT/평)", 'value': f"{storage['space_rate']:,}원"},
                   {'label': '파렛트 렌탈', 'value': f"{storage['pallet_month']:,}원"},
                   {'label': '지게차 배부', 'value': f"{storage['forklift_alloc']:,}원"},
                   {'label': f"평균재고 {storage['avg_stock_plt']:,}PLT · {storage['need_py']:,}평", 'value': ''}],
         'trace': stor_trace, 'off_reason': '보관 미사용 (크로스도킹)'},
        {'key': 'outbound', 'name': '출고 작업비', 'icon': 'bi-box-arrow-up',
         'enabled': bool(out_procs), 'unit': '원/BOX',
         'cost': round(outbound_cpb, 1), 'price': round(_final(outbound_cpb), 1),
         'cpb': round(outbound_cpb, 2),
         'volume_label': f"월 출고 {_w(monthly_box)}BOX",
         'monthly_min': round(_final(outbound_cpb) * monthly_box),
         'monthly_max': round(_final(outbound_cpb) * monthly_box),
         'items': [{'label': f"{r['name']} ({r['productivity']}{r['unit']}/시)",
                    'value': f"{r['unit_cost']:,.0f}원/{r['unit']}"} for r in out_procs]
                  + [{'label': f'간접배부 {oh:.0%} + 소모품', 'value': f'{supplies_cpb:,.0f}원/BOX'}],
         'trace': out_trace, 'off_reason': '출고 공정 없음'},
        {'key': 'delivery', 'name': '배송비', 'icon': 'bi-truck',
         'enabled': (dv_cost_max > 0), 'unit': '원/BOX',
         'cost': _rng(dv_cost_min, dv_cost_max, 0),
         'price': _rng(_final(dv_cost_min), _final(dv_cost_max), 0),
         'cpb': round((dv_cost_min + dv_cost_max) / 2, 2),
         'volume_label': f"월 출고 {_w(monthly_box)}BOX",
         'monthly_min': round(_final(dv_cost_min) * monthly_box),
         'monthly_max': round(_final(dv_cost_max) * monthly_box),
         'items': ([{'label': f"직송 {d_split['direct_pct']}% (점포·일 ≥ {params['direct_plt_threshold']}PLT)",
                     'value': f"{d_split['direct_min']:,.0f}~{d_split['direct_max']:,.0f}원"}]
                   + ([{'label': f"택배 {d_split['parcel_pct']}% (외부 택배사 원가)",
                        'value': f"{d_split['parcel_cost']:,.0f}원"}] if st.get('parcel') else [])
                   + [{'label': f"공동배송 {d_split['joint_pct']}%",
                       'value': f"{d_split['joint_min']:,.0f}~{d_split['joint_max']:,.0f}원"}]
                   if d_split else
                   [{'label': {'manual': '직접입력(단일)', 'region': '권역 단가표 가중평균',
                               'link': '배송시스템 연동'}[mode],
                     'value': f"{dmin:,.0f}~{dmax:,.0f}원" if dmax > dmin else f"{dmin:,.0f}원"}])
                  + ([{'label': f"이고비 ({transfer['share_pct']:.0f}% 물량)",
                       'value': f"{transfer_cpb:,.1f}원/BOX"}] if st['transfer'] else []),
         'trace': dv_trace, 'off_reason': '배송단가 미입력'},
    ]

    # ── 견적 단가표 = 4항목 (종합단가 없음, §5.4) ────────────────────────────
    tariff = [{'item': ('배송비 [이고+배송]' if (sg['key'] == 'delivery' and st['transfer']) else sg['name']),
               'unit': sg['unit'], 'volume': sg['volume_label'],
               'cost': sg['cost'], 'price': sg['price'],
               'monthly': _rng(sg['monthly_min'], sg['monthly_max'], 0)}
              for sg in stages_out if sg['enabled']]

    monthly_min = sum(sg['monthly_min'] for sg in stages_out if sg['enabled'])
    monthly_max = sum(sg['monthly_max'] for sg in stages_out if sg['enabled'])
    final = {'markup': round(markup, 4),
             'monthly_revenue_min': monthly_min, 'monthly_revenue_max': monthly_max,
             'monthly_box': monthly_box, 'in_plt_month': round(in_plt_month)}

    # ── 민감도 (§5.5) — 월 예상 청구액 기준 ─────────────────────────────────
    base_avg = (monthly_min + monthly_max) / 2 or 1
    sens = []
    for label, f in [
        ('물동 −20%', {'volume': 0.8}), ('물동 +20%', {'volume': 1.2}),
        ('생산성 −10%', {'prod': 0.9}), ('재고 +30%', {'stock': 1.3}),
        ('가동률 70%', {'occ': 0.70}),
    ]:
        v  = f.get('volume', 1.0)
        pr = f.get('prod', 1.0)
        stk = f.get('stock', 1.0)
        oc = f.get('occ')
        rev = 0.0
        if in_procs:
            rev += _final(inbound_plt_rate / pr) * in_plt_month * v
        if st['storage']:
            eff2 = params['plt_per_py'] * (oc if oc is not None else params['occupancy'])
            rate2 = ((params['rent_per_py'] + params['mgmt_per_py']) / eff2
                     + params['pallet_month'] + forklift_alloc) if eff2 else 0
            rev += _final(rate2) * avg_stock * stk
        if out_procs:
            rev += _final(outbound_direct_cpb / pr * (1 + oh) + supplies_cpb) * monthly_box * v
        rev += _final((dv_cost_min + dv_cost_max) / 2) * monthly_box * v
        sens.append({'label': label, 'monthly': round(rev),
                     'delta_pct': round((rev / base_avg - 1) * 100, 1)})

    return {
        'stages': stages_out,
        'work': {'processes': proc_rows,
                 'overhead_rate': oh, 'supplies_cpb': round(supplies_cpb, 2)},
        'manpower': manpower,
        'storage': storage,
        'transfer': transfer,
        'delivery': deliv,
        'final': final,
        'tariff': tariff,
        'sensitivity': sens,
        'inputs': {'monthly_box': monthly_box, 'monthly_plt': monthly_plt,
                   'boxes_per_plt': bpp, 'markup': round(markup, 4)},
    }
