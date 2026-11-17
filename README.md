# ZeroMusic163

Cardputer Zero 上的网易云音乐播放器。构建号见 [VERSION](VERSION)。

## 安装

在 Cardputer Zero 上进入源码目录，以运行 APPLaunch 的普通用户执行 `./install.sh`。脚本安装 Node.js、mpv、Pillow 和二维码依赖，并把 ZeroMusic163 注册到启动器。

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
| `S / D` | 播放页降低或提高音量 |
| `H` | 打开或关闭播放页按键帮助 |
| 短按 / 长按 `Esc` 3 秒 | 返回上一级 / 退出界面 |

歌单页提供喜欢的音乐、我的歌单、每日推荐和设置。搜索框可直接打全拼，例如 `qilixiang` 或 `linjunjie`。播放器进程和账户配置存放在本应用自己的目录。整单入队时会跳过不可播放的歌曲。歌单剩余曲目按页加载。播放时会提前准备最多三首的授权地址，并限速缓存下一首的音频。缓存上限是 8 MiB，提前切歌时会直接流式播放。

应用会记住歌曲和播放位置，重开后按 `6` 继续。打开「设置 → 后台播放」后，长按 `Esc` 退出界面时，正在播放的歌曲和队列会继续播放。

设备显示和键盘路径沿用 [Zero SSH](https://github.com/tongfei11/ZeroSSH) 的 Cardputer Zero 适配。搜索、曲库、授权与播放接入 [网易云官方 CLI 0.1.7](https://www.npmjs.com/package/@music163/ncm-cli)。
