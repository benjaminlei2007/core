"""Timer implementation for intents."""

import asyncio
from collections import defaultdict
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from enum import StrEnum
from functools import cached_property
import logging
import time
from typing import Any

import voluptuous as vol

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv, intent
from homeassistant.util import ulid

from .const import TIMER_DATA

_LOGGER = logging.getLogger(__name__)


class TimerEventType(StrEnum):
    """Timer event type."""

    STARTED = "started"
    FINISHED = "finished"
    CANCELLED = "cancelled"
    UPDATED = "updated"


@dataclass(frozen=True)
class TimerEvent:
    """Event sent when a timer changes state."""

    type: TimerEventType
    timer_id: str
    seconds_left: int
    name: str | None = None


@dataclass
class TimerInfo:
    """Information for a single timer."""

    id: str
    name: str | None
    seconds: int
    device_id: str | None
    task: asyncio.Task
    start_hours: int | None
    start_minutes: int | None
    start_seconds: int | None

    updated_at: int
    """Timestamp when timer was last updated (time.monotonic_ns)"""

    @property
    def seconds_left(self) -> int:
        """Return number of seconds left on the timer."""
        now = time.monotonic_ns()
        seconds_running = int((now - self.updated_at) / 1e9)
        return max(0, self.seconds - seconds_running)

    @cached_property
    def name_normalized(self) -> str | None:
        """Return normalized timer name."""
        if self.name is None:
            return None

        return self.name.strip().casefold()


class TimerError(Exception):
    """Base class for timer errors."""


class TimerNotFoundError(TimerError):
    """Error when a timer could not be found by name or start time."""

    def __init__(
        self,
        name: str | None,
        start_hours: int | None,
        start_minutes: int | None,
        start_seconds: int | None,
        device_id: str | None,
    ) -> None:
        """Initialize error."""
        self.name = name
        self.start_hours = start_hours
        self.start_minutes = start_minutes
        self.start_seconds = start_seconds
        self.device_id = device_id

    def __repr__(self) -> str:
        """Return string representation."""
        return (
            f"<TimerNotFoundError "
            f"name={self.name}, "
            f"hours={self.start_hours}, "
            f"minutes={self.start_minutes}, "
            f"seconds={self.start_seconds}, "
            f"device_id={self.device_id}>"
        )


TimerHandler = Callable[[TimerEvent], Coroutine[Any, Any, None]]


