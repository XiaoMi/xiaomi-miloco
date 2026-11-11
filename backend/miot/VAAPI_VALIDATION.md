# VA-API validation update, 2026-11-11

## Goal and status
Keep real-time delivery bounded while preserving hardware decode through normal sampling and timestamp changes. SDK changes are ready for draft review; production acceptance continues. WebUI work remains separate.

## Controlled-order exploratory service comparison
2026-11-10, A/B/B/A; 180 s warm-up and 600 s measurement per round. Same service version; A selects a missing FFmpeg path to exercise software initialization fallback, B enables VA-API. Natural perception workloads were not held constant.
| Round | CPU cores mean | cgroup memory mean MiB | HTTP root p95 ms |
|---|---:|---:|---:|
| A1 | 2.157 | 1297.1 | 159.96 |
| B1 | 1.352 | 1023.9 | 83.96 |
| B2 | 1.431 | 1030.0 | 64.38 |
| A2 | 2.055 | 1277.0 | 124.68 |

2397 measurement samples, no HTTP probe failures. Mean CPU 2.106 versus 1.391 cores, approximately 34% lower. This supports efficiency on this installation, not a universal or pure-decoder gain. Hardware rounds showed three host-wide FFmpeg processes; per-camera delivered work was not measured equivalently in software, so equal four-camera workload is not established. Memory is cgroup accounting, not RSS. Preview activity around 17:05 occurred after these rounds. The immediate post-restore health probe failed during startup; later service and pipeline checks succeeded.

## Long-run observation
Service remained active since 2026-11-10 15:57, with zero automatic service restarts at 2026-11-11 15:03. Available retained metrics cover approximately 02:00 to 15:04 today: 5817 per-stream snapshots across four cameras. All four have recent hardware output. Delivery pending was at most one and callback error counters were zero in observed snapshots. Output-progress age p95 by anonymized stream was 0.964, 1.009, 0.993 and 1.059 seconds; this is not end-to-end video latency.

One explicit software fallback was logged at 07:16:02 because video parameters changed. Three timestamp-reset recoveries occurred on one stream at 09:24, 10:00 and 10:55. Worker changes also accompany subscriber sampling transitions, so process replacement is not itself a fallback. Continuous uninterrupted hardware delivery is not claimed.

Current cgroup memory was approximately 2.47 GiB, higher than the short A/B means; no memory growth slope was collected today. Service cumulative CPU and pipeline counters are available, but no continuous whole-service CPU time series was collected after the A/B run. Downstream perception buffer overflow/drop warnings remain. These are separate follow-up concerns.

## Validation already completed before deployment
Ruff, SDK unit tests, H.264 and HEVC device regressions, wheel build and diff checks passed in the timestamp deployment validation. Tests include slow consumers, bounded pending frames, idle safety, runtime recovery/fallback, sampling changes and timestamp-reset recovery. Deployment-specific camera IDs and allowlists are excluded from this change.

## Next acceptance work
Explain video-parameter fallback and test keyframe reinitialization for compatible new parameters; collect matched per-camera input/output work; sample longer memory trends. Keep this PR draft. Handle WebUI subscription lifetime separately.
