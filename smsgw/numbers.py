import re


class InvalidNumber(ValueError):
    pass


def normalize(raw: str) -> str:
    """Normalize a user-typed NANPA number to the 10-digit form voip.ms expects."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        raise InvalidNumber(f"{raw!r} is not a valid 10-digit US/Canada number")
    return digits


def normalize_lenient(raw: str) -> str:
    """Normalize an inbound sender; short codes and odd formats are kept as bare digits."""
    try:
        return normalize(raw)
    except InvalidNumber:
        return re.sub(r"\D", "", raw or "") or (raw or "unknown")


def pretty(number: str) -> str:
    if len(number) == 10 and number.isdigit():
        return f"+1 ({number[:3]}) {number[3:6]}-{number[6:]}"
    return number
