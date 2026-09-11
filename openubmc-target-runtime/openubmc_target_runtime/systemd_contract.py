"""Literal, bounded system-manager service selection."""
import re

UNIT = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.@:-]{0,246}\.service\Z')


def validate_systemd_names(names):
    if isinstance(names, (tuple, list)) and list(names) == ['failed']:
        return
    if (not isinstance(names, (tuple, list)) or not 1 <= len(names) <= 16
            or any(not isinstance(name, str) or not UNIT.fullmatch(name) for name in names)
            or len(set(names)) != len(names)):
        raise ValueError('systemd names require 1 to 16 distinct literal .service IDs or ["failed"]')
