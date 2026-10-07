"""Internal, pinned C API binding used by workers and the integration spike.

Construct only in an isolated process. Calls can block or crash that process;
this binding is not the AnalysisEngine/ArbiterEngine implementation. Importing
it does not import ctypes or load a library. One owning thread per session.
"""

from pathlib import Path
from types import TracebackType
from typing import Self

from .errors import EngineConfigurationError, EngineError, InvalidRequestError


class MagpieCommandError(EngineError):
    def __init__(self, command: str, exit_code: int, details: str) -> None:
        self.command = command
        self.exit_code = exit_code
        self.details = details
        super().__init__(f"MAGPIE command failed ({exit_code}): {details}")


class MagpieSession:
    """Own one native handle and every returned string; explicitly close it."""

    def __init__(self, library: Path, data: Path, *, settings: Path) -> None:
        import ctypes

        self._ctypes = ctypes
        self._handle = None
        self._pending = False
        self._command = ""
        try:
            self._lib = ctypes.CDLL(str(library.resolve()))
            self._declare_functions()
        except (OSError, AttributeError) as exc:
            raise EngineConfigurationError(f"Cannot load MAGPIE C API: {exc}") from exc
        # Explicit settings path avoids reading a user's ambient settings.txt.
        self._handle = self._lib.magpie_create_with_options(
            self._encode(str(data.resolve())),
            self._encode(str(settings.resolve())), False,
        )
        if not self._handle:
            raise EngineConfigurationError("MAGPIE returned a null handle")
        if self._lib.magpie_has_error(self._handle):
            try:
                details = self._take_string(self._lib.magpie_get_and_clear_error(self._handle))
            finally:
                self.close()
            raise EngineConfigurationError(details)

    def _declare_functions(self) -> None:
        c = self._ctypes
        signatures = {
            "magpie_create_with_options": (c.c_void_p, [c.c_char_p, c.c_char_p, c.c_bool]),
            "magpie_destroy": (None, [c.c_void_p]),
            "magpie_run_sync": (c.c_int, [c.c_void_p, c.c_char_p]),
            "magpie_run_async": (c.c_int, [c.c_void_p, c.c_char_p]),
            "magpie_await": (c.c_int, [c.c_void_p]),
            "magpie_has_error": (c.c_bool, [c.c_void_p]),
            "magpie_get_and_clear_error": (c.c_void_p, [c.c_void_p]),
            "magpie_get_last_command_status_message": (c.c_void_p, [c.c_void_p]),
            "magpie_get_last_command_output": (c.c_void_p, [c.c_void_p]),
            "magpie_stop_current_command": (None, [c.c_void_p]),
            "magpie_get_thread_status": (c.c_int, [c.c_void_p]),
            "magpie_free_string": (None, [c.c_void_p]),
        }
        for name, (result, args) in signatures.items():
            function = getattr(self._lib, name)
            function.restype, function.argtypes = result, args

    @staticmethod
    def _encode(value: str) -> bytes:
        if "\x00" in value:
            raise InvalidRequestError("Native strings cannot contain NUL")
        return value.encode("utf-8")

    def _take_string(self, pointer: int | None) -> str:
        if not pointer:
            return ""
        try:
            return self._ctypes.string_at(pointer).decode("utf-8", errors="strict")
        finally:
            self._lib.magpie_free_string(pointer)

    def _require_open(self, *, idle: bool = False) -> None:
        if self._handle is None:
            raise InvalidRequestError("MAGPIE session is closed")
        if idle and self._pending:
            raise InvalidRequestError("Await the async command before accessing this handle")

    def _check(self, command: str, code: int) -> None:
        if code != 0:
            details = self._take_string(self._lib.magpie_get_and_clear_error(self._handle))
            raise MagpieCommandError(command, code, details)

    def run(self, command: str) -> str:
        self._require_open(idle=True)
        code = self._lib.magpie_run_sync(self._handle, self._encode(command))
        self._check(command, code)
        return self.output()

    def start(self, command: str) -> None:
        self._require_open(idle=True)
        code = self._lib.magpie_run_async(self._handle, self._encode(command))
        self._check(command, code)
        self._command, self._pending = command, True

    def await_output(self) -> str:
        self._require_open()
        if not self._pending:
            raise InvalidRequestError("No async command to await")
        code = self._lib.magpie_await(self._handle)
        self._pending = False
        self._check(self._command, code)
        return self.output()

    def output(self) -> str:
        self._require_open(idle=True)
        return self._take_string(self._lib.magpie_get_last_command_output(self._handle))

    def status(self) -> str:
        self._require_open()
        return self._take_string(self._lib.magpie_get_last_command_status_message(self._handle))

    def thread_status(self) -> int:
        self._require_open()
        return self._lib.magpie_get_thread_status(self._handle)

    def stop(self) -> None:
        self._require_open()
        if self._pending:
            self._lib.magpie_stop_current_command(self._handle)

    def close(self) -> None:
        if self._handle is not None:
            # Native destroy requests stop and joins any outstanding thread.
            self._lib.magpie_destroy(self._handle)
            self._handle, self._pending = None, False

    def __enter__(self) -> Self:
        self._require_open()
        return self

    def __exit__(self, kind: type[BaseException] | None,
                 value: BaseException | None, trace: TracebackType | None) -> None:
        self.close()
