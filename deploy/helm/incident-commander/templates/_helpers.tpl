{{- define "ic.name" -}}{{ .Chart.Name }}{{- end -}}
{{- define "ic.fullname" -}}{{ printf "%s-%s" .Release.Name .Chart.Name | trunc 63 | trimSuffix "-" }}{{- end -}}
{{- define "ic.labels" -}}
app.kubernetes.io/name: {{ include "ic.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end -}}
{{- define "ic.selectorLabels" -}}
app.kubernetes.io/name: {{ include "ic.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{/* Fail the render if no secrets are provided — never deploy with empty secrets. */}}
{{- define "ic.requireSecrets" -}}
{{- if not .Values.secrets.existingSecret -}}
{{- if or (not .Values.secrets.sessionSecret) (not .Values.secrets.fernetKeys) -}}
{{- fail "incident-commander: provide secrets.existingSecret OR both secrets.sessionSecret and secrets.fernetKeys. Never commit real secrets — generate per the chart README." -}}
{{- end -}}
{{- end -}}
{{- end -}}

{{/* Name of the Secret holding SESSION_SECRET / FERNET_KEYS. */}}
{{- define "ic.secretName" -}}
{{- if .Values.secrets.existingSecret -}}{{ .Values.secrets.existingSecret }}{{- else -}}{{ include "ic.fullname" . }}{{- end -}}
{{- end -}}

{{/* Shared env block for the app Deployment and the migrate Job. */}}
{{- define "ic.env" -}}
- name: SESSION_SECRET
  valueFrom:
    secretKeyRef: { name: {{ include "ic.secretName" . }}, key: SESSION_SECRET }
- name: FERNET_KEYS
  valueFrom:
    secretKeyRef: { name: {{ include "ic.secretName" . }}, key: FERNET_KEYS }
- name: BASE_URL
  value: {{ .Values.config.baseUrl | quote }}
- name: IC_ADMIN_EMAIL
  value: {{ .Values.config.adminEmail | quote }}
- name: SESSION_HTTPS_ONLY
  value: {{ .Values.config.sessionHttpsOnly | quote }}
- name: DB_HOST
  value: {{ required "externalDatabase.host is required — the chart does not bundle a database" .Values.externalDatabase.host | quote }}
- name: DB_PORT
  value: {{ .Values.externalDatabase.port | quote }}
- name: DB_USER
  value: {{ .Values.externalDatabase.user | quote }}
- name: DB_NAME
  value: {{ .Values.externalDatabase.database | quote }}
- name: DB_PASSWORD
  valueFrom:
    secretKeyRef:
      name: {{ required "externalDatabase.existingSecret is required — create a Secret holding the DB password" .Values.externalDatabase.existingSecret }}
      key: {{ .Values.externalDatabase.passwordKey | quote }}
{{- end -}}
