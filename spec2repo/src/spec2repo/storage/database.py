from sqlalchemy import create_engine


def make_engine(database_url: str):
    return create_engine(
        database_url,
        pool_pre_ping=True,
        pool_recycle=1800,
        pool_size=5,
        max_overflow=5,
        pool_timeout=10,
        hide_parameters=True,
        connect_args={"connect_timeout": 5, "read_timeout": 10, "write_timeout": 10},
    )
