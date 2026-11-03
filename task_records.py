"""Versioned read adapters. Research payloads are preserved, never migrated in place."""
from typing import Any

SCHEMA_VERSION = 2
COMMON_FIELDS = ["schema_version", "study_code", "participant_code", "condition_code", "run_id",
                 "run_status", "attempt_number", "instrument_version", "run_started_at", "run_completed_at"]
DD_FIELDS = COMMON_FIELDS + [
    "client_event_id", "task", "task_order", "block_name", "magnitude", "trial_in_block", "stimulus_index",
    "left_stimulus", "right_stimulus", "user_choice_type", "user_pressed_key", "response_method",
    "reaction_time_ms", "trial_started_iso", "response_iso", "server_received_at", "visibility_hidden",
    "stimulus_parameters_json", "adaptive_path_json", "record_json", "raw_payload_json",
]
UT_FIELDS = COMMON_FIELDS + [
    "round_id", "phase", "block_code", "block_order", "trial_in_block", "trial_sequence", "role",
    "opponent_condition", "opponent_actual_type", "round_status", "participant_share", "opponent_share",
    "participant_decision", "opponent_decision", "accepted", "points_earned", "potential_points_lost",
    "reaction_time_ms", "planned_wait_duration_ms", "wait_duration_ms", "decision_screen_shown_at",
    "responded_at", "server_received_at", "completed_at", "events_json", "model_json", "device_json",
    "record_json", "raw_payload_json",
]


def select(source: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: source.get(field) for field in fields}


def dd_record(context: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Group every analytical concept once; raw payload remains separately available."""
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": select(context, ("study_code", "participant_code", "condition_code", "run_id", "client_event_id")),
        "run": select(context, ("run_status", "attempt_number", "instrument_version", "run_started_at", "run_completed_at")),
        "design": select(payload, ("task", "task_order", "block_name", "magnitude", "trial_in_block")),
        "stimulus": {"index": payload.get("index"),
                     "left": payload.get("left_stimulus", payload.get("left_image")),
                     "right": payload.get("right_stimulus", payload.get("right_image")),
                     "parameters": select(payload, ("delay", "choice", "immED50", "delED50", "immK", "delK"))},
        "response": select(payload, ("user_choice_type", "user_pressed_key", "response_method")),
        "timing": {**select(payload, ("reaction_time_ms", "trial_started_iso", "response_iso")),
                   "server_received_at": context.get("server_received_at")},
        "adaptive_path": select(payload, ("requested_row", "displayed_row", "boundary_hit", "requested_next_row", "bounded_next_row")),
        "quality": select(payload, ("visibility_hidden",)),
    }


def ultimatum_record(context: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "identity": select(context, ("study_code", "participant_code", "condition_code", "run_id", "round_id")),
        "run": select(context, ("run_status", "attempt_number", "instrument_version", "run_started_at", "run_completed_at")),
        "design": select(context, ("phase", "block_code", "block_order", "trial_in_block", "trial_sequence",
                                   "role", "opponent_condition", "opponent_actual_type", "round_status")),
        "choice": select(context, ("participant_share", "opponent_share", "participant_decision", "opponent_decision")),
        "outcome": select(context, ("accepted", "points_earned", "potential_points_lost")),
        "timing": {**select(context, ("reaction_time_ms", "wait_duration_ms", "decision_screen_shown_at",
                                      "responded_at", "server_received_at", "completed_at")),
                   "planned_wait_duration_ms": payload.get("planned_wait_duration_ms"),
                   "events": payload.get("client_event_timestamps", {})},
        "model": {"offer": payload.get("offer_model"), "decision": payload.get("decision_model")},
        "device": {"at_prepare": payload.get("client_metadata", {}), "at_response": payload.get("client_metadata_resolve", {})},
        "quality": {"visibility_hidden": payload.get("client_metadata_resolve", {}).get("visibility_hidden")},
    }
