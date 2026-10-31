# Reverse-proxy recipes

Snippets marked **(unverified)** follow each tool's documented syntax but have not been run
against GrooveMap. Check them against the version the user runs, and test the result with
`curl -sSI` and a browser sign-in.

## What to expose

| Service | Container : port | Expose? |
| --- | --- | --- |
| Explore (the app) | `groovemap-explore:8006` | Yes. Its origin is `APP_BASE_URL`. It proxies the browser's `/api/*` calls to the API itself |
| Operations console | `groovemap-dashboard:8003` | Admins only. Prefer LAN or VPN access, or put an access layer in front |
| Grafana | `groovemap-grafana:3000` | Optional. If exposed, set `GRAFANA_URL` so the console's deep links work |
| Catalog API | `groovemap-api:8004` | Not needed by the web app. Expose only for a client that calls it directly |

Never proxy PostgreSQL, Neo4j, RabbitMQ or Valkey.

Give each service its own host name, for example `groovemap.example.com`,
`console.groovemap.example.com` and `grafana.groovemap.example.com`. Then set in `.env`:

```bash
APP_BASE_URL=https://groovemap.example.com
GRAFANA_URL=https://grafana.groovemap.example.com   # only if Grafana is proxied
WEB_BIND_ADDRESS=127.0.0.1                           # raw ports off the LAN
DATA_BIND_ADDRESS=127.0.0.1
```

### The console's requirements

The production console's session cookie is always `Secure`, so the console must be reached over
HTTPS. Over plain `http://` the browser drops the cookie and sign-in silently returns to the
login form. Don't work around that with `SESSION_COOKIE_SECURE=false`; put TLS in front
instead. The TLS endpoint must:

- pass `Cookie` and `Set-Cookie` unchanged;
- pass `X-GrooveMap-Console`, `Sec-Fetch-Site`, `Origin` and `Authorization`;
- keep the browser's `Host` header;
- serve the console at the root of its own host name, with no path prefix;
- forward `/ws` with `Upgrade` (WebSocket);
- not log `/ws` query strings or the `Cookie` and `Set-Cookie` headers.

An access layer in front of the console (Authelia, Authentik, Cloudflare Access, a VPN) must
still pass `Authorization` and `Cookie` through unchanged **(unverified with GrooveMap)**.

To reach the console without a proxy, an SSH tunnel works:
`ssh -L 8003:127.0.0.1:8003 <host>`, then open `http://localhost:8003`. Chromium and Firefox
accept a `Secure` cookie on `http://localhost`; Safari doesn't.

## Traefik (Docker provider)

