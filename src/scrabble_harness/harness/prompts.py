"""Deterministic public presentation, strict tool exposure, and bounded results.

The renderer accepts only DecisionView/EvidenceView and cannot inspect private
state or native metadata. ModelRequest.instructions stores the complete prompt;
its structured view/evidence remain host audit data, not a second prompt body.
Provider continuation is never rendered or truncated here.
"""

from __future__ import annotations

import json
from typing import Annotated, Literal, Self, TYPE_CHECKING

from pydantic import Field, TypeAdapter, model_validator

from ..domain import Board, BoardTile, ExchangeMove, ModelSettings, PlacementMove, PublicExchangeMove, canonical_json
from ..domain._base import DomainModel, Identifier, NonNegativeInt, ObservationId
from .contracts import DecisionLimits, DecisionView, EvidenceView, HarnessConfig, PresentationConfig
from .errors import ContextScopeError, PresentationVersionError, PromptSizeError
from .tool_schemas import TOOL_SCHEMA_VERSION, ToolDefinition, tool_definitions

if TYPE_CHECKING:
    from ..providers import ModelRequest, ProviderCapabilities, ProviderContinuation

PROMPT_VERSION = "scrabble-decision-v1"

# Pinned standard English 15x15 layout, checked against MAGPIE's standard15.txt.
STANDARD_PREMIUMS = tuple(tuple(row.split()) for row in (
    "TW .. .. DL .. .. .. TW .. .. .. DL .. .. TW",
    ".. DW .. .. .. TL .. .. .. TL .. .. .. DW ..",
    ".. .. DW .. .. .. DL .. DL .. .. .. DW .. ..",
    "DL .. .. DW .. .. .. DL .. .. .. DW .. .. DL",
    ".. .. .. .. DW .. .. .. .. .. DW .. .. .. ..",
    ".. TL .. .. .. TL .. .. .. TL .. .. .. TL ..",
    ".. .. DL .. .. .. DL .. DL .. .. .. DL .. ..",
    "TW .. .. DL .. .. .. DW .. .. .. DL .. .. TW",
    ".. .. DL .. .. .. DL .. DL .. .. .. DL .. ..",
    ".. TL .. .. .. TL .. .. .. TL .. .. .. TL ..",
    ".. .. .. .. DW .. .. .. .. .. DW .. .. .. ..",
    "DL .. .. DW .. .. .. DL .. .. .. DW .. .. DL",
    ".. .. DW .. .. .. DL .. DL .. .. .. DW .. ..",
    ".. DW .. .. .. TL .. .. .. TL .. .. .. DW ..",
    "TW .. .. DL .. .. .. TW .. .. .. DL .. .. TW",
))

_INSTRUCTIONS = (
    "You are playing two-player English Scrabble. Choose a strong move to maximize your chance of winning.\n"
    "Call exactly one offered tool per reply. Choose only a visible candidate handle; do not invent moves.\n"
    "select_move finishes this decision. compare_moves requests estimated analysis and requires a later selection.\n"
    "Cite only evidence IDs issued in this decision; otherwise use []. Keep brief_note at most 500 characters; "
    "an empty note is allowed. A short justification is enough.\n"
    "The board, own rack, scores, and public unseen inventory are available. Opponent tiles and draw order "
    "are unknown. Unseen counts combine the unknown opponent rack and bag; they are not bag-only counts.\n"
    "Candidates are displayed in seeded order. Handle numbers do not indicate move quality. Exact move scores "
    "are immediate scores, not predictions of final outcome. Comparison values are bounded estimates.\n"
    "Use only the information below. Respect the remaining limits and finish with select_move.\n"
)


def _bounded(text: str, maximum: int, label: str) -> str:
    if len(text.encode("utf-8")) > maximum:
        raise PromptSizeError(f"{label} exceeds its UTF-8 byte ceiling")
    return text


def _square(tile: BoardTile) -> str:
    return f"{chr(65 + tile.column)}{tile.row + 1}"


def _placements(tiles: tuple[BoardTile, ...]) -> str:
    return " ".join(f"{_square(tile)}={tile.displayed_letter.lower() if tile.is_blank else tile.displayed_letter}"
                    for tile in sorted(tiles, key=lambda tile: (tile.row, tile.column)))


