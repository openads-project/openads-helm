# Helmfile generator

This tool resolves a Docker Compose project with `docker compose config` and generates a Helmfile plus per-service values. It exports every Compose profile by default and writes deterministic files below `--output-dir`. Project-specific chart references and conversion rules belong in a caller-owned `--chart-map`.

## Local usage

Install the Python requirements and make Docker Compose and Helm available, then run:

```sh
python3 utils/helmfile_generator/scripts/helmfile_generator.py docker-compose.yml \
  --chart-map deployments/chart-map.yaml \
  --output-dir deployments/generated
```

`--profile NAME` is repeatable and sets the default enabled profiles in the output. `--active-profiles-only` exports only services in the active Compose profiles. `--dry-run` prints generated content without writing. `--env-file FILE` selects the environment used for Compose interpolation and Helmfile validation; the default is `.env` beside the Compose file.

The generator produces `helmfile.yaml`, `environments/services.yaml.gotmpl`, and values and copied assets for every exported service. Generated directories should be treated as replaceable artifacts; keep manually maintained releases and overrides in a separate parent Helmfile.

## GitHub Action

The local composite action packages the runtime dependencies and is consumed by `.github/workflows/helmfile-oci.yml` at the same repository revision:

```yaml
- uses: $/utils/helmfile_generator
  with:
    compose-file: docker-compose.yml
    env-file: .env
    chart-map: deployments/chart-map.yaml
    profiles: traffic,planning
    output-dir: deployments/generated
```

CI reads the selected `env-file` for generation and for `helmfile build`/`helmfile template`. The reusable workflow also supports a caller-owned parent Helmfile through `helmfile-entrypoint` and archives `bundle-dir` as-is; both inputs default to the generated output for simple projects. Set `include-env-file-in-bundle: true` to copy the selected dotenv file to `.env` at the bundle root immediately before validation and archiving. This is opt-in because dotenv files can contain secrets.

The action requires `output-dir` to be a directory below the caller workspace. It replaces that directory in the ephemeral checkout before generation. Use the same path when extracting the archive later; generated host paths for copied assets intentionally refer to this caller-relative location.

## Chart map

The generator targets charts built on the `openadservice` base chart: values are written below `openadservice:`, mounted YAML files containing `ros__parameters` become `rosParamFileData`, and the chart renders `ZENOH_CONFIG_OVERRIDE` itself.

Default comparisons include the `openadservice` dependency bundled in the selected chart, with the service chart's values applied over it. Fixed environment values equal to those effective defaults are omitted; Compose environment expressions remain configurable when Helmfile runs.

Without a map entry, a GHCR image `ghcr.io/<owner>/<repository>:v<X.Y.Z>…` is mapped to the release chart `oci://ghcr.io/<owner>/<repository>/helm/<chart>` at version `<X.Y.Z>`. An image tagged `latest`, `latest-slim` or similar is a default-branch build; it is mapped to the newest `<X.Y.Z>-main` version of that chart, read from the registry's tag list. Services that use another chart or version need an explicit entry. Map keys are full Compose service names, including group prefixes such as `perception.service-name`. Unknown top-level keys are rejected.

```yaml
excluded_profiles: [gui]
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

Generated output reads these environment variables when Helmfile runs: `DEPLOYMENT_PREFIX` (optional) prefixes Helm release names and writes `namePrefix` values. `NODE` (optional) writes a `kubernetes.io/hostname` node selector. `PROJECT_PATH` is the absolute path of the Compose directory on the node and resolves host paths below it; Helmfile fails if it is unset and a `hostPath` volume uses it. CI sets `PROJECT_PATH` to the workspace unless the env file defines it. `dependency_port_env` makes a dependent service wait on a runtime port expression from the named service's Compose environment.

Fixed bind sources use the `copy` policy by default. This policy controls whether the source is included in the generated bundle; it does not specify a Kubernetes volume type:

| Policy | Bundle contents | Kubernetes mount |
| --- | --- | --- |
| `copy` (default) | A copy of the source | ROS parameter YAML and UTF-8 text files smaller than 256 KiB without NUL bytes become ConfigMaps; other files and directories use a `hostPath` pointing to the bundled copy. |
| `hostPath` | No copy | A `hostPath` pointing to the original source on the node, regardless of file size. |
| `omit` | No copy | No mount. |

Set `volume_policies` per service and mount target. An explicit `hostPath` policy takes precedence over ROS parameter detection and the text-file size check, so it can also keep a small CSV on the node. The `copy` policy still uses the automatic ConfigMap/hostPath selection above. ConfigMaps are read-only; use `hostPath` for writable or shared sources. Copied directories are separate for each service and do not preserve sharing of the original source.

Detected runtime bind sources, such as supported variable-selected directories, can remain direct host paths without an explicit policy. Compose environment substitutions remain runtime Helmfile expressions where needed. Every `hostPath`, including one pointing to a bundled copy, requires its source to exist on the Kubernetes node; Helm does not transfer those files. Review generated values before deployment, especially host paths and exposed ports.

An OpenADSim caller configuration is maintained in the [OpenADSim repository](https://github.com/openads-project/openadsim).

## Development

Executable Python scripts live in `scripts/`, output templates in `templates/`, and tests in `tests/`. From this directory, run the tests with:

```sh
PYTHONPATH=scripts python -m unittest discover -s tests -v
```
