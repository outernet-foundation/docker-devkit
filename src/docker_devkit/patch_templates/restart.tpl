# Restart policy for workloads declaring outernet.internal/restart — the
# workload file carrying the one sanctioned exception to the no-restart-policies
# law where its reader lives. The value is validated against the compose
# restart-policy enum by devkit code before generation (templates are data and
# cannot refuse); presence is the trigger, the annotation value the policy.
{{- $matched := false }}
{{- range $workloadName, $workload := .Workloads }}
{{- $restart := dig "metadata" "annotations" "outernet.internal/restart" "" $workload }}
{{- if $restart }}
{{- range $containerName, $_ := $workload.containers }}
{{- $matched = true }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.restart
  value: {{ $restart }}
  description: Restart the {{ $workloadName }}-{{ $containerName }} service {{ $restart }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
