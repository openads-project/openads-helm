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
| `services.<compose-service-name>.values` | deep-merges into the release's Helm values |
| `services.<compose-service-name>.port_policies.<compose-source-port>` | sets the port policy for a Compose published port |
| | `hostPort` (default): maps a Compose published host port to a `hostPort` |
| | `nodePort`: maps a Compose published host port to a `nodePort` |
| `services.<compose-service-name>.volume_policies.<mount-target>` | sets the volume policy for a bind mount |
| | `copy` (default): copies the source to the Helmfile bundle; mounts as `configMap` (text files < 256 KiB) or `hostPath` |
| | `bind`: mounts the source from `${PROJECT_PATH}` on the node as `hostPath`; needed for writable or shared mounts |
| `services.<compose-service-name>.ros_params` | overrides single ROS parameters of the mounted parameter file; appended as a final `/**` block, which ROS 2 applies last; values may use Compose interpolation such as `${VAR:-default}` |

```yaml
excluded_profiles: []

services: {}
  # <compose-service-name>:
  #   chart: <oci-chart-url>
  #   version: <chart-version>
  #   values:
  #     <chart-key>: <value>
  #     openadservice:
  #       <openadservice-key>: <value>
  #   port_policies:
  #     <compose-source-port>: hostPort|nodePort
  #   volume_policies:
  #     <mount-target>: copy|bind
  #   ros_params:
  #     <ros-parameter-name>: <value>
  # --- for example ---
  # zenoh-router:
  #   chart: oci://ghcr.io/openads-project/openads-helm/zenoh-router
  #   version: 0.2.10
  #   values:
  #     allowedRosTopics: [/tf]
  #   port_policies:
  #     7447: nodePort
  # talker:
  #   chart: oci://ghcr.io/openads-project/openads-helm/openadservice
  #   version: 1.2.0
  #   values:
  #     gpu: true
  # perception.point-cloud-object-detection:
  #   ros_params:
  #     prediction.server_url: ${TRITON_HOST:-triton-server}:8001
  # monitoring.rviz-carla:
  #   volume_policies:
  #     /docker-ros/ws/monitoring-volume: bind
```

## CI Integration

For CI integration, a reusable GitHub workflow ([`reusable-helmfile-oci.yml`](../../.github/workflows/reusable-helmfile-oci.yml)) and an equivalent GitLab template ([`reusable-helmfile-oci.yml`](../../.gitlab-ci/reusable-helmfile-oci.yml)) are provided that run the generator and publish the generated Helmfile setup as OCI bundle `<registry>/<repository>/helmfile`. Every run publishes, tagged with the slug of the branch, pull request source branch, or tag and with `sha-<commit>`. See [openadsim](https://github.com/openads-project/openadsim/blob/main/.github/workflows/helmfile-oci.yml) for an integration example.

Call [`reusable-helmfile-cleanup.yml`](../../.github/workflows/reusable-helmfile-cleanup.yml) from a scheduled workflow to delete bundles older than one month, except `main` and `vX.Y.Z`. On GitLab, configure the same rule as container registry cleanup policy of the project: keep `^(main|v\d+\.\d+\.\d+)$`, remove `.*`, older than 30 days.
