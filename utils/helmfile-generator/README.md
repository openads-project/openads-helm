# Helmfile generator

This tool resolves a Docker Compose project with `docker compose config` and generates a Helmfile plus per-service values. It exports every Compose profile by default and writes deterministic files below `--output-dir`. Project-specific chart references and conversion rules belong in a caller-owned `--chart-map`.

## Local usage

Install the Python requirements and make Docker Compose and Helm available, then run:

```sh
python3 utils/helmfile-generator/helmfile_generator.py docker-compose.yml \
  --chart-map deployments/chart-map.yaml \
  --output-dir deployments/generated
```

`--profile NAME` is repeatable and sets the default enabled profiles in the output. `--active-profiles-only` exports only services in the active Compose profiles. `--dry-run` prints generated content without writing. `--env-file FILE` selects the environment used for Compose interpolation and Helmfile validation; the default is `.env` beside the Compose file.

The generator produces `helmfile.yaml`, `environments/services.yaml.gotmpl`, and values and copied assets for every exported service. Generated directories should be treated as replaceable artifacts; keep manually maintained releases and overrides in a separate parent Helmfile.

## GitHub Action

The local composite action packages the runtime dependencies and is consumed by `.github/workflows/helmfile-oci.yml` at the same repository revision:

```yaml
- uses: $/utils/helmfile-generator
  with:
    compose-file: docker-compose.yml
    env-file: .env
    chart-map: deployments/chart-map.yaml
    profiles: traffic,planning
    output-dir: deployments/generated
```

CI reads the selected `env-file` for generation and for `helmfile build`/`helmfile template`. The reusable workflow also supports a caller-owned parent Helmfile through `helmfile-entrypoint` and archives `bundle-dir` as-is; both inputs default to the generated output for simple projects.

The action requires `output-dir` to be a directory below the caller workspace. It replaces that directory in the ephemeral checkout before generation. Use the same path when extracting the archive later; generated host paths for copied assets intentionally refer to this caller-relative location.

## Chart map

Without a map, GHCR images are mapped to the matching `oci://ghcr.io/<owner>/<repository>/helm/<chart>` reference using the image version. Services that use another chart or version need an explicit entry. Map keys are full Compose service names, including group prefixes such as `perception.service-name`.

```yaml
values_root: service
chart_version_suffix: -main
parameter_file:
  yaml_marker: application_parameters
  value_key: parameterFileData
  mount_path_value_key: parameterFileMountPath
  output_filename: parameters.yaml
deployment:
  name_prefix_env: DEPLOYMENT_PREFIX
  node_env: NODE
  host_path_root_env: PROJECT_PATH
excluded_profiles: [gui]
ignored_environment: [CUSTOM_CONFIG_OVERRIDE]
ignored_volume_names: [generated-config.json]
volume_policies:
  recorder:
    /records: hostPath
services:
  api:
    chart: oci://registry.example/team/api
    version: 1.0.0
    dependency_port_env: API_PORT
    value_replacements:
      args:
        - find: old-host
          replace: new-host
    config_replacements:
      application.yaml:
        - find: old-endpoint
          replace: new-endpoint
```

`deployment` settings are optional. `name_prefix_env` prefixes Helm release names and writes `namePrefix` values. `node_env` writes a node selector. `host_path_root_env` resolves repository-relative host paths when Helmfile runs. `dependency_port_env` makes a dependent service wait on a runtime port expression from the named service's Compose environment.

`values_root` selects a nested chart values mapping. Leave it unset for charts that take values at the YAML root. `parameter_file` configures detection and embedding of mounted YAML parameter files.

Fixed bind sources are copied by default. Use `volume_policies` with `copy`, `hostPath`, or `omit` for a specific service and mount target. Compose environment substitutions remain runtime Helmfile expressions where needed. Review generated values before deployment, especially host paths and exposed ports.

An OpenADSim caller configuration is maintained in the [OpenADSim repository](https://github.com/openads-project/openadsim).
