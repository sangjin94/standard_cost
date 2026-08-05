from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()


class SystemConfig(db.Model):
    """시스템 설정 (key-value)"""
    __tablename__ = 'system_config'
    key = db.Column(db.String(50), primary_key=True)
    value = db.Column(db.String(500), nullable=False)
    description = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class WorkCostProcess(db.Model):
    """작업비 공정 마스터: 공정별 생산성 × 시급 → 단위당 작업비
    단위당 작업비 = hourly_wage ÷ productivity_per_hour (unit 기준)
    """
    __tablename__ = 'work_cost_process'
    id = db.Column(db.Integer, primary_key=True)
    process_name = db.Column(db.String(50), nullable=False, unique=True)   # 입고하차, 피킹 등
    unit = db.Column(db.String(10), nullable=False, default='BOX')         # BOX / PLT
    productivity_per_hour = db.Column(db.Float, nullable=False)            # 1인 시간당 처리량
    hourly_wage = db.Column(db.Integer, nullable=False, default=12000)     # 시급 (4대보험 포함)
    is_active = db.Column(db.Boolean, default=True)
    sort_order = db.Column(db.Integer, default=0)
    memo = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)


class StorageCenter(db.Model):
    """보관비 센터 마스터: 센터 월 원가 ÷ 유효 CAPA(×목표가동률) → PLT/월 보관단가"""
    __tablename__ = 'storage_center'
    id = db.Column(db.Integer, primary_key=True)
    center_name = db.Column(db.String(100), nullable=False, unique=True)
    monthly_rent = db.Column(db.Integer, nullable=False, default=0)        # 월 임차료(자가는 감가상각)
    monthly_mgmt = db.Column(db.Integer, nullable=False, default=0)        # 월 관리비·수도광열·보험·설비감가
    effective_plt_capa = db.Column(db.Integer, nullable=False)             # 유효 보관 PLT 수 (통로 제외)
    target_occupancy = db.Column(db.Float, nullable=False, default=85.0)   # 목표 가동률 (%)
    memo = db.Column(db.String(200))
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    @property
    def plt_month_rate(self):
        """PLT당 월 보관단가 (원). 가동률 100%가 아닌 목표가동률로 나눠 공실을 반영."""
        denom = self.effective_plt_capa * (self.target_occupancy / 100.0)
        return (self.monthly_rent + self.monthly_mgmt) / denom if denom > 0 else 0


class StandardQuote(db.Model):
    """표준물류단가 종합 견적 저장본"""
    __tablename__ = 'standard_quote'
    id = db.Column(db.Integer, primary_key=True)
    quote_name = db.Column(db.String(200), nullable=False)
    customer_name = db.Column(db.String(100))              # 화주사명
    center_name = db.Column(db.String(100))                # 보관 센터
    monthly_boxes = db.Column(db.Float)                    # 월평균 출고 BOX
    boxes_per_plt = db.Column(db.Float)                    # BOX/PLT 환산비
    biz_days = db.Column(db.Float)                         # 월 영업일수
    turnover_days = db.Column(db.Float)                    # 재고회전일수 가정
    avg_stock_plt = db.Column(db.Float)                    # 평균 재고 PLT
    work_cpb = db.Column(db.Float)                         # 작업비/박스 (간접배부 포함)
    delivery_cpb = db.Column(db.Float)                     # 물류비(운송)/박스
    delivery_source = db.Column(db.String(100))            # 물류비 출처 (연동/직접입력/차량원가)
    storage_cpb = db.Column(db.Float)                      # 보관비/박스
    work_overhead_rate = db.Column(db.Float)               # 작업 간접배부율 (%)
    admin_rate = db.Column(db.Float)                       # 일반관리비율 (%)
    margin_rate = db.Column(db.Float)                      # 목표이익률 (%)
    final_cpb = db.Column(db.Float)                        # 최종 견적단가/박스
    final_cpp = db.Column(db.Float)                        # 최종 견적단가/PLT
    memo = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.now)
