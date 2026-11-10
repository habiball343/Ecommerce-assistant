from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ecommerce_service.common.auth import (
    Principal,
    create_access_token,
    current_principal,
    require_roles,
)
from ecommerce_service.common.config import get_settings
from ecommerce_service.database import Base, SessionLocal, engine, get_db
from ecommerce_service.models import AfterSale, IdempotencyRecord, Order, Product, User
from ecommerce_service.schemas import AddressUpdate, AfterSaleCreate, DemoLoginRequest, money
from ecommerce_service.seed import seed_demo_data

ORDER_STATUS_LABELS = {
    "paid": "已支付",
    "pending_shipment": "待发货",
    "shipped": "已发货",
    "completed": "已完成",
    "cancelled": "已取消",
    "refunded": "已退款",
}


def iso_utc(value: datetime) -> str:
    """SQLite 不保存时区信息，这里统一补回 UTC，保证前端按正确时区展示。"""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def cancellation_policy(order: Order) -> tuple[bool, str]:
    if order.status in {"paid", "pending_shipment"}:
        return True, ""
    if order.status == "shipped":
        return False, "订单已发货，不能直接取消；可以签收后申请退货。"
    if order.status == "cancelled":
        return False, "订单已经取消。"
    return False, "订单当前状态不能直接取消。"


def product_payload(product: Product) -> dict:
    return {
        "id": product.id,
        "name": product.name,
        "category": product.category,
        "price": money(product.price),
        "stock": product.stock,
        "status": product.status,
        "description": product.description,
        "specs": product.specs,
    }


def order_payload(order: Order) -> dict:
    return {
        "id": order.id,
        "user_id": order.user_id,
        "status": order.status,
        "status_label": ORDER_STATUS_LABELS.get(order.status, order.status),
        "total_amount": money(order.total_amount),
        "address": order.address,
        "created_at": iso_utc(order.created_at),
        "items": [
            {
                "product_id": item.product_id,
                "product_name": item.product_name,
                "quantity": item.quantity,
                "unit_price": money(item.unit_price),
            }
            for item in order.items
        ],
    }


def tool_result(
    success: bool,
    code: str,
    message: str,
    data: dict | list | None = None,
    retryable: bool = False,
):
    return {
        "success": success,
        "code": code,
        "message": message,
        "data": data,
        "retryable": retryable,
    }


def cached_operation(
    db: Session,
    key: str | None,
    principal: Principal,
    operation: str,
) -> dict | None:
    if not key:
        return None
    record = db.get(IdempotencyRecord, key)
    if not record:
        return None
    if record.user_id != principal.user_id or record.operation != operation:
        raise HTTPException(
            status_code=409, detail="Idempotency key was used for another operation"
        )
    return record.response


def store_operation(
    db: Session,
    key: str | None,
    principal: Principal,
    operation: str,
    response: dict,
) -> dict:
    if key:
        db.add(
            IdempotencyRecord(
                key=key,
                user_id=principal.user_id,
                operation=operation,
                response=response,
            )
        )
    db.commit()
    return response


def owned_order(db: Session, order_id: str, principal: Principal) -> Order:
    order = db.get(Order, order_id)
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if principal.role == "user" and order.user_id != principal.user_id:
        raise HTTPException(status_code=403, detail="Order does not belong to current user")
    return order


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_demo_data(db)
    yield


