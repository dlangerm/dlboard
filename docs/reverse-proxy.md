# Reverse proxy and TLS

## What is this

dlboard serves plain HTTP. To use it beyond your own machine, put a reverse proxy in front that
terminates TLS. Sign-in cookies are marked `Secure` by default and are only sent back over HTTPS, so a
password deployment needs this.

## When you'd need this

Any deployment with `PASSWORD_AUTH` that people reach over a network, and any use of an API token over
an untrusted network: tokens are sent in a header and are only as private as the connection.

## Setup

1. Bind dlboard to loopback or an internal network (`--host 127.0.0.1`, the default) so only the proxy
   reaches it.
2. Tell dlboard how many proxies sit in front, so it trusts their `X-Forwarded-*` headers:
   `DLBOARD_TRUSTED_PROXIES=1`. Without it dlboard sees every request as plain HTTP from the proxy's
   address, and sign-in fails.
3. Allow large request bodies at the proxy: at least `DLBOARD_MAX_UPLOAD_MB` (default 256).
4. Don't buffer or time out too aggressively on long requests; artifact uploads can take a while.

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

`$remote_addr` (not `$proxy_add_x_forwarded_for`) makes nginx *replace* any `X-Forwarded-For` a client
sent, which is what `DLBOARD_TRUSTED_PROXIES=1` assumes.

## Under a path

To serve at `https://example.com/dlboard/` instead of a subdomain, set `DLBOARD_URL_PREFIX=dlboard` and
forward the whole path **without stripping the prefix**:

```nginx
location /dlboard/ {
    proxy_pass http://127.0.0.1:8050;   # no trailing path: the prefix is passed through
    # ...same proxy_set_header lines as above
}
```

## Health checks

`/healthz` (the process is up) and `/readyz` (the database answers) need no sign-in; point a load
balancer or orchestrator at them. Under a prefix they move with it (`/dlboard/healthz`).

## Draining

When the proxy or orchestrator stops a dlboard instance, give it time to finish. See
[Shutting down](configuration.md#shutting-down).
