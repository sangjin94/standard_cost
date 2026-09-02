"""표준물류단가 산정 시스템 — 데이터 업로드 기반 TPL 견적 도구.

설계·가정·수식·한계는 docs/DESIGN.md 참조 (앱 내 '설계 문서' 메뉴로도 열람).
"""
import io
import json
import os

import pandas as pd
from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, send_file)

from models import db, CostParam, WorkProcess, RegionRate, Quote
import profile_extract as pe
import engine

_BASE = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, instance_path=os.path.join(_BASE, 'instance'))
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///standard_pricing.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = 'hanex-standard-2026'
app.config['MAX_CONTENT_LENGTH'] = 64 * 1024 * 1024

db.init_app(app)

with app.app_context():
    db.create_all()
    # 기본값 seed (이미 있으면 건드리지 않음 — 사용자가 수정한 값 보존)
    if CostParam.query.count() == 0:
        for i, (k, v, label, unit, grp, a, desc) in enumerate(engine.PARAM_DEFS):
            db.session.add(CostParam(key=k, value=v, label=label, unit=unit,
                                     group=grp, assumption=a, description=desc, sort_order=i))
    if WorkProcess.query.count() == 0:
        for i, (name, flow, unit, prod, wt, memo) in enumerate(engine.PROCESS_DEFS):
            db.session.add(WorkProcess(name=name, flow=flow, unit=unit,
                                       productivity=prod, worker_type=wt, memo=memo, sort_order=i))
    if RegionRate.query.count() == 0:
        for sido, rate in engine.REGION_DEFS:
            db.session.add(RegionRate(sido=sido, cost_per_box=rate,
                                      memo='개략 seed — 실계약 단가로 교체'))
    db.session.commit()


# ─── 공통 헬퍼 ───────────────────────────────────────────────────────────────

def _params(overrides=None):
    """CostParam 기본값 + 견적별 오버라이드 병합."""
    p = {c.key: c.value for c in CostParam.query.all()}
    for k, v, *_ in engine.PARAM_DEFS:          # 마스터에서 지워졌어도 계산은 되게
        p.setdefault(k, v)
    if overrides:
        for k, v in overrides.items():
            if k.startswith('param:'):
                try:
                    p[k[6:]] = float(v)
                except (TypeError, ValueError):
                    pass
    return p


def _processes(excluded_ids=None):
    rows = WorkProcess.query.filter_by(is_active=True).order_by(WorkProcess.sort_order).all()
    ex = set(excluded_ids or [])
    return [{'id': r.id, 'name': r.name, 'flow': r.flow, 'unit': r.unit,
             'productivity': r.productivity, 'worker_type': r.worker_type}
            for r in rows if r.id not in ex]


def _stage_conf(overrides):
    """견적별 스테이지 구성 (A26·A27): 보관/이고 토글 + 이고 단가 + 공정 제외."""
    def _f(key, default=0.0):
        try:
            return float(overrides.get(key) or default)
        except (TypeError, ValueError):
            return default
    excluded = [int(k.split(':', 1)[1]) for k, v in overrides.items()
                if k.startswith('proc_off:') and v]
    return {
        'storage': overrides.get('use_storage', '1') != '0',
        'transfer': overrides.get('use_transfer', '0') == '1',
        'transfer_per_plt': _f('transfer_per_plt'),
    }, excluded


def _load(q):
    profile = json.loads(q.profile_json) if q.profile_json else {}
    overrides = json.loads(q.overrides_json) if q.overrides_json else {}
    return profile, overrides


def _summary_with_overrides(profile, overrides, params):
    s = pe.summarize(profile, params)
    if not s:
        return None
    applied = []
    for k, v in overrides.items():
        if k.startswith('p:') and k[2:] in s:
            key = k[2:]
            try:
                s[key] = float(v)
                applied.append(key)
            except (TypeError, ValueError):
                pass
    s['overridden'] = applied
    return s


def _delivery_conf(overrides):
    return {
        'mode':       overrides.get('delivery_mode', 'manual'),
        'manual_min': overrides.get('manual_min') or 0,
        'manual_max': overrides.get('manual_max') or 0,
        'direct_min': overrides.get('direct_min') or 0,   # split: 직송 단가
        'direct_max': overrides.get('direct_max') or 0,
        'joint_min':  overrides.get('joint_min') or 0,    # split: 공동배송 단가
        'joint_max':  overrides.get('joint_max') or 0,
        'link_min':   overrides.get('link_min') or 0,
        'link_max':   overrides.get('link_max') or 0,
    }


