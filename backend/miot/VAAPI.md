# Optional VA-API packet decoder

This experimental backend keeps camera transport, Python dependencies and callback
signatures unchanged. It is **off by default**. It uses an already-installed
FFmpeg executable; it does not install or replace FFmpeg/PyAV.

## Enabling

Set `MIOT_VAAPI_FFMPEG` to an absolute executable path (for example
`/usr/bin/ffmpeg`) and keep the existing `enable_hw_accel=True` option.
Linux and an accessible `/dev/dri/renderD128` are required. VA-API drivers must
already work for the selected executable. Legacy Intel systems may additionally
need `LIBVA_DRIVER_NAME=i965`. Tested with FFmpeg 8.1.1 and PyAV 18.1.0.

The initial support boundary is H.264 without B frames. HEVC, unavailable devices,
missing executables and failed initialization use the existing PyAV software
decoder. Removing the environment variable restores the default path for new
streams; no migration is needed.

`MIoTMediaDecoder(decoded_frame_interval=0)` preserves the existing full-rate
BGR callback behavior. A caller that accepts sampled frames can specify a positive
interval in milliseconds. The worker samples decoded surfaces before downloading
them; compressed packets are never sampled. JPEG preview requirements are also
respected. This PR does not change application/UI sampling policy. Full-rate
hardware downloads are not covered by the performance result below.

`set_decoded_frame_interval()` updates software admission. Changing it while
hardware is active switches this stream to software at the next keyframe, so a
viewer can request full rate without leaving an old GPU sampling filter active.

## Failure and lifecycle behavior

One persistent process serves one active decoder. NUT preserves device PTS;
output frames recover their original channel and host receive timestamp from
a table limited to 128 entries. Output encoding retains the demux time base
rather than quantizing timestamps to the nominal frame rate.

Initialization failure falls back immediately. Runtime failure, timestamp
regression, parameter-set changes, metadata overflow or a stalled worker disables
hardware for the current decoder lifetime. The process is reaped before software
resumes; after runtime failure, dependent frames are skipped until an IDR/IRAP.
Cached H.264 parameter sets are supplied to the software decoder. It does not
retry hardware for each packet. A new camera decoder can try again.

A five-second no-output deadline terminates a stuck process and unblocks pipe
writes. Stop first aborts child I/O, joins the owning decoder, and then releases
the muxer and worker resources. Existing application logging records selection
and fallback reasons; FFmpeg diagnostics are bounded to the last 4096 bytes.

## Validation and limits

On a J3160, three alternating rounds of the final integrated packet bridge used
a median **3.845 seconds of parent + child CPU**, compared with **6.759 seconds**
for software decoding using the existing 4/2 decode/conversion thread settings.
The same synthetic 20-second 1920x1080 H.264 input contained 300 frames without B
frames, with 60 full-resolution BGR outputs. Source timestamps and frame order
matched. This is about **43.1% less CPU for this sampled chain**, not an estimate
of whole-service savings. Audio, JPEG generation and downstream inference are not
included. Adding a worker increases RSS; earlier isolated trials measured about
80 MiB extra combined RSS. This PR makes no memory-reduction claim.

Validation includes the SDK unit suite and explicit on-device tests for timestamp
preservation, missing-FFmpeg software fallback and recovery after killing the
hardware process. Device tests are opt-in:

    PYTHONPATH=backend/miot/src MIOT_TEST_VAAPI_SAMPLE=/path/to/640x360-nob.h264 \
      LIBVA_DRIVER_NAME=i965 python -m pytest backend/miot/tests/test_vaapi_device.py

The fixture must contain at least 90 H.264 access units at 15 fps without B
frames and periodic IDRs. Pure unit tests require no hardware:

    PYTHONPATH=backend/miot/src python -m pytest -m unit backend/miot/tests

Real-camera long runs, multi-camera contention and deployment remain pending.
Keep this opt-in until those pass. A compatible native PyAV VA-API build could
replace the subprocess implementation later without changing consumers.
