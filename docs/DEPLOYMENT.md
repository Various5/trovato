# Deployment, Code-Signing & Auto-Update

This document covers the production-grade build pipeline for LocalDoc
Intelligence on Windows.

---

## 1. Building

```powershell
pip install -e ".[build]"
python installer/build.py
# Produces dist/LocalDocIntelligence/LocalDocIntelligence.exe
```

For a fully signed installer, follow the steps below.

---

## 2. Code-signing (Authenticode)

You need:

- A **code-signing certificate** from a CA recognised by Windows
  (DigiCert, Sectigo, SSL.com, etc.). For organisations, an **EV certificate**
  is strongly recommended — it removes the SmartScreen warning immediately
  instead of requiring reputation to be built up.
- Either a **PFX file** + password (OV/IV certs) or a **hardware token**
  (EV certs — usually SafeNet/eToken/YubiKey FIPS).
- The Windows SDK's `signtool.exe` (ships with Visual Studio Build Tools).

### Sign the binary

```powershell
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 ^
  /f "C:\path\to\code-sign.pfx" /p "<password>" ^
  "dist\LocalDocIntelligence\LocalDocIntelligence.exe"
```

### Sign the installer

After Inno Setup builds the .exe installer:

```powershell
signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 ^
  /f "C:\path\to\code-sign.pfx" /p "<password>" ^
  "installer\Output\LocalDocIntelligenceSetup-0.1.0.exe"
```

### Inno Setup auto-sign

Add to `localdoc.iss`:

```ini
[Setup]
SignTool=signtool sign /tr http://timestamp.digicert.com /td sha256 /fd sha256 /f "$qC:\path\to\code-sign.pfx$q" /p "<password>" $f
SignedUninstaller=yes
```

Then build via the Compiler — both your installer **and** the uninstaller are
signed in one go.

### Verifying

```powershell
signtool verify /pa /v "installer\Output\LocalDocIntelligenceSetup-0.1.0.exe"
```

---

## 3. Auto-update channel

The app does **not** silently update; instead it checks an endpoint and
surfaces a "new version available" notice. The user clicks through to
download the signed installer themselves.

### Configure the check endpoint

Add to `%APPDATA%/LocalDocIntelligence/settings.json`:

```json
{
  "update_check_url": "https://api.github.com/repos/varous555/localdoc-intelligence/releases/latest"
}
```

Any URL returning a JSON body of the form

```json
{
  "version": "0.2.0",
  "url":     "https://.../LocalDocIntelligenceSetup-0.2.0.exe",
  "notes":   "Release notes here…"
}
```

works. The GitHub Releases API uses `tag_name`/`body`/`html_url` and is also
detected automatically.

### Programmatic check

```bash
curl http://127.0.0.1:8765/api/about/check-update
```

Response:

```json
{
  "current": "0.1.0",
  "latest":  "0.2.0",
  "url":     "https://.../setup.exe",
  "notes":   "…",
  "up_to_date": false
}
```

The UI can poll this on a timer and show a banner with a download link.

---

## 4. Release workflow

A minimal release flow:

1. Bump `version` in `pyproject.toml` and `app/__init__.py`.
2. Tag the commit: `git tag v0.2.0 && git push --tags`.
3. CI builds the unsigned installer (see `installer/build.py`).
4. Sign on a release machine that has the cert/token mounted.
5. Upload the signed installer to a GitHub Release (or your hosting).
6. The release JSON is consumed by the in-app update check.

If you want true unattended auto-update, add a separate **launcher** binary
that:

1. Checks the endpoint.
2. Downloads the new installer.
3. Verifies the Authenticode signature via WinVerifyTrust.
4. Runs the installer in silent mode (`/SILENT /SUPPRESSMSGBOXES`).

This is intentionally **not** the default — see SECURITY.md for the threat
model.
