# score-compose validates and drops workload httpGet probes (httpGet is not a
# supported compose concept), so the generated compose would gate nothing on
# workload health while the k8s rendering emits real probes. This patch-template
# class closes that drift for workloads declaring a probe adapter
# (outernet.internal/probe-adapter): it converts the workload's readinessProbe,
# else livenessProbe, into a compose healthcheck against the computed
# <workload>-<container> service name. The adapter picks the exec spell the
# image runtime can run — python-urllib (python-based workloads shipping
# neither wget nor curl) or bash-http (workloads whose image ships bash and
# nothing else, spelling HTTP over /dev/tcp); a workload declaring an httpGet
# probe without the adapter refuses at generation (devkit code — templates are
# data).
{{- $matched := false }}
{{- range $workloadName, $workload := .Workloads }}
{{- $adapter := dig "metadata" "annotations" "outernet.internal/probe-adapter" "" $workload }}
{{- if or (eq $adapter "python-urllib") (eq $adapter "bash-http") }}
{{- range $containerName, $container := $workload.containers }}
{{- $probe := $container.readinessProbe.httpGet }}
{{- if not $probe }}
{{- $probe = $container.livenessProbe.httpGet }}
{{- end }}
{{- with $probe }}
{{- $matched = true }}
- op: set
  path: services.{{ $workloadName }}-{{ $containerName }}.healthcheck
  value:
    {{- if eq $adapter "python-urllib" }}
    test:
      [
        "CMD",
        "python",
        "-c",
        "import urllib.request; urllib.request.urlopen('http://localhost:{{ .port }}{{ .path }}')",
      ]
    {{- else }}
    test:
      [
        "CMD",
        "bash",
        "-c",
        "exec 3<>/dev/tcp/127.0.0.1/{{ .port }} && printf 'GET {{ .path }} HTTP/1.0\\r\\nHost: localhost\\r\\n\\r\\n' >&3 && read -r status <&3 && case $$status in *200*) exit 0;; *) exit 1;; esac",
      ]
    {{- end }}
    interval: 5s
    start_period: 5s
    timeout: 5s
    retries: 3
  description: Healthcheck from httpGet probe for {{ $workloadName }}-{{ $containerName }}
{{- end }}
{{- end }}
{{- end }}
{{- end }}
{{- if not $matched }}
[]
{{- end }}
