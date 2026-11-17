# ZeroMusic163

<img src="assets/zero-music163.png" width="48" height="48" alt="">

English | [简体中文](README.zh-CN.md)

Play NetEase Cloud Music on Cardputer Zero. Search with pinyin, browse playlists and daily recommendations, and control playback from the built-in keyboard.

ZeroMusic163 is an independent third-party client.

<p>
<img src="docs/screenshots/search.png" width="255" alt="Search results">
<img src="docs/screenshots/library.png" width="255" alt="Playlist library">
<img src="docs/screenshots/player.png" width="255" alt="Now playing">
</p>

## Install and sign in

1. Download the ARM64 Debian package from [GitHub Releases](https://github.com/tongfei11/ZeroMusic163/releases) and copy it to your Cardputer Zero.
2. Run `sudo apt install ./zero-music163_*_arm64.deb` in the download directory. Open ZeroMusic163 from APPLaunch.
3. On a phone or computer on the same Wi-Fi network, open the address shown on the Zero. Enter the access code, your NetEase Open Platform App ID, and your private key. After saving the configuration, scan the QR code with the NetEase Cloud Music app. [NetEase Open Platform setup guide](https://developer.music.163.com/st/developer/document?docId=9504d35aa41a47c6ac9830b2dbf48f94)

## Controls

| Key | Action |
| --- | --- |
| Tab | Switch between Search, Library, Now Playing, and Queue |
| F / X | Move up or down |
| Enter | Search, open, or play the selected item |
| A / R | Add a song or playlist to the queue / remove a queue item |
| Z / C | Previous or next queue page |
| 4 / 5 / 6 / 7 / 8 | Play mode / previous track / play or pause / next track / favorite |
| Hold 5 / 7 | Seek backward or forward |
| S / D | Volume down or up |
| H | Playback key help |
| Esc / hold Esc for 3 seconds | Go back / exit |

Enable background playback in Settings to keep playing after leaving the app. To resume the last saved position after reopening, press 6 on the playback tab. Switching accounts keeps your developer configuration. Initialization clears it and returns you to first-time setup.

## Known issue with Bluetooth playback

On the tested Cardputer Zero, Galaxy Buds2 Pro audio sometimes stuttered or distorted while the built-in Wi-Fi was active. The earbuds also occasionally switched from A2DP stereo to the hands-free profile and went silent. Playback was smoother with Wi-Fi temporarily disabled during a test using already buffered audio. For online playback, use the built-in speaker if Bluetooth audio is unstable.

## Uninstall or build

Exit the app and run `python3 /usr/lib/zero-music163/uninstall.py` as the user who runs ZeroMusic163 to remove it and clear that user's local data.

To build from source, run `sh packaging/build-deb.sh` on ARM64 Debian with Node.js 20, npm and Pillow. The package is written to `dist/`.

Run the tests with `PYTHONPATH=src python3 -m unittest discover -s tests`.

ZeroMusic163 uses the [official NetEase Cloud Music CLI](https://www.npmjs.com/package/@music163/ncm-cli). See [LICENSE](LICENSE) for the app's MIT license.