def _rack(tiles: tuple[str, ...]) -> str:
    return "".join(sorted(tiles)) or "(empty)"


def render_board(board: Board) -> str:
    board = Board.model_validate(board)
    occupied = {(tile.row, tile.column): tile for tile in board.tiles}
    lines = ["    " + " ".join(f"{chr(65 + column):>2}" for column in range(15))]
    for row in range(15):
        cells = []
        for column in range(15):
            tile = occupied.get((row, column))
            cell = STANDARD_PREMIUMS[row][column] if tile is None else (
                tile.displayed_letter.lower() if tile.is_blank else tile.displayed_letter)
            cells.append(f"{cell:>2}")
        lines.append(f"{row + 1:>2}  " + " ".join(cells))
    return "\n".join(lines)


class RenderedPrompt(DomainModel):
    prompt_version: Literal["scrabble-decision-v1"] = PROMPT_VERSION
    tool_schema_version: Literal["scrabble-tools-v1"] = TOOL_SCHEMA_VERSION
    decision_id: Identifier
    observation_id: ObservationId
    instructions: Annotated[str, Field(min_length=1, max_length=16_384)]
    tools: Annotated[tuple[ToolDefinition, ...], Field(min_length=1, max_length=2)]
    force_selection: bool
    retained_history_turns: NonNegativeInt
    omitted_history_turns: NonNegativeInt
    max_request_bytes: Annotated[int, Field(ge=1024, le=16 * 1024 * 1024)]

    def _payload(self) -> dict:
        return {"instructions": self.instructions, "tools": [tool.as_function_tool() for tool in self.tools],
                "tool_choice": {"type": "function", "name": "select_move"} if self.force_selection else "required",
                "parallel_tool_calls": False}

    def as_payload(self) -> dict:
        """Revalidate before exporting a fresh SDK-independent function payload."""
        return type(self).model_validate(self)._payload()

    @model_validator(mode="after")
    def bounded_single_action(self) -> Self:
        names = tuple(tool.name for tool in self.tools)
        if names not in {("select_move",), ("select_move", "compare_moves")}:
            raise ValueError("presentation tools must offer selection first and comparison at most once")
        if self.force_selection != (names == ("select_move",)):
            raise ValueError("selection-only presentation must force selection")
        _bounded(json.dumps(self._payload(), ensure_ascii=False, separators=(",", ":"), allow_nan=False),
                 self.max_request_bytes, "rendered request")
        return self


def render_comparison_result(evidence: EvidenceView, *, presentation: PresentationConfig = PresentationConfig()) -> str:
    """Return complete canonical evidence or fail; never shorten analysis facts."""
    evidence = EvidenceView.model_validate(evidence)
    presentation = PresentationConfig.model_validate(presentation)
    return _bounded(canonical_json(evidence), presentation.max_tool_result_bytes, "comparison result")


