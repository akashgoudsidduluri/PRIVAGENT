"""Regression: a natural email-monitoring setup instruction must route to
the deterministic email-mission handler, not to the browser.

"check my gmail every 5 minutes and delete promo emails" used to fall
through every email-mission pattern (the router's EMAIL_SCAN_MISSION_PATTERN
and mission_setup.SETUP_TRIGGER both lacked a "check/scan + mailbox +
every N minutes" alternative) and hit EMAIL_TRIGGER ("check my gmail") at
router step 5, which sent it to the browser as NEW_BROWSER_TASK. With no
extension connected the mission died with EXTENSION_DISCONNECTED; with one
connected it would have opened Gmail and flailed instead of creating the
deterministic email mission (scan interval + promo deletion).

The fix adds the same narrow alternative to both patterns:
  check|scan|monitor + my gmail/email/mail/inbox + every <n> <unit>
The "every <n> <unit>" requirement keeps one-off reads ("check my gmail
for the interview invite", "check my email now") on the browser path.
"""

from app.email_intelligence.mission_setup import is_email_mission_setup_instruction
from app.intent_router import TaskIntent, classify_intent

# (instruction, expected router intent, expected setup matcher)
CASES = [
    ("check my gmail every 5 minutes and delete promo emails",
     TaskIntent.NON_BROWSER, True),
    ("scan my email every 60 minutes, delete promo emails, notify urgent",
     TaskIntent.NON_BROWSER, True),
    ("check my inbox every hour and delete spam",
     TaskIntent.NON_BROWSER, True),
    ("monitor my gmail every 30 minutes",
     TaskIntent.NON_BROWSER, True),
    ("check my gmail every day",
     TaskIntent.NON_BROWSER, True),
    # A ".com" mailbox ("outlook.com"/"gmail.com") must still match —
    # the filler window between mailbox and "every" cannot cross a ".".
    ("check my outlook.com inbox every hour",
     TaskIntent.NON_BROWSER, True),
    ("check my gmail.com inbox every hour",
     TaskIntent.NON_BROWSER, True),
    # "every second Tuesday" is a weekday, not an interval.
    ("check my gmail every second Tuesday",
     TaskIntent.NEW_BROWSER_TASK, False),
    # One-off reads must stay on the browser path (no interval).
    ("check my gmail for the interview invite",
     TaskIntent.NEW_BROWSER_TASK, False),
    ("check my email now",
     TaskIntent.NEW_BROWSER_TASK, False),
    ("open gmail",
     TaskIntent.NEW_BROWSER_TASK, False),
]


def test_email_monitoring_setup_routing():
    for text, expected_intent, expected_setup in CASES:
        got = classify_intent(text)
        assert got.intent == expected_intent, (
            f"{text!r}: router gave {got.intent} ({got.reason}), "
            f"expected {expected_intent}"
        )
        assert is_email_mission_setup_instruction(text) is expected_setup, (
            f"{text!r}: setup matcher gave "
            f"{is_email_mission_setup_instruction(text)}, expected {expected_setup}"
        )
