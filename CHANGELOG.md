# Changelog

## 1.5.0 — extension 1.0.3
- **English interface.** The app is now available in English and Turkish. It follows the system language by default; switch it in Settings ▸ General ▸ Language (the app restarts and downloads resume).
- Number, size and time formats follow the language (`37% · 23.0 MB · 20 s` / `%37 · 23,0 MB · 20 sn`). GTK's own dialogs follow it too.
- The Firefox extension follows Firefox's language (English or Turkish). Re-sign it: new version 1.0.3.
- Install, uninstall and signing scripts print messages in English or Turkish depending on the terminal language.
- `scripts/i18n.py` to find untranslated strings and add new ones to `ddm/locale/*.po`.

## 1.4.0 — extension 1.0.2
- **Multiple adapters:** spread connections of large files across several internet links (`SO_BINDTODEVICE`), with automatic failover and stall detection.
- **Firefox:** downloads are captured as soon as response headers arrive (`webRequest`), so small files are no longer missed. Needs the new `webRequestBlocking` permission; re-sign the extension.
- More reliable resume offer when a link is opened again from the browser.

## 1.3.x
- Notifications are withdrawn from the GNOME list (also for removed downloads), fixing the stuck dock badge.
- Videos can be saved into their own folder (`Title/Title.mp4`).
- Modernized top bar icon: progress ring, percentage and speed label, progress bars in the menu.

## 1.3.0
- Expired link detection and refresh with same-file verification.
- Scheduler, metered connection handling, prevent sleep, suspend/power off when finished.
- Rules (folder by extension/site/name), archive extraction, automatic checksum verification.

## 1.2.0
- Summary and details views in the detail window, finish window with statistics.

## 1.1.0
- VirusTotal checks for URLs, domains, hash headers and finished files; upload and scan.

## 1.0.0
- Segmented download engine, GTK4/libadwaita UI, yt-dlp video downloads, top bar icon, Firefox extension.