def render_prompt(view: DecisionView, remaining_limits: DecisionLimits, *, config: HarnessConfig = HarnessConfig(),
                  evidence: tuple[EvidenceView, ...] = (), force_selection: bool = False) -> RenderedPrompt:
    view, limits, config = (DecisionView.model_validate(view), DecisionLimits.model_validate(remaining_limits),
                            HarnessConfig.model_validate(config))
    force_selection = TypeAdapter(bool, config={"strict": True}).validate_python(force_selection)
    evidence = TypeAdapter(Annotated[tuple[EvidenceView, ...], Field(max_length=1000)],
                           config={"strict": True}).validate_python(evidence)
    if config.prompt_version != PROMPT_VERSION or config.tool_schema_version != TOOL_SCHEMA_VERSION:
        raise PresentationVersionError("unsupported prompt or tool-schema version")
    if limits.max_model_attempts < 1:
        raise ValueError("no model-attempt slot remains")
    for name in ("max_model_attempts", "max_tool_steps", "max_engine_work_units", "max_api_cost_microusd", "max_elapsed_seconds"):
        if getattr(limits, name) > getattr(config.decision_limits, name):
            raise ValueError("remaining allowance exceeds configured decision limits")
    obs = view.observation
    if len(view.candidates) > config.candidate_config.display_count:
        raise ValueError("visible candidate count exceeds configured display ceiling")
    handles = {candidate.handle for candidate in view.candidates}
    if len({item.evidence_id for item in evidence}) != len(evidence):
        raise ValueError("evidence handles must be unique")
    for item in evidence:
        if item.decision_id != view.decision_id or item.observation_id != obs.observation_id:
            raise ContextScopeError("prompt evidence belongs to another decision or observation")
        if not set(item.requested_handles).issubset(handles):
            raise ValueError("prompt evidence contains invisible handles")
        tier = next((tier for tier in config.comparison_tiers if tier.name == item.tier), None)
        if (tier is None or item.limits.max_work_units > tier.max_work_units
                or item.limits.max_elapsed_seconds > tier.max_elapsed_seconds):
            raise ValueError("prompt evidence exceeds its configured comparison tier")
    can_compare = (not force_selection and limits.max_model_attempts >= 2 and limits.max_tool_steps >= 1
                   and limits.max_engine_work_units >= 2 and len(view.candidates) >= 2
                   and obs.bag_size >= 7 and obs.opponent_rack_size == 7)
    force_selection = not can_compare
    names = ("select_move", "compare_moves") if can_compare else ("select_move",)
    kept = obs.public_history[-config.presentation.max_history_turns:] if config.presentation.max_history_turns else ()
    omitted = len(obs.public_history) - len(kept)
    lines = [_INSTRUCTIONS.rstrip(), "", f"PROTOCOL: {PROMPT_VERSION}; {TOOL_SCHEMA_VERSION}; {view.presentation_version}",
             "", "POSITION", f"Decision: {json.dumps(view.decision_id)}; observation: {obs.observation_id}",
             f"Game: {json.dumps(obs.game_id)}; revision: {obs.revision}; player: {obs.player}; active player: {obs.active_player}",
             f"Ruleset: {json.dumps(obs.ruleset_id)}; lexicon: {json.dumps(obs.lexicon_id)}",
             f"Scores: you={obs.scores[obs.player]}, opponent={obs.scores[1 - obs.player]}; scoreless turns={obs.scoreless_turns}",
             f"Own rack ({len(obs.own_rack)}/7): {_rack(obs.own_rack)}",
             f"Bag size: {obs.bag_size}; opponent rack size: {obs.opponent_rack_size}",
             "Public unseen physical tiles (opponent rack + bag; missing symbols have count 0): " +
             ", ".join(f"{item.tile}:{item.count}" for item in sorted(obs.unseen_tile_counts.counts, key=lambda item: item.tile)),
             "", "BOARD", "Coordinates: columns A-O, rows 1-15; A1 = row 0, column 0.",
             "Uppercase letters are ordinary tiles; lowercase letters are assigned blanks worth 0 points; rack ? is an unassigned blank.",
             "Empty squares: .. normal, DL double letter, TL triple letter, DW double word, TW triple word; center H8 is DW.",
             "Premiums apply only to newly placed tiles. Occupied squares show letters; their premiums cannot be reused.",
             render_board(obs.board), "", "VISIBLE CANDIDATES (new tiles only; existing board letters may lie between them)"]
    for candidate in view.candidates:
        move = candidate.move
        action = ("place " + _placements(move.tiles) if isinstance(move, PlacementMove) else
                  "exchange " + _rack(move.tiles) if isinstance(move, ExchangeMove) else "pass")
        lines.append(f"{candidate.handle} | {action} | exact score={candidate.exact_score} | leave={_rack(candidate.leave)}")
    lines.extend(["", f"PUBLIC HISTORY (last {config.presentation.max_history_turns} of available retained turns; "
                  f"shown={len(kept)}, omitted={omitted})"])
    for turn in kept:
        move = turn.move
        action = ("place " + _placements(move.tiles) if isinstance(move, PlacementMove) else
                  f"exchange {move.tile_count} tiles" if isinstance(move, PublicExchangeMove) else "pass")
        lines.append(f"revision {turn.revision} | player {turn.player} | {action} | score={turn.score} | scores={turn.scores_after[0]},{turn.scores_after[1]}")
    if not kept:
        lines.append("(none shown)")
    lines.extend(["", "ISSUED COMPARISON EVIDENCE"])
    lines.extend(render_comparison_result(item, presentation=config.presentation) for item in evidence)
    if not evidence:
        lines.append("(none)")
    lines.extend(["", "REMAINING LIMITS", f"Model attempts including this request: {limits.max_model_attempts}",
        f"Comparison tool steps: {limits.max_tool_steps}; native work: {limits.max_engine_work_units} {limits.engine_work_unit}",
        f"API cost allowance: {limits.max_api_cost_microusd} microusd (1 USD = 1000000 microusd)",
        f"Decision time remaining: {limits.max_elapsed_seconds:g} seconds; finish reserve: {limits.finish_reserve_seconds:g} seconds",
        "", "AVAILABLE TOOLS"])
    if can_compare:
        max_candidates = min(4, len(view.candidates), limits.max_engine_work_units)
        lines.append(f"compare_moves: 2-{max_candidates} distinct visible handles; one offered tier; then select on a later request.")
        for tier in sorted(config.comparison_tiers, key=lambda tier: (tier.name != "light", tier.name)):
            lines.append(f"Tier {tier.name}: up to {min(tier.max_work_units, limits.max_engine_work_units)} total candidate_rollout; "
                f"up to {min(tier.max_elapsed_seconds, limits.max_elapsed_seconds - limits.finish_reserve_seconds):g} seconds.")
        lines.append("select_move: choose one visible handle now; comparison is optional.")
    else:
        lines.append("select_move only: comparison is unavailable for this request. Select a visible candidate now.")
    instructions = "\n".join(lines) + "\n"
    if len(instructions) > 16_384:
        raise PromptSizeError("rendered prompt exceeds the instructions character ceiling")
    return RenderedPrompt(decision_id=view.decision_id, observation_id=obs.observation_id,
        instructions=instructions, tools=tool_definitions(names, version=config.tool_schema_version),
        force_selection=force_selection, retained_history_turns=len(kept), omitted_history_turns=omitted,
        max_request_bytes=config.presentation.max_request_bytes)


