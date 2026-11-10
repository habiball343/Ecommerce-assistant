import re
from datetime import UTC, datetime
from uuid import uuid4


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex}"


def normalize_resource_id(resource_id: str) -> str:
    """统一订单、商品、售后编号的写法：去空格、转大写、连字符转下划线。"""
    return re.sub(r"[\s-]+", "_", resource_id.strip().upper())
