# 🧩 qBittorrent 跨站拼图自动化引擎 (PT Auto Merger)

![Python](https://img.shields.io/badge/Python-3.x-blue.svg)
![qBittorrent](https://img.shields.io/badge/qBittorrent-API_v2-2b5797.svg)
![License](https://img.shields.io/badge/License-MIT-green.svg)

在 PT (Private Tracker) 玩家的日常中，我们经常会遇到**在多个站点同时下载同一个大文件（如 4K 原盘）**的情况。传统的辅种工具只能等待 A 站 100% 下载完成后，再去校验辅种 B 站，这导致了严重的宽带浪费和硬盘的重复覆写。

本项目利用 qBittorrent 底层引擎的同径单进程写入特性，通过高频拉取区块状态并进行**多维矩阵“按位或”运算**，让多个站点的下载任务直接在物理硬盘上“实时拼图”。

## ✨ 核心功能亮点

*   **🧱 物理级实时拼图**：无需等任一任务下载完毕。只要 A 站和 B 站下载的区块刚好互补达到 100%，脚本瞬间接管并完成合并做种。
*   **🧮 内存级状态矩阵运算**：利用 WebAPI 提取 `pieceStates`，在内存中进行轻量级的二进制位运算，精准计算“联合进度”。
*   **🛡️ 工业级防风控机制**：独创 `暂停 ➔ 校验 ➔ 恢复` 三连击状态机。完美规避“边下边校验”导致的脏读报错，确保向 Tracker 汇报的日志合规平滑，100% 防封禁。
*   **⛽ 动态中途补给站**：特设“绝对差值（默认 5GB）”与“冷却时间（默认 45 分钟）”双重阈值。在网络极度不对等时，适时强制同步进度，榨干闲置带宽的同时，**极致保护机械硬盘寿命**。
*   **🚀 纯反向驱动**：基于已完成状态的懒加载匹配，$O(1)$ 级时间复杂度，不拖垮软路由/NAS 性能。

## 🛠️ 安装与配置

### 1. 环境依赖
*   运行环境：Python 3.x
*   依赖库：`requests`
*   软件版本：qBittorrent (需开启 WebUI)

```bash
# 安装依赖
pip install requests

```

### 2. 修改配置

下载 `pt_merger_v4.py` 后，用文本编辑器打开，修改顶部的基础配置：

```python
# ================= 基础配置区 =================
QB_URL = "[http://192.168.](http://192.168.)x.x:8080"  # 你的 qBittorrent WebUI 地址
USERNAME = "admin"                  # WebUI 用户名
PASSWORD = "adminadmin"             # WebUI 密码

# ================= 阈值系统配置 =================
GAP_THRESHOLD_BYTES = 5 * 1024**3   # 落后多少 GB 触发中途补给 (默认 5GB)
COOLDOWN_SECONDS = 45 * 60          # 强制校验的冷却时间 (默认 45 分钟，保护硬盘)
SAFE_ZONE_RATIO = 0.95              # 最后 5% 冲刺区免打扰
# ==============================================

```

## 🚀 部署与运行模式

你可以直接在终端中运行它来观察魔法生效的过程：

```bash
python3 pt_merger_v4.py

```

### 💡 进阶：在 OpenWrt 软路由中静默挂机 (推荐)

对于使用 N100 等软路由设备的极客，推荐使用系统的 `logger` 机制挂载后台，既能防内存溢出，又能随时看日志。

1. **安装环境**：
```bash
opkg update
opkg install python3 python3-requests

```


2. **挂载后台**（注意加入 `-u` 参数关闭 Python 缓冲）：
```bash
nohup python3 -u /root/pt_merger_v4.py 2>&1 | logger -t pt_merger &

```


*(你可以将此命令加入 OpenWrt 的 `/etc/rc.local` 中实现开机自启)*
3. **随时查岗**：
```bash
logread | grep pt_merger

```



### 🐧 在标准 Linux (Debian/Ubuntu/PVE) 中使用 Systemd

1. 创建服务文件：`nano /etc/systemd/system/pt_merger.service`
2. 填入以下配置：
```ini
[Unit]
Description=PT Cross-Site Auto Merger
After=network-online.target

[Service]
Type=simple
User=root
Restart=always
RestartSec=10
# 请将路径替换为脚本实际路径，并确保使用 -u 参数
ExecStart=/usr/bin/python3 -u /root/pt_merger_v4.py

[Install]
WantedBy=multi-user.target

```


3. 激活并查看状态：
```bash
systemctl daemon-reload
systemctl enable pt_merger
systemctl start pt_merger
journalctl -u pt_merger -f

```


## ⚠️ 注意事项

* **路径一致**：请务必确保不同站点的同名种子，在 qBittorrent 中的**保存路径完全一致**。
* **风控说明**：本脚本调用的 API 流程与 WebUI 右键“强制校验”完全一致，符合规范，但请勿随意大幅度调低 `COOLDOWN_SECONDS`（冷却时间），以免频繁 IO 拖慢系统。

## 🤝 致谢 & 关于

* 本项目的核心业务逻辑、边界情况推演及架构设计由作者独立构思完成。
* 代码的具体语法编写、工程化封装与注释，由 **Google Gemini** 提供 AI 结对编程辅助。

## 📜 许可证

MIT License

