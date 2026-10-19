# Generate Helmfile from Docker Compose

[`generate_helmfile_from_compose.py`](./scripts/generate_helmfile_from_compose.py) converts a Docker Compose project into an equivalent [Helmfile](https://helmfile.readthedocs.io/en/latest/) setup including per-service values. For CI integration, a reusable GitHub workflow is provided.

## Usage

```
> ./generate_helmfile_from_compose.py -h
usage: generate_helmfile_from_compose.py [-h] [--chart-map CHART_MAP] [--env-file ENV_FILE] [--profile PROFILES] [--active-profiles-only] [--output-dir OUTPUT_DIR] [--dry-run] [-v] compose_file

Convert Compose services to Helmfile.

positional arguments:
  compose_file          top-level Docker Compose file

options:
  -h, --help            show this help message and exit
  --chart-map CHART_MAP
                        explicit service-to-OCI-chart mappings
  --env-file ENV_FILE   Compose environment file (default: .env beside compose_file)
  --profile PROFILES    default Compose profile (repeatable; overridden by COMPOSE_PROFILES)
  --active-profiles-only
                        export only the active Compose profiles instead of every profile
  --output-dir OUTPUT_DIR
                        output directory (default: ./helm-output)
  --dry-run             print output without writing files
  -v, --verbose         print conversion details
```

### Chart Map

A special chart map file can be passed to the generator via `--chart-map CHART_MAP`, providing a way to override the default generation rules for specific services.

| Parameter | Description |
| --- | --- |
| `excluded_profiles` | excludes specific compose profiles, even without `--active-profiles-only` |
| `services.<compose-service-name>.chart/version` | overrides the default chart name and version extracted from the compose service's image |
| `services.<compose-service-name>.values` | adds or overrides Helm values |
| `services.<compose-service-name>.value_replacements` | finds and replaces values in the generated Helm values |
| `services.<compose-service-name>.config_replacements` | finds and replaces values in the TODO |
| `services.<compose-service-name>.param_replacements` | finds and replaces values in the TODO |
| `services.<compose-service-name>.dependency_port_env` | TODO |
| `volume_policies.<compose-service-name>.<mount-target>` | sets the volume policy for a specific mount target |
| | `copy` (default): copies the source file/directory to the Helmfile bundle; mounts as `configMap` (files < 256 KiB) or `hostPath` |
| | `hostPath`: mounts the source file/directory as `hostPath` |
| | `omit`: omits the volume mount |

```yaml
excluded_profiles: []

services: {}
  # <compose-service-name>:
  #   chart: <oci-chart-url>
  #   version: <chart-version>
  #   values:
  #     <key>: <value>
  #   value_replacements:
  #     <key>:
  #       - find: <old-value>
  #         replace: <new-value>
  #   config_replacements:
  #     <config-file>:
  #       - find: <old-value>
  #         replace: <new-value>
  #   param_replacements:
  #     <ros-param-file>:
  #       - find: <old-value>
  #         replace: <new-value>
  #   dependency_port_env: <env-var-name>
  # --- for example ---
  # zenoh-router:
  #   chart: oci://ghcr.io/openads-project/openads-helm/zenoh-router
  #   version: 0.2.10

volume_policies: {}
  # <compose-service-name>:
  #   <mount-target>: hostPath|copy|omit
  # --- for example ---
  # monitoring.rviz-carla:
  #   /docker-ros/ws/monitoring-volume: hostPath
```

## CI Integration

For CI integration, a reusable GitHub workflow ([`reusable-helmfile-oci.yml`](../../.github/workflows/reusable-helmfile-oci.yml)) is provided that runs the generator and publishes the generated Helmfile setup as an workflow artifact and OCI bundle. See [openadsim](https://github.com/openads-project/openadsim/blob/k8s/.github/workflows/helmfile-oci.yml) for an integration example.
