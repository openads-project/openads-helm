# zenoh-router-netem

Zenoh Router for ROS 2 RMW Zenoh with pod-level network emulation for reproducible impaired-network testing.

The chart depends on [`zenoh-router`](../zenoh-router) and preserves its behavior and configuration under the `zenoh-router` value. It adds an init container that applies the startup profile and an optional HTTP sidecar for runtime inspection and updates. Both containers receive only the `NET_ADMIN` capability required to manage traffic control in the shared pod network namespace.

Network emulation and its HTTP controller are enabled by default. The default profile applies `0ms` delay, `0ms` jitter, `0%` packet loss, and a `1000mbit` rate limit to matching remote traffic. Set the impairment values explicitly or update them through the runtime API to shape traffic.

## Configuration

Configure the underlying router exactly as with the regular chart, nested below `zenoh-router`:

```yaml
zenoh-router:
  endpoints:
  - tcp/offboard.example.org:30447
  openadservice:
    ports:
    - targetPort: 7447
      nodePort: 30447
      protocol: TCP

netem:
  peerHost: offboard.example.org
  delay: 30ms
  jitter: 5ms
  loss: 0.5%
  rate: 50mbit
  limit: 1000
```

`netem.peerHost` and/or `netem.peerPort` must select the remote traffic to shape. With `autoPeer: true`, the controller prefers the address of an established Zenoh connection and falls back to the IPv4 addresses resolved from `peerHost`. Unmatched traffic uses HTB's direct queue, without an emulated rate limit, delay, or loss. Its throughput is subject to the underlying network and host capacity.

Applying a profile with the updated controller also migrates an existing HTB setup: it removes the legacy `1:10` pass-through class capped at `1000mbit`, so unmatched packets fall back to the direct queue without resetting the WAN queue. Fresh setups use default class `0` for the direct queue. Publish and deploy the updated chart on both shaped routers before relying on this behavior in a running simulation.

The external NodePort service uses `externalTrafficPolicy: Local` by default so the remote source address is retained for symmetric shaping. Consequently, traffic must reach a node on which the router pod is running.

## Runtime API

When `netem.http.enabled` is true, port `18080` is exposed on the router's ClusterIP service:

```bash
kubectl port-forward deploy/zenoh-router 18080:18080
curl --fail http://127.0.0.1:18080/health
curl --fail http://127.0.0.1:18080/v1/netem/state
curl --fail --request POST http://127.0.0.1:18080/v1/netem/apply \
  --header 'Content-Type: application/json' \
  --data '{"delay":"80ms","jitter":"10ms","loss":"0.5%","rate":"50mbit","distribution":"normal","limit":1000}'
```

The update endpoint also accepts `iface`, `autoPeer`, `peerHost`, and `peerPort`. Add `"dryRun": true` to validate an update without applying it.

Inspect the kernel traffic-control counters with:

```bash
kubectl exec deploy/zenoh-router --container netem-http -- tc -s qdisc show dev eth0
```
