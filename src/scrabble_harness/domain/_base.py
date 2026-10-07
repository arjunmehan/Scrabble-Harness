"""Shared validation policy and scalar types for versioned domain records."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field


Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]
ObservationId = Annotated[str, Field(pattern=r"^obs_v1_[0-9a-f]{64}$")]
CandidateId = Annotated[str, Field(pattern=r"^cand_v1_[0-9a-f]{64}$")]
NonEmptyText = Annotated[str, Field(min_length=1, pattern=r"\S")]
NonNegativeInt = Annotated[int, Field(ge=0)]
PositiveInt = Annotated[int, Field(gt=0)]
PlayerIndex = Annotated[int, Field(ge=0, le=1)]
FiniteValue = Annotated[float, Field(allow_inf_nan=False)]
NonNegativeValue = Annotated[float, Field(ge=0, allow_inf_nan=False)]
PositiveValue = Annotated[float, Field(gt=0, allow_inf_nan=False)]
Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
EngineCommit = Annotated[str, Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")]


class DomainModel(BaseModel):
    """Immutable, strict records; unknown fields and unsupported versions fail.

    Nested collections use tuples, not mutable lists or dictionaries. Use
    model_validate_json for JSON inputs. model_construct and model_copy(update=)
    are Pydantic escape hatches and must not be used to validate external input.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        validate_default=True,
        revalidate_instances="always",
    )

    schema_version: Annotated[int, Field(ge=1, le=1)] = 1
