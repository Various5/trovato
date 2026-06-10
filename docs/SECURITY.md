# Security model & disclosure

## Threat model

Trovato is a **local single-tenant** desktop application. The
threat model focuses on:

- **Compromised LM Studio endpoint** — by default the app only talks to
  `127.0.0.1`. If the user points it at a LAN/remote address, that endpoint
  is fully trusted (it can see all queries + retrieved snippets).
- **Other local users on the same machine** — they can read the data
  directory unless the OS-level ACLs restrict it. Sensitive backups should
  be encrypted (Fernet + password-derived key).
- **Stolen/lost laptop** — full-disk encryption is the user's
  responsibility. The app does not encrypt at rest; it does encrypt
  provider credentials in `app_settings` using a key derived from the
  session secret.
- **Brute-force login** — mitigated by the per-IP/per-username rate
  limiter (5 attempts / 5 min → 15 min lockout).

## What we DON'T defend against

- Code injection through a malicious PDF that exploits a parser bug.
  Mitigation: keep PyMuPDF/pdfplumber/Pillow up to date. CI runs Dependabot.
- Side-channel timing attacks on Argon2 verify (irrelevant for local use).
- Memory-resident secrets — anything in process memory is dump-readable by
  another process with the same UID.

## Cryptography

| Use case                    | Primitive                                     |
|-----------------------------|-----------------------------------------------|
| Password hashing            | Argon2id (argon2-cffi defaults)               |
| Recovery key                | Argon2id over a fresh 24-byte token           |
| Session cookies             | itsdangerous HMAC, key in settings.json       |
| Encrypted backups           | Fernet (AES-128-CBC + HMAC) + PBKDF2-HMAC-SHA256 (200k iters) |
| Provider credentials        | Fernet, key derived from session secret_key   |

## Reporting a vulnerability

Mail **varous555@gmail.com** with subject "Trovato security".
We aim to acknowledge within 7 days. Please give us 30 days to ship a fix
before public disclosure where possible.
