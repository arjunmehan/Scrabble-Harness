import subprocess
import sys
from types import SimpleNamespace

import pytest

from scrabble_harness.engine.errors import InvalidRequestError
from scrabble_harness.engine.magpie_ffi import MagpieSession


def test_importing_every_package_module_does_not_load_ctypes_or_native_library():
    code = (
        "import sys, pkgutil, importlib, scrabble_harness; "
        "[importlib.import_module(m.name) for m in pkgutil.walk_packages("
        "scrabble_harness.__path__, scrabble_harness.__name__ + '.')]; "
        "assert '_ctypes' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10)


def test_returned_pointer_freed_even_when_decode_fails():
    # String ownership is independent of the native engine's move behavior.
    freed = []
    instance = object.__new__(MagpieSession)
    instance._ctypes = SimpleNamespace(string_at=lambda pointer: b"\xff")
    instance._lib = SimpleNamespace(magpie_free_string=freed.append)
    with pytest.raises(UnicodeDecodeError):
        instance._take_string(123)
    assert freed == [123]
    assert instance._take_string(None) == ""
    assert freed == [123]


def test_native_strings_reject_nul_instead_of_truncating():
    with pytest.raises(InvalidRequestError):
        MagpieSession._encode("version\x00commit pass")
