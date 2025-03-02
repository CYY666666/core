"""Generic Hue Entity Model."""
from __future__ import annotations

from aiohue.v2.controllers.base import BaseResourcesController
from aiohue.v2.controllers.events import EventType
from aiohue.v2.models.clip import CLIPResource
from aiohue.v2.models.connectivity import ConnectivityServiceStatus
from aiohue.v2.models.resource import ResourceTypes

from homeassistant.core import callback
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_registry import async_get as async_get_entity_registry

from ..bridge import HueBridge
from ..const import DOMAIN

RESOURCE_TYPE_NAMES = {
    # a simple mapping of hue resource type to Hass name
    ResourceTypes.LIGHT_LEVEL: "Illuminance",
    ResourceTypes.DEVICE_POWER: "Battery",
}


class HueBaseEntity(Entity):
    """Generic Entity Class for a Hue resource."""

    _attr_should_poll = False

    def __init__(
        self,
        bridge: HueBridge,
        controller: BaseResourcesController,
        resource: CLIPResource,
    ) -> None:
        """Initialize a generic Hue resource entity."""
        self.bridge = bridge
        self.controller = controller
        self.resource = resource
        self.device = controller.get_device(resource.id)
        self.logger = bridge.logger.getChild(resource.type.value)

        # Entity class attributes
        self._attr_unique_id = resource.id
        # device is precreated in main handler
        # this attaches the entity to the precreated device
        if self.device is not None:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, self.device.id)},
            )
        # some (3th party) Hue lights report their connection status incorrectly
        # causing the zigbee availability to report as disconnected while in fact
        # it can be controlled. Although this is in fact something the device manufacturer
        # should fix, we work around it here. If the light is reported unavailable at
        # startup, we ignore the availability status of the zigbee connection
        self._ignore_availability = False
        if self.device is None:
            return
        if zigbee := self.bridge.api.devices.get_zigbee_connectivity(self.device.id):
            self._ignore_availability = (
                # Official Hue lights are reliable
                self.device.product_data.manufacturer_name != "Signify Netherlands B.V."
                and zigbee.status != ConnectivityServiceStatus.CONNECTED
            )

    @property
    def name(self) -> str:
        """Return name for the entity."""
        if self.device is None:
            # this is just a guard
            # creating a pretty name for device-less entities (e.g. groups/scenes)
            # should be handled in the platform instead
            return self.resource.type.value
        # if resource is a light, use the name from metadata
        if self.resource.type == ResourceTypes.LIGHT:
            return self.resource.name
        # for sensors etc, use devicename + pretty name of type
        dev_name = self.device.metadata.name
        type_title = RESOURCE_TYPE_NAMES.get(
            self.resource.type, self.resource.type.value.replace("_", " ").title()
        )
        return f"{dev_name} {type_title}"

    async def async_added_to_hass(self) -> None:
        """Call when entity is added."""
        # Add value_changed callbacks.
        self.async_on_remove(
            self.controller.subscribe(
                self._handle_event,
                self.resource.id,
                (EventType.RESOURCE_UPDATED, EventType.RESOURCE_DELETED),
            )
        )
        # also subscribe to device update event to catch devicer changes (e.g. name)
        if self.device is None:
            return
        self.async_on_remove(
            self.bridge.api.devices.subscribe(
                self._handle_event,
                self.device.id,
                EventType.RESOURCE_UPDATED,
            )
        )
        # subscribe to zigbee_connectivity to catch availability changes
        if zigbee := self.bridge.api.devices.get_zigbee_connectivity(self.device.id):
            self.bridge.api.sensors.zigbee_connectivity.subscribe(
                self._handle_event,
                zigbee.id,
                EventType.RESOURCE_UPDATED,
            )

    @property
    def available(self) -> bool:
        """Return entity availability."""
        if self.device is None:
            # entities without a device attached should be always available
            return True
        if self.resource.type == ResourceTypes.ZIGBEE_CONNECTIVITY:
            # the zigbee connectivity sensor itself should be always available
            return True
        if self._ignore_availability:
            return True
        if zigbee := self.bridge.api.devices.get_zigbee_connectivity(self.device.id):
            # all device-attached entities get availability from the zigbee connectivity
            return zigbee.status == ConnectivityServiceStatus.CONNECTED
        return True

    @callback
    def on_update(self) -> None:
        """Call on update event."""
        # used in subclasses

    @callback
    def _handle_event(self, event_type: EventType, resource: CLIPResource) -> None:
        """Handle status event for this resource (or it's parent)."""
        if event_type == EventType.RESOURCE_DELETED and resource.id == self.resource.id:
            self.logger.debug("Received delete for %s", self.entity_id)
            # non-device bound entities like groups and scenes need to be removed here
            # all others will be be removed by device setup in case of device removal
            ent_reg = async_get_entity_registry(self.hass)
            ent_reg.async_remove(self.entity_id)
        else:
            self.logger.debug("Received status update for %s", self.entity_id)
            self.on_update()
            self.async_write_ha_state()
