"""Direct SMC (System Management Controller) key reads via IOKit.

`ioreg -n AppleSmartBattery`'s PowerTelemetryData is a *cache* populated by
the battery gauge IC on its own schedule — empirically confirmed on this
machine to sit frozen for 20+ seconds at a stretch even when polled every
second. The SMC, by contrast, exposes live hardware sensor values that can
be sampled sub-second. This mirrors the approach used by WattSec and
exelban/stats (https://github.com/exelban/stats/blob/master/SMC/smc.swift):
open the AppleSMC IOService and call IOConnectCallStructMethod directly.

Struct layout, selector values, and the per-type decode rules below are
ported field-for-field from that implementation (a two-step call: first
kSMCReadKeyInfo to learn a key's size/type, then kSMCReadBytes for the
value) rather than re-derived, since raw struct layout mistakes fail
silently (wrong numbers, not a crash).
"""

from __future__ import annotations

import ctypes
import ctypes.util
import struct


class _SMCVersion(ctypes.Structure):
    _fields_ = [
        ("major", ctypes.c_uint8),
        ("minor", ctypes.c_uint8),
        ("build", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8),
        ("release", ctypes.c_uint16),
    ]


class _SMCPLimitData(ctypes.Structure):
    _fields_ = [
        ("version", ctypes.c_uint16),
        ("length", ctypes.c_uint16),
        ("cpuPLimit", ctypes.c_uint32),
        ("gpuPLimit", ctypes.c_uint32),
        ("memPLimit", ctypes.c_uint32),
    ]


class _SMCKeyInfo(ctypes.Structure):
    _fields_ = [
        ("dataSize", ctypes.c_uint32),
        ("dataType", ctypes.c_uint32),
        ("dataAttributes", ctypes.c_uint8),
    ]


class _SMCKeyData(ctypes.Structure):
    # Note: no explicit padding field here (unlike some Swift ports) — the
    # AppleSMCUserClient kernel dispatch table validates an exact 80-byte
    # struct; adding a padding field pushes this to 84 bytes and every call
    # fails with kIOReturnBadArgument (0x2c2), confirmed empirically.
    _fields_ = [
        ("key", ctypes.c_uint32),
        ("vers", _SMCVersion),
        ("pLimitData", _SMCPLimitData),
        ("keyInfo", _SMCKeyInfo),
        ("result", ctypes.c_uint8),
        ("status", ctypes.c_uint8),
        ("data8", ctypes.c_uint8),
        ("data32", ctypes.c_uint32),
        ("bytes", ctypes.c_uint8 * 32),
    ]


_KERNEL_INDEX_SMC = 2  # IOConnectCallStructMethod selector (fixed)
_SMC_CMD_READ_KEYINFO = 9  # goes in data8, not the selector
_SMC_CMD_READ_BYTES = 5

# Fan keys are exempt from the "all-zero bytes = absent" heuristic upstream;
# irrelevant here since we only read power sensors.
_ALL_ZERO_OK_KEYS: frozenset[str] = frozenset()


