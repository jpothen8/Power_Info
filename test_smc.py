"""Tests for smc.py's pure byte-decoding logic.

Everything else in smc.py (opening the AppleSMC IOService, calling
IOConnectCallStructMethod) is a live hardware boundary that can't be
meaningfully mocked — decode_value() is the one piece of real logic that
can go silently wrong (e.g. picking the wrong endianness), so it's the
one piece worth pinning down with fixed byte inputs.
"""

import ctypes
import struct

from smc import _SMCKeyData, decode_value


def test_smc_key_data_struct_is_80_bytes():
    # The AppleSMCUserClient kernel dispatch table validates an exact
    # struct size; 84 bytes (with an extra padding field some Swift ports
    # add) fails every call with kIOReturnBadArgument, confirmed live.
    assert ctypes.sizeof(_SMCKeyData) == 80


def test_decode_float_is_little_endian():
    # 23.26 as little-endian IEEE-754 float (confirmed against a live PSTR
    # read on real hardware in this session).
    raw = struct.pack("<f", 23.26)
    assert abs(decode_value(raw, "flt ") - 23.26) < 1e-4


def test_decode_uint_types_are_big_endian():
    assert decode_value(bytes([0x2A]), "ui8 ") == 42.0
    assert decode_value((300).to_bytes(2, "big"), "ui16") == 300.0
    assert decode_value((70000).to_bytes(4, "big"), "ui32") == 70000.0


def test_decode_sp78_fixed_point():
    # sp78: signed, big-endian, 8 fractional bits (divide by 256).
    raw = (2560).to_bytes(2, "big", signed=True)  # 2560 / 256 = 10.0
    assert decode_value(raw, "sp78") == 10.0

    raw_neg = (-256).to_bytes(2, "big", signed=True)  # -256 / 256 = -1.0
    assert decode_value(raw_neg, "sp78") == -1.0


def test_decode_unknown_type_returns_none():
    assert decode_value(b"\x00\x00\x00\x00", "xyz ") is None


def test_decode_empty_bytes_returns_none():
    assert decode_value(b"", "flt ") is None


if __name__ == "__main__":
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as e:
                failures += 1
                print(f"FAIL {name}: {e}")
    raise SystemExit(1 if failures else 0)
