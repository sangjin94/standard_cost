import os
from datetime import datetime
from flask import (
    Flask, render_template, request, redirect, url_for, flash
)
from models import (
    db, SystemConfig, WorkCostProcess, VehicleCost, StorageCenter, StandardQuote
)
from quote_calc import (
    work_cost_breakdown, transport_cost_breakdown, storage_cost_breakdown,
    final_price, scenario_table,
)

_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, instance_path=os.path.join(_BASE_DIR, 'instance'))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///standard_pricing.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'hanex-standard-pricing-2026'

db.init_app(app)

DEFAULT_CONFIGS = [
    ('work_overhead_rate', '10', '작업비 간접배부율 % (관리인력·장비비 등)'),
    ('quote_admin_rate', '7', '일반관리비율 %'),
    ('quote_margin_rate', '10', '목표이익률 %'),
    ('quote_turnover_days', '15', '기본 재고회전일수'),
]

# 기본 작업 공정 (작업비 마스터가 비어 있으면 자동 삽입)
DEFAULT_WORK_PROCESSES = [
    # (공정명, 단위, 시간당 생산성, 시급, 정렬)
    ('입고 하차',      'PLT', 25,  12000, 0),
    ('입고 검수·적치', 'PLT', 15,  12000, 1),
    ('피킹',           'BOX', 120, 12000, 2),
    ('검품·포장',      'BOX', 150, 12000, 3),
    ('출고 상차',      'PLT', 20,  12000, 4),
]

# 기본 차량 원가 (운송비 마스터가 비어 있으면 자동 삽입 — 개략 기준값, 실제 값으로 수정 필요)
DEFAULT_VEHICLES = [
    # (차종, 월 고정비, km당 변동비, 1일 km, 최대 PLT, 적재율%, 1일 회전, 월 운행일, 정렬)
    ('1톤',   4200000, 350, 150, 1,  80, 2.0, 24, 0),
    ('2.5톤', 4800000, 420, 180, 3,  80, 1.5, 24, 1),
    ('3.5톤', 5200000, 480, 200, 4,  80, 1.5, 24, 2),
    ('5톤',   5800000, 550, 220, 10, 80, 1.0, 24, 3),
    ('11톤',  7500000, 700, 300, 18, 80, 1.0, 24, 4),
]

with app.app_context():
    db.create_all()
    for key, val, desc in DEFAULT_CONFIGS:
        if not SystemConfig.query.filter_by(key=key).first():
            db.session.add(SystemConfig(key=key, value=val, description=desc))
    if WorkCostProcess.query.count() == 0:
        for pn, u, prod, wage, so in DEFAULT_WORK_PROCESSES:
            db.session.add(WorkCostProcess(
                process_name=pn, unit=u, productivity_per_hour=prod,
                hourly_wage=wage, sort_order=so))
    if VehicleCost.query.count() == 0:
        for vt, fx, vk, km, mp, lf, tr, wd, so in DEFAULT_VEHICLES:
            db.session.add(VehicleCost(
                vehicle_type=vt, monthly_fixed=fx, variable_per_km=vk, km_per_day=km,
                max_plt=mp, load_factor=lf, trips_per_day=tr, working_days=wd,
                sort_order=so, memo='기본값 — 실제 원가로 수정 필요'))
    db.session.commit()


def _cfg_float(key, default):
    cfg = SystemConfig.query.filter_by(key=key).first()
    try:
        return float(cfg.value) if cfg else default
    except (TypeError, ValueError):
        return default


# ─── 종합 견적 (메인) ─────────────────────────────────────────────────────────

