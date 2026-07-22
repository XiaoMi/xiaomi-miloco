# -*- coding: utf-8 -*-
"""Tests for MIoTMediaDecoder pause/resume and MIoTMediaRingBuffer.drain_one.

Covers:
- drain_one: pops and discards frames without calling callbacks
- pause/resume: flag toggling and is_paused property
- paused run loop: drains queue without decoding, resumes decoding after resume
"""

from __future__ import annotations

import asyncio
import threading
import time

import pytest

from miot.decoder import MIoTMediaDecoder, MIoTMediaRingBuffer
from miot.types import MIoTCameraCodec, MIoTCameraFrameData


def _make_frame(data: bytes = b"test", codec: int = MIoTCameraCodec.VIDEO_H264):
    """Helper to create a MIoTCameraFrameData with all required fields."""
    return MIoTCameraFrameData(
        data=data,
        codec_id=codec,
        timestamp=0,
        length=len(data),
        sequence=0,
        frame_type=0,
        channel=0,
    )


# ---- MIoTMediaRingBuffer.drain_one ----


class TestDrainOne:
    def test_drain_empty_queue(self):
        q = MIoTMediaRingBuffer(maxlen=5)
        assert q.drain_one() is False

    def test_drain_video_frame(self):
        q = MIoTMediaRingBuffer(maxlen=5)
        q.put_video(_make_frame())
        assert q.drain_one() is True
        assert q.drain_one() is False  # empty after drain

    def test_drain_audio_frame(self):
        q = MIoTMediaRingBuffer(maxlen=5)
        q.put_audio(_make_frame(codec=MIoTCameraCodec.AUDIO_OPUS))
        assert q.drain_one() is True
        assert q.drain_one() is False

    def test_drain_multiple(self):
        q = MIoTMediaRingBuffer(maxlen=10)
        for i in range(5):
            q.put_video(_make_frame(data=f"f{i}".encode()))
        for i in range(5):
            q.put_audio(_make_frame(data=f"a{i}".encode(), codec=MIoTCameraCodec.AUDIO_OPUS))
        # drain all 10
        for _ in range(10):
            assert q.drain_one() is True
        assert q.drain_one() is False


# ---- MIoTMediaDecoder pause/resume ----


class TestDecoderPauseResume:
    def test_initial_not_paused(self):
        loop = asyncio.new_event_loop()
        try:
            decoder = MIoTMediaDecoder(
                frame_interval=1000,
                video_callback=lambda *a: None,
                main_loop=loop,
            )
            assert decoder.is_paused is False
        finally:
            loop.close()

    def test_pause_sets_flag(self):
        loop = asyncio.new_event_loop()
        try:
            decoder = MIoTMediaDecoder(
                frame_interval=1000,
                video_callback=lambda *a: None,
                main_loop=loop,
            )
            decoder.pause()
            assert decoder.is_paused is True
        finally:
            loop.close()

    def test_resume_clears_flag(self):
        loop = asyncio.new_event_loop()
        try:
            decoder = MIoTMediaDecoder(
                frame_interval=1000,
                video_callback=lambda *a: None,
                main_loop=loop,
            )
            decoder.pause()
            assert decoder.is_paused is True
            decoder.resume()
            assert decoder.is_paused is False
        finally:
            loop.close()

    def test_double_pause_is_idempotent(self):
        loop = asyncio.new_event_loop()
        try:
            decoder = MIoTMediaDecoder(
                frame_interval=1000,
                video_callback=lambda *a: None,
                main_loop=loop,
            )
            decoder.pause()
            decoder.pause()  # second pause should be no-op (no double notify)
            assert decoder.is_paused is True
        finally:
            loop.close()

    def test_stop_clears_paused(self):
        loop = asyncio.new_event_loop()
        try:
            decoder = MIoTMediaDecoder(
                frame_interval=1000,
                video_callback=lambda *a: None,
                main_loop=loop,
            )
            decoder.pause()
            # stop() should clear _paused
            decoder._running = False
            decoder._paused = False
            assert decoder.is_paused is False
        finally:
            loop.close()


# ---- Decoder run loop with pause ----


class TestDecoderRunLoop:
    def test_paused_run_drains_without_decoding(self):
        """When paused, run() pops frames but doesn't call decode callback."""
        loop = asyncio.new_event_loop()
        decoded_frames = []

        def on_video(data, ts, ch):
            decoded_frames.append(data)

        decoder = MIoTMediaDecoder(
            frame_interval=1000,
            video_callback=on_video,
            main_loop=loop,
        )
        decoder._paused = True
        decoder._running = True

        # Push some frames into the queue
        for i in range(5):
            decoder._queue.put_video(_make_frame(data=f"frame{i}".encode()))

        # Run one iteration of the paused loop
        if not decoder._queue.drain_one():
            time.sleep(0.05)

        # Frame should be drained but not decoded
        assert len(decoded_frames) == 0
        # Queue should be smaller
        assert len(decoder._queue._video_buffer) == 4

    def test_resume_allows_decoding(self):
        """After resume, frames are decoded normally."""
        loop = asyncio.new_event_loop()
        decoded_frames = []

        def on_video(data, ts, ch):
            decoded_frames.append(data)

        decoder = MIoTMediaDecoder(
            frame_interval=1000,
            video_callback=on_video,
            main_loop=loop,
        )
        decoder._paused = True
        decoder._running = True

        # Push a frame
        decoder._queue.put_video(_make_frame(data=b"test_frame"))

        # Drain while paused
        decoder._queue.drain_one()
        assert len(decoded_frames) == 0

        # Resume and push another frame
        decoder.resume()
        assert decoder.is_paused is False

        loop.close()