def _compute(q):
    """견적 1건 실시간 산정. (에러 메시지, 결과) 튜플."""
    profile, overrides = _load(q)
    params = _params(overrides)
    s = _summary_with_overrides(profile, overrides, params)
    if not s:
        return '출고내역을 먼저 업로드하세요.', None, None, params
    region_rates = {r.sido: r.cost_per_box for r in RegionRate.query.all()}
    stages, excluded = _stage_conf(overrides)
    try:
        result = engine.compute(s, params, _processes(excluded), region_rates,
                                _delivery_conf(overrides), stages)
    except ValueError as e:
        return str(e), s, None, params
    return None, s, result, params


# ─── 견적 목록/생성 ──────────────────────────────────────────────────────────

@app.route('/')
def index():
    quotes = Quote.query.order_by(Quote.updated_at.desc()).all()
    rows = []
    for q in quotes:
        r = json.loads(q.result_json) if q.result_json else None
        rows.append({'q': q, 'saved': r})
    return render_template('index.html', rows=rows)


@app.route('/quote/new', methods=['POST'])
def quote_new():
    name = request.form.get('name', '').strip()
    if not name:
        flash('견적명을 입력하세요.', 'danger')
        return redirect(url_for('index'))
    q = Quote(name=name, customer_name=request.form.get('customer_name', '').strip())
    db.session.add(q)
    db.session.commit()
    return redirect(url_for('quote_view', qid=q.id))


@app.route('/quote/<int:qid>/delete', methods=['POST'])
def quote_delete(qid):
    q = Quote.query.get_or_404(qid)
    db.session.delete(q)
    db.session.commit()
    flash('견적이 삭제되었습니다.', 'warning')
    return redirect(url_for('index'))


@app.route('/quote/<int:qid>/copy', methods=['POST'])
def quote_copy(qid):
    q = Quote.query.get_or_404(qid)
    c = Quote(name=q.name + ' (복사)', customer_name=q.customer_name, memo=q.memo,
              profile_json=q.profile_json, overrides_json=q.overrides_json)
    db.session.add(c)
    db.session.commit()
    return redirect(url_for('quote_view', qid=c.id))


# ─── 견적 작업 화면 ──────────────────────────────────────────────────────────

def _quote_ctx(q):
    """세 단계 화면이 공유하는 컨텍스트."""
    err, s, result, params = _compute(q)
    profile, overrides = _load(q)
    return dict(q=q, err=err, s=s, result=result,
                params=params,
                param_rows=CostParam.query.order_by(CostParam.sort_order).all(),
                overrides=overrides,
                profile_meta={
                    'ship': bool(profile.get('ship')),
                    'pm': len(profile.get('pm') or {}),
                    'stock': profile.get('stock'),
                    'inbound': profile.get('inbound'),
                },
                delivery=_delivery_conf(overrides),
                all_processes=WorkProcess.query.filter_by(is_active=True)
                                         .order_by(WorkProcess.sort_order).all(),
                stage_conf=_stage_conf(overrides),
                dp_available=os.path.isdir(os.path.join(os.path.dirname(_BASE), 'delivery_pricing')))


@app.route('/quote/<int:qid>')
def quote_view(qid):
    """데이터가 없으면 ①데이터, 있으면 ③결과로."""
    q = Quote.query.get_or_404(qid)
    profile, _ = _load(q)
    step = 'quote_result' if profile.get('ship') else 'quote_data'
    return redirect(url_for(step, qid=qid))


@app.route('/quote/<int:qid>/data')
def quote_data(qid):
    q = Quote.query.get_or_404(qid)
    return render_template('quote_data.html', step='data', **_quote_ctx(q))


@app.route('/quote/<int:qid>/setup')
def quote_setup(qid):
    q = Quote.query.get_or_404(qid)
    return render_template('quote_setup.html', step='setup', **_quote_ctx(q))


@app.route('/quote/<int:qid>/result')
def quote_result(qid):
    q = Quote.query.get_or_404(qid)
    return render_template('quote_result.html', step='result', **_quote_ctx(q))


