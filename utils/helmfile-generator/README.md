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

CI reads the selected `env-file` for generation and for `helmfile build`/`helmfile template`. The reusable workflow also supports a caller-owned parent Helmfile through `helmfile-entrypoint` and archives `bundle-dir` as-is; both inputs default to the generated output for simple projects. Set `include-env-file-in-bundle: true` to copy the selected dotenv file to `.env` at the bundle root immediately before validation and archiving. This is opt-in because dotenv files can contain secrets.

The action requires `output-dir` to be a directory below the caller workspace. It replaces that directory in the ephemeral checkout before generation. Use the same path when extracting the archive later; generated host paths for copied assets intentionally refer to this caller-relative location.

## Chart map

The generator targets charts built on the `openadservice` base chart: values are written below `openadservice:`, mounted YAML files containing `ros__parameters` become `rosParamFileData`, and the chart renders `ZENOH_CONFIG_OVERRIDE` itself.

Without a map entry, a GHCR image `ghcr.io/<owner>/<repository>:v<X.Y.Z>…` is mapped to the release chart `oci://ghcr.io/<owner>/<repository>/helm/<chart>` at version `<X.Y.Z>`. An image tagged `latest`, `latest-slim` or similar is a default-branch build; it is mapped to the newest `<X.Y.Z>-main` version of that chart, read from the registry's tag list. Services that use another chart or version need an explicit entry. Map keys are full Compose service names, including group prefixes such as `perception.service-name`. Unknown top-level keys are rejected.

```yaml
runtime_env:
  name_prefix: DEPLOYMENT_PREFIX
  node: NODE
  host_path_root: PROJECT_PATH
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

`runtime_env` names the environment variables read when Helmfile runs; each is optional. `name_prefix` prefixes Helm release names and writes `namePrefix` values. `node` writes a node selector. `host_path_root` resolves repository-relative host paths. `dependency_port_env` makes a dependent service wait on a runtime port expression from the named service's Compose environment.

Fixed bind sources are copied by default. Use `volume_policies` with `copy`, `hostPath`, or `omit` for a specific service and mount target. Compose environment substitutions remain runtime Helmfile expressions where needed. Review generated values before deployment, especially host paths and exposed ports.

An OpenADSim caller configuration is maintained in the [OpenADSim repository](https://github.com/openads-project/openadsim).
