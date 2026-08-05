"""배송비 단가 시스템(delivery_pricing) 연동 브리지.

별도 프로세스로 실행되어 delivery_pricing의 앱 컨텍스트 안에서
메인 화면(analytics)과 동일한 집계 로직으로 고객사별 박스당 배송단가를 계산해
JSON 한 줄로 출력한다. (모듈명 충돌을 피하기 위해 subprocess로만 사용)

사용법:
    python delivery_bridge.py <delivery_pricing_dir> list
    python delivery_bridge.py <delivery_pricing_dir> summary <customer_id>
"""
import sys
import os
import json


def main():
    dp_dir = sys.argv[1]
    cmd = sys.argv[2]
    sys.path.insert(0, dp_dir)
    os.chdir(dp_dir)

    from app import app as dp_app, _hub_vehicle_daily_cost_both
    from models import db, Customer, CalculationResult, ShippingHistory, SystemConfig, OurCenter
    from calculator import compute_joint_breakdown_detail_both
    from sqlalchemy import func as sqlfunc

    with dp_app.app_context():
        if cmd == 'list':
            # 산정 이력이 있는 고객사만 노출
            counts = dict(db.session.query(
                CalculationResult.customer_id, sqlfunc.count(CalculationResult.id)
            ).group_by(CalculationResult.customer_id).all())
            out = [
                {'id': c.id, 'name': c.name, 'result_rows': counts.get(c.id, 0)}
                for c in Customer.query.order_by(Customer.name).all()
                if counts.get(c.id, 0) > 0
            ]
            print(json.dumps(out))
            return

        cid = int(sys.argv[3])
        customer = Customer.query.get(cid)
        if not customer:
            print(json.dumps({'error': f'고객사 id={cid} 없음'}))
            return

        # ── 물동 요약 (출고내역 기반 월평균) ────────────────────────────────
        volume = None
        vrows = db.session.query(
            sqlfunc.strftime('%Y-%m', ShippingHistory.shipping_date).label('ym'),
            sqlfunc.sum(ShippingHistory.box_qty).label('boxes'),
            sqlfunc.sum(ShippingHistory.plt_qty_decimal).label('plt'),
            sqlfunc.count(sqlfunc.distinct(ShippingHistory.shipping_date)).label('days'),
        ).filter(
            ShippingHistory.customer_id == cid,
            ShippingHistory.shipping_date.isnot(None),
        ).group_by('ym').all()
        if vrows:
            months = len(vrows)
            total_boxes = sum(float(r.boxes or 0) for r in vrows)
            total_plt = sum(float(r.plt or 0) for r in vrows)
            total_days = sum(int(r.days or 0) for r in vrows)
            if total_boxes > 0:
                volume = {
                    'months': months,
                    'period': f"{vrows[0].ym} ~ {vrows[-1].ym}" if months > 1 else vrows[0].ym,
                    'monthly_boxes': total_boxes / months,
                    'monthly_plt': total_plt / months,
                    'boxes_per_plt': (total_boxes / total_plt) if total_plt > 0 else 0,
                    'biz_days_per_month': total_days / months if months else 0,
                }

        # ── 배송단가 (최신 배치, analytics 화면과 동일 규칙) ─────────────────
        delivery = None
        _br = db.session.query(
            CalculationResult.batch_id,
            sqlfunc.max(CalculationResult.calc_date).label('max_date'),
        ).filter(
            CalculationResult.customer_id == cid,
            CalculationResult.cost_per_box.isnot(None),
        ).group_by(CalculationResult.batch_id).order_by(
            sqlfunc.max(CalculationResult.calc_date).desc()
        ).first()

        if _br:
            bid = _br.batch_id
            r = db.session.query(
                sqlfunc.count(CalculationResult.id).label('cnt'),
                sqlfunc.sum(CalculationResult.total_box_qty).label('boxes'),
                sqlfunc.sum(db.case((CalculationResult.delivery_mode == '직송', 1), else_=0)).label('direct_cnt'),
                sqlfunc.sum(db.case((CalculationResult.delivery_mode == '직송',
                                      CalculationResult.delivery_cost), else_=0)).label('direct_cost'),
            ).filter(
                CalculationResult.customer_id == cid,
                CalculationResult.batch_id == bid,
            ).first()

            boxes = int(r.boxes or 0)
            if boxes:
                direct_cost = int(r.direct_cost or 0)
                joint_cnt = r.cnt - int(r.direct_cnt or 0)

                tc_min = tc_max = hvc_min = hvc_max = 0
                if joint_cnt > 0:
                    _spv_cfg = SystemConfig.query.filter_by(key='stops_per_vehicle').first()
                    stops_per_vehicle = int(_spv_cfg.value) if _spv_cfg else 8
                    _main_ctr = OurCenter.query.filter_by(is_main_center=True)\
                        .order_by(OurCenter.sort_order).first()
                    _main_code = _main_ctr.center_code if _main_ctr else None
                    total_biz = db.session.query(
                        sqlfunc.count(sqlfunc.distinct(CalculationResult.shipping_date))
                    ).filter(
                        CalculationResult.customer_id == cid,
                        CalculationResult.batch_id == bid,
                        CalculationResult.shipping_date.isnot(None),
                    ).scalar() or 1
                    _jbd = compute_joint_breakdown_detail_both(
                        cid, _main_code, stops_per_vehicle, db.session, batch_id=bid)
                    tc_min = sum(i['total_cost'] for i in _jbd['min']['transfer'])
                    tc_max = sum(i['total_cost'] for i in _jbd['max']['transfer'])
                    _hvc_d_min, _hvc_d_max = _hub_vehicle_daily_cost_both(cid, stops_per_vehicle)
                    hvc_min = round(_hvc_d_min * total_biz)
                    hvc_max = round(_hvc_d_max * total_biz)

                total_min = direct_cost + tc_min + hvc_min
                total_max = direct_cost + tc_max + hvc_max
                delivery = {
                    'batch_id': bid,
                    'boxes': boxes,
                    'direct_cost': direct_cost,
                    'transfer_min': tc_min, 'transfer_max': tc_max,
                    'hub_min': hvc_min, 'hub_max': hvc_max,
                    'cpb_min': total_min / boxes,
                    'cpb_max': total_max / boxes,
                }

        print(json.dumps({
            'customer_id': cid,
            'customer_name': customer.name,
            'volume': volume,
            'delivery': delivery,
        }))


if __name__ == '__main__':
    main()
