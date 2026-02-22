{{/*
Expand the name of the chart.
*/}}
{{- define "openshrimp.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "openshrimp.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Component fullnames (for multi-component deployments)
*/}}
{{- define "openshrimp.frontendFullname" -}}
{{- printf "%s-frontend" (include "openshrimp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openshrimp.agentFullname" -}}
{{- printf "%s-agent" (include "openshrimp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openshrimp.pgvectorFullname" -}}
{{- printf "%s-pgvector" (include "openshrimp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{- define "openshrimp.browserlessFullname" -}}
{{- printf "%s-browserless" (include "openshrimp.fullname" .) | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "openshrimp.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "openshrimp.labels" -}}
helm.sh/chart: {{ include "openshrimp.chart" . }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels (generic)
*/}}
{{- define "openshrimp.selectorLabels" -}}
app.kubernetes.io/name: {{ include "openshrimp.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
Selector labels per component
*/}}
{{- define "openshrimp.frontendSelectorLabels" -}}
{{ include "openshrimp.selectorLabels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{- define "openshrimp.agentSelectorLabels" -}}
{{ include "openshrimp.selectorLabels" . }}
app.kubernetes.io/component: agent
{{- end }}

{{- define "openshrimp.pgvectorSelectorLabels" -}}
{{ include "openshrimp.selectorLabels" . }}
app.kubernetes.io/component: pgvector
{{- end }}

{{- define "openshrimp.browserlessSelectorLabels" -}}
{{ include "openshrimp.selectorLabels" . }}
app.kubernetes.io/component: browserless
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "openshrimp.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "openshrimp.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}
