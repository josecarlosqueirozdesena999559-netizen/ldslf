def get_next_value(base_value: float, step: int, multiplier: float) -> float:
    return round(base_value * (2.0 ** step), 2)


def should_continue_after_loss(step: int, max_steps: int, enabled: bool) -> bool:
    return enabled and step < max_steps


def attempt_name(step: int) -> str:
    return "normal" if step == 0 else f"G{step}"


def reset_cycle() -> int:
    return 0
