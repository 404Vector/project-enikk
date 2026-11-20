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

{{- define "enikk-core.image" -}}
{{ .Values.image.repository }}:{{ .Values.image.tag | default "unset" }}
{{- end -}}

{{/* Database connection settings from the CloudNativePG Secret. */}}
{{- define "enikk-core.databaseEnv" -}}
- name: ENIKK_CORE_DATABASE__HOST
  valueFrom: { secretKeyRef: { name: {{ .Values.database.secret }}, key: host } }
- name: ENIKK_CORE_DATABASE__PORT
  valueFrom: { secretKeyRef: { name: {{ .Values.database.secret }}, key: port } }
- name: ENIKK_CORE_DATABASE__NAME
  valueFrom: { secretKeyRef: { name: {{ .Values.database.secret }}, key: dbname } }
- name: ENIKK_CORE_DATABASE__USER
  valueFrom: { secretKeyRef: { name: {{ .Values.database.secret }}, key: username } }
- name: ENIKK_CORE_DATABASE__PASSWORD
  valueFrom: { secretKeyRef: { name: {{ .Values.database.secret }}, key: password } }
{{- end -}}

{{- define "enikk-core.podSecurityContext" -}}
runAsNonRoot: true
runAsUser: 10001
runAsGroup: 10001
seccompProfile:
  type: RuntimeDefault
{{- end -}}

{{- define "enikk-core.containerSecurityContext" -}}
allowPrivilegeEscalation: false
readOnlyRootFilesystem: true
capabilities:
  drop: [ALL]
{{- end -}}
