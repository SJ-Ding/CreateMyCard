"""模型内部合同；不扩展端侧协议或运行数据。"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

Scalar = StrictStr | StrictBool | StrictInt | StrictFloat
Operator = Literal[
    "ref",
    "literal",
    "+",
    "-",
    "*",
    "/",
    "%",
    "==",
    "!=",
    "<",
    ">",
    "<=",
    ">=",
    "&&",
    "||",
    "!",
    "size",
]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Expression(Contract):
    op: Operator
    value: Scalar | None = None
    args: list[Expression] = Field(default_factory=list, max_length=2)


class Outcome(Contract):
    theme: str | None = None
    texts: dict[str, Expression] = Field(default_factory=dict)


class State(Outcome):
    when: Expression
    priority: Literal["critical", "warning", "milestone", "success", "normal"] = "normal"


class PresentationRule(Contract):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    origin: Literal["user", "model"]
    requirement: Literal["required", "optional"]
    scope: Literal["card", "local"]
    local_targets: list[str] = Field(default_factory=list, max_length=32)
    states: list[State] = Field(min_length=1, max_length=8)
    default: Outcome
    unknown: Outcome


class PresentationPlan(Contract):
    version: Literal["presentation-v1"] = "presentation-v1"
    rules: list[PresentationRule] = Field(default_factory=list, max_length=8)


class PropertyTarget(Contract):
    component_id: str
    property: str
    role: str
    source_id: str


class PresentationArchive(Contract):
    version: Literal["presentation-v1"] = "presentation-v1"
    plan: PresentationPlan
    layout: str
    targets: list[PropertyTarget]
    compiled_digest: str
    base_theme: str | None = None
    color_overrides: dict[str, dict[str, str]] = Field(default_factory=dict)


class PresentationError(ValueError):
    """可回注现有修复链路的具名展示诊断。"""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")
