"""Tests for `_assess_truncation` — the fix for roleplay/downbad enormous replies.

Roleplay/downbad personas routinely finish naturally without emitting the
completion sentinel. Previously that was read as "truncated" and triggered the
continuation loop, stitching multiple full generations into one huge message.
The assessor now trusts the provider's stop reason over sentinel-absence.
"""

from __future__ import annotations

from app.domain.conversation.pipeline import _assess_truncation


def _resp(text="hi there", *, done_reason=None, finish_reason=None):
    raw = {}
    if done_reason is not None:
        raw["done_reason"] = done_reason
    if finish_reason is not None:
        raw["choices"] = [{"finish_reason": finish_reason}]
    return {"text": text, "raw": raw}


def test_natural_stop_without_sentinel_is_not_truncated():
    # The roleplay case: model stopped on its own, just didn't emit the sentinel.
    assert _assess_truncation(_resp(done_reason="stop"), sentinel_found=False) is False


def test_length_stop_is_truncated():
    assert _assess_truncation(_resp(done_reason="length"), sentinel_found=False) is True


def test_cloud_length_finish_reason_is_truncated():
    assert _assess_truncation(_resp(finish_reason="length"), sentinel_found=False) is True


def test_cloud_natural_finish_reason_without_sentinel_is_not_truncated():
    assert _assess_truncation(_resp(finish_reason="stop"), sentinel_found=False) is False


def test_no_provider_signal_falls_back_to_sentinel_absence():
    # No done_reason/finish_reason at all → sentinel presence decides.
    assert _assess_truncation(_resp(), sentinel_found=False) is True
    assert _assess_truncation(_resp(), sentinel_found=True) is False


def test_empty_or_nondict_is_not_truncated():
    assert _assess_truncation({"text": ""}, sentinel_found=False) is False
    assert _assess_truncation(None, sentinel_found=False) is False
