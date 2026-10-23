"""Stable scan codes for locations.

Codes are ``L`` followed by six Crockford base32 characters (alphabet without
I, L, O and U), e.g. ``L7K2QX``. They are random, unique per tenant and never
change once assigned, so a reprinted label keeps working.
"""

from __future__ import annotations

import re
import secrets

CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
LOCATION_CODE_PREFIX = "L"
LOCATION_CODE_LENGTH = 6

_CODE_RE = re.compile(r"^L[0-9A-HJKMNP-TV-Z]{6}$")


def generate_location_code() -> str:
    return LOCATION_CODE_PREFIX + "".join(secrets.choice(CROCKFORD_ALPHABET) for _ in range(LOCATION_CODE_LENGTH))


def normalize_location_code(raw: str) -> str:
    """Upper-case and strip a scanned code; Crockford look-alikes after the
    prefix are folded (I/L -> 1, O -> 0)."""
    value = (raw or "").strip().upper()
    if not value:
        return value
    head, body = value[:1], value[1:]
    body = body.replace("I", "1").replace("L", "1").replace("O", "0")
    return head + body


def is_location_code(value: str) -> bool:
    return bool(_CODE_RE.match(value or ""))