def build_model_request(view: DecisionView, *, attempt_id: str, model: ModelSettings,
                        capabilities: ProviderCapabilities, remaining_limits: DecisionLimits,
                        config: HarnessConfig = HarnessConfig(), evidence: tuple[EvidenceView, ...] = (),
                        force_selection: bool = False, continuation: ProviderContinuation | None = None,
                        repair: bool = False) -> ModelRequest:
    """Construct a capability-checked host request with the complete public prompt.

    The record and neutral function payload are bounded separately. The later
    provider adapter must also bound its final serialized transport payload.
    """
    from ..providers import ModelRequest

    config = HarnessConfig.model_validate(config)
    prompt = render_prompt(view, remaining_limits, config=config, evidence=evidence, force_selection=force_selection)
    repair = TypeAdapter(bool, config={"strict": True}).validate_python(repair)
    instructions = prompt.instructions
    if repair:
        instructions = _bounded(instructions + "\nREPAIR: The previous response was unusable. Return exactly one select_move with a visible candidate handle and only displayed evidence IDs.", 16_384, "repair instructions")
    request = ModelRequest(attempt_id=attempt_id, model=model, capabilities=capabilities, view=view,
        remaining_limits=remaining_limits, instructions=instructions,
        prompt_version=prompt.prompt_version, tool_schema_version=prompt.tool_schema_version,
        tools=tuple(tool.name for tool in prompt.tools), force_selection=prompt.force_selection, evidence=evidence,
        continuation=continuation, max_request_bytes=config.presentation.max_request_bytes)
    envelope = prompt.as_payload() | {"instructions": instructions, "model_settings": request.model.model_dump(mode="json"),
        "continuation": None if request.continuation is None else request.continuation.model_dump(mode="json")}
    _bounded(json.dumps(envelope, ensure_ascii=False, separators=(",", ":"), allow_nan=False),
             request.max_request_bytes, "rendered request with continuation")
    return request
