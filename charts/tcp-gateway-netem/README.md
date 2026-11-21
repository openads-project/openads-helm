# tcp-gateway-netem

Generic TCP forwarding with an optional shared egress network-emulation budget. HAProxy forwards byte streams without inspecting payloads or terminating encryption. Each configured listener is exposed through a ClusterIP Service and optionally a host port.

Traffic to the IPv4 addresses of `netem.peerHost` shares one bandwidth limit, delay and loss profile across all ports. Other destinations remain unshaped. Set `netem.enabled: false` to keep only TCP forwarding.

See [values.yaml](values.yaml) for configuration options and forwarding examples.

## Runtime API

The HTTP controller exposes `GET /health`, `GET /v1/netem/state` and `POST /v1/netem/apply`:

```bash
curl --fail --request POST http://gateway:18080/v1/netem/apply \
  --header 'Content-Type: application/json' \
  --data '{"delay":"30ms","jitter":"5ms","loss":"0.5%","rate":"10mbit"}'
```

Updates also accept `peerHost`, `iface`, `distribution`, `limit` and `dryRun`. Filter replacement preserves existing filters if installation fails; rate and queue updates are separate operations without rollback.