class TimerManager:
    """Manager for intent timers."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize timer manager."""
        self.hass = hass

        # timer id -> timer
        self.timers: dict[str, TimerInfo] = {}

        # device id -> handlers
        self.handlers: dict[str | None, list[TimerHandler]] = defaultdict(list)

    def register_handler(
        self, handler: TimerHandler, device_id: str | None
    ) -> Callable[[], None]:
        """Register a timer event handler for a device."""
        self.handlers[device_id].append(handler)

        return lambda: self.handlers[device_id].remove(handler)

    async def start_timer(
        self,
        hours: int | None,
        minutes: int | None,
        seconds: int | None,
        device_id: str | None,
        name: str | None = None,
    ) -> str:
        """Start a timer."""
        total_seconds = 0
        if hours is not None:
            total_seconds += 60 * 60 * hours

        if minutes is not None:
            total_seconds += 60 * minutes

        if seconds is not None:
            total_seconds += seconds

        timer_id = ulid.ulid_now()
        created_at = time.monotonic_ns()
        self.timers[timer_id] = TimerInfo(
            id=timer_id,
            name=name,
            start_hours=hours,
            start_minutes=minutes,
            start_seconds=seconds,
            seconds=total_seconds,
            device_id=device_id,
            task=self.hass.async_create_background_task(
                self._wait_for_timer(timer_id, total_seconds, created_at),
                name=f"Timer {timer_id}",
            ),
            updated_at=created_at,
        )

        event = TimerEvent(
            TimerEventType.STARTED, timer_id, name=name, seconds_left=total_seconds
        )
        await asyncio.gather(*(handler(event) for handler in self.handlers[device_id]))

        _LOGGER.debug(
            "Timer started: id=%s, name=%s, hours=%s, minutes=%s, seconds=%s",
            timer_id,
            name,
            hours,
            minutes,
            seconds,
        )

        return timer_id

    async def _wait_for_timer(
        self, timer_id: str, seconds: int, updated_at: int
    ) -> None:
        """Sleep until timer is up. Timer is only finished if it hasn't been updated."""
        try:
            await asyncio.sleep(seconds)
            if (timer := self.timers.get(timer_id)) and (
                timer.updated_at == updated_at
            ):
                await self._timer_finished(timer_id, timer.device_id)
        except asyncio.CancelledError:
            pass  # expected when timer is updated

    def find_timer_by_name(self, name: str, device_id: str | None) -> str | None:
        """Find a timer by name."""
        name = name.strip().casefold()
        for timer in self.timers.values():
            if timer.name_normalized == name:
                return timer.id

        return None

    def find_timer_by_start(
        self,
        hours: int | None,
        minutes: int | None,
        seconds: int | None,
        device_id: str | None,
    ) -> str | None:
        """Find a timer by its starting time."""
        for timer in self.timers.values():
            if (
                (timer.start_hours == hours)
                and (timer.start_minutes == minutes)
                and (timer.start_seconds == seconds)
            ):
                return timer.id

        return None

    async def cancel_timer(self, timer_id: str) -> None:
        """Cancel a timer."""
        timer = self.timers.pop(timer_id, None)
        if timer is None:
            return

        timer.seconds = 0
        timer.updated_at = time.monotonic_ns()
        timer.task.cancel()
        event = TimerEvent(
            TimerEventType.CANCELLED, timer_id, name=timer.name, seconds_left=0
        )
        await asyncio.gather(
            *(handler(event) for handler in self.handlers[timer.device_id])
        )
        _LOGGER.debug("Timer cancelled: id=%s", timer_id)

    async def add_time(self, timer_id: str, seconds: int) -> None:
        """Add time to a timer."""
        if seconds == 0:
            return

        timer = self.timers.get(timer_id)
        if timer is None:
            return

        timer.seconds = max(0, timer.seconds_left + seconds)
        timer.updated_at = time.monotonic_ns()
        timer.task.cancel()
        timer.task = self.hass.async_create_background_task(
            self._wait_for_timer(timer_id, timer.seconds, timer.updated_at),
            name=f"Timer {timer_id}",
        )
        event = TimerEvent(
            TimerEventType.UPDATED,
            timer_id,
            name=timer.name,
            seconds_left=timer.seconds,
        )
        await asyncio.gather(
            *(handler(event) for handler in self.handlers[timer.device_id])
        )

        if seconds > 0:
            _LOGGER.debug("Timer increased by %s second(s): id=%s", seconds, timer_id)
        else:
            _LOGGER.debug("Timer decreased by %s second(s): id=%s", -seconds, timer_id)

    async def remove_time(self, timer_id: str, seconds: int) -> None:
        """Remove time from a timer."""
        await self.add_time(timer_id, -seconds)

    async def _timer_finished(self, timer_id: str, device_id: str | None) -> None:
        """Call event handlers when a timer finishes."""
        timer = self.timers.pop(timer_id, None)
        if timer is None:
            return

        event = TimerEvent(
            TimerEventType.FINISHED, timer_id, name=timer.name, seconds_left=0
        )
        await asyncio.gather(*(handler(event) for handler in self.handlers[device_id]))

        _LOGGER.debug("Timer finished: id=%s", timer_id)


def _find_timer(
    hass: HomeAssistant, slots: dict[str, Any], device_id: str | None
) -> str:
    timer_manager: TimerManager = hass.data[TIMER_DATA]

    timer_id: str | None = None
    if "name" in slots:
        name: str = slots["name"]["value"]
        timer_id = timer_manager.find_timer_by_name(name, device_id=device_id)

        if timer_id is not None:
            return timer_id

    start_hours: int | None = None
    if "start_hours" in slots:
        start_hours = int(slots["start_hours"]["value"])

    start_minutes: int | None = None
    if "start_minutes" in slots:
        start_minutes = int(slots["start_minutes"]["value"])

    start_seconds: int | None = None
    if "start_seconds" in slots:
        start_seconds = int(slots["start_seconds"]["value"])

    timer_id = timer_manager.find_timer_by_start(
        start_hours, start_minutes, start_seconds, device_id=device_id
    )

    if timer_id is None:
        raise TimerNotFoundError(
            name=name,
            start_hours=start_hours,
            start_minutes=start_minutes,
            start_seconds=start_seconds,
            device_id=device_id,
        )

    return timer_id


