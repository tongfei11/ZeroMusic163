# ZeroMusic163

<img src="assets/zero-music163.png" width="48" height="48" alt="">

[English](README.md) | 简体中文

Cardputer Zero 上的网易云音乐播放器，由第三方独立开发。

<p>
<img src="docs/screenshots/search.png" width="255" alt="Search results">
<img src="docs/screenshots/library.png" width="255" alt="Playlist library">
<img src="docs/screenshots/player.png" width="255" alt="Now playing">
</p>

## 安装

从 [GitHub Releases](https://github.com/tongfei11/ZeroMusic163/releases) 下载 ARM64 Debian 安装包，复制到 Cardputer Zero，再用 `sudo apt install ./zero-music163_*_arm64.deb` 安装。完成后从 APPLaunch 打开 ZeroMusic163。

首次打开时，用同一 Wi-Fi 下的手机或电脑访问设备屏幕上的地址，输入访问码并粘贴开放平台的 App ID 和 Private Key。保存后用网易云音乐 App 扫描设备上的二维码。[网易云开放平台入驻指南](https://developer.music.163.com/st/developer/document?docId=9504d35aa41a47c6ac9830b2dbf48f94)

## 按键

| 按键 | 操作 |
| --- | --- |
| `Tab` | 搜索 → 歌单 → 播放 → 队列 |
| `F / X` | 列表移动 |
| `Enter` | 搜索、打开或播放选中项 |
| `A` / `R` | 添加选中的歌曲或整张歌单 / 从队列移除 |
| `Z / C` | 队列上一页 / 下一页，每页 30 首 |
| `4 / 5 / 6 / 7 / 8` | 播放页的模式、上一首、播放或暂停、下一首、收藏或取消收藏 |
| 长按 `5 / 7` | 快退 / 快进，按住后每次移动 5 秒，暂停时保持暂停 |
| `S / D` | 播放页降低或提高音量 |
| `H` | 打开或关闭播放页按键帮助 |
| 短按 / 长按 `Esc` 3 秒 | 返回上一级 / 退出界面 |

歌单页提供喜欢的音乐、我的歌单、每日推荐和设置。选中长歌名或歌单名后会横向滚动。搜索框可直接打全拼，例如 `qilixiang` 或 `linjunjie`。播放器进程和账户配置存放在本应用自己的目录。整单入队时会跳过不可播放的歌曲。歌单剩余曲目按页加载。当前歌曲开始播放 12 秒后，以 64 KiB/s 限速准备下一首音频，缓存上限是 8 MiB，提前切歌时会直接流式播放。

应用会记住歌曲和播放位置，重开后按 `6` 继续。打开「设置 → 后台播放」后，长按 `Esc` 退出界面时，正在播放的歌曲和队列会继续播放。

应用启动时初始化一个常驻的官方 CLI 进程，搜索、歌单和播放地址共用该进程。此适配使用锁定的 CLI 0.1.7 命令树，升级官方 CLI 时需要一并验证适配。

网络中断时会先播放已有缓存，缓冲不足时显示等待状态。断流或切歌连接失败会保留进度和队列，按 3、6、12、24、30 秒间隔重试，最多五次。播放页按 `6` 立即重试，`7` 跳到下一首，`Esc` 取消重连。搜索和歌单失败后可再次确认重试，已加载内容会保留。登录失效时会提示重新授权。

其他播放异常也会分别处理。过期播放地址会刷新后重试，接口限流至少等待 30 秒，额度耗尽会提示稍后再试。明确无权限、下架或格式不支持的曲目在自动播放时跳过，手动选择时显示原因。播放器失去响应会尝试恢复，缓存失败会退回流式播放。

设置依次为隐藏不可播放曲目、后台播放、初始化、切换账号。切换账号保留开发者配置并重新扫码。初始化须确认，会清除开发者配置、音乐登录、播放记录、队列和设置，然后打开首次配置网页。

## 蓝牙播放已知问题

实测 Cardputer Zero 连接 Galaxy Buds2 Pro 时，板载 Wi‑Fi 开启后可能出现间歇性卡顿或破音。耳机偶尔也会从 A2DP 立体声切到通话模式，导致无声。用已缓存的音频做对照时，临时关闭 Wi‑Fi 后播放明显更流畅。在线听歌时，若蓝牙输出不稳定，可切回内置扬声器。

## 卸载

退出应用后，以日常使用 ZeroMusic163 的账户运行 `python3 /usr/lib/zero-music163/uninstall.py`。完整卸载会清除该账户的开发者信息、登录和本地数据。

开发者可在 Debian ARM64、Node.js 20 环境运行 `sh packaging/build-deb.sh` 构建，产物写入 `dist/`。

用 `PYTHONPATH=src python3 -m unittest discover -s tests` 运行测试。

设备显示和键盘路径沿用 [Zero SSH](https://github.com/tongfei11/ZeroSSH) 的 Cardputer Zero 适配。搜索、曲库、授权与播放接入 [网易云官方 CLI 0.1.7](https://www.npmjs.com/package/@music163/ncm-cli)。