@app.route('/')
def quote_page():
    storage_rows = StorageCenter.query.order_by(StorageCenter.center_name).all()
    vehicles = VehicleCost.query.order_by(VehicleCost.sort_order, VehicleCost.id).all()

    # 입력 파라미터 (기본값은 시스템 설정)
    customer_name = request.args.get('customer_name', '').strip()
    monthly_boxes = request.args.get('monthly_boxes', type=float)
    boxes_per_plt = request.args.get('boxes_per_plt', type=float)
    biz_days = request.args.get('biz_days', type=float) or 22.0
    turnover_days = request.args.get('turnover_days', type=float) or _cfg_float('quote_turnover_days', 15)
    admin_rate = request.args.get('admin_rate', type=float)
    if admin_rate is None:
        admin_rate = _cfg_float('quote_admin_rate', 7)
    margin_rate = request.args.get('margin_rate', type=float)
    if margin_rate is None:
        margin_rate = _cfg_float('quote_margin_rate', 10)
    center_name = request.args.get('center_name', '').strip()
    vehicle_type = request.args.get('vehicle_type', '').strip()
    delivery_override = request.args.get('delivery_override', type=float)  # 박스당 운송비 직접 입력 (선택)

    ctx = {
        'storage_rows': storage_rows, 'vehicles': vehicles,
        'customer_name': customer_name, 'monthly_boxes': monthly_boxes,
        'boxes_per_plt': boxes_per_plt, 'biz_days': biz_days,
        'turnover_days': turnover_days, 'admin_rate': admin_rate,
        'margin_rate': margin_rate, 'center_name': center_name,
        'vehicle_type': vehicle_type, 'delivery_override': delivery_override,
        'computed': False,
        'saved_quotes': StandardQuote.query.order_by(StandardQuote.created_at.desc()).limit(50).all(),
    }
    if not monthly_boxes or monthly_boxes <= 0 or not boxes_per_plt or boxes_per_plt <= 0:
        return render_template('quote.html', **ctx)

    monthly_plt = monthly_boxes / boxes_per_plt
    daily_plt = monthly_plt / biz_days if biz_days > 0 else 0
    avg_stock_plt = daily_plt * turnover_days

    overhead_rate = _cfg_float('work_overhead_rate', 10)
    processes = WorkCostProcess.query.order_by(WorkCostProcess.sort_order, WorkCostProcess.id).all()
    work_cpb, work_detail, work_direct = work_cost_breakdown(processes, boxes_per_plt, overhead_rate)

    vehicle = VehicleCost.query.filter_by(vehicle_type=vehicle_type).first() if vehicle_type else None
    transport = transport_cost_breakdown(vehicle, boxes_per_plt)
    delivery_cpb = delivery_override if delivery_override is not None else transport['cost_per_box']

    storage_center = StorageCenter.query.filter_by(center_name=center_name).first() if center_name else None
    storage = storage_cost_breakdown(storage_center, avg_stock_plt, monthly_boxes)

    fp = final_price(work_cpb, delivery_cpb, storage['cost_per_box'],
                     admin_rate, margin_rate, boxes_per_plt)
    scenarios = scenario_table(processes, boxes_per_plt, overhead_rate,
                               None if delivery_override is not None else vehicle,
                               storage_center, avg_stock_plt, monthly_boxes,
                               admin_rate, margin_rate)
    # 운송비를 직접 입력한 경우 시나리오의 운송비도 입력값으로 고정
    if delivery_override is not None:
        for s in scenarios:
            s['transport_cpb'] = delivery_override
            refp = final_price(s['work_cpb'], delivery_override, s['storage_cpb'],
                               admin_rate, margin_rate, boxes_per_plt)
            s['final_cpb'] = refp['final_cpb']
            s['final_cpp'] = refp['final_cpp']

    ctx.update({
        'computed': True,
        'monthly_plt': monthly_plt, 'daily_plt': daily_plt, 'avg_stock_plt': avg_stock_plt,
        'overhead_rate': overhead_rate,
        'work_cpb': work_cpb, 'work_detail': work_detail, 'work_direct': work_direct,
        'vehicle': vehicle, 'transport': transport, 'delivery_cpb': delivery_cpb,
        'storage_center': storage_center, 'storage': storage,
        'fp': fp, 'scenarios': scenarios,
    })
    return render_template('quote.html', **ctx)


@app.route('/quote/save', methods=['POST'])
def quote_save():
    try:
        q = StandardQuote(
            quote_name=request.form.get('quote_name', '').strip() or f"견적 {datetime.now():%Y-%m-%d %H:%M}",
            customer_name=request.form.get('customer_name', '').strip(),
            center_name=request.form.get('center_name') or None,
            vehicle_type=request.form.get('vehicle_type') or None,
            monthly_boxes=float(request.form.get('monthly_boxes') or 0),
            boxes_per_plt=float(request.form.get('boxes_per_plt') or 0),
            biz_days=float(request.form.get('biz_days') or 0),
            turnover_days=float(request.form.get('turnover_days') or 0),
            avg_stock_plt=float(request.form.get('avg_stock_plt') or 0),
            work_cpb=float(request.form.get('work_cpb') or 0),
            delivery_cpb=float(request.form.get('delivery_cpb') or 0),
            storage_cpb=float(request.form.get('storage_cpb') or 0),
            work_overhead_rate=float(request.form.get('work_overhead_rate') or 0),
            admin_rate=float(request.form.get('admin_rate') or 0),
            margin_rate=float(request.form.get('margin_rate') or 0),
            final_cpb=float(request.form.get('final_cpb') or 0),
            final_cpp=float(request.form.get('final_cpp') or 0),
            memo=request.form.get('memo', '').strip(),
        )
        db.session.add(q)
        db.session.commit()
        flash(f'견적 [{q.quote_name}]이 저장되었습니다.', 'success')
    except Exception as e:
        flash(f'견적 저장 오류: {e}', 'danger')
    return redirect(url_for('quote_page'))


