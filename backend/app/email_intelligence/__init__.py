"""Autonomous Email Intelligence Package for PrivAgent."""
from .models import (
    EmailItem,
    EmailClassificationResult,
    CleanupPolicy,
    NotificationPolicy,
    EmailMissionSetupInput,
)
from .classifier import classify_email
from .cleanup_executor import evaluate_and_execute_cleanup, restore_email_from_trash
from .neglect_detector import evaluate_email_neglect
from .sync_engine import run_email_mission_tick, get_demo_mailbox
from .companion_queries import is_email_companion_query, answer_email_companion_query
from .controlled_mailbox import ControlledTestMailbox, TEST_DATASET

__all__ = [
    "EmailItem",
    "EmailClassificationResult",
    "CleanupPolicy",
    "NotificationPolicy",
    "EmailMissionSetupInput",
    "classify_email",
    "evaluate_and_execute_cleanup",
    "restore_email_from_trash",
    "evaluate_email_neglect",
    "run_email_mission_tick",
    "get_demo_mailbox",
    "is_email_companion_query",
    "answer_email_companion_query",
    "ControlledTestMailbox",
    "TEST_DATASET",
]

