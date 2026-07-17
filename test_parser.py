"""Tests for parser.py against real-format ioreg fixtures.

Samples 1-3 encode the ground-truth numbers from real machine snapshots
(see README/plan). fixtures/live_sample.txt is a full, unmodified capture
of `ioreg -rw0 -r -n AppleSmartBattery`.

Run with `python3 -m pytest` or plain `python3 test_parser.py`.
"""

from pathlib import Path

from parser import (
    PowerReading,
    apply_smc_overlay,
    balance_error_w,
    compute_streams,
    is_idle_at_full_charge,
    parse_ioreg,
    to_signed64,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _wrap(n: int) -> int:
    """Encode a negative value the way ioreg does: unsigned 64-bit."""
    return n % 2**64


# Sample 1: plugged in, light load, trickle charging. No USB-out.
SAMPLE_1 = (
    '+-o AppleSmartBattery  <class AppleSmartBattery, id 0x100000c29>\n'
    '    {\n'
    '      "CurrentCapacity" = 95\n'
    '      "Amperage" = 172\n'
    '      "ExternalConnected" = Yes\n'
    '      "AppleRawBatteryVoltage" = 13130\n'
    '      "BatteryData" = {"CellVoltage"=(4377,4377,4376),"StateOfCharge"=95,'
    '"Voltage"=13126,"DesignCapacity"=8579}\n'
    '      "AdapterDetails" = {"Watts"=140,"Description"="pd charger",'
    '"Name"="140W USB-C Power Adapter","AdapterVoltage"=28000,'
    '"UsbHvcMenu"=({"Index"=0,"MaxCurrent"=2960,"MaxVoltage"=5000})}\n'
    '      "PowerTelemetryData" = {"SystemLoad"=9810,"SystemVoltageIn"=27625,'
    '"SystemPowerIn"=12080,"SystemCurrentIn"=437,"BatteryPower"=2270}\n'
    '    }\n'
)

# Sample 2: plugged in but load exceeds adapter; battery discharging.
# BatteryPower and Amperage appear as unsigned 64-bit wraparounds.
SAMPLE_2 = (
    '+-o AppleSmartBattery  <class AppleSmartBattery, id 0x100000c29>\n'
    '    {\n'
    '      "CurrentCapacity" = 72\n'
    f'      "Amperage" = {_wrap(-2103)}\n'
    '      "ExternalConnected" = Yes\n'
    '      "AppleRawBatteryVoltage" = 12700\n'
    '      "BatteryData" = {"StateOfCharge"=72,"Voltage"=12695}\n'
    '      "AdapterDetails" = {"Watts"=70,"Name"="70W USB-C Power Adapter"}\n'
    '      "PowerTelemetryData" = {"SystemLoad"=75100,"SystemPowerIn"=48400,'
    f'"BatteryPower"={_wrap(-26700)}}}\n'
    '    }\n'
)

# Sample 3: plugged in, charging hard, phone charging off a USB-C port.
SAMPLE_3 = (
    '+-o AppleSmartBattery  <class AppleSmartBattery, id 0x100000c29>\n'
    '    {\n'
    '      "CurrentCapacity" = 88\n'
    '      "Amperage" = 2380\n'
    '      "ExternalConnected" = Yes\n'
    '      "AppleRawBatteryVoltage" = 13070\n'
    '      "BatteryData" = {"StateOfCharge"=88,"Voltage"=13066}\n'
    '      "AdapterDetails" = {"Watts"=140,"Name"="140W USB-C Power Adapter"}\n'
    '      "PowerOutDetails" = ({"PowerState"=0,"PortIndex"=3,"FilteredPower"=14300,'
    '"Watts"=14100,"ConfiguredVoltage"=5000,"Current"=2860,"AdapterVoltage"=5141})\n'
    '      "PowerTelemetryData" = {"SystemLoad"=31200,"SystemPowerIn"=62300,'
    '"BatteryPower"=31100}\n'
    '    }\n'
)


def test_to_signed64():
    assert to_signed64(17390) == 17390
    assert to_signed64(0) == 0
    assert to_signed64(_wrap(-26700)) == -26700
    assert to_signed64(2**63) == -(2**63)


def test_sample_1_trickle_charge():
    r = parse_ioreg(SAMPLE_1)
    assert r.input_w == 12.08
    assert r.system_w == 9.81
    assert r.battery_w == 2.27
    assert r.charging
    assert r.usb_out_w == 0.0
    assert r.usb_ports == []
    assert r.soc_percent == 95
    assert r.voltage_v == 13.13
    assert r.adapter_name == "140W USB-C Power Adapter"
    assert r.adapter_watts == 140  # scoped: not confused with USB-out mW
    assert r.external_connected


def test_sample_2_discharge_twos_complement():
    r = parse_ioreg(SAMPLE_2)
    assert r.input_w == 48.4
    assert r.system_w == 75.1
    assert r.battery_w == -26.7
    assert not r.charging
    assert r.usb_out_w == 0.0
    assert r.soc_percent == 72


def test_sample_3_usb_out():
    r = parse_ioreg(SAMPLE_3)
    assert r.input_w == 62.3
    assert r.system_w == 31.2
    assert r.battery_w == 31.1
    assert r.usb_ports == [14.3]  # FilteredPower preferred over Watts
    assert r.usb_out_w == 14.3
    assert r.adapter_watts == 140


def test_amperage_voltage_fallback():
    # Strip BatteryPower so the parser must fall back to Amperage x Voltage.
    text = SAMPLE_2.replace(f',"BatteryPower"={_wrap(-26700)}', "")
    r = parse_ioreg(text)
    assert r.battery_w is not None
    assert abs(r.battery_w - (-2.103 * 12.700)) < 0.001  # ≈ -26.7 W


def test_garbage_input_never_raises():
    for text in ("", "not ioreg output at all", '"BatteryPower"='):
        r = parse_ioreg(text)
        assert r.battery_w is None
        assert r.input_w is None
        assert r.usb_out_w == 0.0
        assert balance_error_w(r) is None


def test_live_capture():
    path = FIXTURES / "live_sample.txt"
    if not path.exists():
        return  # fixture only exists on the machine it was captured on
    r = parse_ioreg(path.read_text())
    assert r.input_w == 46.392
    assert r.system_w == 29.002
    assert r.battery_w == 17.39
    assert r.usb_ports == [13.591]
    assert r.soc_percent == 99
    assert r.voltage_v == 13.135
    assert r.adapter_name == "140W USB-C Power Adapter"
    assert r.adapter_watts == 140
    assert r.external_connected
    assert balance_error_w(r) < 1.0  # 46.4 ≈ 29.0 + 17.4


def test_smc_overlay_replaces_wattage_and_derives_battery():
    r = parse_ioreg(SAMPLE_3)  # ioreg-only: input=62.3, system=31.2, battery=31.1
    out = apply_smc_overlay(r, {"PSTR": 40.0, "PDTR": 55.0})
    assert out.input_w == 55.0
    assert out.system_w == 40.0
    assert out.battery_w == 15.0  # PDTR - PSTR, not the ioreg value
    # usb_out_w, soc_percent, adapter identity untouched by the overlay
    assert out.usb_ports == r.usb_ports
    assert out.soc_percent == r.soc_percent
    assert out.adapter_name == r.adapter_name


def test_smc_overlay_discharge_case():
    r = parse_ioreg(SAMPLE_2)
    out = apply_smc_overlay(r, {"PSTR": 75.0, "PDTR": 48.0})
    assert out.battery_w == -27.0  # deficit drawn from battery
    assert not out.charging


def test_smc_overlay_partial_data_degrades_gracefully():
    r = parse_ioreg(SAMPLE_1)
    only_pstr = apply_smc_overlay(r, {"PSTR": 12.0, "PDTR": None})
    assert only_pstr.system_w == 12.0
    assert only_pstr.input_w == r.input_w  # ioreg value kept, no PDTR to overlay
    assert only_pstr.battery_w == r.battery_w  # can't derive without both

    empty = apply_smc_overlay(r, {})
    assert empty.system_w == r.system_w
    assert empty.input_w == r.input_w
    assert empty.battery_w == r.battery_w
    assert empty.soc_percent == r.soc_percent  # no BRSC, ioreg value kept


def test_smc_overlay_brsc_replaces_soc_percent():
    # StateOfCharge (ioreg) and BRSC (SMC) are independent live reads of
    # the same fuel-gauge chip and routinely differ by ~1 point; BRSC wins.
    r = parse_ioreg(SAMPLE_1)  # ioreg soc_percent == 95
    out = apply_smc_overlay(r, {"BRSC": 91.0})
    assert out.soc_percent == 91


def test_smc_overlay_does_not_mutate_input():
    r = parse_ioreg(SAMPLE_3)
    original_system_w = r.system_w
    apply_smc_overlay(r, {"PSTR": 999.0, "PDTR": 999.0})
    assert r.system_w == original_system_w


def test_compute_streams_basic():
    r = parse_ioreg(SAMPLE_3)  # charging, one USB-C device
    inputs, outputs = compute_streams(r)
    assert inputs == [("Adapter", 62.3)]
    names = {name for name, _ in outputs}
    assert "System" in names
    assert "USB-C device" in names
    assert "Battery" in names  # charging -> battery is an output


def test_compute_streams_discharging_battery_is_input():
    r = parse_ioreg(SAMPLE_2)  # discharging, unplugged-equivalent load
    inputs, outputs = compute_streams(r)
    input_names = {name for name, _ in inputs}
    assert "Battery" in input_names
    output_names = {name for name, _ in outputs}
    assert "Battery" not in output_names


def _idle_reading(**overrides):
    base = dict(
        battery_w=0.4, soc_percent=98, external_connected=True,
        system_w=30.0, usb_out_w=0.0, adapter_watts=140,
    )
    base.update(overrides)
    return PowerReading(**base)


def test_idle_all_conditions_met():
    assert is_idle_at_full_charge(_idle_reading())


def test_idle_soc_too_low():
    assert not is_idle_at_full_charge(_idle_reading(soc_percent=90))


def test_idle_ignores_instantaneous_battery_flow():
    # Deliberately not a signal any more — near-100% trickle jitter is
    # noise regardless of its exact magnitude, as long as SoC and the
    # adapter both check out.
    assert is_idle_at_full_charge(_idle_reading(battery_w=6.0))
    assert is_idle_at_full_charge(_idle_reading(battery_w=None))


def test_idle_weak_adapter_is_not_decent():
    assert not is_idle_at_full_charge(_idle_reading(adapter_watts=30))


def test_idle_unknown_adapter_watts_is_not_decent():
    # Fail closed: an unrated/unrecognized adapter isn't assumed decent.
    assert not is_idle_at_full_charge(_idle_reading(adapter_watts=None))


def test_idle_not_plugged_in():
    assert not is_idle_at_full_charge(_idle_reading(external_connected=False))


def test_idle_missing_soc_is_never_idle():
    assert not is_idle_at_full_charge(_idle_reading(soc_percent=None))


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
