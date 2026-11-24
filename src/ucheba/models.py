from pydantic import BaseModel, Field


class Segment(BaseModel):
    start: float = Field(ge=0, allow_inf_nan=False)
    end: float = Field(ge=0, allow_inf_nan=False)
    text: str = Field(min_length=1)
    speaker: str | None = None


class Assignment(BaseModel):
    title: str
    requirements: str
    deadline: str | None = None
    plan: list[str] = Field(default_factory=list)
    quote: str
    speaker: str | None = None
    start: float
    end: float


class Analysis(BaseModel):
    summary: list[str] = Field(default_factory=list)
    details: list[str] = Field(default_factory=list)
    assignments: list[Assignment] = Field(default_factory=list)
    model: str = ""
    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
