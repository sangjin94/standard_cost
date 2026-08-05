"""표준물류단가 종합 견적 계산 로직 (작업비 · 물류비(운송) · 보관비 → 최종 단가)

모든 비용의 공통 공식: 총원가 ÷ 처리능력(생산성·적재효율·CAPA) = 단위원가.
여기에 간접비·일반관리비·목표이익률을 얹어 청구단위(BOX/PLT)로 환산한다.
"""


def work_cost_breakdown(processes, boxes_per_plt, overhead_rate=0.0):
    """활성 공정별 작업비를 박스당 원가로 환산해 합산.

    processes     : WorkCostProcess 목록
    boxes_per_plt : PLT당 BOX 수 (PLT 단위 공정을 박스당으로 환산할 때 사용)
    overhead_rate : 간접배부율 % (관리인력·장비비 등을 직접작업비에 가산)

    returns (박스당 작업비 합계(간접 포함), 공정별 상세 리스트, 직접작업비 합계)
    """
    detail = []
    direct_total = 0.0
    for p in processes:
        if not p.is_active or not p.productivity_per_hour or p.productivity_per_hour <= 0:
            continue
        cost_per_unit = p.hourly_wage / p.productivity_per_hour
        if p.unit == 'PLT':
            cpb = cost_per_unit / boxes_per_plt if boxes_per_plt and boxes_per_plt > 0 else 0.0
        else:
            cpb = cost_per_unit
        detail.append({
            'name': p.process_name,
            'unit': p.unit,
            'productivity': p.productivity_per_hour,
            'wage': p.hourly_wage,
            'cost_per_unit': cost_per_unit,   # 해당 단위(BOX 또는 PLT)당 원가
            'cost_per_box': cpb,              # 박스당 환산 원가
        })
        direct_total += cpb
    total_with_overhead = direct_total * (1 + overhead_rate / 100.0)
    return total_with_overhead, detail, direct_total


def storage_cost_breakdown(storage_center, avg_stock_plt, monthly_boxes):
    """보관비: PLT/월 단가 × 평균 재고 PLT ÷ 월 출고 박스 = 박스당 보관비.

    storage_center : StorageCenter (None 가능)
    avg_stock_plt  : 평균 재고 PLT (일평균 출고 PLT × 재고회전일수)
    monthly_boxes  : 월평균 출고 BOX
    """
    if not storage_center or not monthly_boxes or monthly_boxes <= 0:
        return {'plt_month_rate': 0, 'monthly_storage_cost': 0, 'cost_per_box': 0}
    rate = storage_center.plt_month_rate
    monthly_cost = rate * (avg_stock_plt or 0)
    return {
        'plt_month_rate': rate,
        'monthly_storage_cost': monthly_cost,
        'cost_per_box': monthly_cost / monthly_boxes,
    }


def final_price(work_cpb, delivery_cpb, storage_cpb, admin_rate, margin_rate, boxes_per_plt):
    """3대 비용 합산 → 일반관리비 가산 → 목표이익률 역산 방식으로 최종 견적단가 산출.

    최종단가 = 원가합 × (1 + 일반관리비율) ÷ (1 − 목표이익률)
    """
    subtotal = (work_cpb or 0) + (delivery_cpb or 0) + (storage_cpb or 0)
    admin_amt = subtotal * (admin_rate / 100.0)
    cost_total = subtotal + admin_amt
    margin = min(margin_rate / 100.0, 0.95)  # 100% 이상 마진 입력으로 인한 0나눗셈 방지
    final_cpb = cost_total / (1 - margin) if margin < 1 else cost_total
    return {
        'subtotal': subtotal,
        'admin_amt': admin_amt,
        'cost_total': cost_total,
        'margin_amt': final_cpb - cost_total,
        'final_cpb': final_cpb,
        'final_cpp': final_cpb * boxes_per_plt if boxes_per_plt else 0,
    }


def scenario_table(processes, boxes_per_plt, overhead_rate,
                   storage_center, avg_stock_plt, monthly_boxes,
                   delivery_cpb, admin_rate, margin_rate):
    """민감도 시나리오: 생산성 · 보관가동률 전제를 ±10% 흔들었을 때 최종 단가 비교.

    견적이 틀어지는 주원인은 원가 자체보다 '처리량·가동률 전제'이므로 분모를 흔들어 본다.
    물류비는 배송단가 시스템 산정값(또는 직접입력)을 그대로 쓰므로 시나리오에서 고정.
    """
    scenarios = []
    for label, prod_factor, occ_delta in [
        ('보수 (생산성 -10%, 가동률 -10%p)', 0.9, -10.0),
        ('기준', 1.0, 0.0),
        ('공격 (생산성 +10%, 가동률 +10%p)', 1.1, +10.0),
    ]:
        # 작업비: 생산성만 factor 적용해 재계산
        direct = 0.0
        for p in processes:
            if not p.is_active or not p.productivity_per_hour or p.productivity_per_hour <= 0:
                continue
            cpu = p.hourly_wage / (p.productivity_per_hour * prod_factor)
            direct += (cpu / boxes_per_plt) if (p.unit == 'PLT' and boxes_per_plt) else cpu
        work = direct * (1 + overhead_rate / 100.0)

        # 보관비: 목표가동률에 delta 적용 (10~100% 범위로 제한)
        storage = 0.0
        if storage_center and monthly_boxes:
            occ = max(10.0, min(100.0, storage_center.target_occupancy + occ_delta))
            denom = storage_center.effective_plt_capa * (occ / 100.0)
            rate = (storage_center.monthly_rent + storage_center.monthly_mgmt) / denom if denom > 0 else 0
            storage = rate * (avg_stock_plt or 0) / monthly_boxes

        fp = final_price(work, delivery_cpb, storage, admin_rate, margin_rate, boxes_per_plt)
        scenarios.append({
            'label': label,
            'work_cpb': work,
            'transport_cpb': delivery_cpb,
            'storage_cpb': storage,
            'final_cpb': fp['final_cpb'],
            'final_cpp': fp['final_cpp'],
        })
    return scenarios
