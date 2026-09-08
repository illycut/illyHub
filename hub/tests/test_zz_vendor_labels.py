"""Runs last (file name): every user-facing message produced anywhere in the suite must name the
ecosystems by label ("HEOS", "Sonos"), never by the raw vendor id."""

from __future__ import annotations

import re

from conftest import RECORDED_MESSAGES
from illyhub_hub.state import VENDOR_LABEL, vendor_label

RAW_VENDOR = re.compile(r"\b(heos|sonos|denon)\b")


def test_vendor_label_map() -> None:
    assert VENDOR_LABEL == {"heos": "HEOS", "sonos": "Sonos", "denon": "Denon"}
    assert vendor_label("heos") == "HEOS" and vendor_label("unknown") == "Unknown"


def test_no_user_facing_message_leaks_a_raw_vendor_id() -> None:
    assert RECORDED_MESSAGES, "the recorder fixture did not run"
    leaks = sorted({f"{kind}: {msg}" for kind, msg in RECORDED_MESSAGES if RAW_VENDOR.search(msg)})
    assert not leaks, "raw vendor ids in user copy:\n" + "\n".join(leaks)
