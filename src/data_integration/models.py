from typing import Literal

from pydantic import BaseModel, Field


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["file", "http", "webhook"] = "file"
    connection_settings: dict = Field(default_factory=dict)
    refresh_strategy: Literal["manual", "on_upload"] = "manual"


class ColumnRef(BaseModel):
    dataset_id: str
    column: str


class MappingField(BaseModel):
    inputs: list[ColumnRef] = Field(min_length=1)
    outputs: list[ColumnRef] = Field(min_length=1)
    operation: Literal["copy", "combine", "split"] = "copy"
    separator: str = " "


class MappingCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    fields: list[MappingField] = Field(min_length=1)


class SuggestRequest(BaseModel):
    left_dataset_id: str
    right_dataset_id: str
