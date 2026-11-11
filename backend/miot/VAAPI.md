# Optional VA-API video decoding

Hardware decoding is off by default. Enable it with an absolute
`MIOT_VAAPI_FFMPEG` executable path and `enable_hw_accel=True` on the decoder.
Linux, a usable VA-API render node and a compatible FFmpeg build are required.
No additional Python dependency is installed.

The initial supported streams are H.264 and HEVC without B-frame reordering.
Unsupported inputs or failed initialization use the existing PyAV software
decoder. The bridge preserves device PTS and host receive timestamps through
NUT; it does not invent DTS for reordered input.

## Processing and overload policy

All compressed packets accepted by the worker are muxed in order. The existing
SDK ingress queue is unchanged. GPU frame selection happens before hwdownload;
a zero decoded-frame interval retains full-rate defaults when consumers keep up.

The output reader matches lightweight timestamp metadata and offers decoded
frames to a separate delivery thread. There is at most one frame in progress
and one pending frame. A new pending frame replaces the previous pending frame.
Frames older than one second since worker input admission are discarded before
conversion. This is a real-time policy and is not a lossless archival recorder.

The metadata index remains capped at 128 entries. Its oldest entry can expire
without discarding the compressed packet. A later decoded output with no matching
metadata is obsolete and is discarded; this is not a device failure. Consumer
exceptions are counted and logged separately, and do not stop output reading.

Normal rate changes coalesce and rebuild the worker at the next keyframe.
The old worker continues until then, and is reaped before its replacement starts.
One runtime hardware-worker recovery is allowed per decoder lifetime, at a new
keyframe. A failed recovery or a repeated device failure leaves the stream on
software. Timestamp discontinuities rebuild at a keyframe without consuming the hardware-failure recovery budget. Video parameter changes still select software and remain a follow-up limitation. Initialization failure stays fail-fast. Output deadlines account for
the configured sampling interval and do not treat an inactive input as a device
failure.

Stop discards pending delivery, terminates the child and joins reader, watcher
and delivery threads. A consumer that cannot stop within the join bound is
reported as an unclean stop; arbitrary user callbacks cannot be force-cancelled.

## Diagnostics and scope

Every 30 seconds, aggregate pipeline metrics report input/output counts, mux
time, pending/expired metadata, replaced/expired frames, callback wall/CPU time
and output progress. No per-frame log is emitted.

Application live-FPS policy, encoding, async subscriber fan-out and slow-client
queues remain the application's responsibility. This bounded delivery stage
does not claim to bound every downstream task. Audio handling is unchanged.

## Validation

Unit tests cover bounded latest-frame ownership, slow/error consumers, expiry,
shutdown, metadata eviction, idle input, keyframe rate changes and bounded
hardware recovery. Optional device tests use the environment variables below
with a 640x360, 15 fps, at least 90-packet, no-B-frame sample with regular
keyframes:

```sh
PYTHONPATH=backend/miot/src python -m pytest -m unit backend/miot/tests
MIOT_TEST_VAAPI_SAMPLE=/path/to/sample.hevc \
MIOT_TEST_VAAPI_CODEC=hevc LIBVA_DRIVER_NAME=i965 \
PYTHONPATH=backend/miot/src python -m pytest \
  backend/miot/tests/test_vaapi_device.py \
  backend/miot/tests/test_vaapi_slow_consumer.py
```

Use `MIOT_TEST_VAAPI_CODEC=h264` for H.264. Device tests verify timestamp
preservation, slow-consumer pipe draining, worker recovery, software fallback
after repeated failure, dynamic rates and process/thread cleanup.
Production camera identifiers, canary allowlists and deployment units are not
part of this SDK change. Whole-service CPU and long-run memory improvement
require workload-matched measurements.
