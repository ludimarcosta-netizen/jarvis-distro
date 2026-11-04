"""Pure send-state decisions shared by the UI script and simulated tests."""


def classify_send(triggered: bool, confirmed: bool) -> str:
    if not triggered:
        return "failed_before_trigger"
    return "confirmed" if confirmed else "triggered_unconfirmed"


def is_new_exact_message(message: str, before: list[str] | None, after: list[str] | None) -> bool:
    return before is not None and after is not None and after.count(message) > before.count(message)
