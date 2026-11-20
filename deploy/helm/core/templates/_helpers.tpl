{{- define "enikk-core.labels" -}}
app.kubernetes.io/name: enikk-core
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/part-of: enikk
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end -}}

{{- define "enikk-core.selectorLabels" -}}
app.kubernetes.io/name: enikk-core
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}
