# tests/test_events.py
import json

from clio.events import (
    EVENT_JOB_PERSISTED, Event, EventBus, SseFormatter,
)


def test_event_ts_autofills():
    event = Event(type=EVENT_JOB_PERSISTED, job_id="clio-1")
    assert event.ts.startswith("2")
    assert len(event.ts) > 10


def test_event_ts_preserved():
    event = Event(type="x", job_id="j", ts="fixed")
    assert event.ts == "fixed"


def test_bus_delivers_to_subscribers():
    bus = EventBus()
    received = []
    bus.subscribe(received.append)
    event = Event(type="a", job_id="j")
    bus.publish(event)
    assert received == [event]


def test_bus_multiple_subscribers_in_order():
    bus = EventBus()
    first, second = [], []
    bus.subscribe(first.append)
    bus.subscribe(second.append)
    event = Event(type="a", job_id="j")
    bus.publish(event)
    assert first == second == [event]


def test_sse_format():
    event = Event(type=EVENT_JOB_PERSISTED, job_id="clio-1", data={"status": "ok"})
    raw = SseFormatter.format(event)
    assert raw.startswith("data: {")
    assert raw.endswith("\n\n")
    payload = json.loads(raw[len("data: "):])
    assert payload["type"] == EVENT_JOB_PERSISTED
    assert payload["job_id"] == "clio-1"
    assert payload["data"] == {"status": "ok"}
