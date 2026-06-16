# Security Policy

## Supported versions

Trovato is pre-1.0 and ships frequently. Only the **latest released version**
receives security fixes. Please update before reporting.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Report privately via either:

- **GitHub Security Advisories** — the *Security* tab → *Report a vulnerability*
  (preferred; keeps the report private until a fix ships), or
- **Email** — `varous555@gmail.com` with `SECURITY` in the subject.

Include: affected version, a description, reproduction steps, and the impact you
observed. We aim to acknowledge within a few days.

## Threat model (please read before reporting)

Trovato is a **local-first, single-user desktop application**. By design it
runs on `127.0.0.1` and stores everything locally; it makes no outbound network
calls except to a **local** LM Studio endpoint you configure. The intended
trust boundary is "the machine it runs on."

In scope:

- Authentication/authorization bypass within the app.
- Arbitrary file read/write or path traversal beyond a document's source.
- Injection (SQL/command/template), unsafe deserialization, SSRF.
- License-verification bypass that doesn't require local file access.
- Secret exposure in builds, logs, or the repository.

Out of scope / known trade-offs (documented, not bugs):

- **Plain-HTTP exposure to a network.** Running with `LDI_ALLOW_LAN=1` or behind
  a non-TLS proxy sends credentials and content in cleartext. Put it behind TLS.
  This is an operator choice, not a product vulnerability.
- **Offline license expiry can be bypassed by setting the system clock back** —
  an accepted trade-off for a local-first, honesty-based product.
- Anything requiring an attacker to already have local filesystem/OS access on
  the user's machine (they could read the SQLite DB directly anyway).

## Cryptography / keys

The app embeds only the **public** Ed25519 verification key
(`PUBLIC_KEY_HEX`). The signing **private** key never lives in the repo or a
build; it stays on the vendor's machine. Reporting the public key is not a
vulnerability.
