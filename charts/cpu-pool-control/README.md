# cpu-pool-control

Assign labelled pods to shared CPU pools, with optional frequency caps.
A privileged controller applies the settings on each selected node.

```yaml
active: true
nodeSelector:
  kubernetes.io/hostname: onboard-host
pools:
  ads:
    cpus: "8-11,24-27"
    maxFrequencyMHz: 2000 # optional
  simulation:
    cpus: "0-7,16-23"
```

```bash
helm upgrade --install cpu-pool-control ./charts/cpu-pool-control -f values.yaml
```

Label pods in the release namespace with
`openads-project.github.io/cpu-pool-control: ads` or `simulation`
(using `openadservice.labels` for dependent charts).
See [values.yaml](values.yaml) for settings; checks default to every 5 seconds.
CPU numbers must match the host, and pools must not share physical cores.
Requires cgroup v2, systemd cgroups and CPU Manager policy `none`.

To reset, set `active: false` and wait for `/tmp/status.json` in each controller
to report `active: false` before uninstalling. Reset clears pod masks and returns
managed frequency limits to hardware bounds; previous custom settings are not
saved. After a crash, keep pod labels and pool definitions available for cleanup.
