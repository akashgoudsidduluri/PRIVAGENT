"""Evidence-only action receipts. Missing evidence remains unknown, never inferred."""
from dataclasses import dataclass
from typing import Literal

State = Literal['passed', 'failed', 'unknown']

@dataclass(frozen=True)
class ActionEvidence:
    target_confidence: float | None = None
    dispatch_acknowledged: bool | None = None
    surface_changed: bool | None = None
    transport_status: int | None = None
    authoritative_confirmation: bool | None = None
    goal_aligned: bool | None = None


def verify_layers(evidence: ActionEvidence, *, mutating: bool = False) -> dict:
    def state(value):
        return 'unknown' if value is None else ('passed' if value else 'failed')
    layers = {
        'target': state(None if evidence.target_confidence is None else .85 <= evidence.target_confidence <= 1),
        'dispatch': state(evidence.dispatch_acknowledged),
        'surface': state(evidence.surface_changed),
        'transport': state(None if evidence.transport_status is None else 200 <= evidence.transport_status < 400),
        'authority': state(evidence.authoritative_confirmation),
        'goal': state(evidence.goal_aligned),
    }
    required = ['target', 'dispatch', 'goal'] + (['surface', 'authority'] if mutating else [])
    verified = all(layers[key] == 'passed' for key in required) and 'failed' not in layers.values()
    return {'layers': layers, 'verified': verified,
            'state': 'completed' if verified else 'unverified'}