def _read_upload():
    f = request.files.get('file')
    if not f or not f.filename:
        raise ValueError('파일을 선택하세요.')
    if f.filename.lower().endswith(('.xlsx', '.xls')):
        return pd.read_excel(f)
    return pd.read_csv(f)


@app.route('/quote/<int:qid>/upload/<kind>', methods=['POST'])
def quote_upload(qid, kind):
    q = Quote.query.get_or_404(qid)
    profile, overrides = _load(q)
    params = _params(overrides)
    try:
        df = _read_upload()
        if kind == 'ship':
            profile['ship'] = pe.parse_shipments(df)
            flash(f"출고내역 {profile['ship']['rows']:,}행 업로드"
                  + (f" (무효 {profile['ship']['skipped']:,}행 제외)" if profile['ship']['skipped'] else ''),
                  'success')
        elif kind == 'product':
            profile['pm'] = pe.parse_products(df)
            flash(f"상품마스터 {len(profile['pm']):,}개 업로드", 'success')
        elif kind == 'stock':
            pm = dict((profile.get('ship') or {}).get('pm_extra') or {})
            pm.update(profile.get('pm') or {})
            profile['stock'] = pe.parse_stock(df, pm, params['default_boxes_per_plt'])
            flash(f"재고 스냅샷 {profile['stock']['days']}일 업로드 "
                  f"(평균 {profile['stock']['avg_stock_plt']:,}PLT)", 'success')
        elif kind == 'inbound':
            pm = dict((profile.get('ship') or {}).get('pm_extra') or {})
            pm.update(profile.get('pm') or {})
            profile['inbound'] = pe.parse_inbound(df, pm, params['default_boxes_per_plt'])
            flash(f"입고내역 {profile['inbound']['days']}일 업로드", 'success')
        else:
            raise ValueError('알 수 없는 업로드 종류')
        q.profile_json = json.dumps(profile, ensure_ascii=False)
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        flash(f'업로드 오류: {e}', 'danger')
    return redirect(url_for('quote_data', qid=qid))


@app.route('/quote/<int:qid>/clear/<kind>', methods=['POST'])
def quote_clear(qid, kind):
    q = Quote.query.get_or_404(qid)
    profile, _ = _load(q)
    profile.pop({'ship': 'ship', 'product': 'pm', 'stock': 'stock', 'inbound': 'inbound'}.get(kind, kind), None)
    q.profile_json = json.dumps(profile, ensure_ascii=False)
    db.session.commit()
    flash('삭제했습니다.', 'warning')
    return redirect(url_for('quote_data', qid=qid))


@app.route('/quote/<int:qid>/override', methods=['POST'])
def quote_override(qid):
    """프로파일 보정값 · 파라미터 오버라이드 · 배송 설정 저장."""
    q = Quote.query.get_or_404(qid)
    _, overrides = _load(q)
    if request.form.get('stage_form'):      # 체크박스는 미체크 시 미전송 → marker로 일괄 재구성
        overrides['use_storage'] = '1' if request.form.get('use_storage') else '0'
        overrides['use_transfer'] = '1' if request.form.get('use_transfer') else '0'
        for k in [k for k in overrides if k.startswith('proc_off:')]:
            overrides.pop(k)
        for k in request.form:
            if k.startswith('proc_off:'):
                overrides[k] = '1'
    for k, v in request.form.items():
        if k in ('csrf', 'stage_form', 'use_storage', 'use_transfer') or k.startswith('proc_off:'):
            continue
        v = v.strip()
        if k.startswith(('p:', 'param:')):
            if v == '':
                overrides.pop(k, None)      # 빈칸 = 추출값/기본값 복귀
            else:
                overrides[k] = v
        elif k in ('delivery_mode', 'manual_min', 'manual_max', 'transfer_per_plt',
                   'direct_min', 'direct_max', 'joint_min', 'joint_max', 'memo'):
            if k == 'memo':
                q.memo = v
            elif v == '':
                overrides.pop(k, None)
            else:
                overrides[k] = v
    q.overrides_json = json.dumps(overrides, ensure_ascii=False)
    db.session.commit()
    flash('저장했습니다 — 단가를 다시 계산했습니다.', 'success')
    nxt = {'data': 'quote_data', 'setup': 'quote_setup', 'result': 'quote_result'}
    return redirect(url_for(nxt.get(request.form.get('next'), 'quote_result'), qid=qid))


