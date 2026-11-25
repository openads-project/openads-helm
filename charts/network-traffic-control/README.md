# network-traffic-control

Run one release on each receiving WAN node. A host-network controller redirects
the configured IPv4/TCP flows to a dedicated IFB using Linux traffic control.
All selected flows share one rate, delay, jitter and loss profile. TCP still
runs between the original endpoints.

Set `peerAddress` and `flows` in [values.yaml](values.yaml). On a multi-node
cluster, set `nodeSelector` to pin the controller to the receiving WAN node.
Run one controller per node. At startup it uses the route to `peerAddress`
to discover the interface and local IPv4 address. The return route must use
the same interface and address as the incoming traffic. The controller uses
`ifb-wan` and reserves ingress filter priorities 49000 through 49015 on that
interface. The host must provide IFB,
clsact, flower, mirred, HTB and netem support. Verify ingress classifier
ordering and packet aggregation before use.
The controller preserves the host interface's root queue and leaves a shared
`clsact` qdisc available for other users after cleanup.

The controller loads the initial profile from `netem` and starts in bypass.
Send `{"bypass":false}` to enable that profile, or include any overrides in the
same request. Later requests retain the current values for omitted fields.
The controller and its ClusterIP Service use `http.port` (default `18080`).
Set `http.nodePort` for cross-cluster access. The NodePort targets an
unprivileged relay on the pod network, which forwards only the control API to
the controller through a separate ClusterIP Service. Any client that can
reach the HTTP API can update the profile.

```sh
curl -fsS http://network-traffic-control:18080/health
curl -fsS http://network-traffic-control:18080/v1/netem/state
curl -fsS -H 'Content-Type: application/json' \
  --data '{"bypass":false,"delay":"25ms","jitter":"5ms","loss":"1%","rate":"10mbit"}' \
  http://network-traffic-control:18080/v1/netem/apply
curl -fsS -H 'Content-Type: application/json' --data '{"bypass":true}' \
  http://network-traffic-control:18080/v1/netem/apply
```

`dryRun: true` validates an update without applying it. Bypass stops redirecting
packets and discards any queued packets. Send the active profile periodically so
a restarted controller resumes it. Inspect delivered and dropped traffic using
`tc -s class show dev ifb-wan` and `tc -s qdisc show dev ifb-wan`.
These counters cover the receiving emulator.

For manual cleanup after abrupt controller termination, stop the controller and
run `python3 controller.py config.json --cleanup` in the same host network
namespace with `NET_ADMIN`, using the controller source and configuration from
the release ConfigMap. The cleanup checks the rule cookies and IFB ownership.
