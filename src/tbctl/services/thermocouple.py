"""Type-K thermocouple conversion, NIST ITS-90 polynomials.

Forward:  T (degC) -> EMF (mV), used for cold-junction compensation and by
the simulator. Inverse: EMF (mV) -> T (degC), used by acquisition:

    T_hot = inverse( V_measured_mV + forward(T_cold_junction) )

Coefficients from the NIST SRD-60 type-K tables. Cross-checked in tests by
forward/inverse round-trip agreement (independent fits) and table anchors.
"""

from __future__ import annotations

import math

# forward t90 -> E, range -270..0 degC
_FWD_NEG = [
    0.000000000000e00,
    0.394501280250e-01,
    0.236223735980e-04,
    -0.328589067840e-06,
    -0.499048287770e-08,
    -0.675090591730e-10,
    -0.574103274280e-12,
    -0.310888728940e-14,
    -0.104516093650e-16,
    -0.198892668780e-19,
    -0.163226974860e-22,
]
# forward t90 -> E, range 0..1372 degC (plus exponential term a0*exp(a1*(t-a2)^2))
_FWD_POS = [
    -0.176004136860e-01,
    0.389212049750e-01,
    0.185587700320e-04,
    -0.994575928740e-07,
    0.318409457190e-09,
    -0.560728448890e-12,
    0.560750590590e-15,
    -0.320207200030e-18,
    0.971511471520e-22,
    -0.121047212750e-25,
]
_A0, _A1, _A2 = 0.118597600000e00, -0.118343200000e-03, 0.126968600000e03

# inverse E -> t90, three ranges
_INV_NEG = [  # -5.891..0 mV  (-200..0 degC)
    0.0000000e00,
    2.5173462e01,
    -1.1662878e00,
    -1.0833638e00,
    -8.9773540e-01,
    -3.7342377e-01,
    -8.6632643e-02,
    -1.0450598e-02,
    -5.1920577e-04,
]
_INV_MID = [  # 0..20.644 mV  (0..500 degC)
    0.000000e00,
    2.508355e01,
    7.860106e-02,
    -2.503131e-01,
    8.315270e-02,
    -1.228034e-02,
    9.804036e-04,
    -4.413030e-05,
    1.057734e-06,
    -1.052755e-08,
]
_INV_HIGH = [  # 20.644..54.886 mV  (500..1372 degC)
    -1.318058e02,
    4.830222e01,
    -1.646031e00,
    5.464731e-02,
    -9.650715e-04,
    8.802193e-06,
    -3.110810e-08,
]


def _poly(coeffs: list[float], x: float) -> float:
    acc = 0.0
    for c in reversed(coeffs):
        acc = acc * x + c
    return acc


def type_k_emf_mv(t_degc: float) -> float:
    """Forward: junction temperature (degC) -> EMF (mV)."""
    if t_degc < 0.0:
        return _poly(_FWD_NEG, t_degc)
    return _poly(_FWD_POS, t_degc) + _A0 * math.exp(_A1 * (t_degc - _A2) ** 2)


def type_k_temp_degc(emf_mv: float) -> float:
    """Inverse: EMF (mV) -> junction temperature (degC)."""
    if emf_mv < 0.0:
        return _poly(_INV_NEG, emf_mv)
    if emf_mv < 20.644:
        return _poly(_INV_MID, emf_mv)
    return _poly(_INV_HIGH, emf_mv)


def compensated_temp_degc(v_measured: float, cold_junction_degc: float) -> float:
    """Measured differential voltage (V) + CJ temperature -> hot-junction degC."""
    total_mv = v_measured * 1000.0 + type_k_emf_mv(cold_junction_degc)
    return type_k_temp_degc(total_mv)
