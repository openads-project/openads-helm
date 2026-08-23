{{/*
normalizes a string into an RFC 1123 compliant Kubernetes resource name
example: "My_Service.Config" becomes "my-service-config"
*/}}
{{- define "openadservice.rfc1123CompliantName" -}}
{{- printf "%s" . | lower | replace "_" "-" | replace "." "-" | trunc 63 | trimPrefix "-" | trimSuffix "-" -}}
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
