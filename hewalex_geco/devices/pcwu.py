from .base import BaseDevice

class PCWU(BaseDevice):
    """Hewalex PCWU 2.3k heat pump with G426 controller.
    
    Register map verified against live controller screen dumps.
    2026-08-05 — addresses 274–302 confirmed by matching displayed values.
    """

    REG_MAX_ADR = 536
    REG_MAX_NUM = 100
    REG_CONFIG_START = 274   # Was 302 — verified correct for this hardware
    REG_STATUS_NUM = 104

    registers = {

        # ── Status registers ─────────────────────────────────────────
        120: { 'type': 'date', 'name': 'date', 'desc': 'Date', 'options': None },
        124: { 'type': 'time', 'name': 'time', 'desc': 'Time', 'options': None },

        128: { 'type': 'te10', 'name': 'T1', 'desc': 'T1 - Ambient temperature', 'options': None },
        130: { 'type': 'te10', 'name': 'T2', 'desc': 'T2 - Tank bottom temperature', 'options': None },
        132: { 'type': 'te10', 'name': 'T3', 'desc': 'T3 - Tank top temperature', 'options': None },
        134: { 'type': 'te10', 'name': 'T4', 'desc': 'T4 - Unknown sensor', 'options': None },
        136: { 'type': 'te10', 'name': 'T5', 'desc': 'T5 - Unused sensor (absent=-50)', 'options': None },
        138: { 'type': 'te10', 'name': 'T6', 'desc': 'T6 - HP water inlet temperature', 'options': None },
        140: { 'type': 'te10', 'name': 'T7', 'desc': 'T7 - HP water outlet temperature', 'options': None },
        142: { 'type': 'te10', 'name': 'T8', 'desc': 'T8 - Evaporator temperature', 'options': None },
        144: { 'type': 'te10', 'name': 'T9', 'desc': 'T9 - Before compressor', 'options': None },
        146: { 'type': 'te10', 'name': 'T10', 'desc': 'T10 - After compressor', 'options': None },

        148: { 'type': 'te10', 'name': 'T11', 'desc': 'T11 - Max limit placeholder (130°C)', 'options': None },
        150: { 'type': 'te10', 'name': 'T12', 'desc': 'T12 - Max limit placeholder (130°C)', 'options': None },
        152: { 'type': 'te10', 'name': 'T13', 'desc': 'T13 - Max limit placeholder (130°C)', 'options': None },
        154: { 'type': 'te10', 'name': 'T14', 'desc': 'T14 - Max limit placeholder (130°C)', 'options': None },
        156: { 'type': 'te10', 'name': 'T15', 'desc': 'T15 - Unused sensor (absent=-50)', 'options': None },
        158: { 'type': 'te10', 'name': 'T16', 'desc': 'T16 - Unused sensor (absent=-50)', 'options': None },

        194: { 'type': 'bool', 'name': 'IsManual', 'desc': 'Manual mode active', 'options': None },
        196: { 'type': 'mask', 'name': [
            'FanON',
            None,
            'CirculationPumpON',
            None,
            None,
            'HeatPumpON',
            None,
            None,
            None,
            None,
            None,
            'CompressorON',
            'HeaterEON',
        ], 'desc': 'Status bitmask', 'options': None },
        198: { 'type': 'word', 'name': 'EV1', 'desc': 'Expansion valve position (raw %, do NOT /10)', 'options': None },
        202: { 'type': 'word', 'name': 'WaitingStatus', 'desc': '0=Running, 1=ExtOff, 2=MQTTOff, 16=Defrost', 'options': None },

        216: { 'type': 'fl10', 'name': 'AverageCOP', 'desc': 'Average COP', 'options': None },
        222: { 'type': 'fl10', 'name': 'HourlyCOP', 'desc': 'Hourly COP', 'options': None },

        # ── Config registers — VERIFIED against controller screen ────
        274: { 'type': 'te10', 'name': 'TapWaterTemp', 'options': list(range(100, 610, 10)), 'desc': 'Target water temperature [10-60 °C]' },
        276: { 'type': 'te10', 'name': 'TapWaterHysteresis', 'options': list(range(20, 110, 10)), 'desc': 'HP start hysteresis [2-10 K]' },
        278: { 'type': 'te10', 'name': 'AmbientMinTemp', 'options': list(range(-100, 110, 10)), 'desc': 'Minimum ambient T1 [-10-10 °C]' },

        298: { 'type': 'word', 'name': 'DefrostingInterval', 'options': list(range(30, 91)), 'desc': 'Defrost cycle delay [30-90 min] (RAW, no /10)' },
        300: { 'type': 'te10', 'name': 'DefrostingStartTemp', 'options': list(range(-300, 10, 10)), 'desc': 'Defrost trigger temp [-30-0 °C]' },
        302: { 'type': 'te10', 'name': 'DefrostingStopTemp', 'options': list(range(20, 310, 10)), 'desc': 'Defrost end temp [2-30 °C]' },
    }

    # NOTE: disable/enable removed — register 304 is NOT HeatPumpEnabled on this hardware.
    # Writing to the old (wrong) address caused factory resets. Do NOT re-enable until
    # the correct on/off register is found via further testing.
