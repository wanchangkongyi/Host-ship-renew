# 代理 / 过盾配置说明

## 1. 添加 Secret
仓库 `Settings → Secrets and variables → Actions → New repository secret`：

| 名称 | 说明 |
|---|---|
| `PANEL_USERNAME` / `PANEL_PASSWORD` | 面板账号密码（原有） |
| `PROXY_URL` | **新增，可选**。填一条节点分享链接即可 |

`PROXY_URL` 支持：`vless://`（含 Reality / Vision / ws / grpc / httpupgrade）、`vmess://`、`trojan://`、`ss://`、`hysteria2://`(`hy2://`)、`socks5://`、`http://`。
不填则直连（与原来行为一致）。

> 不支持 xhttp/splithttp 传输（sing-box 暂不支持），请选用 tcp/ws/grpc/Reality 节点。

## 2. 工作原理
1. workflow 下载 sing-box；脚本把 `PROXY_URL` 转成 sing-box 配置，在本机 `127.0.0.1:10808` 起 SOCKS5 入站
2. Playwright 的 Chromium 通过该本地代理出网（DNS 也走代理）
3. 启动后先访问 ipify 打印出口 IP，确认代理生效；不通则直接终止
4. 遇到 Cloudflare 「Just a moment」页面会自动等待，并尝试点击 Turnstile 复选框
5. CI 中使用 `xvfb-run` + `HEADLESS=false` 有头模式，比 headless 更容易过盾

## 3. 本地运行
```bash
pip install playwright && playwright install chromium
# 安装 sing-box 并保证在 PATH（或设置 SING_BOX_PATH）
export PANEL_USERNAME=xxx PANEL_PASSWORD=xxx
export PROXY_URL='vless://...'
python hostship_renew.py
```

## 4. 排障
- 日志里没有 "当前浏览器出口 IP" → 节点不可用，换一个
- 出现 "等待 Cloudflare 验证超时" → 出口 IP 信誉差（机房 IP 常见），换住宅/优质线路节点；`cf_blocked.png` 会作为 Artifact 上传
- 可通过 `PROXY_LOCAL_PORT` 修改本地端口（默认 10808）
