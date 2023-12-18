"""Support for Honeywell's RAMSES-II RF protocol, as used by CH/DHW & HVAC.

Requires a Honeywell HGI80 (or compatible) gateway.
"""
from __future__ import annotations

import logging
from typing import Any

from ramses_rf.entity_base import Entity as RamsesRFEntity
from ramses_tx.exceptions import TransportSerialError
import voluptuous as vol

from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.const import (
    ATTR_ID,
    CONF_SCAN_INTERVAL,
    EVENT_HOMEASSISTANT_START,
    Platform,
)
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.service import verify_domain_control
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    ATTR_DEVICE_ID,
    BROKER,
    CONF_ADVANCED_FEATURES,
    CONF_MESSAGE_EVENTS,
    CONF_SEND_PACKET,
    DOMAIN,
    SIGNAL_UPDATE,
    SVC_FAKE_DEVICE,
    SVC_FORCE_UPDATE,
    SVC_SEND_PACKET,
)
from .coordinator import RamsesBroker
from .schemas import SCH_DOMAIN_CONFIG

_LOGGER = logging.getLogger(__name__)


CONFIG_SCHEMA = vol.Schema({DOMAIN: SCH_DOMAIN_CONFIG}, extra=vol.ALLOW_EXTRA)

PLATFORMS = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.REMOTE,
    Platform.WATER_HEATER,
]

SVC_FAKE_DEVICE_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.matches_regex(r"^[0-9]{2}:[0-9]{6}$"),
        vol.Optional("create_device", default=False): vol.Any(None, cv.boolean),
        vol.Optional("start_binding", default=False): vol.Any(None, cv.boolean),
    }
)

