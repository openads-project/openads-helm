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
| `excluded_profiles` | excludes services in these Compose profiles, even if active |
| `services.<compose-service-name>.chart/version` | overrides the chart and version derived from the service's image |
| `services.<compose-service-name>.values` | deep-merges into the generated Helm values |
| `services.<compose-service-name>.value_replacements` | finds and replaces text in generated list values (e.g. `command`, `args`) |
| `services.<compose-service-name>.config_replacements` | finds and replaces text in mounted config files exported as `configMap` |
| `services.<compose-service-name>.param_replacements` | finds and replaces text in the mounted ROS parameter file |
| `services.<compose-service-name>.dependency_port_env` | env variable holding the service's port; dependent services wait on it instead of the lowest exposed port |
| `volume_policies.<compose-service-name>.<mount-target>` | sets the volume policy for a bind mount |
| | `copy` (default): copies the source to the Helmfile bundle; mounts as `configMap` (text files < 256 KiB) or `hostPath` |
| | `bind`: mounts the source from `${PROJECT_PATH}` on the node as `hostPath`; needed for writable or shared mounts |

```yaml
excluded_profiles: []

services: {}
  # <compose-service-name>:
  #   chart: <oci-chart-url>
  #   version: <chart-version>
  #   values:
  #     <key>: <value>
  #   value_replacements:
  #     <value-name>:  # command|args
  #       - find: <old-text>
  #         replace: <new-text>
  #   config_replacements:
  #     <config-file-name>:
  #       - find: <old-text>
  #         replace: <new-text>
  #   param_replacements:
  #     - find: <old-text>
  #       replace: <new-text>
  #   dependency_port_env: <env-var-name>
  # --- for example ---
  # zenoh-router:
  #   chart: oci://ghcr.io/openads-project/openads-helm/zenoh-router
  #   version: 0.2.10
  # carla-server:
  #   dependency_port_env: CARLA_PORT

volume_policies: {}
  # <compose-service-name>:
  #   <mount-target>: copy|bind
  # --- for example ---
  # monitoring.rviz-carla:
  #   /docker-ros/ws/monitoring-volume: bind
```

## CI Integration

For CI integration, a reusable GitHub workflow ([`reusable-helmfile-oci.yml`](../../.github/workflows/reusable-helmfile-oci.yml)) is provided that runs the generator and publishes the generated Helmfile setup as an workflow artifact and OCI bundle. See [openadsim](https://github.com/openads-project/openadsim/blob/main/.github/workflows/helmfile-oci.yml) for an integration example.
