"""Regression coverage for the macOS TIS background-thread crash."""
import ctypes
import importlib
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from ankiagent.hotkeys import EventHotKeyID, NativeHotkey, SIGNATURE, NOT_HANDLED, parse_shortcut


class FakeCarbon:
    def __init__(self, register_status=0):
        self.GetApplicationEventTarget = Mock(return_value=300)
        self.InstallEventHandler = Mock(side_effect=self.install)
        self.RegisterEventHotKey = Mock(side_effect=lambda *args: self.register(register_status, *args))
        self.GetEventParameter = Mock(side_effect=self.parameter)
        self.UnregisterEventHotKey = Mock(return_value=0)
        self.RemoveEventHandler = Mock(return_value=0)
        self.event_id = 1
    def install(self, _target, _callback, _count, _types, _data, reference):
        ctypes.cast(reference, ctypes.POINTER(ctypes.c_void_p)).contents.value = 200
        return 0
    def register(self, status, _code, _modifiers, _identifier, _target, _options, reference):
        if not status:
            ctypes.cast(reference, ctypes.POINTER(ctypes.c_void_p)).contents.value = 100
        return status
    def parameter(self, _event, _name, _type, _actual, _size, _actual_size, data):
        identifier = ctypes.cast(data, ctypes.POINTER(EventHotKeyID)).contents
        identifier.signature, identifier.id = SIGNATURE, self.event_id
        return 0


class NativeHotkeyTests(unittest.TestCase):
    def test_default_shortcut_uses_virtual_space_without_keyboard_layout_lookup(self):
        self.assertEqual(parse_shortcut("<ctrl>+<alt>+<space>"), (49, 6144))
        self.assertEqual(parse_shortcut("<option>+<space>"), (49, 2048))
        for value in ("<space>", "<ctrl>", "<ctrl>+unknown"):
            with self.assertRaises(ValueError):
                parse_shortcut(value)

    def test_native_registration_disposal_and_callback(self):
        api, action = FakeCarbon(), Mock()
        shortcut = NativeHotkey("<ctrl>+<alt>+<space>", action, api=api).start()
        self.assertEqual(shortcut.hotkey_ref.value, 100)
        self.assertEqual(shortcut._handler(None, None, None), 0)
        action.assert_called_once()
        api.event_id = 2
        self.assertEqual(shortcut._handler(None, None, None), NOT_HANDLED)
        self.assertEqual(action.call_count, 1)
        shortcut.stop(); shortcut.stop()
        api.UnregisterEventHotKey.assert_called_once()
        api.RemoveEventHandler.assert_called_once()

    def test_worker_thread_is_rejected_before_any_native_api_call(self):
        api = FakeCarbon()
        shortcut = NativeHotkey("<ctrl>+<alt>+<space>", lambda: None, api=api)
        errors = []
        def work():
            try: shortcut.start()
            except RuntimeError as error: errors.append(str(error))
        worker = threading.Thread(target=work)
        worker.start(); worker.join()
        self.assertEqual(len(errors), 1)
        api.GetApplicationEventTarget.assert_not_called()
        api.RegisterEventHotKey.assert_not_called()

    def test_registration_failure_cleans_up_the_event_handler(self):
        api = FakeCarbon(register_status=-9878)
        shortcut = NativeHotkey("<ctrl>+<alt>+<space>", lambda: None, api=api)
        with self.assertRaisesRegex(RuntimeError, "register the shortcut"):
            shortcut.start()
        api.RemoveEventHandler.assert_called_once()
        self.assertIsNone(shortcut._handler)


class ShortcutLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.launcher = importlib.import_module("launcher")

    def test_save_callback_from_worker_only_queues_native_work_on_main(self):
        queued = []
        listener = Mock()
        with patch.object(self.launcher, "manager", SimpleNamespace(config={"enable_hotkey": True})), \
             patch.object(self.launcher, "hotkey_listener", None), \
             patch.object(self.launcher, "_schedule_on_main", side_effect=queued.append), \
             patch.object(self.launcher, "NativeHotkey", return_value=listener) as factory:
            worker = threading.Thread(target=self.launcher.update_hotkey)
            worker.start(); worker.join()
            factory.assert_not_called()
            self.assertEqual(queued, [self.launcher._apply_hotkey])
            queued[0]()
            factory.assert_called_once()
            listener.start.assert_called_once()

    def test_saving_unchanged_settings_does_not_restart_the_shortcut(self):
        listener = Mock()
        with patch.object(self.launcher, "manager", SimpleNamespace(config={"enable_hotkey": True})), \
             patch.object(self.launcher, "hotkey_listener", listener), \
             patch.object(self.launcher, "NativeHotkey") as factory:
            self.launcher._apply_hotkey()
            listener.stop.assert_not_called()
            factory.assert_not_called()

    def test_disabling_shortcut_unregisters_it_on_the_ui_queue(self):
        listener = Mock()
        with patch.object(self.launcher, "manager", SimpleNamespace(config={"enable_hotkey": False})), \
             patch.object(self.launcher, "hotkey_listener", listener):
            self.launcher._apply_hotkey()
            listener.stop.assert_called_once()
            self.assertIsNone(self.launcher.hotkey_listener)

    def test_native_callback_does_not_block_the_ui_queue_on_webkit(self):
        with patch.object(self.launcher.threading, "Thread") as thread:
            self.launcher._hotkey_pressed()
        thread.assert_called_once_with(target=self.launcher.toggle, daemon=True)
        thread.return_value.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
