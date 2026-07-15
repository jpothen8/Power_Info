"""Pure parsing/calculation functions for AppleSmartBattery ioreg output.

The input is the output of:

    ioreg -rw0 -r -n AppleSmartBattery

which uses Apple's custom debug serialization (nested {}/()/<> structures,
NOT plist or JSON). We extract the handful of keys we need with scoped
regex/balanced-delimiter scanning rather than fully parsing the format.

All raw electrical values are mV / mA / mW unless noted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace


# Values above this are assumed to be negative numbers that went through a
# 64-bit unsigned wraparound (confirmed in real dumps: a discharging battery
# reports e.g. BatteryPower=18446744073709524916).
_SUSPICIOUS = 10**12


def to_signed64(n: int) -> int:
    """Interpret an unsigned 64-bit value as two's-complement signed."""
    return n - 2**64 if n >= 2**63 else n


def _fix_sign(n: int | None) -> int | None:
    """Apply the two's-complement fix to suspiciously huge values."""
    if n is None:
        return None
    return to_signed64(n) if n > _SUSPICIOUS else n


def _find_block(text: str, key: str) -> str | None:
    """Return the balanced {...} or (...) block assigned to "key", or None.

    Handles arbitrary nesting of {} and () inside the block. <...> data
    blobs and quoted strings never contain braces in this format, so a
    simple depth counter is sufficient.
    """
    m = re.search(r'"%s"\s*=\s*([({])' % re.escape(key), text)
    if not m:
        return None
    opener = m.group(1)
    closer = "}" if opener == "{" else ")"
    depth = 0
    start = m.end() - 1
    for i in range(start, len(text)):
        c = text[i]
        if c == opener:
            depth += 1
        elif c == closer:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _get_int(text: str | None, key: str) -> int | None:
    """First integer value assigned to "key" in text, or None."""
    if not text:
        return None
    m = re.search(r'"%s"\s*=\s*(-?\d+)' % re.escape(key), text)
    return int(m.group(1)) if m else None


def _get_str(text: str | None, key: str) -> str | None:
    """First string value assigned to "key" in text, or None."""
    if not text:
        return None
    m = re.search(r'"%s"\s*=\s*"([^"]*)"' % re.escape(key), text)
    return m.group(1) if m else None


def _get_bool(text: str | None, key: str) -> bool | None:
    if not text:
        return None
    m = re.search(r'"%s"\s*=\s*(Yes|No)' % re.escape(key), text)
    return m.group(1) == "Yes" if m else None


def _split_array_items(block: str) -> list[str]:
    """Split a (...) array block into its top-level {...} dict items."""
    items = []
    depth = 0
    start = None
    for i, c in enumerate(block):
        if c == "{":
            if depth == 0:
                start = i
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0 and start is not None:
                items.append(block[start : i + 1])
                start = None
    return items


@dataclass
class PowerReading:
    """One parsed snapshot. Power values in watts, voltage in volts."""

    battery_w: float | None = None  # + charging, - discharging (menu bar title)
    input_w: float | None = None  # SystemPowerIn: wall -> Mac
    system_w: float | None = None  # raw SystemLoad (includes USB-out; see laptop_w)
    usb_out_w: float = 0.0  # power delivered out of USB ports, 0 if none
    usb_ports: list[float] = field(default_factory=list)  # per-port watts
    soc_percent: int | None = None
    voltage_v: float | None = None
    adapter_name: str | None = None
    adapter_watts: int | None = None
    external_connected: bool = False

    @property
    def charging(self) -> bool:
        return self.battery_w is not None and self.battery_w > 0

    @property
    def laptop_w(self) -> float | None:
        """Power the laptop itself is consuming: total system draw minus
        USB devices. (Real telemetry shows SystemLoad includes USB-out —
        input = SystemLoad + BatteryPower holds exactly.)"""
        if self.system_w is None:
            return None
        return max(self.system_w - self.usb_out_w, 0.0)


def parse_ioreg(text: str) -> PowerReading:
    """Extract a PowerReading from raw ioreg output. Never raises on
    missing fields — anything unparseable stays None/0."""
    r = PowerReading()

    telemetry = _find_block(text, "PowerTelemetryData")
    power_in = _fix_sign(_get_int(telemetry, "SystemPowerIn"))
    system_load = _fix_sign(_get_int(telemetry, "SystemLoad"))
    battery_power = _fix_sign(_get_int(telemetry, "BatteryPower"))

    if power_in is not None:
        r.input_w = power_in / 1000
    if system_load is not None:
        r.system_w = system_load / 1000

    # Battery voltage: prefer the raw top-level reading; "Voltage" alone is
    # ambiguous (it also appears inside BatteryData), so fall back to that
    # scoped occurrence.
    voltage = _get_int(text, "AppleRawBatteryVoltage")
    if voltage is None:
        voltage = _get_int(_find_block(text, "BatteryData"), "Voltage")
    if voltage:
        r.voltage_v = voltage / 1000

    if battery_power is not None:
        r.battery_w = battery_power / 1000
    else:
        # Fallback: Amperage (mA, signed via wraparound) x pack voltage.
        amperage = _fix_sign(_get_int(text, "Amperage"))
        if amperage is not None and r.voltage_v is not None:
            r.battery_w = (amperage / 1000) * r.voltage_v

    battery_data = _find_block(text, "BatteryData")
    r.soc_percent = _get_int(battery_data, "StateOfCharge")
    if r.soc_percent is None:
        r.soc_percent = _get_int(text, "CurrentCapacity")

    adapter = _find_block(text, "AdapterDetails")
    if adapter is None:
        raw_adapters = _find_block(text, "AppleRawAdapterDetails")
        adapter = _split_array_items(raw_adapters)[0] if raw_adapters else None
    if adapter:
        r.adapter_name = _get_str(adapter, "Name") or _get_str(adapter, "Description")
        r.adapter_watts = _get_int(adapter, "Watts")

    r.external_connected = bool(_get_bool(text, "ExternalConnected"))

    out_details = _find_block(text, "PowerOutDetails")
    if out_details:
        for item in _split_array_items(out_details):
            mw = _get_int(item, "FilteredPower")
            if mw is None:
                mw = _get_int(item, "Watts")  # already mW despite the name
            if mw:
                r.usb_ports.append(mw / 1000)
        r.usb_out_w = sum(r.usb_ports)

    return r