SVC_SEND_PACKET_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_DEVICE_ID): cv.matches_regex(r"^[0-9]{2}:[0-9]{6}$"),
        vol.Required("verb"): vol.In((" I", "I", "RQ", "RP", " W", "W")),
        vol.Required("code"): cv.matches_regex(r"^[0-9A-F]{4}$"),
        vol.Required("payload"): cv.matches_regex(r"^[0-9A-F]{1,48}$"),
    }
)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Create a ramses_rf (RAMSES_II)-based system."""

    _LOGGER.debug("\r\n\nConfig = %s\r\n", config[DOMAIN])

    broker = RamsesBroker(hass, config)
    try:
        await broker.start()
    except TransportSerialError as exc:
        _LOGGER.error("There is a problem with the serial port: %s", exc)
        return False

    hass.data.setdefault(DOMAIN, {})[BROKER] = broker

    coordinator: DataUpdateCoordinator = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=DOMAIN,
        update_interval=config[DOMAIN][CONF_SCAN_INTERVAL],
    )
    coordinator.update_method = broker.async_update

    if _LOGGER.isEnabledFor(logging.DEBUG):  # TODO: remove
        app_storage = await broker._async_load_storage()
        _LOGGER.debug("\r\n\nStore = %s\r\n", app_storage)

    # NOTE: .async_listen_once(EVENT_HOMEASSISTANT_START, awaitable_coro)
    # NOTE: will be passed event, as: async def awaitable_coro(_event: Event):
    await coordinator.async_config_entry_first_refresh()  # will save access tokens too
    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, broker.async_update)

    register_domain_services(hass, broker)
    register_domain_events(hass, broker)

    return True


# TODO: add async_ to routines where required to do so
@callback  # TODO: the following is a mess - to add register/deregister of clients
def register_domain_events(hass: HomeAssistant, broker: RamsesBroker) -> None:
    """Set up the handlers for the system-wide events."""

    @callback
    def process_msg(msg, *args, **kwargs):  # process_msg(msg, prev_msg=None)
        if (
            regex := broker.config[CONF_ADVANCED_FEATURES][CONF_MESSAGE_EVENTS]
        ) and regex.match(f"{msg!r}"):
            event_data = {
                "dtm": msg.dtm.isoformat(),
                "src": msg.src.id,
                "dst": msg.dst.id,
                "verb": msg.verb,
                "code": msg.code,
                "payload": msg.payload,
                "packet": str(msg._pkt),
            }
            hass.bus.async_fire(f"{DOMAIN}_message", event_data)

        if broker.learn_device_id and broker.learn_device_id == msg.src.id:
            event_data = {
                "src": msg.src.id,
                "code": msg.code,
                "packet": str(msg._pkt),
            }
            hass.bus.async_fire(f"{DOMAIN}_learn", event_data)

    broker.client.add_msg_handler(process_msg)


@callback  # TODO: add async_ to routines where required to do so
def register_domain_services(hass: HomeAssistant, broker: RamsesBroker):
    """Set up the handlers for the domain-wide services."""

    @verify_domain_control(hass, DOMAIN)
    async def async_fake_device(call: ServiceCall) -> None:
        try:
            broker.client.fake_device(**call.data)
        except LookupError as exc:
            _LOGGER.error("%s", exc)
            return
        hass.helpers.event.async_call_later(5, broker.async_update)

    @verify_domain_control(hass, DOMAIN)
    async def async_force_update(_: ServiceCall) -> None:
        await broker.async_update()

    @verify_domain_control(hass, DOMAIN)
    async def async_send_packet(call: ServiceCall) -> None:
        kwargs = dict(call.data.items())  # is ReadOnlyDict
        if (
            call.data["device_id"] == "18:000730"
            and kwargs.get("from_id", "18:000730") == "18:000730"
            and broker.client.hgi.id
        ):
            kwargs["device_id"] = broker.client.hgi.id
        broker.client.send_cmd(broker.client.create_cmd(**kwargs))
        hass.helpers.event.async_call_later(5, broker.async_update)

        hass.services.async_register(
            DOMAIN, SVC_FAKE_DEVICE, async_fake_device, schema=SVC_FAKE_DEVICE_SCHEMA
        )
        hass.services.async_register(DOMAIN, SVC_FORCE_UPDATE, async_force_update)

        if broker.config[CONF_ADVANCED_FEATURES].get([CONF_SEND_PACKET]):
            hass.services.async_register(
                DOMAIN,
                SVC_SEND_PACKET,
                async_send_packet,
                schema=SVC_SEND_PACKET_SCHEMA,
            )


class RamsesEntity(Entity):
    """Base for any RAMSES II-compatible entity (e.g. Climate, Sensor)."""

    _broker: RamsesBroker
    _device: RamsesRFEntity

    _attr_should_poll = False

    def __init__(self, broker: RamsesBroker, device) -> None:
        """Initialize the entity."""
        self.hass = broker.hass
        self._broker = broker
        self._device = device

        self._attr_unique_id = device.id

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the integration-specific state attributes."""
        return {
            ATTR_ID: self._device.id,
        }

    async def async_added_to_hass(self) -> None:
        """Run when entity about to be added to hass."""
        self._broker._entities[self.unique_id] = self
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass, SIGNAL_UPDATE, self.async_write_ha_state
            )
        )

    @callback
    def async_write_ha_state_delayed(self, delay=3) -> None:
        """Write the state to the state machine after a short delay to allow system to quiesce."""
        async_call_later(self.hass, delay, self.async_write_ha_state)


class RamsesSensorBase(RamsesEntity):
    """Base for any Ramses sensor/binary_sensor entity."""

    def __init__(
        self,
        broker: RamsesBroker,
        device,
        state_attr,
        device_class: SensorDeviceClass | None = None,
        unique_id_attr: str | None = None,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(broker, device)

        self.entity_id = f"{DOMAIN}.{device.id}-{state_attr}"

        self._attr_device_class = device_class
        self._attr_unique_id = f"{device.id}-{unique_id_attr or state_attr}"
        self._state_attr = state_attr

    @property
    def available(self) -> bool:
        """Return True if the sensor is available."""
        return getattr(self._device, self._state_attr) is not None

    @property
    def name(self) -> str:
        """Return the name of the binary_sensor/sensor."""
        if not hasattr(self._device, "name") or not self._device.name:
            return f"{self._device.id} {self._state_attr}"
        return f"{self._device.name} {self._state_attr}"
