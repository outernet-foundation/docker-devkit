# CUDA device access for GPU-marked workloads (metadata annotation
# outernet.internal/gpu): `gpus: all` semantics in the long-form device
# reservation (score-compose's output schema accepts no `gpus` scalar and no
# `count: all`; an omitted count means every GPU). Per-variant twin of
# gpu-rocm.tpl — the devkit registers this file exactly when the compose
# rendering's gpu variant is cuda. Service names are computed from the
# workload/container pair, never hardcoded.
{{- $matched := false }}
{{- range $workloadName, $workload := .Workloads }}
{{- if eq (dig "metadata" "annotations" "outernet.internal/gpu" "" $workload) "true" }}
{{- range $containerName, $_ := $workload.containers }}
{{- $matched = true }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.deploy.resources.reservations.devices
  value:
    - driver: nvidia
      capabilities: [gpu]
  description: CUDA device access for {{ $workloadName }}-{{ $containerName }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
