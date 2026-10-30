# GPU pod plumbing for workloads marked outernet.internal/gpu, ported from the
# infra Pulumi prior art: runtimeClassName nvidia, the GPU nodeSelector, and one
# nvidia.com/gpu per container. Score has no GPU concept, so the annotation on
# the workload file is the single declaration and this template computes every
# patch target — nothing is keyed on hardcoded service names. The runtime class,
# label, and resource name are a seam contract with the cluster tier (device
# plugin + worker labeling, both Pulumi-side); the annotation is org-namespaced
# outernet.internal/* while the nodeSelector label stays placeframe.io/gpu —
# exactly what Pulumi labels GPU nodes with. .Workloads must be captured before
# the range: inside a range body this template engine resolves the outer dot's
# map fields to zero values.
{{- $workloads := .Workloads }}
{{- $matched := false }}
{{- range $i, $manifest := .Manifests }}
{{- if eq $manifest.kind "Deployment" }}
{{- $workloadName := dig "metadata" "annotations" "k8s.score.dev/workload-name" "" $manifest }}
{{- if $workloadName }}
{{- $workload := index $workloads $workloadName }}
{{- if and $workload (eq (dig "metadata" "annotations" "outernet.internal/gpu" "" $workload) "true") }}
{{- $matched = true }}
- op: set
  path: {{ $i }}.spec.template.spec.runtimeClassName
  value: nvidia
  description: NVIDIA runtime class for {{ $workloadName }}
- op: set
  path: {{ $i }}.spec.template.spec.nodeSelector
  value:
    placeframe.io/gpu: "true"
  description: GPU node selector for {{ $workloadName }}
{{- range $containerIndex, $_ := $manifest.spec.template.spec.containers }}
- op: set
  path: {{ $i }}.spec.template.spec.containers.{{ $containerIndex }}.resources.limits
  value:
    nvidia.com/gpu: "1"
  description: One NVIDIA GPU for container {{ $containerIndex }} of {{ $workloadName }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
