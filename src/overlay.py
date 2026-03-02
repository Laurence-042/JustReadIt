"""Topmost transparent overlay window for displaying translations.

Handles both hover mode and Freeze mode.
Focus handoff uses AllowSetForegroundWindow(pid) — direct cross-process
SetForegroundWindow is blocked by Windows.
"""
# TODO: implement TranslationOverlay
