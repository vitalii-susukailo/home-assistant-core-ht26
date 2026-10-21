"""Tests for Cast device-profile spoofing protection."""

from uuid import UUID

import pychromecast
from pychromecast.discovery import HostServiceInfo
import pytest

from homeassistant.components.cast.const import (
    CONF_DEVICE_PROFILES,
    DOMAIN,
    ISSUE_PROFILE_MISMATCH,
)
from homeassistant.components.cast.helpers import (
    PROFILE_ATTRS_CHANGED,
    PROFILE_FIRST_SIGHT,
    PROFILE_HOST_CHANGED,
    PROFILE_INCOMPLETE,
    PROFILE_MATCH,
    PROFILE_MISMATCH,
    ChromecastInfo,
    build_device_profile,
    compare_device_profile,
)
from homeassistant.components.cast.media_player import CastDevice
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir

from tests.common import MockConfigEntry

UUID_1 = UUID("12345678-1234-5678-1234-567812345678")
HOST_A = "192.168.178.42"
HOST_B = "192.168.178.99"


def cast_info(
    *,
    host: str = HOST_A,
    model_name: str | None = "Chromecast",
    manufacturer: str | None = "Nabu Casa",
    cast_type: str | None = "cast",
) -> ChromecastInfo:
    """Build a ChromecastInfo the way discovery would."""
    return ChromecastInfo(
        cast_info=pychromecast.models.CastInfo(
            services={HostServiceInfo(host, 8009)},
            uuid=UUID_1,
            model_name=model_name,
            friendly_name="Speaker",
            host=host,
            port=8009,
            cast_type=cast_type,
            manufacturer=manufacturer,
        )
    )


async def _device(hass: HomeAssistant, entry: MockConfigEntry) -> CastDevice:
    """Build a CastDevice wired to the config entry, without connecting."""
    return CastDevice(hass, entry, cast_info())


@pytest.fixture
def entry(hass: HomeAssistant) -> MockConfigEntry:
    """A cast config entry carrying no profiles yet."""
    config_entry = MockConfigEntry(domain=DOMAIN, data={"known_hosts": []})
    config_entry.add_to_hass(hass)
    return config_entry


def _stored(hass: HomeAssistant, config_entry: MockConfigEntry) -> dict | None:
    return config_entry.data.get(CONF_DEVICE_PROFILES, {}).get(str(UUID_1))


async def test_first_pairing_records_profile(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A device seen for the first time is accepted and its profile stored."""
    device = await _device(hass, entry)

    assert _stored(hass, entry) is None
    assert device._async_profile_accepted(cast_info()) is True
    await hass.async_block_till_done()

    assert _stored(hass, entry) == {
        "model_name": "Chromecast",
        "manufacturer": "Nabu Casa",
        "cast_type": "cast",
        "host": HOST_A,
    }


async def test_matching_profile_is_accepted(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """An unchanged device keeps updating the entity as before."""
    device = await _device(hass, entry)
    device._async_profile_accepted(cast_info())
    await hass.async_block_till_done()
    before = _stored(hass, entry)

    assert device._async_profile_accepted(cast_info()) is True
    await hass.async_block_till_done()
    assert _stored(hass, entry) == before


async def test_mismatch_is_refused_and_raises_issue(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A new host reporting different hardware is refused, and warns the user."""
    device = await _device(hass, entry)
    device._async_profile_accepted(cast_info())
    await hass.async_block_till_done()
    original = _stored(hass, entry)

    spoofed = cast_info(host=HOST_B, model_name="Evil Speaker")
    assert device._async_profile_accepted(spoofed) is False
    await hass.async_block_till_done()

    # The recorded profile must survive the refusal.
    assert _stored(hass, entry) == original
    assert issue_registry.async_get_issue(DOMAIN, f"{ISSUE_PROFILE_MISMATCH}_{UUID_1}")


async def test_dhcp_host_change_is_not_refused(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """A device that only moved address must not be refused."""
    device = await _device(hass, entry)
    device._async_profile_accepted(cast_info())
    await hass.async_block_till_done()

    moved = cast_info(host=HOST_B)
    assert device._async_profile_accepted(moved) is True
    await hass.async_block_till_done()

    assert _stored(hass, entry)["host"] == HOST_B
    assert (
        issue_registry.async_get_issue(DOMAIN, f"{ISSUE_PROFILE_MISMATCH}_{UUID_1}")
        is None
    )


async def test_firmware_attribute_change_is_not_refused(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    issue_registry: ir.IssueRegistry,
) -> None:
    """An attribute change at the same address is accepted and recorded."""
    device = await _device(hass, entry)
    device._async_profile_accepted(cast_info())
    await hass.async_block_till_done()

    upgraded = cast_info(model_name="Chromecast HD")
    assert device._async_profile_accepted(upgraded) is True
    await hass.async_block_till_done()

    assert _stored(hass, entry)["model_name"] == "Chromecast HD"
    assert (
        issue_registry.async_get_issue(DOMAIN, f"{ISSUE_PROFILE_MISMATCH}_{UUID_1}")
        is None
    )


async def test_incomplete_announcement_keeps_stored_profile(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """A device that did not answer must not overwrite its profile with nulls."""
    device = await _device(hass, entry)
    device._async_profile_accepted(cast_info())
    await hass.async_block_till_done()
    original = _stored(hass, entry)

    offline = cast_info(model_name=None, manufacturer=None, cast_type=None)
    assert device._async_profile_accepted(offline) is True
    await hass.async_block_till_done()

    # Accepted, but the good profile is kept - otherwise the check disarms itself.
    assert _stored(hass, entry) == original


@pytest.mark.parametrize(
    ("stored_kwargs", "current_kwargs", "expected"),
    [
        (None, {}, PROFILE_FIRST_SIGHT),
        ({}, {}, PROFILE_MATCH),
        ({}, {"host": HOST_B}, PROFILE_HOST_CHANGED),
        ({}, {"model_name": "Chromecast HD"}, PROFILE_ATTRS_CHANGED),
        ({}, {"host": HOST_B, "model_name": "Evil"}, PROFILE_MISMATCH),
        ({}, {"cast_type": None}, PROFILE_INCOMPLETE),
    ],
    ids=["first_sight", "match", "host_only", "attrs_only", "both", "incomplete"],
)
def test_compare_device_profile(
    stored_kwargs: dict | None, current_kwargs: dict, expected: str
) -> None:
    """Each cell of the decision matrix maps to one verdict."""
    stored = (
        None
        if stored_kwargs is None
        else build_device_profile(cast_info(**stored_kwargs).cast_info)
    )
    current = build_device_profile(cast_info(**current_kwargs).cast_info)
    assert compare_device_profile(stored, current) == expected
