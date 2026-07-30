{{- define "rfc1123CompliantName" -}}
{{- printf "%s" . | lower | replace "_" "-" | replace "." "-" | trunc 63 | trimPrefix "-" | trimSuffix "-" -}}
{{- end -}}
