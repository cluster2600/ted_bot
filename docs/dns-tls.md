# Free FQDN + free TLS certificate

> **Do you need this?** `ted_bot` is outbound-only — it polls TED and pushes to
> Telegram, with no inbound listener (the OCI security list allows SSH only). You
> need a domain and cert **only if you add a service** to the box: a status page, a
> `/healthz` endpoint, or a Telegram *webhook* (instead of the current polling). If
> you're not serving anything, skip this file.

Two free pieces:

| Piece | Free option | Why |
|-------|-------------|-----|
| **FQDN** | [DuckDNS](https://www.duckdns.org) — `yourname.duckdns.org` | No cost, no domain purchase, and a token-based API that Let's Encrypt can use for DNS-01. |
| **Certificate** | [Let's Encrypt](https://letsencrypt.org) (90-day, auto-renewing) | Free, trusted, automatable. |

Alternatives: **sslip.io / nip.io** give you a zero-registration hostname that
encodes an IP (`10-0-1-5.sslip.io`) — handy, but you can't easily get a cert for
them, so use DuckDNS when you need TLS. If you already own a domain, **Cloudflare**
(free tier) gives DNS + an Origin certificate.

---

## 1. Get the FQDN (DuckDNS)

1. Sign in at <https://www.duckdns.org> (GitHub/Google) and create a subdomain,
   e.g. `patrimonium-ted`. Copy your **token** from the top of the page.
2. Point it at the instance's public IP (Terraform prints `public_ip`). Blank `ip`
   lets DuckDNS auto-detect the caller's address:

   ```bash
   curl "https://www.duckdns.org/update?domains=patrimonium-ted&token=$DUCKDNS_TOKEN&ip="
   ```
3. **If the OCI public IP is ephemeral**, keep the record current with a 5-min cron
   (or reserve a Public IP in OCI — free within the Always-Free limits):

   ```cron
   */5 * * * * curl -fsS "https://www.duckdns.org/update?domains=patrimonium-ted&token=YOUR_TOKEN&ip=" >/dev/null 2>&1
   ```

Your FQDN is now `patrimonium-ted.duckdns.org`.

---

## 2. Get the certificate

Pick the path that matches whether you want to open inbound ports.

### Path A — DNS-01, no inbound ports (recommended; keeps the box locked down)

Let's Encrypt proves the domain via a DNS TXT record set through the DuckDNS API,
so **nothing has to listen on 80/443** and the security list stays SSH-only.

```bash
# Oracle Linux 9
sudo dnf install -y socat
curl https://get.acme.sh | sh -s email=admin@patrimonium.ch
export DuckDNS_Token="YOUR_DUCKDNS_TOKEN"

~/.acme.sh/acme.sh --issue --dns dns_duckdns -d patrimonium-ted.duckdns.org

# Install the files where your service reads them, and reload it on renewal:
sudo mkdir -p /etc/ssl/ted
~/.acme.sh/acme.sh --install-cert -d patrimonium-ted.duckdns.org \
  --key-file       /etc/ssl/ted/key.pem \
  --fullchain-file /etc/ssl/ted/fullchain.pem \
  --reloadcmd      "sudo systemctl reload caddy || true"
```

`acme.sh` installs its **own cron entry** and auto-renews (~60 days) — no extra
setup. DuckDNS allows only one TXT record, so issue **one hostname at a time** (no
wildcards).

### Path B — HTTP-01 via Caddy (laziest if you're already serving HTTP)

[Caddy](https://caddyserver.com) provisions **and renews** a Let's Encrypt cert
automatically with zero cert commands — you just name the domain. It needs inbound
80 + 443, so you must open them (see §3).

```bash
sudo dnf install -y 'dnf-command(copr)'
sudo dnf copr enable -y @caddy/caddy && sudo dnf install -y caddy
```

`/etc/caddy/Caddyfile`:

```caddy
patrimonium-ted.duckdns.org {
    respond /healthz "ok" 200
    # reverse_proxy localhost:8080   # ...or proxy your own app
}
```

```bash
sudo systemctl enable --now caddy
```

Caddy fetches the cert on first request and renews it forever. To pin
DNS-01 in Caddy instead (no open ports), build Caddy with the DuckDNS DNS plugin —
Path A is simpler for that case.

---

## 3. OCI security list — only if you serve inbound (Path B)

DNS-01 (Path A) needs **nothing** here. For Path B, add HTTPS (and HTTP for the
ACME challenge / redirect) ingress. In `terraform/main.tf`, inside
`oci_core_security_list.sl`, add:

```hcl
ingress_security_rules {
  protocol = "6" # TCP
  source   = "0.0.0.0/0"
  tcp_options { min = 80,  max = 80 }
}
ingress_security_rules {
  protocol = "6"
  source   = "0.0.0.0/0"
  tcp_options { min = 443, max = 443 }
}
```

Then also open the host firewall on the box:

```bash
sudo firewall-cmd --permanent --add-service=http --add-service=https
sudo firewall-cmd --reload
```

`terraform apply` to push the security-list change.

---

## Renewal & verification

- **Renewal is automatic** — `acme.sh` via its cron, Caddy internally. Nothing to do.
- Check expiry any time:

  ```bash
  echo | openssl s_client -servername patrimonium-ted.duckdns.org \
    -connect patrimonium-ted.duckdns.org:443 2>/dev/null \
    | openssl x509 -noout -dates
  ```
- Force a test renewal (Path A): `~/.acme.sh/acme.sh --renew -d patrimonium-ted.duckdns.org --force`.

> Keep `DUCKDNS_TOKEN` in `.env` (already gitignored), not in the repo. It grants
> control of your DuckDNS records — treat it like a password.
