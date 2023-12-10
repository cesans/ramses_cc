"""Support for RAMSES climate entities."""
from __future__ import annotations

from datetime import datetime as dt
import json
import logging
from typing import Any

from homeassistant.components.climate import (
    DOMAIN as PLATFORM,
    FAN_AUTO,
    FAN_HIGH,
    FAN_LOW,
    FAN_MEDIUM,
    FAN_OFF,
    PRECISION_TENTHS,
    PRESET_AWAY,
    PRESET_ECO,
    PRESET_HOME,
    PRESET_NONE,
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType

from . import RamsesEntity, RamsesZoneBase
from .const import (
    ATTR_SETPOINT,
    BROKER,
    DATA,
    DOMAIN,
    SERVICE,
    UNIQUE_ID,
    SystemMode,
    ZoneMode,
)
from .schemas import (
    CONF_MODE,
    CONF_SYSTEM_MODE,
    SVC_RESET_SYSTEM_MODE,
    SVC_SET_SYSTEM_MODE,
    SVCS_CLIMATE_EVO_TCS,
    SVCS_CLIMATE_EVO_ZONE,
)

_LOGGER = logging.getLogger(__name__)

MODE_TCS_TO_HA = {
    SystemMode.AUTO: HVACMode.HEAT,  # NOTE: don't use _AUTO
    SystemMode.HEAT_OFF: HVACMode.OFF,
}
MODE_TCS_TO_HA[SystemMode.RESET] = MODE_TCS_TO_HA[SystemMode.AUTO]

MODE_TO_TCS = {
    HVACMode.HEAT: SystemMode.AUTO,
    HVACMode.OFF: SystemMode.HEAT_OFF,
    HVACMode.AUTO: SystemMode.RESET,  # not all systems support this
}

PRESET_CUSTOM = "custom"  # NOTE: not an offical PRESET

PRESET_TCS_TO_HA = {
    SystemMode.AUTO: PRESET_NONE,
    SystemMode.AWAY: PRESET_AWAY,
    SystemMode.CUSTOM: PRESET_CUSTOM,
    SystemMode.DAY_OFF: PRESET_HOME,
    SystemMode.ECO_BOOST: PRESET_ECO,  # or: PRESET_BOOST
    SystemMode.HEAT_OFF: PRESET_NONE,
}
PRESET_TCS_TO_HA[SystemMode.DAY_OFF_ECO] = PRESET_TCS_TO_HA[SystemMode.DAY_OFF]
PRESET_TCS_TO_HA[SystemMode.RESET] = PRESET_TCS_TO_HA[SystemMode.AUTO]

PRESET_TO_TCS = (
    SystemMode.AUTO,
    SystemMode.AWAY,
    SystemMode.CUSTOM,
    SystemMode.DAY_OFF,
    SystemMode.ECO_BOOST,
)
PRESET_TO_TCS = {v: k for k, v in PRESET_TCS_TO_HA.items() if k in PRESET_TO_TCS}

MODE_ZONE_TO_HA = {
    ZoneMode.ADVANCED: HVACMode.HEAT,
    ZoneMode.SCHEDULE: HVACMode.AUTO,
}
MODE_ZONE_TO_HA[ZoneMode.PERMANENT] = MODE_ZONE_TO_HA[ZoneMode.ADVANCED]
MODE_ZONE_TO_HA[ZoneMode.TEMPORARY] = MODE_ZONE_TO_HA[ZoneMode.ADVANCED]

MODE_TO_ZONE = (ZoneMode.SCHEDULE, ZoneMode.PERMANENT)
MODE_TO_ZONE = {v: k for k, v in MODE_ZONE_TO_HA.items() if k in MODE_TO_ZONE}
PRESET_ZONE_TO_HA = {
    ZoneMode.SCHEDULE: PRESET_NONE,
    ZoneMode.TEMPORARY: "temporary",
    ZoneMode.PERMANENT: "permanent",
}
PRESET_TO_ZONE = {v: k for k, v in PRESET_ZONE_TO_HA.items()}


async def async_setup_platform(
    hass: HomeAssistant,
    _: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType = None,
) -> None:
    """Create climate entities for CH/DHW (heat) & HVAC."""

    def entity_factory(entity_class, broker, device):  # TODO: deprecate
        return entity_class(broker, device)

    if discovery_info is None:
        return

    platform = entity_platform.async_get_current_platform()

    broker = hass.data[DOMAIN][BROKER]
    new_entities = []

    if discovery_info.get("fans"):
        if not broker._services.get(f"{PLATFORM}_hvac"):
            broker._services[f"{PLATFORM}_hvac"] = True
            # for name, schema in SVCS_CLIMATE_HVAC.items():
            #     platform.async_register_entity_service(name, schema, f"svc_{name}")

        for fan in discovery_info["fans"]:
            new_entities.append(RamsesHvac(broker, fan))

    if discovery_info.get("ctls") or discovery_info.get("zons"):
        if not broker._services.get(f"{PLATFORM}_heat"):
            broker._services[f"{PLATFORM}_heat"] = True
            for name, schema in SVCS_CLIMATE_EVO_TCS.items():
                platform.async_register_entity_service(name, schema, f"svc_{name}")
            for name, schema in SVCS_CLIMATE_EVO_ZONE.items():
                platform.async_register_entity_service(name, schema, f"svc_{name}")

        for tcs in discovery_info.get("ctls", []):
            new_entities.append(entity_factory(RamsesController, broker, tcs))

        for zone in discovery_info.get("zons", []):
            new_entities.append(entity_factory(RamsesZone, broker, zone))

    if new_entities:
        async_add_entities(new_entities)


class RamsesController(RamsesZoneBase, ClimateEntity):
    """Base for a Honeywell Controller/Location."""

    _attr_icon: str = "mdi:thermostat"
    _attr_hvac_modes: list[str] = list(MODE_TO_TCS)
    _attr_preset_modes: list[str] = list(PRESET_TO_TCS)
    _attr_supported_features: int = ClimateEntityFeature.PRESET_MODE
    _attr_max_temp: float | None = None
    _attr_min_temp: float | None = None

    def __init__(self, broker, device) -> None:
        """Initialize a TCS Controller."""
        _LOGGER.info("Found a Controller: %r", device)
        super().__init__(broker, device)

    @property
    def current_temperature(self) -> float | None:
        """Return the average current temperature of the heating Zones.

        Controllers do not have a current temp, but one is expected by HA.
        """
        temps = [z.temperature for z in self._device.zones if z.temperature is not None]
        temps = [t for t in temps if t is not None]  # above is buggy, why?
        try:
            return round(sum(temps) / len(temps), 1) if temps else None
        except TypeError:
            _LOGGER.warning("Temp (%s) contains None", temps)
            return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        return {
            "heat_demand": self._device.heat_demand,
            "heat_demands": self._device.heat_demands,
            "relay_demands": self._device.relay_demands,
            "system_mode": self._device.system_mode,
            "tpi_params": self._device.tpi_params,
            # "faults": self._device.faultlog,
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Controller's current running hvac operation."""

        if self._device.system_mode is None:
            return  # unable to determine
        if self._device.system_mode[CONF_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        if self._device.heat_demand:
            return HVACAction.HEATING
        if self._device.heat_demand is not None:
            return HVACAction.IDLE

        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Controller's current operating mode of a Controller."""

        if self._device.system_mode is None:
            return  # unable to determine
        if self._device.system_mode[CONF_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF
        if self._device.system_mode[CONF_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO  # users can't adjust setpoints in away mode
        return HVACMode.HEAT

    @property
    def name(self) -> str:
        """Return the name of the Controller."""
        return "Controller"

    @property
    def preset_mode(self) -> str | None:
        """Return the Controller's current preset mode, e.g., home, away, temp."""

        if self._device.system_mode is None:
            return  # unable to determine
        return PRESET_TCS_TO_HA[self._device.system_mode[CONF_SYSTEM_MODE]]

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""

        zones = [z for z in self._device.zones if z.setpoint is not None]
        temps = [z.setpoint for z in zones if z.heat_demand is not None]
        return max(z.setpoint for z in zones) if temps else None

        # temps = [z.setpoint for z in self._device.zones]
        # return round(sum(temps) / len(temps), 1) if temps else None

    @callback
    def set_hvac_mode(self, hvac_mode: str) -> None:
        """Set an operating mode for a Controller."""
        self.svc_set_system_mode(MODE_TO_TCS.get(hvac_mode))

    @callback
    def set_preset_mode(self, preset_mode: str | None) -> None:
        """Set the preset mode; if None, then revert to 'Auto' mode."""
        self.svc_set_system_mode(PRESET_TO_TCS.get(preset_mode, SystemMode.AUTO))

    @callback
    def svc_reset_system_mode(self) -> None:
        """Reset the (native) operating mode of the Controller."""
        self._device.reset_mode()
        self.async_write_ha_state_delayed()

    @callback
    def svc_set_system_mode(self, mode, period=None, days=None) -> None:
        """Set the (native) operating mode of the Controller."""
        if period is not None:
            until = dt.now() + period
        elif days is not None:
            until = dt.now() + days  # TODO: round down
        else:
            until = None
        self._device.set_mode(system_mode=mode, until=until)
        self.async_write_ha_state_delayed()


class RamsesZone(RamsesZoneBase, ClimateEntity):
    """Base for a Honeywell TCS Zone."""

    _attr_icon: str = "mdi:radiator"
    _attr_hvac_modes: list[str] = list(MODE_TO_ZONE)
    _attr_preset_modes: list[str] = list(PRESET_TO_ZONE)
    _attr_supported_features: int = (
        ClimateEntityFeature.PRESET_MODE | ClimateEntityFeature.TARGET_TEMPERATURE
    )
    _attr_target_temperature_step: float = PRECISION_TENTHS

    def __init__(self, broker, device) -> None:
        """Initialize a TCS Zone."""
        _LOGGER.info("Found a Zone: %r", device)
        super().__init__(broker, device)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        return {
            "zone_idx": self._device.idx,
            "heating_type": self._device.heating_type,
            "mode": self._device.mode,
            "config": self._device.config,
            **super().extra_state_attributes,
            "schedule": self._device.schedule,
            "schedule_version": self._device.schedule_version,
        }

    @property
    def hvac_action(self) -> str | None:
        """Return the Zone's current running hvac operation."""

        if self._device.tcs.system_mode is None:
            return None  # unable to determine
        if self._device.tcs.system_mode[CONF_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACAction.OFF

        if self._device.heat_demand:
            return HVACAction.HEATING
        if self._device.heat_demand is not None:
            return HVACAction.IDLE
        return None

    @property
    def hvac_mode(self) -> str | None:
        """Return the Zone's hvac operation ie. heat, cool mode."""

        if self._device.tcs.system_mode is None:
            return  # unable to determine
        if self._device.tcs.system_mode[CONF_SYSTEM_MODE] == SystemMode.AWAY:
            return HVACMode.AUTO
        if self._device.tcs.system_mode[CONF_SYSTEM_MODE] == SystemMode.HEAT_OFF:
            return HVACMode.OFF

        if self._device.mode is None or self._device.mode[ATTR_SETPOINT] is None:
            return  # unable to determine
        if (
            self._device.config
            and self._device.mode[ATTR_SETPOINT] <= self._device.config["min_temp"]
        ):
            return HVACMode.OFF
        return HVACMode.HEAT

    @property
    def max_temp(self) -> float | None:
        """Return the maximum target temperature of a Zone."""
        try:
            return self._device.config["max_temp"]
        except TypeError:  # 'NoneType' object is not subscriptable
            return None

    @property
    def min_temp(self) -> float | None:
        """Return the minimum target temperature of a Zone."""
        try:
            return self._device.config["min_temp"]
        except TypeError:  # 'NoneType' object is not subscriptable
            return None

    @property
    def preset_mode(self) -> str | None:
        """Return the Zone's current preset mode, e.g., home, away, temp."""

        if self._device.tcs.system_mode is None:
            return None  # unable to determine
        # if self._device.tcs.system_mode[CONF_SYSTEM_MODE] in MODE_TCS_TO_HA:
        if self._device.tcs.system_mode[CONF_SYSTEM_MODE] in (
            SystemMode.AWAY,
            SystemMode.HEAT_OFF,
        ):
            return PRESET_TCS_TO_HA[self._device.tcs.system_mode[CONF_SYSTEM_MODE]]

        if self._device.mode is None:
            return None  # unable to determine
        if self._device.mode[CONF_MODE] == ZoneMode.SCHEDULE:
            return PRESET_TCS_TO_HA[self._device.tcs.system_mode[CONF_SYSTEM_MODE]]
        return PRESET_ZONE_TO_HA.get(self._device.mode[CONF_MODE])

    @property
    def target_temperature(self) -> float | None:
        """Return the temperature we try to reach."""
        return self._device.setpoint

    @callback
    def set_hvac_mode(self, hvac_mode: str) -> None:
        """Set a Zone to one of its native operating modes."""
        if hvac_mode == HVACMode.AUTO:  # FollowSchedule
            self.svc_reset_zone_mode()
        elif hvac_mode == HVACMode.HEAT:  # TemporaryOverride
            self.svc_set_zone_mode(mode=ZoneMode.PERMANENT, setpoint=25)  # TODO:
        else:  # HVACMode.OFF, PermentOverride, temp = min
            self.svc_set_zone_mode(self._device.set_frost_mode)  # TODO:

    @callback
    def set_preset_mode(self, preset_mode: str | None) -> None:
        """Set the preset mode; if None, then revert to following the schedule."""
        self.svc_set_zone_mode(
            mode=PRESET_TO_ZONE.get(preset_mode),
            setpoint=self.target_temperature if preset_mode == "permanent" else None,
        )

    @callback
    def set_temperature(self, temperature: float = None, **kwargs) -> None:
        """Set a new target temperature."""
        self.svc_set_zone_mode(setpoint=temperature)

    @callback
    def svc_put_zone_temp(
        self, temperature: float, **kwargs
    ) -> None:  # set_current_temp
        """Fake the measured temperature of the Zone sensor.

        This is not the setpoint (see: set_temperature), but the measured temperature.
        """
        self._device.sensor._make_fake()
        self._device.sensor.temperature = temperature
        self._device._get_temp()
        self.async_write_ha_state()

    @callback
    def svc_reset_zone_config(self) -> None:
        """Reset the configuration of the Zone."""
        self._device.reset_config()
        self.async_write_ha_state_delayed()

    @callback
    def svc_reset_zone_mode(self) -> None:
        """Reset the (native) operating mode of the Zone."""
        self._device.reset_mode()
        self.async_write_ha_state_delayed()

    @callback
    def svc_set_zone_config(self, **kwargs) -> None:
        """Set the configuration of the Zone (min/max temp, etc.)."""
        self._device.set_config(**kwargs)
        self.async_write_ha_state_delayed()

    @callback
    def svc_set_zone_mode(
        self, mode=None, setpoint=None, duration=None, until=None
    ) -> None:
        """Set the (native) operating mode of the Zone."""
        if until is None and duration is not None:
            until = dt.now() + duration
        self._device.set_mode(mode=mode, setpoint=setpoint, until=until)
        self.async_write_ha_state_delayed()

    async def svc_get_zone_schedule(self, **kwargs) -> None:
        """Get the latest weekly schedule of the Zone."""
        # {{ state_attr('climate.ramses_cc_01_145038_04', 'schedule') }}
        await self._device.get_schedule()
        self.async_write_ha_state()

    async def svc_set_zone_schedule(self, schedule: str, **kwargs) -> None:
        """Set the weekly schedule of the Zone."""
        await self._device.set_schedule(json.loads(schedule))


class RamsesHvac(RamsesEntity, ClimateEntity):
    """Base for a Honeywell HVAC unit (Fan, HRU, MVHR, PIV, etc)."""

    # PRESET_AWAY = away
    # PRESET_BOOST (timed), 15, 30, 60, other mins
    # PRESET_COMFORT: auto with lower CO2
    # PRESET_NONE: off, low/med/high or auto

    # Climate attrs....
    _attr_precision: float = PRECISION_TENTHS
    _attr_temperature_unit: str = UnitOfTemperature.CELSIUS
    _attr_fan_modes: list[str] | None = [
        FAN_OFF,
        FAN_AUTO,
        FAN_LOW,
        FAN_MEDIUM,
        FAN_HIGH,
    ]
    _attr_hvac_modes: list[HVACMode] | list[str] = [HVACMode.AUTO, HVACMode.OFF]
    _attr_preset_modes: list[str] | None = None
    _attr_supported_features: int = (
        ClimateEntityFeature.FAN_MODE | ClimateEntityFeature.PRESET_MODE
    )

    def __init__(self, broker, device) -> None:
        """Initialize a HVAC system."""
        _LOGGER.info("Found a HVAC system: %r", device)

        super().__init__(broker, device)

        self._attr_unique_id = (
            device.id
        )  # dont include domain (ramses_cc) / platform (climate)

    @property
    def current_humidity(self) -> int | None:
        """Return the current humidity."""
        if self._device.indoor_humidity is not None:
            return int(self._device.indoor_humidity * 100)
        return None

    @property
    def current_temperature(self) -> float | None:
        """Return the current temperature."""
        return self._device.indoor_temp

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        return {
            **super().extra_state_attributes,
        }

    @property
    def fan_mode(self) -> str | None:
        """Return the fan setting."""
        return None

    @property
    def hvac_action(self) -> HVACAction | str | None:
        """Return the current running hvac operation if supported."""
        if self._device.fan_info is not None:
            return self._device.fan_info

    @property
    def hvac_mode(self) -> HVACMode | str | None:
        """Return hvac operation ie. heat, cool mode."""
        if self._device.fan_info is not None:
            return HVACMode.OFF if self._device.fan_info == "off" else HVACMode.AUTO

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        return "mdi:hvac-off" if self._device.fan_info == "off" else "mdi:hvac"

    @property
    def name(self) -> str:
        """Return the name of the entity."""
        return self._device.id

    @property
    def preset_mode(self) -> str | None:
        """Return the current preset mode, e.g., home, away, temp."""
        return PRESET_NONE
