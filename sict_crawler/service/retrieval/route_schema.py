"""Hợp đồng dữ liệu không tin cậy do LLM định tuyến trả về."""
from __future__ import annotations

import json
from typing import Literal

from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError


class RouteDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    media: Literal["none", "optional", "required"]
    wants_file: StrictBool
    oos: StrictBool
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False, strict=True)

    @classmethod
    def from_llm(cls, text: str) -> RouteDecision | None:
        try:
            obj = json.JSONDecoder().raw_decode(text[text.index("{"):])[0]
            # raw_decode buộc JSON hoàn chỉnh; parser LangChain kiểm tra Pydantic schema.
            return PydanticOutputParser(pydantic_object=cls).parse(json.dumps(obj))
        except (ValueError, TypeError, ValidationError, OutputParserException):
            return None
