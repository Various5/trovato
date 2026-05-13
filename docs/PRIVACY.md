# Privacy

LocalDoc Intelligence runs **entirely on your machine**:

- Documents stay on local disks. No file content is uploaded.
- The chat LLM, vision model, and embeddings are served by **LM Studio**
  running on `localhost`. We never call hosted OpenAI/Anthropic/Google.
- The only outbound network traffic the app makes is:
  - HTTP requests to your configured LM Studio URL (default `127.0.0.1`)
  - Reads from cloud-sync folders (OneDrive/Dropbox/Drive) **only because
    the OS already syncs them locally** — the app reads them like any other
    folder.

The app does not collect telemetry. ChromaDB telemetry is explicitly
disabled (`anonymized_telemetry=False`).

Passwords are hashed with **Argon2id**. Backups are optionally encrypted with
Fernet (AES-128-CBC + HMAC) using PBKDF2-HMAC-SHA256 with 200,000 iterations.

If you point a source at a folder synced to a cloud provider, **that provider
will see those files** — that's outside the app's control. To stay 100 %
offline, only configure local or USB sources.
