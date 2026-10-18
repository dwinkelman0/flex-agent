# Gitea Webhook Proxy

Transparent HTTP forwarder that sits in front of the gateway's Gitea webhook
receiver.

## Purpose

Gitea delivers repo webhooks to this proxy. The proxy forwards the raw request
body and all `X-Gitea-*` headers verbatim to the real gateway receiver
(`$FORWARD_TO`), then returns the gateway's response to Gitea. The proxy holds
no secret of its own; HMAC verification remains in the gateway
(`chat/lib/gitea_hooks.py`).

## Environment contract

| Variable | Default | Description |
|---|---|---|
| `LISTEN_HOST` | `0.0.0.0` | Interface to bind |
| `LISTEN_PORT` | `3030` | Port to listen on |
| `FORWARD_TO` | *(required)* | Base URL of the gateway webhook receiver |

`FORWARD_TO` should end with `/` if you want the incoming request path
appended (e.g. `http://gateway:3030/`).

## Example repo webhook URL

If the proxy is reachable at `http://host.docker.internal:3030` and the
gateway receiver is at `http://gateway:3030/`, set
`FORWARD_TO=http://gateway:3030/` and configure the Gitea repo webhook as:

```text
http://host.docker.internal:3030/
```