@app.route('/quote/<int:qid>/save-result', methods=['POST'])
def quote_save_result(qid):
    """현재 산정 결과를 스냅샷으로 확정 저장 (목록·이력용)."""
    q = Quote.query.get_or_404(qid)
    err, s, result, params = _compute(q)
    if err or not result:
        flash(f'저장 불가: {err or "산정 결과 없음"}', 'danger')
        return redirect(url_for('quote_result', qid=qid))
    q.result_json = json.dumps({'summary': s, 'result': result,
                                'params': params, 'saved_at': str(pd.Timestamp.now())[:16]},
                               ensure_ascii=False)
    db.session.commit()
    flash('견적 결과를 확정 저장했습니다.', 'success')
    return redirect(url_for('quote_result', qid=qid))


# ─── 배송비 연동 (모드③) ─────────────────────────────────────────────────────

def _dp_dir():
    return os.path.join(os.path.dirname(_BASE), 'delivery_pricing')


@app.route('/api/delivery-customers')
def api_delivery_customers():
    try:
        import delivery_link
        return jsonify({'ok': True, 'customers': delivery_link.list_customers(_dp_dir())})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)})


@app.route('/quote/<int:qid>/link-delivery', methods=['POST'])
def quote_link_delivery(qid):
    q = Quote.query.get_or_404(qid)
    _, overrides = _load(q)
    try:
        import delivery_link
        cid = int(request.form['customer_id'])
        data = delivery_link.get_summary(_dp_dir(), cid)
        d = data.get('delivery') or {}
        if not d.get('cost_per_box_min'):
            raise RuntimeError('연동 결과에 박스당 단가가 없습니다.')
        overrides['delivery_mode'] = 'link'
        overrides['link_min'] = d['cost_per_box_min']
        overrides['link_max'] = d.get('cost_per_box_max') or d['cost_per_box_min']
        overrides['link_customer'] = data.get('customer_name') or str(cid)
        q.overrides_json = json.dumps(overrides, ensure_ascii=False)
        db.session.commit()
        flash(f"배송단가 연동: {overrides['link_customer']} "
              f"{overrides['link_min']:,.0f}~{overrides['link_max']:,.0f}원/BOX", 'success')
    except Exception as e:
        flash(f'배송단가 연동 실패: {e}', 'danger')
    return redirect(url_for('quote_setup', qid=qid))


# ─── 엑셀: 업로드 템플릿 · 견적서 ────────────────────────────────────────────

_TEMPLATES = {
    'ship': ('출고내역_템플릿.xlsx', pd.DataFrame({
        '출고일자': ['2026-08-01', '2026-08-01'], '주문번호': ['SO-1001', 'SO-1002'],
        '배송처코드': ['ST001', 'ST002'], '배송처명': ['이마트 A점', '홈플러스 B점'],
        '주소': ['경기 용인시 기흥구 …', '서울 송파구 …'],
        '상품코드': ['P001', 'P002'], '박스수': [12, 30], '출고수량(PLT)': ['', 0.5]})),
    'product': ('상품마스터_템플릿.xlsx', pd.DataFrame({
        '상품코드': ['P001', 'P002'], '상품명': ['상품A', '상품B'],
        'PLT입수(BOX/PLT)': [60, 48], '박스중량(kg)': [8.5, 12.0],
        '가로(mm)': [400, 500], '세로(mm)': [300, 350], '높이(mm)': [250, 300]})),
    'stock': ('재고_템플릿.xlsx', pd.DataFrame({
        '기준일자': ['2026-08-01', '2026-08-01'], '상품코드': ['P001', 'P002'],
        '재고박스': [3600, ''], '재고PLT': ['', 25]})),
    'inbound': ('입고내역_템플릿.xlsx', pd.DataFrame({
        '입고일자': ['2026-08-01', '2026-08-02'], '상품코드': ['P001', 'P002'],
        '박스수': [1200, ''], 'PLT수': ['', 20]})),
}


