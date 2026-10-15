"""Parameter table for the TC 600 electronic drive unit.

Provenance note: the operating instructions in ../Documentation (PM 0504 BE)
cover wiring and the electrical spec but defer the parameter list to PM 0488 BN
and PM 0547 BN, neither of which is in this repository. The table below is the
standard Pfeiffer Vacuum parameter set, which the TC 600 implements, but treat
it as a strong starting point rather than gospel for your exact firmware.

Run `./tc600ctl.py verify` to have the pump itself confirm which of these it
accepts -- anything the firmware does not implement answers NO_DEF, and the
verify command reports that per parameter.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import protocol


@dataclass(frozen=True)
class Parameter:
    """One addressable parameter: how to reach it and how to read its payload."""

    number: int
    name: str
    datatype: type[protocol.DataType]
    access: str  # "r", "w" or "rw"
    unit: str = ""
    description: str = ""

    @property
    def readable(self) -> bool:
        return "r" in self.access

    @property
    def writable(self) -> bool:
        return "w" in self.access

    def decode(self, payload: str):
        return self.datatype.decode(payload)

    def encode(self, value) -> str:
        return self.datatype.encode(value)


_T = protocol
_BOOL = _T.BooleanOld
_UINT = _T.UInteger
_REAL = _T.UReal
_STR = _T.String6
_SHORT = _T.UShortInt
_EXPO = _T.UExpoNew

PARAMETERS: tuple[Parameter, ...] = (
    # --- commands -------------------------------------------------------
    Parameter(1, "Heating", _BOOL, "rw", "", "Pump heating on/off"),
    Parameter(2, "Standby", _BOOL, "rw", "", "Run at the standby setpoint (P717)"),
    Parameter(4, "RUTimeCtrl", _BOOL, "rw", "", "Run-up time monitoring"),
    Parameter(9, "ErrorAckn", _BOOL, "w", "", "Acknowledge/clear a pending fault"),
    Parameter(10, "PumpgStatn", _BOOL, "rw", "", "Pumping station on/off"),
    Parameter(12, "EnableVent", _BOOL, "rw", "", "Release the venting valve"),
    Parameter(13, "EnableBrake", _BOOL, "rw", "", "Pre-selection brake"),
    Parameter(17, "CfgSpdSwPt", _SHORT, "rw", "", "Speed switchpoint configuration"),
    Parameter(19, "CfgDO2", _SHORT, "rw", "", "Digital output 2 function"),
    Parameter(23, "MotorPump", _BOOL, "rw", "", "Turbopump motor on/off"),
    Parameter(24, "CfgDO1", _SHORT, "rw", "", "Digital output 1 function"),
    Parameter(25, "OpModeBKP", _SHORT, "rw", "", "Backing pump operating mode"),
    # Confirmed against firmware 017500: P26 answers "000", a 3-character
    # payload, so it is a mode selector rather than the boolean assumed here
    # originally. PM 0547 BE calls it OpModeTMP.
    Parameter(26, "OpModeTMP", _SHORT, "rw", "",
              "0=final rotation speed operation, 1=rotation speed setting mode"),
    Parameter(27, "GasMode", _SHORT, "rw", "", "0=heavy gases, 1=light gases, 2=helium"),
    Parameter(28, "OpModeRemote", _SHORT, "rw", "",
              "0=standard, 1=remote priority, 2=remote error acknowledge"),
    Parameter(29, "OpModeDrive", _BOOL, "rw", "",
              "Drive unit: off=maximum, on=reduced power intake"),
    Parameter(30, "VentMode", _SHORT, "rw", "",
              "0=controlled venting, 1=no venting, 2=venting on"),
    Parameter(32, "CfgOut4", _SHORT, "rw", "",
              "HEAT/TMS output: 0=heating or TMS, 1=sealing gas valve"),
    Parameter(35, "CfgAccA1", _SHORT, "rw", "", "Accessory connection A1 function"),
    Parameter(36, "CfgAccB2", _SHORT, "rw", "", "Accessory connection B2 function"),
    Parameter(37, "CfgAccB1", _SHORT, "rw", "", "Accessory connection B1 function"),
    Parameter(38, "CfgAccA2", _SHORT, "rw", "", "Accessory connection A2 function"),
    Parameter(50, "SealingGas", _BOOL, "rw", "", "Sealing gas valve on/off"),
    Parameter(55, "CfgAO1", _SHORT, "rw", "", "Analog output 1 function"),
    Parameter(60, "CtrlViaInt", _SHORT, "rw", "", "Control source: 1=remote, 2=RS485"),
    Parameter(61, "IntSelLckd", _BOOL, "rw", "", "Lock the interface selector"),
    Parameter(62, "CfgDI1", _SHORT, "rw", "", "Digital input 1 function"),
    Parameter(63, "CfgDI2", _SHORT, "rw", "", "Digital input 2 function"),
    # --- status readouts -------------------------------------------------
    Parameter(300, "RemotePrio", _BOOL, "r", "", "Remote plug has priority"),
    Parameter(301, "OilDeficiency", _BOOL, "r", "", "Turbopump oil deficiency"),
    Parameter(302, "SpdSwPtAtt", _BOOL, "r", "", "Speed switchpoint attained"),
    Parameter(303, "ErrorCode", _STR, "r", "", "Active error code"),
    Parameter(304, "OvTempElec", _BOOL, "r", "", "Electronics over temperature"),
    Parameter(305, "OvTempPump", _BOOL, "r", "", "Pump over temperature"),
    Parameter(306, "SetSpdAtt", _BOOL, "r", "", "Set rotation speed attained"),
    Parameter(307, "PumpAccel", _BOOL, "r", "", "Pump is accelerating"),
    Parameter(308, "SetRotSpd", _UINT, "r", "Hz", "Rotation speed setpoint"),
    Parameter(309, "ActualSpd", _UINT, "r", "Hz", "Actual rotation speed"),
    Parameter(310, "DrvCurrent", _REAL, "r", "A", "Drive current"),
    Parameter(311, "OpHrsPump", _UINT, "r", "h", "Pump operating hours"),
    Parameter(312, "FwVersion", _STR, "r", "", "Electronic drive unit firmware"),
    Parameter(313, "DrvVoltage", _REAL, "r", "V", "Drive voltage"),
    Parameter(314, "OpHrsElec", _UINT, "r", "h", "Electronics operating hours"),
    Parameter(315, "NominalSpd", _UINT, "r", "Hz", "Nominal rotation speed"),
    Parameter(316, "DrvPower", _UINT, "r", "W", "Drive power"),
    Parameter(319, "PumpCycles", _UINT, "r", "", "Pump start/stop cycles"),
    # The TMS (temperature management system) block, all confirmed present on
    # firmware 017500. P335 reads 1 on this unit, i.e. it is a TMS installation
    # rather than a conventional casing heater.
    Parameter(331, "TMSActTemp", _UINT, "r", "°C", "TMS heating actual value"),
    Parameter(333, "TMSSteady", _BOOL, "r", "", "TMS regulator engaged"),
    Parameter(334, "TMSMaxTemp", _UINT, "r", "°C", "Maximum TMS temperature"),
    Parameter(335, "HeatType", _SHORT, "r", "",
              "0=conventional heating, 1=TMS"),
    Parameter(326, "TempElec", _UINT, "r", "°C", "Electronics temperature"),
    Parameter(330, "TempPmpBot", _UINT, "r", "°C", "Pump bottom part temperature"),
    Parameter(336, "AccelDecel", _UINT, "r", "rpm/s", "Acceleration / deceleration"),
    Parameter(342, "TempBearng", _UINT, "r", "°C", "Bearing temperature"),
    Parameter(346, "TempMotor", _UINT, "r", "°C", "Motor temperature"),
    Parameter(349, "ElecName", _STR, "r", "", "Electronic drive unit type"),
    Parameter(354, "HWVersion", _STR, "r", "", "Hardware version"),
    Parameter(360, "ErrHist1", _STR, "r", "", "Error history, most recent"),
    Parameter(361, "ErrHist2", _STR, "r", "", "Error history 2"),
    Parameter(362, "ErrHist3", _STR, "r", "", "Error history 3"),
    Parameter(363, "ErrHist4", _STR, "r", "", "Error history 4"),
    Parameter(364, "ErrHist5", _STR, "r", "", "Error history 5"),
    Parameter(365, "ErrHist6", _STR, "r", "", "Error history 6"),
    Parameter(366, "ErrHist7", _STR, "r", "", "Error history 7"),
    Parameter(367, "ErrHist8", _STR, "r", "", "Error history 8"),
    Parameter(368, "ErrHist9", _STR, "r", "", "Error history 9"),
    Parameter(369, "ErrHist10", _STR, "r", "", "Error history, oldest"),
    Parameter(397, "SetRotSpdRpm", _UINT, "r", "rpm", "Rotation speed setpoint"),
    Parameter(398, "ActualSpdRpm", _UINT, "r", "rpm", "Actual rotation speed"),
    Parameter(399, "NominalSpdRpm", _UINT, "r", "rpm", "Nominal rotation speed"),
    # --- setpoints -------------------------------------------------------
    Parameter(700, "RUTimeSVal", _UINT, "rw", "min", "Run-up time limit"),
    Parameter(701, "SpdSwPt", _SHORT, "rw", "%", "Speed switchpoint 1"),
    Parameter(704, "TMSHeatSVal", _UINT, "rw", "°C",
              "TMS heating setpoint, 30-90"),
    Parameter(707, "SpdSVal", _REAL, "rw", "%", "Speed setpoint in setting mode"),
    Parameter(708, "PwrSVal", _SHORT, "rw", "%", "Power consumption limit"),
    Parameter(710, "SwOffBKP", _UINT, "rw", "W", "Backing pump switch-off threshold"),
    Parameter(711, "SwOnBKP", _UINT, "rw", "W", "Backing pump switch-on threshold"),
    # Confirmed against firmware 017500: answers "066", i.e. 66 % as a
    # 3-character payload, not the 6-character u_real assumed originally.
    Parameter(717, "StdbySVal", _SHORT, "rw", "%", "Standby speed setpoint"),
    Parameter(719, "SpdSwPt2", _SHORT, "rw", "%", "Speed switchpoint 2"),
    Parameter(720, "VentSpd", _SHORT, "rw", "%", "Venting speed threshold"),
    Parameter(721, "VentTime", _UINT, "rw", "s", "Venting valve open time"),
    Parameter(730, "PrsSn1Name", _STR, "rw", "", "Pressure gauge 1 identifier"),
    Parameter(738, "PrsCorrPi1", _REAL, "rw", "", "Pressure correction factor"),
    Parameter(739, "Pressure", _EXPO, "rw", "hPa", "Measured pressure"),
    Parameter(777, "NominalSpdCfg", _UINT, "rw", "Hz", "Nominal speed configuration"),
    Parameter(794, "ParamSet", _SHORT, "rw", "", "Parameter set selection"),
    Parameter(795, "ServiceLine", _SHORT, "rw", "", "Insert service line"),
    Parameter(797, "RS485Address", _SHORT, "rw", "", "Serial interface address"),
)

BY_NUMBER: dict[int, Parameter] = {p.number: p for p in PARAMETERS}
BY_NAME: dict[str, Parameter] = {p.name.lower(): p for p in PARAMETERS}

#: Parameters shown by `status` and `monitor`, in display order.
#:
#: The temperature readouts (326, 330, 342, 346) are deliberately absent: TC 600
#: firmware 017500 answers NO_DEF for all four, so polling them only cost four
#: wasted round trips per refresh. The over-temperature flags 304 and 305 are
#: implemented and stand in for them.
STATUS_VIEW: tuple[int, ...] = (
    309, 308, 310, 313, 316, 303, 304, 305, 307, 306, 302, 311,
)


def lookup(key: str | int) -> Parameter:
    """Resolve a parameter by number ("309", 309) or by name ("ActualSpd")."""
    if isinstance(key, int):
        number = key
    else:
        text = key.strip()
        if text.isdigit():
            number = int(text)
        else:
            try:
                return BY_NAME[text.lower()]
            except KeyError:
                raise KeyError(f"unknown parameter name {key!r}") from None
    try:
        return BY_NUMBER[number]
    except KeyError:
        raise KeyError(f"unknown parameter number {number}") from None
