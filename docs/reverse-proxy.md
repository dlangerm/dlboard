# Reverse proxy and TLS

## What is this

dlboard serves plain HTTP unless it is given a certificate.
To use it beyond your own machine, either put a reverse proxy in front that terminates TLS, or, for a small deployment, let dlboard serve HTTPS itself ([Without a proxy](#without-a-proxy-native-tls)).
Sign-in cookies are marked `Secure` by default and are only sent back over HTTPS, so a password deployment needs one or the other.

## When you'd need this

Any deployment with `PASSWORD_AUTH` that people reach over a network, and any use of an API token over an untrusted network: tokens are sent in a header and are only as private as the connection.

## Setup

1. Bind dlboard to loopback or an internal network (`--host 127.0.0.1`, the default) so only the proxy reaches it.
2. Tell dlboard how many proxies sit in front, so it trusts their `X-Forwarded-*` headers: `DLBOARD_TRUSTED_PROXIES=1`.
   Without it dlboard sees every request as plain HTTP from the proxy's address, and sign-in fails.
3. Allow large request bodies at the proxy: at least `DLBOARD_MAX_UPLOAD_MB` (default 256).
4. Don't buffer or time out too aggressively on long requests; artifact uploads can take a while.
5. dlboard compresses its responses itself (`DLBOARD_COMPRESS_RESPONSES`).
   If the proxy compresses too, set `DLBOARD_COMPRESS_RESPONSES=false`, so that they aren't compressed twice.

## Caddy

Caddy gets and renews a certificate by itself.

```caddyfile
dlboard.example.com {
    request_body {
        max_size 256MB
    }
    reverse_proxy 127.0.0.1:8050
}
```

## nginx

```nginx
server {
    listen 443 ssl;
    http2 on;
    server_name dlboard.example.com;
    ssl_certificate     /etc/ssl/dlboard.example.com/fullchain.pem;
    ssl_certificate_key /etc/ssl/dlboard.example.com/privkey.pem;

    client_max_body_size 256m;

    location / {
        proxy_pass http://127.0.0.1:8050;
        proxy_set_header Host              $host;
        proxy_set_header X-Forwarded-For   $remote_addr;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
        proxy_request_buffering off;
    }
}

server {
    listen 80;
    server_name dlboard.example.com;
    return 301 https://$host$request_uri;
}
```

`$remote_addr` (not `$proxy_add_x_forwarded_for`) makes nginx *replace* any `X-Forwarded-For` a client sent, which is what `DLBOARD_TRUSTED_PROXIES=1` assumes.

## Under a path

To serve at `https://example.com/dlboard/` instead of a subdomain, set `DLBOARD_URL_PREFIX=dlboard` and forward the whole path **without stripping the prefix**:

```nginx
location /dlboard/ {
    proxy_pass http://127.0.0.1:8050;   # no trailing path: the prefix is passed through
    # ...same proxy_set_header lines as above
}
```

## Without a proxy (native TLS)

A home lab or tailnet install that already has a certificate and key (from `tailscale cert`, Let's Encrypt or a private CA) can have dlboard serve HTTPS itself:

```bash
export DLBOARD_SSL_CERTIFICATE=/etc/dlboard/host.ts.net.crt   # the certificate, with its chain, as PEM
export DLBOARD_SSL_KEYFILE=/etc/dlboard/host.ts.net.key       # its key, as PEM, unencrypted
dlboard serve custom --plugins mydeployment:PLUGINS --host 0.0.0.0
```

- The port then answers HTTPS only (HTTP/2 or HTTP/1.1, as the client offers); plain HTTP to it is refused.
  Leave `DLBOARD_TRUSTED_PROXIES` at `0`: requests really are HTTPS, so sign-in works as it is.
- Set both or neither: half a pair, a key that isn't the certificate's, or an encrypted key fails at startup.
  The key must be unencrypted (as `tailscale cert` and certbot write it), so keep it readable by dlboard's user alone.
- **Renewal:** the workers read the files as they start.
  After replacing them, send the server `SIGHUP` (`kill -HUP <pid>`, `docker kill -s HUP <container>`): it restarts its workers, which serve the new certificate.
  `tailscale cert` certificates last about 90 days, so renew and signal from a timer.
- A training script trusts a self-signed or private-CA certificate through `DLBOARD_CA_BUNDLE` (see [client.md](client.md), "A server with its own certificate").
- `serve local --logdir` and `--tensorboard-logdir` don't work with native TLS: run `dlboard sync --watch` or `dlboard import tensorboard --watch` against the `https://` URL instead.

Anything public still belongs behind a proxy or a gateway in front of dlboard: that is also where rate limiting and blocking abusive clients live, which dlboard leaves to them.

## Health checks

`/healthz` (the process is up) and `/readyz` (the database answers) need no sign-in; point a load balancer or orchestrator at them.
`/healthz/custodian` answers 503 when no custodian process has checked in lately or a background task has gone three of its intervals without succeeding: alert on it, rather than take the server out of rotation, since the server still serves requests without one.
Under a prefix they move with it (`/dlboard/healthz`).

## Draining

When the proxy or orchestrator stops a dlboard instance, give it time to finish.
See [Shutting down](configuration.md#shutting-down).
