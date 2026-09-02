from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class CostParam(db.Model):
    """원가 파라미터 마스터 (docs/DESIGN.md §4 가정 A1~A25의 기본값 저장소).

    견적별로 overrides_json 이 있으면 그 값이 우선한다.
    """
    __tablename__ = 'cost_param'
    key         = db.Column(db.String(50), primary_key=True)
    value       = db.Column(db.Float, nullable=False)
    label       = db.Column(db.String(120), nullable=False)
    unit        = db.Column(db.String(30))
    group       = db.Column(db.String(30))          # 인건비/물동/보관/배송/간접
    assumption  = db.Column(db.String(10))          # 대응 가정 번호 (A1 등)
    description = db.Column(db.String(300))
    sort_order  = db.Column(db.Integer, default=0)
    updated_at  = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class WorkProcess(db.Model):
    """작업 공정 마스터 (가정 A6). 생산성은 실측으로 교체하는 것이 전제."""
    __tablename__ = 'work_process'
    id            = db.Column(db.Integer, primary_key=True)
    name          = db.Column(db.String(100), nullable=False)
    flow          = db.Column(db.String(10), nullable=False, default='출고')   # 입고 / 출고
    unit          = db.Column(db.String(10), nullable=False, default='BOX')   # BOX / PLT
    productivity  = db.Column(db.Float, nullable=False)      # 단위/인·시
    worker_type   = db.Column(db.String(20), nullable=False, default='일용')  # 일용 / 지게차
    is_active     = db.Column(db.Boolean, default=True)
    memo          = db.Column(db.String(200))
    sort_order    = db.Column(db.Integer, default=0)


class RegionRate(db.Model):
    """배송비 모드② 권역(시도) 단가표 (가정 A19)."""
    __tablename__ = 'region_rate'
    sido         = db.Column(db.String(30), primary_key=True)
    cost_per_box = db.Column(db.Float, nullable=False)
    memo         = db.Column(db.String(200))


class Quote(db.Model):
    """견적 1건. 업로드 데이터의 집계(프로파일)와 산정 결과를 JSON으로 보관한다.

    원본 행을 저장하지 않고 업로드 시점에 집계만 남긴다(§3) — 12만행 파일도
    프로파일 JSON 은 수 KB 수준이라 재계산·재열람이 즉시 된다.
    """
    __tablename__ = 'quote'
    id             = db.Column(db.Integer, primary_key=True)
    name           = db.Column(db.String(200), nullable=False)
    customer_name  = db.Column(db.String(120))
    memo           = db.Column(db.String(500))
    created_at     = db.Column(db.DateTime, default=datetime.now)
    updated_at     = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    profile_json   = db.Column(db.Text)    # 물동 프로파일 (§3 추출값 + 업로드 메타)
    overrides_json = db.Column(db.Text)    # 파라미터/프로파일 수동 보정값 {key: value}
    result_json    = db.Column(db.Text)    # 최종 산정 결과 (engine.compute 출력)
