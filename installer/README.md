# Windows Installer

Three-step build:

1. **Install build deps**
   ```bash
   pip install -e ".[build]"
   ```

2. **Freeze the app with PyInstaller**
   ```bash
   python installer/build.py
   ```
   Produces `dist/Trovato/Trovato.exe` plus
   side files. You can ship this directory as a *portable* version.

3. **Wrap it with Inno Setup**
   - Install Inno Setup: <https://jrsoftware.org/isinfo.php>
   - Open `installer/trovato.iss` in the Compiler and hit *Build*.
   - The signed installer drops into `installer/Output/`.

### Notes

- App data (DB, vector store, cache, backups, logs) lives in
  `%APPDATA%/Trovato/` — **not** in the program folder, so updates
  and uninstalls don't wipe user data.
- For OCR, the user still needs **Tesseract OCR** on the system PATH (or set
  the path in Settings). Bundling tesseract.exe + tessdata via Inno Setup is
  documented inline in `trovato.iss`.
- LM Studio is **not** bundled — it ships independently from
  <https://lmstudio.ai/>.