@app.route('/quote/<int:qid>/delete', methods=['POST'])
def quote_delete(qid):
    q = StandardQuote.query.get_or_404(qid)
    db.session.delete(q)
    db.session.commit()
    flash('견적이 삭제되었습니다.', 'warning')
    return redirect(url_for('quote_page'))


# ─── 작업비 마스터 ────────────────────────────────────────────────────────────

@app.route('/masters/work-cost')
def work_cost_master():
    processes = WorkCostProcess.query.order_by(WorkCostProcess.sort_order, WorkCostProcess.id).all()
    overhead_rate = _cfg_float('work_overhead_rate', 10)
    preview_bpp = _cfg_float('work_preview_boxes_per_plt', 60)
    total_cpb, detail, direct_total = work_cost_breakdown(processes, preview_bpp, overhead_rate)
    detail_map = {d['name']: d for d in detail}
    return render_template('work_cost.html',
                           processes=processes, overhead_rate=overhead_rate,
                           preview_bpp=preview_bpp, detail_map=detail_map,
                           direct_total=direct_total, total_cpb=total_cpb)


@app.route('/masters/work-cost/add', methods=['POST'])
def work_cost_add():
    name = request.form.get('process_name', '').strip()
    unit = request.form.get('unit', 'BOX').strip()
    prod_str = request.form.get('productivity', '').replace(',', '').strip()
    wage_str = request.form.get('hourly_wage', '').replace(',', '').strip()
    memo = request.form.get('memo', '').strip()
    if not name or not prod_str or not wage_str:
        flash('공정명, 생산성, 시급을 모두 입력해주세요.', 'danger')
        return redirect(url_for('work_cost_master'))
    try:
        prod = float(prod_str)
        wage = int(float(wage_str))
        if prod <= 0 or wage <= 0:
            raise ValueError('생산성과 시급은 0보다 커야 합니다.')
        existing = WorkCostProcess.query.filter_by(process_name=name).first()
        if existing:
            existing.unit = unit
            existing.productivity_per_hour = prod
            existing.hourly_wage = wage
            existing.memo = memo
            flash(f'[{name}] 공정이 업데이트되었습니다.', 'success')
        else:
            max_so = db.session.query(db.func.max(WorkCostProcess.sort_order)).scalar() or 0
            db.session.add(WorkCostProcess(
                process_name=name, unit=unit, productivity_per_hour=prod,
                hourly_wage=wage, memo=memo, sort_order=max_so + 1))
            flash(f'[{name}] 공정이 추가되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('work_cost_master'))


@app.route('/masters/work-cost/<int:pid>/toggle', methods=['POST'])
def work_cost_toggle(pid):
    p = WorkCostProcess.query.get_or_404(pid)
    p.is_active = not p.is_active
    db.session.commit()
    flash(f'[{p.process_name}] 공정이 {"활성화" if p.is_active else "비활성화"}되었습니다.', 'info')
    return redirect(url_for('work_cost_master'))


@app.route('/masters/work-cost/<int:pid>/delete', methods=['POST'])
def work_cost_delete(pid):
    p = WorkCostProcess.query.get_or_404(pid)
    name = p.process_name
    db.session.delete(p)
    db.session.commit()
    flash(f'[{name}] 공정이 삭제되었습니다.', 'warning')
    return redirect(url_for('work_cost_master'))


@app.route('/masters/work-cost/config', methods=['POST'])
def work_cost_config():
    for key, form_key, desc in [
        ('work_overhead_rate', 'overhead_rate', '작업비 간접배부율 % (관리인력·장비비 등)'),
        ('work_preview_boxes_per_plt', 'preview_bpp', '작업비 미리보기 BOX/PLT 환산비'),
    ]:
        val = request.form.get(form_key, '').strip()
        if not val:
            continue
        try:
            float(val)
        except ValueError:
            flash(f'{desc}: 숫자를 입력해주세요.', 'danger')
            return redirect(url_for('work_cost_master'))
        cfg = SystemConfig.query.filter_by(key=key).first()
        if cfg:
            cfg.value = val
        else:
            db.session.add(SystemConfig(key=key, value=val, description=desc))
    db.session.commit()
    flash('작업비 설정이 저장되었습니다.', 'success')
    return redirect(url_for('work_cost_master'))


# ─── 운송비(물류비) 마스터 ────────────────────────────────────────────────────

@app.route('/masters/vehicle-cost')
def vehicle_cost_master():
    vehicles = VehicleCost.query.order_by(VehicleCost.sort_order, VehicleCost.id).all()
    return render_template('vehicle_cost.html', vehicles=vehicles)


@app.route('/masters/vehicle-cost/add', methods=['POST'])
def vehicle_cost_add():
    vt = request.form.get('vehicle_type', '').strip()

    def _num(key, default='0'):
        return float((request.form.get(key, default) or default).replace(',', '').strip() or default)

    if not vt:
        flash('차종을 입력해주세요.', 'danger')
        return redirect(url_for('vehicle_cost_master'))
    try:
        fx = int(_num('monthly_fixed'))
        vk = int(_num('variable_per_km'))
        km = _num('km_per_day')
        mp = _num('max_plt')
        lf = _num('load_factor', '80')
        tr = _num('trips_per_day', '1')
        wd = _num('working_days', '24')
        memo = request.form.get('memo', '').strip()
        if mp <= 0 or not (0 < lf <= 100) or tr <= 0 or wd <= 0:
            raise ValueError('적재 PLT·회전수·운행일수는 0보다 크고, 적재율은 0~100% 사이여야 합니다.')
        existing = VehicleCost.query.filter_by(vehicle_type=vt).first()
        if existing:
            existing.monthly_fixed = fx
            existing.variable_per_km = vk
            existing.km_per_day = km
            existing.max_plt = mp
            existing.load_factor = lf
            existing.trips_per_day = tr
            existing.working_days = wd
            existing.memo = memo
            flash(f'[{vt}] 차량 원가가 업데이트되었습니다.', 'success')
        else:
            max_so = db.session.query(db.func.max(VehicleCost.sort_order)).scalar() or 0
            db.session.add(VehicleCost(
                vehicle_type=vt, monthly_fixed=fx, variable_per_km=vk, km_per_day=km,
                max_plt=mp, load_factor=lf, trips_per_day=tr, working_days=wd,
                memo=memo, sort_order=max_so + 1))
            flash(f'[{vt}] 차량 원가가 추가되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('vehicle_cost_master'))


@app.route('/masters/vehicle-cost/<int:vid>/delete', methods=['POST'])
def vehicle_cost_delete(vid):
    v = VehicleCost.query.get_or_404(vid)
    vt = v.vehicle_type
    db.session.delete(v)
    db.session.commit()
    flash(f'[{vt}] 차량 원가가 삭제되었습니다.', 'warning')
    return redirect(url_for('vehicle_cost_master'))


# ─── 보관비 마스터 ────────────────────────────────────────────────────────────

@app.route('/masters/storage')
def storage_master():
    rows = StorageCenter.query.order_by(StorageCenter.center_name).all()
    return render_template('storage_cost.html', rows=rows)


@app.route('/masters/storage/add', methods=['POST'])
def storage_add():
    name = request.form.get('center_name', '').strip()
    rent_str = request.form.get('monthly_rent', '0').replace(',', '').strip() or '0'
    mgmt_str = request.form.get('monthly_mgmt', '0').replace(',', '').strip() or '0'
    capa_str = request.form.get('effective_plt_capa', '').replace(',', '').strip()
    occ_str = request.form.get('target_occupancy', '85').strip() or '85'
    memo = request.form.get('memo', '').strip()
    if not name or not capa_str:
        flash('센터명과 유효 CAPA를 입력해주세요.', 'danger')
        return redirect(url_for('storage_master'))
    try:
        rent = int(float(rent_str))
        mgmt = int(float(mgmt_str))
        capa = int(float(capa_str))
        occ = float(occ_str)
        if capa <= 0 or not (0 < occ <= 100):
            raise ValueError('CAPA는 0보다 크고 가동률은 0~100% 사이여야 합니다.')
        existing = StorageCenter.query.filter_by(center_name=name).first()
        if existing:
            existing.monthly_rent = rent
            existing.monthly_mgmt = mgmt
            existing.effective_plt_capa = capa
            existing.target_occupancy = occ
            existing.memo = memo
            flash(f'[{name}] 보관비 원가가 업데이트되었습니다.', 'success')
        else:
            db.session.add(StorageCenter(
                center_name=name, monthly_rent=rent, monthly_mgmt=mgmt,
                effective_plt_capa=capa, target_occupancy=occ, memo=memo))
            flash(f'[{name}] 보관비 원가가 등록되었습니다.', 'success')
        db.session.commit()
    except Exception as e:
        flash(f'오류: {e}', 'danger')
    return redirect(url_for('storage_master'))


@app.route('/masters/storage/<int:sid>/delete', methods=['POST'])
def storage_delete(sid):
    s = StorageCenter.query.get_or_404(sid)
    name = s.center_name
    db.session.delete(s)
    db.session.commit()
    flash(f'[{name}] 보관비 원가가 삭제되었습니다.', 'warning')
    return redirect(url_for('storage_master'))


if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5001)
