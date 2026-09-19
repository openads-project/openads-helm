{{/*
normalizes a string into an RFC 1123 compliant Kubernetes resource name
example: "My_Service.Config" becomes "my-service-config"
*/}}
{{- define "openadservice.rfc1123CompliantName" -}}
{{- $rawName := printf "%s" . | lower | replace "_" "-" | replace "." "-" -}}
{{- $name := regexReplaceAll "[^a-z0-9-]+" $rawName "-" | trimAll "-" -}}
{{- if gt (len $name) 63 -}}
{{- printf "%s-%s" ($name | trunc 54 | trimSuffix "-") (sha256sum $name | trunc 8) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}

{{/*
prefixes and normalizes a name, unless it already contains the prefix
*/}}
{{- define "openadservice.prefixedName" -}}
{{- $name := include "openadservice.rfc1123CompliantName" .name -}}
{{- $prefix := .namePrefix | default "" -}}
{{- $prefix = include "openadservice.rfc1123CompliantName" $prefix -}}
{{- if and $prefix (ne $name $prefix) (not (hasPrefix (printf "%s-" $prefix) $name)) -}}
{{- include "openadservice.rfc1123CompliantName" (printf "%s-%s" $prefix $name) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}

{{/*
creates the effective base name for a release
*/}}
{{- define "openadservice.baseName" -}}
{{- include "openadservice.prefixedName" (dict "name" (.name | default .releaseName) "namePrefix" .namePrefix) -}}
{{- end -}}

{{/*
appends the required instance key to a base name
*/}}
{{- define "openadservice.instanceName" -}}
{{- if .hasInstances -}}
{{- if empty .instanceKey -}}
{{- fail "openadservice.instances keys must not be empty" -}}
{{- end -}}
{{- include "openadservice.rfc1123CompliantName" (printf "%s-%s" .baseName .instanceKey) -}}
{{- else -}}
{{- .baseName -}}
{{- end -}}
{{- end -}}

{{/*
creates a volume name from the basename and a hash of the full mount path
example: "/some/path/hello.txt" becomes "hello-txt-6245b242f7e0"
*/}}
{{- define "openadservice.volumeName" -}}
{{- $basename := include "openadservice.rfc1123CompliantName" (base .) | trunc 50 | trimSuffix "-" -}}
{{- printf "%s-%s" $basename (sha256sum . | trunc 12) -}}
{{- end -}}

{{/*
creates an inline ConfigMap name from the service name, basename, and full-path hash
example: service "example" and "/some/path/hello.txt" become "example-hello-txt-6245b242f7e0"
*/}}
{{- define "openadservice.inlineConfigMapName" -}}
{{- $serviceName := include "openadservice.rfc1123CompliantName" (.root.Values.name | default .root.Release.Name) -}}
{{- $basename := include "openadservice.rfc1123CompliantName" (base .mountPath) -}}
{{- printf "%s-%s" (printf "%s-%s" $serviceName $basename | trunc 50 | trimSuffix "-") (sha256sum .mountPath | trunc 12) -}}
{{- end -}}
