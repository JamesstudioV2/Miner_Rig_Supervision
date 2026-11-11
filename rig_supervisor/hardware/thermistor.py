"""NTC thermistor maths.

Wiring (as described):   supply ── R_fixed (10 kΩ) ──┬── ADC input
                                                     NTC (10 kΩ @ 25 °C)
                                                      │
                                                     GND

    V_out = V_s * R_ntc / (R_ntc + R_fixed)   =>   R_ntc = R_fixed * V_out / (V_s - V_out)

Temperature from the Beta equation:  1/T = 1/T0 + ln(R/R25) / B   (T0 = 298.15 K)
"""

from __future__ import annotations

import math

T0_K = 298.15
KELVIN = 273.15

FAULT_TEXT = {
    "open": "Szakadás – nincs szenzor vagy elszakadt a vezeték",
    "short": "Rövidzár a szenzor vezetékében",
    "range": "A mért érték a mérési tartományon kívül esik",
    "adc": "ADC olvasási hiba",
    "noadc": "Nincs ADC beállítva",
}

VALID_RANGE_C = (-40.0, 150.0)


def resistance_from_voltage(v_out: float, v_supply: float, r_series: float) -> float:
    if v_out <= 0.0:
        return 0.0
    if v_out >= v_supply:
        return float("inf")
    return r_series * v_out / (v_supply - v_out)


def temperature_from_resistance(r_ntc: float, r25: float, beta: float) -> float:
    inv_t = 1.0 / T0_K + math.log(r_ntc / r25) / beta
    return 1.0 / inv_t - KELVIN


def resistance_from_temperature(temp_c: float, r25: float, beta: float) -> float:
    t_k = temp_c + KELVIN
    return r25 * math.exp(beta * (1.0 / t_k - 1.0 / T0_K))


def voltage_from_temperature(temp_c: float, v_supply: float, r_series: float, r25: float, beta: float) -> float:
    r = resistance_from_temperature(temp_c, r25, beta)
    return v_supply * r / (r + r_series)


def evaluate(v_out, v_supply, r_series, r25, beta, offset=0.0, gain=1.0, saturated=False) -> dict:
    """Convert a divider voltage to a calibrated temperature.

    Returns a dict: ``value`` (calibrated °C or None), ``raw_value`` (uncalibrated °C),
    ``ohm`` and ``fault`` (None or one of the FAULT_TEXT keys).
    """
    result = {"value": None, "raw_value": None, "ohm": None, "fault": None, "voltage": v_out}
    if v_out is None:
        result["fault"] = "adc"
        return result
    if saturated or v_out >= 0.985 * v_supply:
        result["fault"] = "open"
        result["ohm"] = float("inf")
        return result
    if v_out <= 0.01 * v_supply:
        result["fault"] = "short"
        result["ohm"] = 0.0
        return result
    r = resistance_from_voltage(v_out, v_supply, r_series)
    result["ohm"] = r
    try:
        t_raw = temperature_from_resistance(r, r25, beta)
    except (ValueError, ZeroDivisionError):
        result["fault"] = "range"
        return result
    result["raw_value"] = t_raw
    if not (VALID_RANGE_C[0] <= t_raw <= VALID_RANGE_C[1]):
        result["fault"] = "range"
        return result
    result["value"] = t_raw * gain + offset
    return result
