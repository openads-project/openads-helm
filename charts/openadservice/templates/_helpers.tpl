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

{{/*
creates an init container that downloads files from an S3-compatible MinIO endpoint
into the emptyDir volume belonging to a volume entry
*/}}
{{- define "openadservice.minioDownloadInitContainer" -}}
{{- $minio := .volume.minio -}}
{{- $credentials := $minio.credentials | required "minio volume requires credentials" -}}
{{- $mountPath := .volume.mountPath | required "minio volume requires mountPath" -}}
{{- $basename := include "openadservice.rfc1123CompliantName" (base $mountPath) | trunc 32 | trimSuffix "-" -}}
- name: "{{ printf "minio-download-%s-%s" $basename (sha256sum $mountPath | trunc 12) }}"
  image: minio/mc:latest
  imagePullPolicy: Always
  env:
  - name: MINIO_ACCESS_KEY
    {{- if $credentials.secretName }}
    valueFrom:
      secretKeyRef:
        name: "{{ tpl $credentials.secretName .root }}"
        key: MINIO_ACCESS_KEY
    {{- else }}
    value: {{ $credentials.accessKey | required "minio volume requires credentials provided directly or via secret" | quote }}
    {{- end }}
  - name: MINIO_SECRET_KEY
    {{- if $credentials.secretName }}
    valueFrom:
      secretKeyRef:
        name: "{{ tpl $credentials.secretName .root }}"
        key: MINIO_SECRET_KEY
    {{- else }}
    value: {{ $credentials.secretKey | required "minio volume requires credentials provided directly or via secret" | quote }}
    {{- end }}
  volumeMounts:
  - name: "{{ include "openadservice.volumeName" $mountPath }}"
    mountPath: /shared
  command:
  - /bin/bash
  - -c
  args:
  - |
    mc alias set minio http://{{ $minio.host | required "minio volume requires host" }} "$MINIO_ACCESS_KEY" "$MINIO_SECRET_KEY"
    {{- range $minio.targets | required "minio volume requires at least one target" }}
    {{- $sourcePath := .sourcePath | required "minio volume target requires sourcePath" }}
    {{- $targetPath := .targetPath | required "minio volume target requires targetPath" }}
    if [[ "{{ $sourcePath }}" == */ ]]; then
      mc cp --recursive "minio/{{ $sourcePath }}" "/shared/{{ $targetPath }}" || { echo "Failed to download directory"; exit 1; }
    else
      mc cp "minio/{{ $sourcePath }}" "/shared/{{ $targetPath }}" || { echo "Failed to download file"; exit 1; }
    fi
    {{- end }}
{{- end -}}
