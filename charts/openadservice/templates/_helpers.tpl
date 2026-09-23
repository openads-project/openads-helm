{{/*
Normalizes a string into an RFC 1123-compliant Kubernetes resource name.
Names longer than 63 characters are shortened and receive an eight-character hash.

Examples:
  "My_Service.Config" -> "my-service-config"
  "--My Service--"    -> "my-service"
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
Normalizes a name and prepends the normalized prefix unless it is already present.

Examples:
  {name: "Zenoh_Router", namePrefix: "demo"}      -> "demo-zenoh-router"
  {name: "demo-zenoh-router", namePrefix: "demo"} -> "demo-zenoh-router"
  {name: "zenoh-router", namePrefix: ""}          -> "zenoh-router"
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
Prefixes and normalizes an in-stack hostname. Without a prefix, the original host
is returned unchanged so external hostnames are preserved.

Examples:
  {host: "zenoh-router", namePrefix: "demo"} -> "demo-zenoh-router"
  {host: "external.example.com", namePrefix: ""} -> "external.example.com"
*/}}
{{- define "openadservice.prefixedHost" -}}
{{- $host := .host | toString -}}
{{- if .namePrefix -}}
{{- include "openadservice.prefixedName" (dict "name" $host "namePrefix" .namePrefix) -}}
{{- else -}}
{{- $host -}}
{{- end -}}
{{- end -}}

{{/*
Creates the effective base name from an explicit name, or the Helm release name
when no name is set, and then applies namePrefix.

Examples:
  {name: "service", releaseName: "my-release", namePrefix: "demo"} -> "demo-service"
  {name: null, releaseName: "my-release", namePrefix: ""}          -> "my-release"
*/}}
{{- define "openadservice.baseName" -}}
{{- include "openadservice.prefixedName" (dict "name" (.name | default .releaseName) "namePrefix" .namePrefix) -}}
{{- end -}}

{{/*
Appends and normalizes the instance key when instances are configured. Without
instances, the base name is returned unchanged.

Examples:
  {baseName: "demo-service", instanceKey: "Node_A", hasInstances: true} -> "demo-service-node-a"
  {baseName: "demo-service", instanceKey: "", hasInstances: false}     -> "demo-service"
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
Creates a volume name from the normalized basename and a hash of the full mount
path, preventing collisions between equal basenames in different directories.

Example:
  "/some/path/hello.txt" -> "hello-txt-6245b242f7e0"
*/}}
{{- define "openadservice.volumeName" -}}
{{- $basename := include "openadservice.rfc1123CompliantName" (base .) | trunc 50 | trimSuffix "-" -}}
{{- printf "%s-%s" $basename (sha256sum . | trunc 12) -}}
{{- end -}}

{{/*
Creates an inline ConfigMap name from the service name, normalized basename, and
full-path hash.

Example:
  service "example" with mountPath "/some/path/hello.txt"
  -> "example-hello-txt-6245b242f7e0"
*/}}
{{- define "openadservice.inlineConfigMapName" -}}
{{- $serviceName := include "openadservice.rfc1123CompliantName" (.root.Values.name | default .root.Release.Name) -}}
{{- $basename := include "openadservice.rfc1123CompliantName" (base .mountPath) -}}
{{- printf "%s-%s" (printf "%s-%s" $serviceName $basename | trunc 50 | trimSuffix "-") (sha256sum .mountPath | trunc 12) -}}
{{- end -}}