app = FastAPI(title="E-commerce Service", version="1.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=get_settings().cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"status": "ok", "service": "ecommerce-service"}


@app.post("/api/v1/auth/demo-token")
def demo_token(request: DemoLoginRequest, db: Annotated[Session, Depends(get_db)]):
    user = db.get(User, request.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="Demo user not found")
    expected_role = (
        "admin"
        if user.id.startswith("admin_")
        else "staff"
        if user.id.startswith("agent_")
        else "user"
    )
    if request.role != expected_role:
        raise HTTPException(status_code=403, detail="Role does not match demo account")
    principal = Principal(user_id=user.id, role=request.role)
    return {
        "access_token": create_access_token(principal),
        "token_type": "bearer",
        "principal": principal.model_dump(),
        "display_name": user.name,
    }


@app.get("/api/v1/demo/users")
def demo_users(db: Annotated[Session, Depends(get_db)]):
    users = db.scalars(select(User)).all()
    return [
        {"id": user.id, "name": user.name, "membership": user.membership}
        for user in users
        if not user.id.startswith(("agent_", "admin_"))
    ]


@app.get("/api/v1/products")
def search_products(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[Principal, Depends(current_principal)],
    query: str = Query(default="", max_length=100),
):
    statement = select(Product).where(Product.status == "active")
    if query:
        like = f"%{query}%"
        statement = statement.where(
            or_(
                Product.name.ilike(like),
                Product.category.ilike(like),
                Product.description.ilike(like),
            )
        )
    products = db.scalars(statement.order_by(Product.id).limit(20)).all()
    return tool_result(
        True, "OK", f"找到 {len(products)} 个商品", [product_payload(p) for p in products]
    )


@app.get("/api/v1/products/{product_id}")
def get_product(
    product_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[Principal, Depends(current_principal)],
):
    product = db.get(Product, product_id)
    if not product:
        return tool_result(False, "PRODUCT_NOT_FOUND", "商品不存在")
    return tool_result(True, "OK", "商品查询成功", product_payload(product))


@app.get("/api/v1/products/{product_id}/stock")
def get_product_stock(
    product_id: str,
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[Principal, Depends(current_principal)],
):
    product = db.get(Product, product_id)
    if not product:
        return tool_result(False, "PRODUCT_NOT_FOUND", "商品不存在")
    return tool_result(
        True,
        "OK",
        "库存查询成功",
        {"product_id": product.id, "stock": product.stock, "status": product.status},
    )


@app.get("/api/v1/orders")
def list_orders(
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
):
    statement = select(Order)
    if principal.role == "user":
        statement = statement.where(Order.user_id == principal.user_id)
    orders = db.scalars(statement.order_by(Order.created_at.desc())).unique().all()
    return tool_result(True, "OK", f"找到 {len(orders)} 个订单", [order_payload(o) for o in orders])


@app.get("/api/v1/orders/{order_id}")
def get_order(
    order_id: str,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
):
    try:
        order = owned_order(db, order_id, principal)
    except HTTPException as exc:
        code = "ORDER_FORBIDDEN" if exc.status_code == 403 else "ORDER_NOT_FOUND"
        return tool_result(False, code, str(exc.detail))
    return tool_result(True, "OK", "订单查询成功", order_payload(order))


@app.get("/api/v1/orders/{order_id}/logistics")
def get_logistics(
    order_id: str,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
):
    try:
        order = owned_order(db, order_id, principal)
    except HTTPException as exc:
        return tool_result(False, "ORDER_NOT_FOUND", str(exc.detail))
    if not order.logistics:
        return tool_result(False, "LOGISTICS_NOT_AVAILABLE", "订单尚未产生物流信息")
    item = order.logistics
    return tool_result(
        True,
        "OK",
        "物流查询成功",
        {
            "order_id": order.id,
            "company": item.company,
            "tracking_number": item.tracking_number,
            "status": item.status,
            "latest_event": item.latest_event,
            "events": item.events,
        },
    )


@app.post("/api/v1/orders/{order_id}/cancel")
def cancel_order(
    order_id: str,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if cached := cached_operation(db, idempotency_key, principal, "cancel_order"):
        return cached
    try:
        order = owned_order(db, order_id, principal)
    except HTTPException as exc:
        return tool_result(False, "ORDER_NOT_FOUND", str(exc.detail))
    if order.status == "cancelled":
        result = tool_result(True, "ALREADY_CANCELLED", "订单已经取消", order_payload(order))
        return store_operation(db, idempotency_key, principal, "cancel_order", result)
    can_cancel, cancel_reason = cancellation_policy(order)
    if not can_cancel:
        return tool_result(False, "ORDER_NOT_CANCELLABLE", cancel_reason)
    order.status = "cancelled"
    result = tool_result(True, "OK", "订单取消成功", order_payload(order))
    return store_operation(db, idempotency_key, principal, "cancel_order", result)


@app.patch("/api/v1/orders/{order_id}/address")
def update_address(
    order_id: str,
    request: AddressUpdate,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if cached := cached_operation(db, idempotency_key, principal, "update_shipping_address"):
        return cached
    try:
        order = owned_order(db, order_id, principal)
    except HTTPException as exc:
        return tool_result(False, "ORDER_NOT_FOUND", str(exc.detail))
    if order.status not in {"paid", "pending_shipment"}:
        return tool_result(False, "ADDRESS_NOT_EDITABLE", "订单当前状态不能修改地址")
    order.address = request.address
    result = tool_result(True, "OK", "收货地址修改成功", order_payload(order))
    return store_operation(db, idempotency_key, principal, "update_shipping_address", result)


@app.post("/api/v1/orders/{order_id}/after-sales")
def create_after_sale(
    order_id: str,
    request: AfterSaleCreate,
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
):
    if cached := cached_operation(db, idempotency_key, principal, "create_after_sale"):
        return cached
    try:
        order = owned_order(db, order_id, principal)
    except HTTPException as exc:
        return tool_result(False, "ORDER_NOT_FOUND", str(exc.detail))
    if order.status in {"cancelled", "refunded"}:
        return tool_result(False, "AFTER_SALE_NOT_ALLOWED", "该订单不能发起售后")
    existing = db.scalar(
        select(AfterSale).where(
            AfterSale.order_id == order.id,
            AfterSale.status.in_(["pending", "approved", "processing"]),
        )
    )
    if existing:
        result = tool_result(
            True,
            "ALREADY_EXISTS",
            "该订单已有进行中的售后申请",
            {"after_sale_id": existing.id, "status": existing.status},
        )
        return store_operation(db, idempotency_key, principal, "create_after_sale", result)
    record = AfterSale(
        id=f"AS_{uuid4().hex[:10].upper()}",
        user_id=order.user_id,
        order_id=order.id,
        kind=request.kind,
        reason=request.reason,
    )
    db.add(record)
    result = tool_result(
        True,
        "OK",
        "售后申请创建成功",
        {"after_sale_id": record.id, "status": record.status},
    )
    return store_operation(db, idempotency_key, principal, "create_after_sale", result)


@app.get("/api/v1/after-sales")
def list_after_sales(
    db: Annotated[Session, Depends(get_db)],
    principal: Annotated[Principal, Depends(current_principal)],
    order_id: str | None = None,
):
    statement = select(AfterSale)
    if principal.role == "user":
        statement = statement.where(AfterSale.user_id == principal.user_id)
    if order_id:
        statement = statement.where(AfterSale.order_id == order_id)
    records = db.scalars(statement.order_by(AfterSale.created_at.desc())).all()
    return tool_result(
        True,
        "OK",
        f"找到 {len(records)} 条售后记录",
        [
            {
                "id": item.id,
                "order_id": item.order_id,
                "kind": item.kind,
                "reason": item.reason,
                "status": item.status,
                "created_at": iso_utc(item.created_at),
            }
            for item in records
        ],
    )


@app.get("/api/v1/admin/summary")
def admin_summary(
    db: Annotated[Session, Depends(get_db)],
    _: Annotated[Principal, Depends(require_roles("staff", "admin"))],
):
    return {
        "products": len(db.scalars(select(Product)).all()),
        "orders": len(db.scalars(select(Order)).all()),
        "after_sales": len(db.scalars(select(AfterSale)).all()),
        "generated_at": datetime.now(UTC).isoformat(),
    }
