from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ecommerce_service.models import Logistics, Order, OrderItem, Product, User


def seed_demo_data(db: Session) -> None:
    if db.scalar(select(User.id).limit(1)):
        return
    users = [
        User(id="user_001", name="李明", membership="金牌会员"),
        User(id="user_002", name="王芳", membership="普通会员"),
        User(id="agent_001", name="客服小周", membership="员工"),
        User(id="admin_001", name="系统管理员", membership="员工"),
    ]
    products = [
        Product(
            id="PRODUCT_001",
            name="Aurora Pro 降噪耳机",
            category="数码音频",
            price=Decimal("899.00"),
            stock=26,
            description="头戴式主动降噪蓝牙耳机，续航 40 小时，支持双设备连接。",
            specs={"颜色": "曜石黑", "续航": "40小时", "降噪": "自适应主动降噪"},
        ),
        Product(
            id="PRODUCT_002",
            name="Aurora Lite 真无线耳机",
            category="数码音频",
            price=Decimal("399.00"),
            stock=0,
            description="轻量真无线耳机，单次续航 8 小时，支持通话降噪。",
            specs={"颜色": "云雾白", "续航": "8小时", "防水": "IPX4"},
        ),
        Product(
            id="PRODUCT_003",
            name="Trail X1 智能运动手表",
            category="智能穿戴",
            price=Decimal("1299.00"),
            stock=18,
            description="支持双频 GPS、心率血氧监测和 5ATM 防水。",
            specs={"屏幕": "1.43英寸", "续航": "14天", "防水": "5ATM"},
        ),
        Product(
            id="PRODUCT_004",
            name="Breeze 机械键盘",
            category="电脑外设",
            price=Decimal("529.00"),
            stock=42,
            description="87 键热插拔机械键盘，支持有线、蓝牙和 2.4G 连接。",
            specs={"轴体": "线性轴", "配列": "87键", "连接": "三模"},
        ),
        Product(
            id="PRODUCT_005",
            name="Flow 人体工学鼠标",
            category="电脑外设",
            price=Decimal("269.00"),
            stock=33,
            description="右手人体工学无线鼠标，静音按键，可连接三台设备。",
            specs={"DPI": "4000", "连接": "蓝牙/2.4G", "重量": "96g"},
        ),
    ]
    db.add_all(users + products)
    db.flush()
    orders = [
        Order(
            id="ORDER_001",
            user_id="user_001",
            status="pending_shipment",
            total_amount=Decimal("899.00"),
            address="上海市浦东新区世纪大道 100 号",
            items=[
                OrderItem(
                    product_id="PRODUCT_001",
                    product_name="Aurora Pro 降噪耳机",
                    quantity=1,
                    unit_price=Decimal("899.00"),
                )
            ],
        ),
        Order(
            id="ORDER_002",
            user_id="user_001",
            status="shipped",
            total_amount=Decimal("399.00"),
            address="上海市浦东新区世纪大道 100 号",
            items=[
                OrderItem(
                    product_id="PRODUCT_002",
                    product_name="Aurora Lite 真无线耳机",
                    quantity=1,
                    unit_price=Decimal("399.00"),
                )
            ],
            logistics=Logistics(
                company="顺丰速运",
                tracking_number="SF1234567890",
                status="in_transit",
                latest_event="包裹已到达上海浦东分拣中心",
                events=[
                    {"time": "2026-07-28 09:10", "description": "快件已揽收"},
                    {"time": "2026-07-29 08:30", "description": "到达上海浦东分拣中心"},
                ],
            ),
        ),
        Order(
            id="ORDER_003",
            user_id="user_001",
            status="completed",
            total_amount=Decimal("529.00"),
            address="上海市浦东新区世纪大道 100 号",
            items=[
                OrderItem(
                    product_id="PRODUCT_004",
                    product_name="Breeze 机械键盘",
                    quantity=1,
                    unit_price=Decimal("529.00"),
                )
            ],
            logistics=Logistics(
                company="京东物流",
                tracking_number="JD9876543210",
                status="delivered",
                latest_event="本人签收",
                events=[{"time": "2026-07-20 16:20", "description": "本人签收"}],
            ),
        ),
        Order(
            id="ORDER_004",
            user_id="user_002",
            status="paid",
            total_amount=Decimal("1299.00"),
            address="北京市海淀区中关村大街 10 号",
            items=[
                OrderItem(
                    product_id="PRODUCT_003",
                    product_name="Trail X1 智能运动手表",
                    quantity=1,
                    unit_price=Decimal("1299.00"),
                )
            ],
        ),
    ]
    db.add_all(orders)
    db.commit()