def _load_iokit():
    iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    iokit.IOServiceMatching.restype = ctypes.c_void_p
    iokit.IOServiceMatching.argtypes = [ctypes.c_char_p]
    iokit.IOServiceGetMatchingServices.restype = ctypes.c_int
    iokit.IOServiceGetMatchingServices.argtypes = [
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOIteratorNext.restype = ctypes.c_uint32
    iokit.IOIteratorNext.argtypes = [ctypes.c_uint32]
    iokit.IOServiceOpen.restype = ctypes.c_int
    iokit.IOServiceOpen.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    iokit.IOServiceClose.restype = ctypes.c_int
    iokit.IOServiceClose.argtypes = [ctypes.c_uint32]
    iokit.IOObjectRelease.restype = ctypes.c_int
    iokit.IOObjectRelease.argtypes = [ctypes.c_uint32]
    iokit.IOConnectCallStructMethod.restype = ctypes.c_int
    iokit.IOConnectCallStructMethod.argtypes = [
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.c_size_t,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    return iokit


def _key_to_uint32(key: str) -> int:
    return struct.unpack(">I", key.encode("ascii"))[0]


def _uint32_to_key(v: int) -> str:
    return struct.pack(">I", v).decode("ascii", errors="replace")


def decode_value(raw: bytes, data_type: str) -> float | None:
    """Decode raw SMC bytes per the type code, mirroring exelban/stats'
    per-type table. flt is host-native (little-endian on Intel/ARM Macs);
    ui8/ui16/ui32 and the spXX fixed-point types are big-endian."""
    if not raw:
        return None
    if data_type == "ui8 ":
        return float(raw[0])
    if data_type == "ui16":
        return float(int.from_bytes(raw[:2], "big"))
    if data_type == "ui32":
        return float(int.from_bytes(raw[:4], "big"))
    if data_type == "flt ":
        return struct.unpack("<f", raw[:4])[0]
    if data_type.startswith("sp") and len(raw) >= 2:
        frac_bits = {
            "sp1e": 14, "sp3c": 12, "sp4b": 11, "sp5a": 10, "sp69": 9,
            "sp78": 8, "sp87": 7, "sp96": 6, "spa5": 5, "spb4": 4, "spf0": 0,
        }.get(data_type)
        if frac_bits is None:
            return None
        raw_i16 = int.from_bytes(raw[:2], "big", signed=True)
        return raw_i16 / (2**frac_bits)
    return None


class SMCReader:
    """A held-open connection to the AppleSMC IOService. Not thread-safe;
    intended for single-threaded polling."""

    def __init__(self):
        self._iokit = _load_iokit()
        self._conn = ctypes.c_uint32(0)
        self._open()

    def _open(self):
        iokit = self._iokit
        matching = iokit.IOServiceMatching(b"AppleSMC")
        if not matching:
            raise OSError("IOServiceMatching(AppleSMC) returned NULL")
        iterator = ctypes.c_uint32(0)
        # masterPort 0 == the default port in modern IOKit.
        kr = iokit.IOServiceGetMatchingServices(0, matching, ctypes.byref(iterator))
        if kr != 0:
            raise OSError(f"IOServiceGetMatchingServices failed: {kr}")
        device = iokit.IOIteratorNext(iterator.value)
        iokit.IOObjectRelease(iterator.value)
        if device == 0:
            raise OSError("AppleSMC service not found")
        libc = ctypes.CDLL(ctypes.util.find_library("c"))
        task_self = ctypes.c_uint32.in_dll(libc, "mach_task_self_").value
        kr = iokit.IOServiceOpen(device, task_self, 0, ctypes.byref(self._conn))
        iokit.IOObjectRelease(device)
        if kr != 0:
            raise OSError(f"IOServiceOpen failed: {kr}")

    def close(self):
        if self._conn.value:
            self._iokit.IOServiceClose(self._conn)
            self._conn = ctypes.c_uint32(0)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _call(self, input_struct: _SMCKeyData) -> _SMCKeyData:
        output = _SMCKeyData()
        out_size = ctypes.c_size_t(ctypes.sizeof(_SMCKeyData))
        kr = self._iokit.IOConnectCallStructMethod(
            self._conn,
            _KERNEL_INDEX_SMC,
            ctypes.byref(input_struct),
            ctypes.sizeof(input_struct),
            ctypes.byref(output),
            ctypes.byref(out_size),
        )
        if kr != 0:
            raise OSError(f"IOConnectCallStructMethod failed: {kr}")
        return output

    def read_raw(self, key: str) -> tuple[str, bytes] | None:
        """(dataType, raw bytes) for `key`, or None if absent/unreadable."""
        info_req = _SMCKeyData()
        info_req.key = _key_to_uint32(key)
        info_req.data8 = _SMC_CMD_READ_KEYINFO
        info = self._call(info_req)
        data_size = info.keyInfo.dataSize
        if data_size == 0 or data_size > 32:
            return None
        data_type = _uint32_to_key(info.keyInfo.dataType)

        read_req = _SMCKeyData()
        read_req.key = _key_to_uint32(key)
        read_req.keyInfo.dataSize = data_size
        read_req.data8 = _SMC_CMD_READ_BYTES
        out = self._call(read_req)
        raw = bytes(out.bytes[:data_size])

        # Upstream treats all-zero payloads as "sensor not implemented on
        # this model" (the key exists in the table but reads as filler).
        if key not in _ALL_ZERO_OK_KEYS and not any(raw):
            return None
        return data_type, raw

    def read(self, key: str) -> float | None:
        """Decoded value for `key`, or None if absent/unreadable."""
        result = self.read_raw(key)
        if result is None:
            return None
        data_type, raw = result
        return decode_value(raw, data_type)

    def read_power_keys(self) -> dict[str, float | None]:
        """PSTR (System Total) and PDTR (DC In), the two live sensors the
        app overlays onto ioreg's slower-updating telemetry. Individual
        key failures degrade to None rather than raising, so one bad read
        doesn't take down the whole poll cycle."""
        out = {}
        for key in ("PSTR", "PDTR"):
            try:
                out[key] = self.read(key)
            except OSError:
                out[key] = None
        return out

    def read_battery_keys(self) -> dict[str, float | None]:
        """BRSC (state-of-charge %), reported live by the battery pack's
        own fuel-gauge chip via SMC — used in place of ioreg's BatteryData
        cache. Individual key failures degrade to None rather than
        raising."""
        out = {}
        for key in ("BRSC",):
            try:
                out[key] = self.read(key)
            except OSError:
                out[key] = None
        return out


if __name__ == "__main__":
    import time

    probe_keys = [
        "PSTR",  # System Total
        "PDTR",  # DC In (adapter)
        "PPBR",  # Battery
        "PCPC",  # CPU Package
        "PCTR",  # CPU Total
        "PC0C",  # CPU Core
        "PMTR",  # Memory Total
    ]
    with SMCReader() as smc:
        for name in probe_keys:
            result = smc.read_raw(name)
            print(f"{name}: {result}")
        print("\nlive samples (Ctrl-C to stop):")
        while True:
            row = " ".join(
                f"{k}={smc.read(k):.2f}" if smc.read(k) is not None else f"{k}=—"
                for k in probe_keys
            )
            print(f"{time.strftime('%H:%M:%S')} {row}")
            time.sleep(1)
