from sqlalchemy import Column, String, Integer, Float, DateTime, Text
from sqlalchemy.sql import func
from database import Base

class Order(Base):
    __tablename__ = "orders"

    id          = Column(Integer, primary_key=True, index=True)
    order_code = Column(String, index=True)
    sync_id    = Column(String, index=True)
    shop_id     = Column(String, index=True)
    shop_name   = Column(String)
    buyer_name  = Column(String)
    customer_name = Column(String)
    phone       = Column(String)
    address     = Column(Text)
    product     = Column(Text)
    quantity    = Column(Integer, default=0)
    total       = Column(Float, default=0)
    status      = Column(String)
    order_date  = Column(String)
    raw_data    = Column(Text)   # JSON toàn bộ row gốc
    fetched_at  = Column(DateTime, server_default=func.now())

class ShopMeta(Base):
    __tablename__ = "shop_meta"

    shop_id     = Column(String, primary_key=True)
    shop_name   = Column(String)
    shop_url    = Column(String)
    last_sync   = Column(DateTime)
    order_count = Column(Integer, default=0)


class ExternalOrderTrackingHoang(Base):
    __tablename__ = "external_order_tracking_hoang"

    order_code = Column(String, primary_key=True)
    cod_amount = Column(Integer, default=0)
    status = Column(String, default="unknown")
    is_paid = Column(Integer, default=0)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class ExternalOrderConfigHoang(Base):
    __tablename__ = "external_order_config_hoang"

    id = Column(Integer, primary_key=True, default=1)
    fee_items_json = Column(Text)
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


class TakenOrder(Base):
    __tablename__ = "taken_orders"

    id = Column(Integer, primary_key=True, index=True)
    order_code = Column(String, unique=True, index=True)
    lookup_order_code = Column(String, index=True)
    shop_name = Column(String)
    order_date = Column(String)
    customer_name = Column(String)
    phone = Column(String)
    address = Column(Text)
    product = Column(Text)
    quantity = Column(Integer, default=0)
    prepaid_amount = Column(String)
    payment_status = Column(String)
    take_status = Column(String, default="waiting_waybill")
    taken_by = Column(String)
    taken_at = Column(DateTime, server_default=func.now())
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())