@app.route('/template/<kind>')
def template_download(kind):
    if kind not in _TEMPLATES:
        return '알 수 없는 템플릿', 404
    fname, df = _TEMPLATES[kind]
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        df.to_excel(w, index=False)
    buf.seek(0)
    return send_file(buf, download_name=fname, as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@app.route('/quote/<int:qid>/export')
def quote_export(qid):
    """견적서 엑셀 (§8): ①요약 ②원가 상세 ③적용 가정 ④물동 프로파일."""
    q = Quote.query.get_or_404(qid)
    err, s, r, params = _compute(q)
    if err or not r:
        flash(f'출력 불가: {err}', 'danger')
        return redirect(url_for('quote_result', qid=qid))
    _, overrides = _load(q)

    sum_rows = [
        ['견적명', q.name], ['화주사', q.customer_name or '-'],
        ['분석기간', f"{s['period_from']} ~ {s['period_to']} ({s['biz_days']}영업일)"],
        ['월평균 물동', f"{s['monthly_box']:,} BOX / {s['monthly_plt']:,} PLT"],
        [], ['항목', '청구단위', '월 물동', '원가', '견적단가', '월 예상 금액'],
    ] + [[t['item'], t['unit'], t['volume'], t['cost'], t['price'], t['monthly']] for t in r['tariff']] +         [[sg['name'], '미사용', sg['off_reason'], '', '', ''] for sg in r['stages'] if not sg['enabled']] + [
        [], ['월 예상 청구액 합계',
             f"{r['final']['monthly_revenue_min']:,}~{r['final']['monthly_revenue_max']:,} 원"],
        ['적용 배율', f"×{r['final']['markup']} (일반관리비 {params['admin_rate']:.0%}, 이익률 {params['margin_rate']:.0%})"],
        [], ['※ 견적은 종합단가 없이 위 항목별 단가로 구성됨 — docs/DESIGN.md 가정·한계 전제 (부가세 별도)'],
    ]

    cost_rows = []
    # 스테이지별 계산 추적(§5) — 화면 모달과 동일한 내용
    for sg in r['stages']:
        cost_rows.append([f"[{sg['name']}]",
                          (f"{sg['price']:,}" if isinstance(sg['price'], (int, float)) else sg['price'])
                          + f" {sg['unit']}" if sg['enabled'] else '미사용'])
        if sg['enabled']:
            cost_rows.append(['단계', '계산식 (실제 값 대입)', '결과'])
            for t in sg['trace']:
                cost_rows.append([t['label'], t['expr'], t['val']])
        cost_rows.append([])
    cost_rows += [
        ['[작업 공정]'], ['공정', '구분', '단위', '생산성(단위/인시)', '실부담시급', '일 인시'],
    ] + [[p['name'], p['flow'], p['unit'], p['productivity'], p['wage'], p['day_manhours']]
         for p in r['work']['processes']] + [
        [], ['[인력 소요]'], [r['manpower']['note']],
        ['평시 필요인원', r['manpower']['heads_avg'], '피크 필요인원', r['manpower']['heads_p95']],
        [], ['[민감도 — 월 예상 청구액 기준]'], ['시나리오', '월 청구액', '기준 대비'],
    ] + [[x['label'], x['monthly'], f"{x['delta_pct']:+}%"] for x in r['sensitivity']]

    assum_rows = [['키', '값', '항목', '가정번호', '견적별 수정', '설명']]
    for c in CostParam.query.order_by(CostParam.sort_order).all():
        ov = overrides.get('param:' + c.key)
        assum_rows.append([c.key, params.get(c.key), c.label, c.assumption or '',
                           ov if ov is not None else '', c.description or ''])
    for k in sorted(overrides):
        if k.startswith('p:'):
            assum_rows.append([k, overrides[k], '물동 프로파일 수동 보정', '', '✔', ''])

    prof_rows = [['지표', '값'],
                 ['월평균 출고 BOX', s['monthly_box']], ['월평균 출고 PLT', s['monthly_plt']],
                 ['일평균 BOX', s['avg_day_box']], ['피크(P95) BOX', s['p95_day_box']],
                 ['피크배율', s['peak_ratio']], ['일평균 주문수', s['orders_per_day']],
                 ['주문당 BOX', s['box_per_order']], ['주문당 라인수', s['lines_per_order']],
                 ['PLT입수(가중평균)', s['boxes_per_plt']], ['PLT입수 커버리지(%)', s['ppb_coverage']],
                 ['평균재고 PLT', s['avg_stock_plt']], ['재고 근거', s['stock_source']],
                 ['입고 근거', s['inbound_source']],
                 [], ['권역 분포(%)', '']] + \
                [[k, v] for k, v in (s.get('region_share') or {}).items()] + \
                [[], ['경고', '']] + [[w, ''] for w in (s.get('warnings') or [])]

    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine='openpyxl') as w:
        pd.DataFrame(sum_rows).to_excel(w, index=False, header=False, sheet_name='견적 요약')
        pd.DataFrame(cost_rows).to_excel(w, index=False, header=False, sheet_name='원가 상세')
        pd.DataFrame(assum_rows).to_excel(w, index=False, header=False, sheet_name='적용 가정')
        pd.DataFrame(prof_rows).to_excel(w, index=False, header=False, sheet_name='물동 프로파일')
    buf.seek(0)
    fname = f"표준단가견적_{(q.customer_name or q.name)}.xlsx"
    return send_file(buf, download_name=fname, as_attachment=True,
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# ─── 마스터 관리 ─────────────────────────────────────────────────────────────

@app.route('/masters', methods=['GET'])
def masters():
    return render_template('masters.html',
                           params=CostParam.query.order_by(CostParam.sort_order).all(),
                           processes=WorkProcess.query.order_by(WorkProcess.sort_order).all(),
                           regions=RegionRate.query.order_by(RegionRate.sido).all(),
                           all_sido=pe.ALL_SIDO)


@app.route('/masters/params', methods=['POST'])
def masters_params():
    for c in CostParam.query.all():
        v = request.form.get('param:' + c.key)
        if v is not None and v.strip() != '':
            try:
                c.value = float(v)
            except ValueError:
                pass
    db.session.commit()
    flash('원가 파라미터를 저장했습니다.', 'success')
    return redirect(url_for('masters'))


@app.route('/masters/process/add', methods=['POST'])
def process_add():
    try:
        db.session.add(WorkProcess(
            name=request.form['name'].strip(),
            flow=request.form.get('flow', '출고'),
            unit=request.form.get('unit', 'BOX'),
            productivity=float(request.form['productivity']),
            worker_type=request.form.get('worker_type', '일용'),
            memo=request.form.get('memo', '').strip(),
            sort_order=int(request.form.get('sort_order') or 99),
        ))
        db.session.commit()
        flash('공정을 추가했습니다.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'추가 실패: {e}', 'danger')
    return redirect(url_for('masters'))


@app.route('/masters/process/<int:pid>/update', methods=['POST'])
def process_update(pid):
    p = WorkProcess.query.get_or_404(pid)
    try:
        p.name = request.form.get('name', p.name).strip()
        p.flow = request.form.get('flow', p.flow)
        p.unit = request.form.get('unit', p.unit)
        p.productivity = float(request.form.get('productivity') or p.productivity)
        p.worker_type = request.form.get('worker_type', p.worker_type)
        p.is_active = request.form.get('is_active') == 'on'
        db.session.commit()
        flash('공정을 수정했습니다.', 'success')
    except Exception as e:
        db.session.rollback()
        flash(f'수정 실패: {e}', 'danger')
    return redirect(url_for('masters'))


@app.route('/masters/process/<int:pid>/delete', methods=['POST'])
def process_delete(pid):
    db.session.delete(WorkProcess.query.get_or_404(pid))
    db.session.commit()
    flash('공정을 삭제했습니다.', 'warning')
    return redirect(url_for('masters'))


@app.route('/masters/regions', methods=['POST'])
def masters_regions():
    for sido in pe.ALL_SIDO:
        v = request.form.get('region:' + sido)
        if v is None:
            continue
        v = v.strip()
        row = RegionRate.query.get(sido)
        if v == '':
            if row:
                db.session.delete(row)
            continue
        try:
            rate = float(v)
        except ValueError:
            continue
        if row:
            row.cost_per_box = rate
        else:
            db.session.add(RegionRate(sido=sido, cost_per_box=rate))
    db.session.commit()
    flash('권역 단가표를 저장했습니다.', 'success')
    return redirect(url_for('masters'))


# ─── 설계 문서 ───────────────────────────────────────────────────────────────

@app.route('/design')
def design_doc():
    import markdown
    path = os.path.join(_BASE, 'docs', 'DESIGN.md')
    with open(path, encoding='utf-8') as f:
        html = markdown.markdown(f.read(), extensions=['tables', 'fenced_code'])
    return render_template('design.html', doc_html=html)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5001, debug=True)
