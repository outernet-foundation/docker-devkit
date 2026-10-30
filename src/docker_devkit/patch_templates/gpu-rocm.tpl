# ROCm device access for GPU-marked workloads (metadata annotation
# outernet.internal/gpu): the render/video device nodes plus the groups allowed
# to open them (score-compose's output schema wants DeviceMapping objects and
# string group ids). Per-variant twin of gpu-cuda.tpl — the devkit registers
# this file exactly when the compose rendering's gpu variant is rocm.
{{- $matched := false }}
{{- range $workloadName, $workload := .Workloads }}
{{- if eq (dig "metadata" "annotations" "outernet.internal/gpu" "" $workload) "true" }}
{{- range $containerName, $_ := $workload.containers }}
{{- $matched = true }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.devices
  value:
    - source: /dev/kfd
      target: /dev/kfd
    - source: /dev/dri
      target: /dev/dri
  description: ROCm device nodes for {{ $workloadName }}-{{ $containerName }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.group_add
  value:
    - "44"
    - "110"
  description: ROCm render/video group membership for {{ $workloadName }}-{{ $containerName }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
