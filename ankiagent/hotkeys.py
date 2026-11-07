"""Register one macOS shortcut without a keyboard listener or layout queries.

Carbon hotkey registration and disposal must run on the Cocoa main thread.
The callback should return promptly; window actions are dispatched separately.
"""
from __future__ import annotations

import ctypes
import logging
import threading


class EventHotKeyID(ctypes.Structure):
    _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]


class EventTypeSpec(ctypes.Structure):
    _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]


EventHandler = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
SIGNATURE = int.from_bytes(b"AnkA", "big")
NOT_HANDLED = -9874


def parse_shortcut(shortcut: str) -> tuple[int, int]:
    modifiers = {"cmd": 1 << 8, "command": 1 << 8, "shift": 1 << 9,
                 "alt": 1 << 11, "option": 1 << 11, "ctrl": 1 << 12, "control": 1 << 12}
    keys = {"space": 49, "tab": 48, "enter": 36, "return": 36, "escape": 53, "esc": 53,
            "left": 123, "right": 124, "down": 125, "up": 126}
    mask, code = 0, None
    for token in shortcut.casefold().split("+"):
        token = token.strip().strip("<>")
        if token in modifiers:
            mask |= modifiers[token]
        elif token in keys and code is None:
            code = keys[token]
        else:
            raise ValueError("Use a modifier plus Space, Tab, Enter, Escape, or an arrow for HOTKEY.")
    if code is None or not mask:
        raise ValueError("The global shortcut needs a modifier and a supported key.")
    return code, mask


def _assert_main_thread():
    if threading.current_thread() is not threading.main_thread():
        raise RuntimeError("macOS hotkeys must be changed on the main thread.")


def _load_carbon():
    api = ctypes.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")
    pointer = ctypes.c_void_p
    api.GetApplicationEventTarget.argtypes = []
    api.GetApplicationEventTarget.restype = pointer
    api.InstallEventHandler.argtypes = [pointer, EventHandler, ctypes.c_uint32,
        ctypes.POINTER(EventTypeSpec), pointer, ctypes.POINTER(pointer)]
    api.InstallEventHandler.restype = ctypes.c_int32
    api.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID,
        pointer, ctypes.c_uint32, ctypes.POINTER(pointer)]
    api.RegisterEventHotKey.restype = ctypes.c_int32
    api.GetEventParameter.argtypes = [pointer, ctypes.c_uint32, ctypes.c_uint32, pointer,
        ctypes.c_uint32, pointer, pointer]
    api.GetEventParameter.restype = ctypes.c_int32
    api.UnregisterEventHotKey.argtypes = [pointer]
    api.UnregisterEventHotKey.restype = ctypes.c_int32
    api.RemoveEventHandler.argtypes = [pointer]
    api.RemoveEventHandler.restype = ctypes.c_int32
    return api


class NativeHotkey:
    def __init__(self, shortcut, callback, *, api=None):
        self.code, self.modifiers = parse_shortcut(shortcut)
        self.callback = callback
        self.api = api
        self.hotkey_ref = ctypes.c_void_p()
        self.handler_ref = ctypes.c_void_p()
        self._handler = None  # Keep the native callback alive until unregistering.

    def start(self):
        _assert_main_thread()
        if self.hotkey_ref.value:
            return self
        self.api = self.api or _load_carbon()

        @EventHandler
        def handle(_next, event, _user):
            identifier = EventHotKeyID()
            status = self.api.GetEventParameter(event, int.from_bytes(b"----", "big"),
                int.from_bytes(b"hkid", "big"), None, ctypes.sizeof(identifier), None,
                ctypes.byref(identifier))
            if status or identifier.signature != SIGNATURE or identifier.id != 1:
                return NOT_HANDLED
            try:
                self.callback()
            except Exception:
                logging.getLogger(__name__).exception("The shortcut action failed")
            return 0

        self._handler = handle
        target = self.api.GetApplicationEventTarget()
        event = EventTypeSpec(int.from_bytes(b"keyb", "big"), 5)  # kEventHotKeyPressed
        try:
            status = self.api.InstallEventHandler(target, handle, 1, ctypes.byref(event),
                None, ctypes.byref(self.handler_ref))
            if status:
                raise RuntimeError(f"Could not install the shortcut handler (code {status}).")
            status = self.api.RegisterEventHotKey(self.code, self.modifiers,
                EventHotKeyID(SIGNATURE, 1), target, 0, ctypes.byref(self.hotkey_ref))
            if status:
                raise RuntimeError(f"Could not register the shortcut (code {status}).")
        except Exception:
            self.stop()
            raise
        return self

    def stop(self):
        _assert_main_thread()
        if self.hotkey_ref.value:
            self.api.UnregisterEventHotKey(self.hotkey_ref)
            self.hotkey_ref = ctypes.c_void_p()
        if self.handler_ref.value:
            self.api.RemoveEventHandler(self.handler_ref)
            self.handler_ref = ctypes.c_void_p()
        self._handler = None
