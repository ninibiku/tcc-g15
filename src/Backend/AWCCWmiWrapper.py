from enum import Enum
from typing import Optional, Tuple, Union
from wmi import _wmi_object # type: ignore

class AWCCWmiWrapper:
    SENSOR_ID_FIRST = 0x01
    SENSOR_ID_LAST = 0x30
    FAN_ID_FIRST = 0x31
    FAN_ID_LAST = 0x63

    class ThermalMode(Enum):
        Custom = 0
        Quiet = 0x96          # 150 - classic family
        Balanced = 0x97
        Performance = 0x98    # 152 - classic family
        FullSpeed = 0x99      # 153 - classic family
        G_Mode = 0xAB

    _balancedModePatch = None # type: Optional[Union[False, int]]
    _USTT_Balanced = 0xA0

    # Some models only answer the classic 150-153 family, others only the USTT
    # 160-165 family (G15 5520 with i7-12700H is USTT-only: every classic id is
    # rejected). Try both, then remember which one the firmware accepted.
    _MODE_CANDIDATES = {
        ThermalMode.Quiet: (0x96, 0xA3),          # 150 | 163 USTT_Quiet
        ThermalMode.Performance: (0x98, 0xA1),    # 152 | 161 USTT_Performance
        ThermalMode.FullSpeed: (0x99, 0xA4),      # 153 | 164 USTT_FullSpeed
    }
    _resolvedModeCandidate: dict = {}

    def __init__(self, awcc: _wmi_object) -> None:
        self._awcc = awcc
        self._resolvedModeCandidate = {}
        self.lastAppliedModeId = None # type: Optional[int]

    def GetSensorTemperature(self, sensorId: int) -> Optional[int]:
        if not (sensorId in range(self.SENSOR_ID_FIRST, self.SENSOR_ID_LAST + 1)): return None
        arg = ((sensorId & 0xFF) << 8) | 4
        return self._call('Thermal_Information', arg)

    def GetFanRPMPercent(self, fanId: int) -> Optional[int]:
        if not (fanId in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1)): return None
        arg = ((fanId & 0xFF) << 8) | 6
        return self._call('Thermal_Information', arg)

    def GetFanRPM(self, fanId: int) -> Optional[int]:
        if not (fanId in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1)): return None
        arg = ((fanId & 0xFF) << 8) | 5
        return self._call('Thermal_Information', arg)


    def GetFanRelatedSensorsCountById(self, fanId: int) -> Optional[int]:
        if not (fanId in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1)): return None
        arg = ((fanId & 0xFF) << 8) | 1
        return self._call('GetFanSensors', arg)

    def GetFanRelatdSensorsById(self, fanId: int, sensorIndex: int) -> Optional[int]:
        if not (fanId in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1)): return None
        arg = ((sensorIndex & 0xFF) << 16) | ((fanId & 0xFF) << 8) | 2
        return self._call('GetFanSensors', arg)

    def GetFanIdsAndRelatedSensorsCount(self) -> list[Tuple[int, int]]:
        res: list[Tuple[int, int]] = []
        for idx in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1):
            count = self.GetFanRelatedSensorsCountById(idx)
            if count is not None and count > 0: res.append((idx, count))
        return res

    def GetFanIdsAndRelatedSensorsIds(self) -> list[Tuple[int, Tuple[int, ...]]]:
        return [ 
            (
                fanId, 
                tuple(
                    id for id in (
                        self.GetFanRelatdSensorsById(fanId, idx) for idx in range(sensorCount)
                    ) if id is not None
                )
            ) for fanId, sensorCount in self.GetFanIdsAndRelatedSensorsCount() 
        ]


    def ApplyThermalMode(self, mode: ThermalMode) -> bool:
        if not isinstance(mode, self.ThermalMode):
            raise Exception('Invalid argument: mode is not instance of ThermalMode')

        # Modes that exist in two numbering families: pick the one this
        # firmware actually answers, then stick to it.
        candidates = self._MODE_CANDIDATES.get(mode)
        if candidates is not None:
            known = self._resolvedModeCandidate.get(mode)
            if known is not None:
                return self._Thermal_Control(known)
            for candidate in candidates:
                if self._Thermal_Control(candidate):
                    self._resolvedModeCandidate[mode] = candidate
                    print(f'{mode.name}: firmware accepted 0x{candidate:02X}')
                    return True
            return False

        value = mode.value

        # Patch balanced mode value for laptops with USTT support
        if mode == self.ThermalMode.Balanced:
            if self._balancedModePatch is None:
                self._balancedModePatch = self._USTT_Balanced if self._Thermal_Control(self._USTT_Balanced) else False
                print(f'Balanced mode patch: {self._balancedModePatch}')
            # NOTE: the sentinel is False, and isinstance(False, int) is True in
            # Python, so this must compare identity - upstream's isinstance()
            # check silently sends mode 0 (Custom) on machines without USTT.
            if self._balancedModePatch is not False:
                value = self._balancedModePatch

        return self._Thermal_Control(value)

    def SetAddonSpeedPercent(self, fanId: int, speed: int) -> bool:
        if not (fanId in range(self.FAN_ID_FIRST, self.FAN_ID_LAST + 1)): return False
        if speed > 0xFF: speed = 0xFF
        arg = ((speed & 0xFF) << 16) | ((fanId & 0xFF) << 8) | 2
        return self._call('Thermal_Control', arg) == 0

    def _Thermal_Control(self, modeId: int) -> bool:
        arg = ((modeId & 0xFF) << 8) | 1
        ok = self._call('Thermal_Control', arg) == 0
        if ok:
            self.lastAppliedModeId = modeId & 0xFF
        return ok
        
    def _call(self, method: str, arg: int) -> Optional[int]:
        if not hasattr(self._awcc, method) or not callable(getattr(self._awcc, method)):
            return None
        try:
            val: int = getattr(self._awcc, method)(arg)[0]
        except Exception as ex:
            # The Dell provider raises a COM error for some invalid arguments
            # instead of returning 0xFFFFFFFF. Treat it as a rejection so the
            # caller can fall through to the next candidate id.
            print(f'{method}(0x{arg:X}) raised: {ex}')
            return None
        if not isinstance(val, int) or val == -1 or val == 0xFFFFFFFF: 
            return None
        return val
