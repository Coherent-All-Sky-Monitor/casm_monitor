# Web services

Use `casm-monitor-preview.service` for the current workspace on 8061.
Its historical service name is retained to avoid changing collector/worker setup.

On September 25, 2026, `casm-monitor-web.service` (8060) and the separate
`t3-web.service` (8050) were stopped and disabled at the operator's request.
Do not enable the legacy `casm-monitor-web.service` template. Unit templates
remain in their owning repositories for historical reference; no data was deleted.
systemd removed their installed unit/enablement symlinks during disable.

`casm-monitor-collect`, `casm-monitor-jobs`, `casm-monitor-calibration`, `t2d`,
`t2-inject`, `t3-collect`, dump plotters and `t3-weights-watch` remain separate
services. The workspace's guarded SNAP request uses the existing job queue,
with the same lease and cooldown; hourly acquisition belongs to the collector.
