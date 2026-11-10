from sqlalchemy import text

from ecommerce_service import models  # noqa: F401
from ecommerce_service.database import Base, SessionLocal, engine
from ecommerce_service.seed import seed_demo_data


def reset_data() -> None:
    if engine.dialect.name == "postgresql":
        with engine.begin() as connection:
            connection.execute(text("DROP SCHEMA public CASCADE"))
            connection.execute(text("CREATE SCHEMA public"))
    else:
        Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with SessionLocal() as db:
        seed_demo_data(db)


if __name__ == "__main__":
    reset_data()
    print("E-commerce service data reset completed.")