def _get_total_seconds(slots: dict[str, Any]) -> int:
    total_seconds = 0
    if "hours" in slots:
        total_seconds += 60 * 60 * int(slots["hours"]["value"])

    if "minutes" in slots:
        total_seconds += 60 * int(slots["minutes"]["value"])

    if "seconds" in slots:
        total_seconds += int(slots["seconds"]["value"])

    return total_seconds


class SetTimerIntentHandler(intent.IntentHandler):
    """Intent handler for starting a new timer."""

    intent_type = intent.INTENT_SET_TIMER
    slot_schema = {
        vol.Required(vol.Any("hours", "minutes", "seconds")): cv.positive_int,
        vol.Optional("name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        timer_manager: TimerManager = hass.data[TIMER_DATA]
        slots = self.async_validate_slots(intent_obj.slots)

        device_id: str | None = None
        if "device_id" in slots:
            device_id = slots["device_id"]["value"]

        name: str | None = None
        if "name" in slots:
            name = slots["name"]["value"]

        hours: int | None = None
        if "hours" in slots:
            hours = int(slots["hours"]["value"])

        minutes: int | None = None
        if "minutes" in slots:
            minutes = int(slots["minutes"]["value"])

        seconds: int | None = None
        if "seconds" in slots:
            seconds = int(slots["seconds"]["value"])

        await timer_manager.start_timer(
            hours, minutes, seconds, device_id=device_id, name=name
        )

        return intent_obj.create_response()


class CancelTimerIntentHandler(intent.IntentHandler):
    """Intent handler for cancelling running timer."""

    intent_type = intent.INTENT_CANCEL_TIMER
    slot_schema = {
        vol.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        vol.Optional("name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        timer_manager: TimerManager = hass.data[TIMER_DATA]
        slots = self.async_validate_slots(intent_obj.slots)

        device_id: str | None = None
        if "device_id" in slots:
            device_id = slots["device_id"]["value"]

        timer_id = _find_timer(hass, slots, device_id=device_id)
        await timer_manager.cancel_timer(timer_id)

        return intent_obj.create_response()


class IncreaseTimerIntentHandler(intent.IntentHandler):
    """Intent handler for increasing the time of a running timer."""

    intent_type = intent.INTENT_INCREASE_TIMER
    slot_schema = {
        vol.Any("hours", "minutes", "seconds"): cv.positive_int,
        vol.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        vol.Optional("name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        timer_manager: TimerManager = hass.data[TIMER_DATA]
        slots = self.async_validate_slots(intent_obj.slots)

        device_id: str | None = None
        if "device_id" in slots:
            device_id = slots["device_id"]["value"]

        total_seconds = _get_total_seconds(slots)

        timer_id = _find_timer(hass, slots, device_id=device_id)
        await timer_manager.add_time(timer_id, total_seconds)

        return intent_obj.create_response()


class DecreaseTimerIntentHandler(intent.IntentHandler):
    """Intent handler for decreasing the time of a running timer."""

    intent_type = intent.INTENT_DECREASE_TIMER
    slot_schema = {
        vol.Required(vol.Any("hours", "minutes", "seconds")): cv.positive_int,
        vol.Any("start_hours", "start_minutes", "start_seconds"): cv.positive_int,
        vol.Optional("name"): cv.string,
        vol.Optional("device_id"): cv.string,
    }

    async def async_handle(self, intent_obj: intent.Intent) -> intent.IntentResponse:
        """Handle the intent."""
        hass = intent_obj.hass
        timer_manager: TimerManager = hass.data[TIMER_DATA]
        slots = self.async_validate_slots(intent_obj.slots)

        device_id: str | None = None
        if "device_id" in slots:
            device_id = slots["device_id"]["value"]

        total_seconds = _get_total_seconds(slots)

        timer_id = _find_timer(hass, slots, device_id=device_id)
        await timer_manager.remove_time(timer_id, total_seconds)

        return intent_obj.create_response()
