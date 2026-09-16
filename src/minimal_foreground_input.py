"""
MinimalForegroundInput: lightest-weight foreground input for WeChat WebView.

Usage:
    mfi = MinimalForegroundInput()
    mfi.set_text("hello")
    duration = mfi.last_duration_ms

This is NOT a "stealth" or "background" mechanism.
It is an honest foreground transition that:
1. Saves the user's current foreground window.
2. Allows the required SetValue call.
3. Restores the user's window.
4. Reports the duration.

The FOREGROUND_REQUIRED state is documented in UIA_BACKGROUND_CAPABILITY.md.
"""

import ctypes
import ctypes.wintypes
import time
from dataclasses import dataclass, field
from typing import Optional, Tuple


# --- Win32 API ---
user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32


@dataclass
class ForegroundSnapshot:
    """Snapshot of the foreground window state."""
    hwnd: int
    pid: int
    process_name: str
    title: str
    timestamp: float = field(default_factory=time.time)


class MinimalForegroundInput:
    """
    Manages foreground for SetValue operations.

    Provides:
    - save_foreground() -> ForegroundSnapshot
    - restore_foreground(snapshot: ForegroundSnapshot) -> bool
    - set_text(target_element, text: str) -> bool
    - last_duration_ms: int
    - last_restore_success: bool
    """

    def __init__(self):
        self.last_duration_ms: int = 0
        self.last_restore_success: bool = False
        self._last_snapshot: Optional[ForegroundSnapshot] = None

    def _get_foreground(self) -> ForegroundSnapshot:
        hwnd = user32.GetForegroundWindow()
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetWindowTextW(hwnd, buf, 256)
        title = buf.value.strip()
        process_name = ""
        try:
            h_process = kernel32.OpenProcess(0x0010, False, pid.value)
            if h_process:
                import psutil
                try:
                    process_name = psutil.Process(pid.value).name()
                except:
                    pass
                kernel32.CloseHandle(h_process)
        except:
            pass
        return ForegroundSnapshot(
            hwnd=hwnd if isinstance(hwnd, int) else hwnd,
            pid=pid.value,
            process_name=process_name,
            title=title,
        )

    def save_foreground(self) -> ForegroundSnapshot:
        """Capture current foreground window state."""
        snapshot = self._get_foreground()
        self._last_snapshot = snapshot
        return snapshot

    def restore_foreground(self, snapshot: Optional[ForegroundSnapshot] = None) -> bool:
        """
        Attempt to restore the foreground window to the given snapshot.

        Returns True if the foreground was restored to the target window.
        """
        target = snapshot or self._last_snapshot
        if target is None:
            return False

        target_hwnd = ctypes.wintypes.HWND(target.hwnd)
        result = user32.SetForegroundWindow(target_hwnd)
        time.sleep(0.3)  # Allow window manager to process

        # Verify
        current_hwnd = user32.GetForegroundWindow()
        self.last_restore_success = (current_hwnd == target_hwnd)
        return self.last_restore_success

    def set_text(self, target_element, text: str) -> bool:
        """
        Set text on a UIA element using ValuePattern.SetValue().
        This operation is FOREGROUND_REQUIRED.

        Args:
            target_element: UIA AutomationElement with ValuePattern
            text: Text to set

        Returns:
            True if the operation completed successfully
        """
        # Save current foreground
        before = self._get_foreground()
        start = time.time()

        try:
            # Get ValuePattern
            vp = target_element.GetValuePattern()
            vp.SetValue(text)

            # Verify
            readback = vp.Value
            success = (readback == text)

            elapsed = (time.time() - start) * 1000
            self.last_duration_ms = int(elapsed)

            return success

        except Exception as e:
            elapsed = (time.time() - start) * 1000
            self.last_duration_ms = int(elapsed)
            print(f"MinimalForegroundInput.set_text failed: {e}")
            return False

    def set_text_with_restore(self, target_element, text: str) -> Tuple[bool, int, bool]:
        """
        Complete cycle: save foreground, set text, restore foreground.

        Returns:
            (set_success, duration_ms, restore_success)
        """
        before = self.save_foreground()
        set_success = self.set_text(target_element, text)
        restore_success = self.restore_foreground(before)
        return (set_success, self.last_duration_ms, restore_success)