Use `../assets/override-traefik.yml`. Replace the network (`proxy`), the entrypoint
(`websecure`), the certificate resolver (`letsencrypt`) and the host names with the values the
running Traefik uses ([discovery.md](discovery.md#the-reverse-proxy)). The labels are the same
in Traefik v2 and v3.

- `traefik.docker.network` is required. Without it, Traefik may route to the container's
  `groovemap` address, which it can't reach, and return 502 or 504.
- `loadbalancer.server.port` is required, because Explore and the API publish two ports each.
- `traefik.enable: "true"` matters when Traefik runs with `exposedByDefault=false`, which is
  common.
- Traefik keeps the `Host` header and proxies WebSockets by default.
- If Traefik's access log is on, it records request paths with their query strings. To keep the
  console out of it, Traefik v3.1 and later accept a per-router
  `traefik.http.routers.groovemap-console.observability.accessLogs: "false"` label
  **(unverified)**.
- If the user's Traefik uses the file provider instead of labels, write routers and services to
  its dynamic configuration with the URLs `http://groovemap-explore:8006` (and so on) over a
  shared network, or `http://127.0.0.1:<port>` on the host.

## Caddy

**Caddy in Docker on a shared network:** add GrooveMap's web services to Caddy's network with
`../assets/override-proxy-network.yml`, then add to the Caddyfile:

```caddyfile
groovemap.example.com {
	reverse_proxy groovemap-explore:8006
}

console.groovemap.example.com {
	reverse_proxy groovemap-dashboard:8003
}

grafana.groovemap.example.com {
	reverse_proxy groovemap-grafana:3000
}
```

**Caddy on the host:** use `127.0.0.1:8006`, `127.0.0.1:8003` and `127.0.0.1:3000`, or the
remapped `*_HOST_PORT` values. No override is needed.

Caddy obtains certificates automatically for public names. For LAN-only names, add `tls
internal` inside the site block, or use the user's DNS-challenge setup. `reverse_proxy` keeps
the `Host` header and proxies WebSockets without extra configuration. Caddy writes no access
log unless the site has a `log` directive, and its access log redacts `Cookie` and
`Authorization` by default. If logging is on, skip `/ws` with `log_skip /ws*` (Caddy 2.8 and
later; `skip_log` in older releases) **(unverified)**.

With caddy-docker-proxy (labels instead of a Caddyfile), put labels like these on `explore` in
the override **(unverified)**:

```yaml
labels:
  caddy: groovemap.example.com
  caddy.reverse_proxy: "{{upstreams 8006}}"
```

## nginx (and SWAG)

Use `127.0.0.1:<port>` upstreams for nginx on the host, or container names over a shared
network for nginx in Docker. Here is the console, the strictest of the three. Explore and
Grafana need only the plain `location /` block. Replace the certificate paths with the user's:

```nginx
server {
    listen 443 ssl;
    http2 on;                      # nginx 1.25.1+; older: "listen 443 ssl http2;"
    server_name console.groovemap.example.com;
    ssl_certificate     /etc/letsencrypt/live/console.groovemap.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/console.groovemap.example.com/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8003;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    location /ws {
        proxy_pass http://127.0.0.1:8003;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_set_header Host $host;
        proxy_read_timeout 1h;
        access_log off;            # the default log format records query strings
    }
}
```

nginx passes the other request headers (`Cookie`, `Origin`, `Authorization`,
`X-GrooveMap-Console`) by default. Its default log format records the request line but not
cookies. SWAG is nginx: put the same `location` blocks in a file under `proxy-confs/`, using
SWAG's `include /config/nginx/proxy.conf;` convention for the common headers **(unverified)**.

## Nginx Proxy Manager

NPM runs in Docker. Join GrooveMap's web services to NPM's network with
`../assets/override-proxy-network.yml`. Then add one Proxy Host per service in NPM's web UI:

- **Details:** domain `groovemap.example.com`, scheme `http`, forward hostname
  `groovemap-explore`, port `8006`. For the console, use `groovemap-dashboard` and `8003`, and
  turn on **Websockets Support**.
- **SSL:** request a Let's Encrypt certificate (or pick the user's existing one), and turn on
  **Force SSL** and **HTTP/2 Support**.
- To keep `/ws` out of the access log, add this to the console's **Advanced** tab
  **(unverified)**:

  ```nginx
  location /ws {
      proxy_pass http://groovemap-dashboard:8003;
      proxy_http_version 1.1;
      proxy_set_header Upgrade $http_upgrade;
      proxy_set_header Connection "upgrade";
      proxy_set_header Host $host;
      access_log off;
  }
  ```

NPM keeps the `Host` header by default.

## Cloudflare Tunnel (cloudflared)

cloudflared makes outbound connections only, so no inbound port is opened. Cloudflare
terminates TLS, which satisfies the console's HTTPS requirement.

- **cloudflared in Docker:** join its network with `../assets/override-proxy-network.yml` and
  use `http://groovemap-explore:8006` and the other container URLs.
- **cloudflared on the host:** use `http://127.0.0.1:8006` and the other host ports.

For a dashboard-managed tunnel (run with `--token`), add a **Public Hostname** per service in
Zero Trust under Networks, then Tunnels: the subdomain, the domain, type `HTTP`, and the URL
above. For a locally managed tunnel, add to `config.yml`:

```yaml
ingress:
  - hostname: groovemap.example.com
    service: http://groovemap-explore:8006
  - hostname: console.groovemap.example.com
    service: http://groovemap-dashboard:8003
  - service: http_status:404
```

WebSockets pass through by default. Leave `httpHostHeader` unset so the origin sees the public
host name **(unverified for GrooveMap)**. Strongly suggest a Cloudflare Access policy in front
of the console hostname.

## Tailscale

Tailscale fits when GrooveMap should be reachable only from the user's tailnet. It needs
MagicDNS and HTTPS certificates turned on in the tailnet's admin console. With Tailscale
installed on the host, use the current CLI (1.52 and later) **(unverified for GrooveMap)**:

```bash
tailscale serve --bg http://127.0.0.1:8006                 # https://<machine>.<tailnet>.ts.net/
tailscale serve --bg --https=8443 http://127.0.0.1:8003    # console on its own HTTPS port
tailscale serve status
```

Set `APP_BASE_URL=https://<machine>.<tailnet>.ts.net`. Serving the console on a second port of
the same name keeps it at the root path, but it doesn't give it a host name of its own. If
console sign-in misbehaves, give the console its own machine name with a Tailscale sidecar
container (`TS_HOSTNAME=groovemap-console` with a `TS_SERVE_CONFIG`) **(unverified)**.
`tailscale funnel` publishes a served port to the internet. Don't use it for the console.

Older Tailscale releases use a different `serve` syntax. Check `tailscale serve --help` on the
host.