def apply_smc_overlay(r: PowerReading, smc_values: dict[str, float | None]) -> PowerReading:
    """Return a copy of `r` with wattage fields replaced by live SMC sensor
    readings, when available.

    `ioreg`'s AppleSmartBattery telemetry is a cache populated by the
    battery gauge IC on its own schedule — confirmed empirically to sit
    frozen for 20+ seconds at a stretch even polled every second. The SMC
    keys PSTR ("System Total") and PDTR ("DC In" / adapter) are live
    hardware sensor values that update sub-second, so they replace
    input_w/system_w as the real-time signal. Battery net flow is derived
    as PDTR - PSTR (adapter surplus charges the battery, positive;
    adapter deficit is drawn from the battery, negative) rather than
    trusting the SMC battery-rail key's sign convention, which isn't
    independently confirmed. usb_out_w, soc_percent, voltage_v, and
    adapter identity stay sourced from ioreg — they don't need to be
    sub-second and have no SMC equivalent found on this hardware.

    Missing/None entries in `smc_values` leave the corresponding ioreg
    value in place, so a partial or failed SMC read degrades gracefully.
    """
    pstr = smc_values.get("PSTR")
    pdtr = smc_values.get("PDTR")
    out = replace(r)
    if pstr is not None:
        out.system_w = pstr
    if pdtr is not None:
        out.input_w = pdtr
    if pstr is not None and pdtr is not None:
        out.battery_w = pdtr - pstr
    return out


def compute_streams(
    r: PowerReading,
) -> tuple[list[tuple[str, float]], list[tuple[str, float]]]:
    """Streams for the flow graphic: (inputs, outputs), watts per stream.

    Inputs (left side): the AC adapter, and the battery while discharging.
    Outputs (right side): the laptop itself ("System"), each USB-out
    device, and the battery while charging. Streams below 0.05 W are
    dropped.
    """
    min_w = 0.05
    inputs: list[tuple[str, float]] = []
    outputs: list[tuple[str, float]] = []

    if r.external_connected and r.input_w is not None and r.input_w > min_w:
        inputs.append(("Adapter", r.input_w))
    if r.battery_w is not None and r.battery_w < -min_w:
        inputs.append(("Battery", -r.battery_w))

    if r.laptop_w is not None and r.laptop_w > min_w:
        outputs.append(("System", r.laptop_w))
    for i, w in enumerate(r.usb_ports):
        if w > min_w:
            name = "USB-C device" if len(r.usb_ports) == 1 else f"USB-C device {i + 1}"
            outputs.append((name, w))
    if r.battery_w is not None and r.battery_w > min_w:
        outputs.append(("Battery", r.battery_w))

    return inputs, outputs


# Tunable thresholds for is_idle_at_full_charge(): plugged into a "decent"
# (fixed-wattage-floor) charger at high SoC, full stop — deliberately not
# looking at instantaneous battery_w at all, since near-100% charge-
# controller trickle jitter is noise regardless of its exact magnitude,
# and a weak/unknown charger is excluded by the wattage floor rather than
# by comparing against current draw.
IDLE_SOC_THRESHOLD = 97  # percent
IDLE_MIN_ADAPTER_WATTS = 60  # below this is phone/tablet-charger territory,
# not a "decent" charger for a MacBook — smallest standard MacBook Pro/Air
# adapters start around 60-67W


def is_idle_at_full_charge(r: PowerReading) -> bool:
    """True when the machine is plugged into a decent charger and nearly
    full — the two conditions worth hiding the wattage number for,
    regardless of what the instantaneous battery flow happens to read."""
    if not r.external_connected:
        return False
    if r.soc_percent is None or r.soc_percent < IDLE_SOC_THRESHOLD:
        return False
    if r.adapter_watts is None or r.adapter_watts < IDLE_MIN_ADAPTER_WATTS:
        return False
    return True


def balance_error_w(r: PowerReading) -> float | None:
    """Internal sanity check: input power should roughly equal what the
    machine does with it. Real dumps show SystemLoad already includes
    USB-out power, so accept whichever interpretation balances better:

        input ≈ system + battery                  (USB-out inside SystemLoad)
        input ≈ system + usb_out + battery        (disjoint)

    Returns the absolute mismatch in watts, or None if fields are missing.
    Noise of a few watts is normal — never treat this as an error.
    """
    if r.input_w is None or r.system_w is None or r.battery_w is None:
        return None
    base = r.system_w + r.battery_w
    return min(
        abs(r.input_w - base),
        abs(r.input_w - (base + r.usb_out_w)),
    )
