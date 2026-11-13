"""Shared support for the checkpoint process-death tests."""

import errno
import os
import time

from offline.checkpoint_build import _state_lock


def wait_for_windows_lock_release(state_dir):
    """Probe for release after the test has terminated and joined the child."""
    if os.name != "nt":
        return
    # Windows may still be releasing the byte-range lock after child.wait().
    deadline = time.monotonic() + 1.0
    while True:
        try:
            with _state_lock(state_dir):
                return
        except (PermissionError, ValueError) as error:
            cause = error.__cause__ if isinstance(error, ValueError) else error
            if (not isinstance(cause, PermissionError) or cause.errno != errno.EACCES
                    or time.monotonic() >= deadline):
                raise
            time.sleep(0.01)
