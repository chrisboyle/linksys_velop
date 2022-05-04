"""Device trackers for the mesh"""
import logging
from abc import ABC
from typing import (
    Callable,
    List,
    Optional,
)

import homeassistant.helpers.device_registry as dr
from homeassistant.components.device_tracker import (
    CONF_CONSIDER_HOME,
    DOMAIN as ENTITY_DOMAIN,
)
from homeassistant.components.device_tracker import SOURCE_TYPE_ROUTER
from homeassistant.components.device_tracker.config_entry import ScannerEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_time
from homeassistant.util import dt as dt_util
# noinspection PyProtectedMember
from pyvelop.const import _PACKAGE_AUTHOR as PYVELOP_AUTHOR
# noinspection PyProtectedMember
from pyvelop.const import _PACKAGE_NAME as PYVELOP_NAME
# noinspection PyProtectedMember
from pyvelop.const import _PACKAGE_VERSION as PYVELOP_VERSION
from pyvelop.device import Device
from pyvelop.exceptions import MeshDeviceNotFoundResponse
from pyvelop.mesh import Mesh

from .const import (
    CONF_COORDINATOR_MESH,
    CONF_DEVICE_TRACKERS,
    DEF_CONSIDER_HOME,
    DOMAIN,
    ENTITY_SLUG,
    SIGNAL_UPDATE_DEVICE_TRACKER,
)
from .logger import VelopLogger

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up the device trackers based on a config entry"""

    device_trackers: List[str] = config.options.get(CONF_DEVICE_TRACKERS, [])

    mesh: Mesh = hass.data[DOMAIN][config.entry_id][CONF_COORDINATOR_MESH]
    entities: list = []

    # region #-- get the mesh device from the registry --#
    device_registry = dr.async_get(hass=hass)
    config_devices: List[dr.DeviceEntry] = dr.async_entries_for_config_entry(
        registry=device_registry,
        config_entry_id=config.entry_id
    )
    mesh_device = [
        d
        for d in config_devices
        if d.name.lower() == "mesh"
    ]
    # endregion

    for device_tracker in device_trackers:
        try:
            device = await mesh.async_get_device_from_id(device_id=device_tracker)
        except MeshDeviceNotFoundResponse:
            _LOGGER.warning(VelopLogger().message_format("Device tracker with id %s was not found"), device_tracker)
        else:
            # region #-- add the connection info to the mesh device --#
            if mesh_device:
                mac_address = [
                    adapter
                    for adapter in device.network
                ]
                # TODO: Fix up the try/except block when setting the minimum HASS version to 2022.2
                # HASS 2022.2 introduces some new ways of working with device_trackers, this makes
                # sure that the device_tracker MAC is listed as a connection against the mesh allowing
                # the device tracker to automatically enable and link to the mesh device.
                try:
                    device_registry.async_update_device(
                        device_id=mesh_device[0].id,
                        merge_connections={(dr.CONNECTION_NETWORK_MAC, dr.format_mac(mac_address[0].get("mac")))},
                    )
                except TypeError:  # this will be thrown if merge_connections isn't available (device_id should be)
                    pass
            # endregion

            entities.append(
                LinksysVelopMeshDeviceTracker(
                    device=device,
                    config_entry=config,
                    hass=hass,
                )
            )

    async_add_entities(entities)


class LinksysVelopMeshDeviceTracker(ScannerEntity, ABC):
    """Representation of a device tracker"""

    def __init__(
        self,
        config_entry: ConfigEntry,
        device: Device,
        hass: HomeAssistant,
    ) -> None:
        """Constructor"""

        super().__init__()

        self._config: ConfigEntry = config_entry
        self._consider_home_listener: Optional[Callable] = None
        self._device: Device = device
        self._is_connected: bool = device.status
        self._log_formatter: VelopLogger = VelopLogger(unique_id=self._config.unique_id)
        self._mesh: Mesh = hass.data[DOMAIN][self._config.entry_id][CONF_COORDINATOR_MESH]

        self._attr_name = f"{ENTITY_SLUG} Mesh: {self._device.name}"

    async def _async_get_current_device_status(self, evt: Optional[dt_util.dt.datetime] = None) -> None:
        """"""

        # region #-- get the current device status --#
        devices: List[Device] = await self._mesh.async_get_devices()
        device: List[Device] = [
            d
            for d in devices
            if d.unique_id == self._device.unique_id
        ]
        if device:
            self._device = device[0]
        # endregion

        if self._is_connected != self._device.status:
            if evt:  # made it here because of the listener so must be offline now
                _LOGGER.debug(self._log_formatter.message_format("%s is offline"), self._device.name)
                self._is_connected = False
                self._consider_home_listener = None

        if not self._device.status:  # start listener for the CONF_CONSIDER_HOME period
            if not self._consider_home_listener and self._is_connected != self._device.status:
                _LOGGER.debug(
                    self._log_formatter.message_format("%s: setting consider home listener"), self._device.name
                )
                # noinspection PyTypeChecker
                self._consider_home_listener = async_track_point_in_time(
                    hass=self.hass,
                    action=self._async_get_current_device_status,
                    point_in_time=dt_util.dt.datetime.fromtimestamp(
                        int(self._device.results_time) +
                        self._config.options.get(CONF_CONSIDER_HOME, DEF_CONSIDER_HOME)
                    )
                )
        else:
            if self._is_connected != self._device.status:
                _LOGGER.debug(self._log_formatter.message_format("%s is online"), self._device.name)
            self._is_connected = True
            # stop listener if it is going
            if self._consider_home_listener:
                _LOGGER.debug(
                    self._log_formatter.message_format("%s: cancelling consider home listener"), self._device.name
                )
                self._consider_home_listener()
                self._consider_home_listener = None

        await self.async_update_ha_state()

    async def async_added_to_hass(self) -> None:
        """Register callbacks and set initial status"""

        self.async_on_remove(
            async_dispatcher_connect(
                hass=self.hass,
                signal=SIGNAL_UPDATE_DEVICE_TRACKER,
                target=self._async_get_current_device_status,
            )
        )

    @property
    def device_info(self) -> DeviceInfo:
        """Return the device information of the entity."""

        # noinspection HttpUrlsUsage
        ret = DeviceInfo(**{
            "configuration_url": f"http://{self._mesh.connected_node}",
            "identifiers": {(DOMAIN, self._config.entry_id)},
            "manufacturer": PYVELOP_AUTHOR,
            "model": f"{PYVELOP_NAME} ({PYVELOP_VERSION})",
            "name": "Mesh",
            "sw_version": "",
        })
        return ret

    @property
    def is_connected(self) -> bool:
        """Return True if the tracker is connected, False otherwise"""

        return self._is_connected

    @property
    def mac_address(self) -> str:
        """Set the MAC address for the tracker"""

        ret = ""
        mac_address = [adapter for adapter in self._device.connected_adapters]
        if mac_address:
            ret = mac_address[0].get("mac", "")

        return ret

    @property
    def source_type(self) -> str:
        """Return the source type"""

        return SOURCE_TYPE_ROUTER

    @property
    def unique_id(self) -> Optional[str]:
        """"""

        return f"{self._config.entry_id}::" \
               f"{ENTITY_DOMAIN.lower()}::" \
               f"{self._device.unique_id}"
