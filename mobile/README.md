# QuantSphere Mobile App

A native iOS/Android wrapper around the existing QuantSphere web app, built with
[Capacitor](https://capacitorjs.com) in **remote URL mode**: the app is just a
native shell around a WebView pointed at your live, hosted QuantSphere server —
not a bundled/offline copy of the frontend. This means:

- No frontend code changes were needed — the app behaves identically to opening
  QuantSphere in a mobile browser, just with a native icon and no browser chrome.
- It requires network connectivity and your QuantSphere server to be running,
  same as the web app already does for every feature (there's no offline mode
  to preserve).

## 1. Point it at your deployed server

Before building anything, edit `capacitor.config.json` and replace the
placeholder:

```json
"server": {
  "url": "https://REPLACE_WITH_YOUR_DEPLOYED_DOMAIN",
  "cleartext": true
}
```

with your real deployed QuantSphere URL (this needs the server actually
deployed and reachable — see the repo root for deployment docs; Docker
packaging for the server is a separate, tracked piece of work). `cleartext`
only matters if you're pointing at a plain `http://` address for local testing
(e.g. `http://192.168.1.50:8000` to hit your dev machine from a phone on the
same network) — a real `https://` deployment doesn't need it.

After changing the URL, run:

```
npm install
npx cap sync
```

## 2. Building for Android

**Requires:** [Android Studio](https://developer.android.com/studio) with the
Android SDK installed. **Not currently installed on this dev machine** — either
install it here, or open this project on a machine that has it.

```
npx cap open android
```

This opens the `android/` project in Android Studio, where you can run it on
an emulator or a connected device, or build a signed release bundle for the
Play Store via Android Studio's Build menu.

## 3. Building for iOS

**Requires a Mac with Xcode installed.** This is not something that can be
done from Windows at all — the `ios/` project can be generated on any OS (it
already has been, in this folder), but only opened, built, and signed on a
Mac.

```
npx cap open ios
```

(run on the Mac, after copying this whole `mobile/` folder over, or via a
shared repo checkout)

## 4. App store submission

Not something that can be automated here — you'll need:

- An [Apple Developer Program](https://developer.apple.com/programs/)
  membership (for iOS/App Store Connect) and a
  [Google Play Console](https://play.google.com/console) account (for Android).
- Your own signing certificates/keys for each platform.
- Store listing content: description, screenshots, privacy policy, etc.
- To go through each store's review process.

## Not included yet

- Custom app icon / splash screen — currently Capacitor's default placeholders.
  Once you have a real logo, regenerate them with
  [`@capacitor/assets`](https://github.com/ionic-team/capacitor-assets).
- Push notifications, native camera/biometrics integration, or any other
  native-only capability — none of QuantSphere's current features need these,
  but Capacitor supports adding plugins for them later if that changes.
