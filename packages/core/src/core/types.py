from typing import Any, Generic, TypeVar
from pydantic import BaseModel

T = TypeVar("T")


class BaseResponse(BaseModel, Generic[T]):
    success: bool = True
    data: T | None = None
    message: str = ""


class ErrorResponse(BaseModel):
    success: bool = False
    error: str
    detail: Any = None
