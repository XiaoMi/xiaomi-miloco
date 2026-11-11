"""Behavioral tests for a bounded, non-blocking decoded-frame delivery stage."""

import gc
import threading
import time
import weakref

import pytest
from miot.flow import LatestFrameDelivery

pytestmark = pytest.mark.unit


def until(predicate):
    deadline = time.monotonic() + 3
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert predicate()


def test_slow_consumer_never_blocks_producer_and_keeps_latest():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def consume(frame, metadata):
        seen.append(metadata)
        if metadata == 0:
            entered.set()
            assert release.wait(3)

    delivery = LatestFrameDelivery(consume)
    try:
        delivery.offer(object(), 0)
        assert entered.wait(2)
        done = threading.Event()

        def produce():
            for i in range(1, 100):
                delivery.offer(object(), i)
            done.set()

        producer = threading.Thread(target=produce)
        producer.start()
        assert done.wait(1), "reader blocked behind consumer"
        producer.join()
        assert delivery.snapshot()["pending"] == 1
        assert delivery.snapshot()["replaced"] == 98
        release.set()
        until(lambda: len(seen) == 2)
        assert seen == [0, 99]
    finally:
        release.set()
        delivery.close()
    assert not delivery.snapshot()["alive"]


def test_replaced_frame_is_released():
    class Frame:
        pass

    entered = threading.Event()
    release = threading.Event()

    def consume(frame, metadata):
        if metadata == 0:
            entered.set()
            release.wait(3)

    delivery = LatestFrameDelivery(consume)
    try:
        delivery.offer(Frame(), 0)
        assert entered.wait(2)
        old = Frame()
        reference = weakref.ref(old)
        delivery.offer(old, 1)
        del old
        delivery.offer(Frame(), 2)
        gc.collect()
        assert reference() is None
    finally:
        release.set()
        delivery.close()


def test_expired_frame_is_dropped_before_conversion():
    def callback(*args):
        pytest.fail("expired frame reached expensive callback")

    delivery = LatestFrameDelivery(callback, max_age=0.1)
    try:
        delivery.offer(object(), 0, received_at=time.monotonic() - 1)
        until(lambda: delivery.snapshot()["expired"] == 1)
        assert delivery.snapshot()["processed"] == 0
    finally:
        delivery.close()


def test_consumer_error_is_counted_and_next_frame_continues():
    seen = []

    def consume(frame, metadata):
        if metadata == 0:
            raise ValueError("consumer error")
        seen.append(metadata)

    delivery = LatestFrameDelivery(consume)
    try:
        delivery.offer(object(), 0)
        until(lambda: delivery.snapshot()["errors"] == 1)
        delivery.offer(object(), 1)
        until(lambda: seen == [1])
        assert delivery.snapshot()["processed"] == 1
    finally:
        delivery.close()


def test_abort_discards_pending_and_refuses_new_frames():
    entered = threading.Event()
    release = threading.Event()
    seen = []

    def consume(frame, metadata):
        seen.append(metadata)
        entered.set()
        release.wait(3)

    delivery = LatestFrameDelivery(consume)
    try:
        delivery.offer(object(), 0)
        assert entered.wait(2)
        delivery.offer(object(), 1)
        delivery.abort()
        assert not delivery.offer(object(), 2)
        assert delivery.snapshot()["pending"] == 0
        release.set()
        delivery.close()
        assert seen == [0]
        delivery.close()
    finally:
        release.set()
        delivery.close()


def test_callback_cost_is_measured():
    delivery = LatestFrameDelivery(lambda *args: time.sleep(0.03))
    try:
        delivery.offer(object(), 0)
        until(lambda: delivery.snapshot()["processed"] == 1)
        assert delivery.snapshot()["callback_wall_ms"] >= 20
        assert delivery.snapshot()["callback_cpu_ms"] >= 0
    finally:
        delivery.close()
