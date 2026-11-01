{{/* mirror openadservice.prefixedName so peer DNS names match chart resources */}}
{{- define "openadservice.prefixedName" -}}
{{- $prefix := regexReplaceAll "[^a-z0-9-]+" (index . 0 | lower | replace "_" "-" | replace "." "-") "-" | trimAll "-" -}}
{{- if gt (len $prefix) 63 -}}
{{- $prefix = printf "%s-%s" ($prefix | trunc 54 | trimSuffix "-") (sha256sum $prefix | trunc 8) -}}
{{- end -}}
{{- $name := regexReplaceAll "[^a-z0-9-]+" (index . 1 | lower | replace "_" "-" | replace "." "-") "-" | trimAll "-" -}}
{{- if gt (len $name) 63 -}}
{{- $name = printf "%s-%s" ($name | trunc 54 | trimSuffix "-") (sha256sum $name | trunc 8) -}}
{{- end -}}
{{- if and $prefix (ne $name $prefix) (not (hasPrefix (printf "%s-" $prefix) $name)) -}}
{{- $name = printf "%s-%s" $prefix $name -}}
{{- if gt (len $name) 63 -}}
{{- $name = printf "%s-%s" ($name | trunc 54 | trimSuffix "-") (sha256sum $name | trunc 8) -}}
{{- end -}}
{{- end -}}
{{- $name -}}
{{- end -}}
{{- include "openadservice.prefixedName" . -}}
