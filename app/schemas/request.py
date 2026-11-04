"""Request schema for POST /query.

Only `query` is required. The optional fields are "hard" filters: when given,
they always override whatever the planner (LLM) reads from the query text.
"""

from datetime import date

from pydantic import BaseModel, Field, field_validator, model_validator

from .common import OverallStatus, Phase

MIN_YEAR = 1990
MAX_YEAR = date.today().year + 10  # planned trials can start in the future


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=3, max_length=500,
                       description="Natural-language question about clinical trials.")

    drug_name: str | None = Field(None, max_length=100)
    condition: str | None = Field(None, max_length=100)
    sponsor: str | None = Field(None, max_length=100)
    country: str | None = Field(None, max_length=60)
    trial_phase: list[Phase] | None = None
    status: list[OverallStatus] | None = None
    start_year: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR)
    end_year: int | None = Field(None, ge=MIN_YEAR, le=MAX_YEAR)

    max_trials: int = Field(10000, ge=1, le=20000,
                            description="Cap on trials fetched from the API.")

    @field_validator("query", "drug_name", "condition", "sponsor", "country", mode="before")
    @classmethod
    def strip_and_blank_to_none(cls, v):
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @model_validator(mode="after")
    def check_year_range(self):
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must be <= end_year")
        return self

    model_config = {
        "json_schema_extra": {
            "examples": [{
                "query": "How has the number of trials for this drug changed over time?",
                "drug_name": "Pembrolizumab",
            }]
        }
    }