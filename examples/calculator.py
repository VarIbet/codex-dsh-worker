"""Simple calculator module."""


def add(a, b):
    """Return the sum of a and b."""
    return a + b


def safe_divide(a, b):
    """Return a / b, raising ValueError for division by zero."""
    if b == 0:
        raise ValueError("division by zero")
    return a / b
