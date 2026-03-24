from datetime import UTC, datetime

import sqlalchemy as sa
from sqlmodel import Field, SQLModel

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "pk": "pk_%(table_name)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}

sql_model_metadata = SQLModel.metadata
sql_model_metadata.naming_convention = NAMING_CONVENTION


class BaseModel(SQLModel):
    __abstract__ = True

    created_at: datetime | None = Field(  # type: ignore
        nullable=True,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={"server_default": sa.text("(now())")},
    )

    modified_at: datetime | None = Field(  # type: ignore
        nullable=True,
        sa_type=sa.DateTime(timezone=True),
        sa_column_kwargs={
            "server_default": sa.text("(now())"),
            "onupdate": sa.text("(now())"),
        },
    )

    deleted_at: datetime | None = Field(  # type: ignore
        default=None,
        nullable=True,
        sa_type=sa.DateTime(timezone=True),
    )


@sa.event.listens_for(BaseModel, "before_update", propagate=True)
def refresh(_: sa.orm.Mapper, __: sa.engine.Connection, target: BaseModel) -> None:
    target.modified_at = datetime.now(UTC)
