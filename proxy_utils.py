"""
代理支持模块：把常见代理分享链接转成 sing-box 配置，在本地起一个 SOCKS5/HTTP 混合入站，
再让 Playwright(Chromium) 通过 127.0.0.1 的本地端口出网。

支持的 PROXY_URL 格式：
  vless://   (含 Reality / XTLS-Vision / ws / grpc / httpupgrade / h2)
  vmess://   (base64 JSON 格式)
  trojan://
  ss://      (SIP002 与旧版 base64 格式)
  hysteria2:// 或 hy2://
  socks5:// / socks://  / http:// / https://   (直接透传给浏览器，不需要 sing-box)

注意：为避免泄露，本模块的日志不会输出完整代理链接，只输出协议类型。
"""
import base64
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from urllib.parse import urlparse, parse_qs, unquote

LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = int(os.environ.get("PROXY_LOCAL_PORT", "10808"))

DIRECT_SCHEMES = {"http", "https", "socks", "socks5", "socks5h"}


# ---------------------------------------------------------------- 小工具

def _b64decode(s: str) -> str:
    s = s.strip().replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    return base64.b64decode(s).decode("utf-8", errors="strict")


def _q(params, key, default=""):
    """取 query 参数第一个值"""
    v = params.get(key)
    return v[0] if v else default


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes")


def _split_csv(v):
    return [x.strip() for x in v.split(",") if x.strip()] if v else []


# ---------------------------------------------------------------- 传输层 / TLS

def _build_transport(net, host, path, service_name=""):
    net = (net or "tcp").lower()
    if net in ("tcp", "raw", ""):
        return None
    if net == "ws":
        t = {"type": "ws", "path": path or "/"}
        if host:
            t["headers"] = {"Host": host}
        return t
    if net == "grpc":
        return {"type": "grpc", "service_name": service_name or path or ""}
    if net in ("http", "h2"):
        t = {"type": "http", "path": path or "/"}
        if host:
            t["host"] = _split_csv(host)
        return t
    if net == "httpupgrade":
        t = {"type": "httpupgrade", "path": path or "/"}
        if host:
            t["host"] = host
        return t
    raise ValueError(f"不支持的传输类型: {net}（sing-box 不支持 xhttp/splithttp 等，请换用 ws/grpc/tcp 节点）")


def _build_tls(server, params, default_enabled=False):
    """根据 query 参数构造 sing-box 的 tls 字段；不需要 TLS 时返回 None"""
    security = _q(params, "security", "tls" if default_enabled else "none").lower()
    if security in ("", "none"):
        return None

    sni = _q(params, "sni") or _q(params, "peer") or _q(params, "host") or server
    tls = {
        "enabled": True,
        "server_name": sni,
        "insecure": _truthy(_q(params, "allowInsecure", _q(params, "insecure", "0"))),
    }
    alpn = _split_csv(_q(params, "alpn"))
    if alpn:
        tls["alpn"] = alpn

    fp = _q(params, "fp")
    if fp or security == "reality":
        tls["utls"] = {"enabled": True, "fingerprint": fp or "chrome"}

    if security == "reality":
        pbk = _q(params, "pbk")
        if not pbk:
            raise ValueError("Reality 节点缺少公钥参数 pbk")
        tls["reality"] = {"enabled": True, "public_key": pbk, "short_id": _q(params, "sid")}
    return tls


# ---------------------------------------------------------------- 各协议解析

def _parse_vless(u):
    p = parse_qs(u.query)
    out = {
        "type": "vless",
        "tag": "proxy",
        "server": u.hostname,
        "server_port": u.port or 443,
        "uuid": unquote(u.username or ""),
        "packet_encoding": "xudp",
    }
    flow = _q(p, "flow")
    if flow:
        out["flow"] = flow
    tls = _build_tls(u.hostname, p)
    if tls:
        out["tls"] = tls
    tr = _build_transport(_q(p, "type", "tcp"), _q(p, "host"), unquote(_q(p, "path")),
                          _q(p, "serviceName"))
    if tr:
        out["transport"] = tr
    return out


def _parse_vmess(raw):
    body = raw[len("vmess://"):].split("#", 1)[0]
    try:
        d = json.loads(_b64decode(body))
    except Exception:
        # 兼容 URL 风格 vmess://uuid@host:port?...
        u = urlparse(raw)
        p = parse_qs(u.query)
        out = {
            "type": "vmess", "tag": "proxy", "server": u.hostname, "server_port": u.port or 443,
            "uuid": unquote(u.username or ""), "security": _q(p, "encryption", "auto"),
            "alter_id": int(_q(p, "aid", "0") or 0),
        }
        tls = _build_tls(u.hostname, p)
        if tls:
            out["tls"] = tls
        tr = _build_transport(_q(p, "type", "tcp"), _q(p, "host"), unquote(_q(p, "path")),
                              _q(p, "serviceName"))
        if tr:
            out["transport"] = tr
        return out

    server = d.get("add")
    out = {
        "type": "vmess",
        "tag": "proxy",
        "server": server,
        "server_port": int(d.get("port", 443)),
        "uuid": d.get("id", ""),
        "security": d.get("scy") or "auto",
        "alter_id": int(d.get("aid") or 0),
    }
    if str(d.get("tls", "")).lower() in ("tls", "1", "true"):
        tls = {
            "enabled": True,
            "server_name": d.get("sni") or d.get("host") or server,
            "insecure": _truthy(d.get("allowInsecure", "0")),
        }
        if d.get("alpn"):
            tls["alpn"] = _split_csv(d["alpn"])
        if d.get("fp"):
            tls["utls"] = {"enabled": True, "fingerprint": d["fp"]}
        out["tls"] = tls
    net = d.get("net", "tcp")
    tr = _build_transport(net, d.get("host", ""), d.get("path", ""),
                          d.get("path", "") if net == "grpc" else "")
    if tr:
        out["transport"] = tr
    return out


def _parse_trojan(u):
    p = parse_qs(u.query)
    out = {
        "type": "trojan",
        "tag": "proxy",
        "server": u.hostname,
        "server_port": u.port or 443,
        "password": unquote(u.username or ""),
    }
    tls = _build_tls(u.hostname, p, default_enabled=True)
    if tls:
        out["tls"] = tls
    tr = _build_transport(_q(p, "type", "tcp"), _q(p, "host"), unquote(_q(p, "path")),
                          _q(p, "serviceName"))
    if tr:
        out["transport"] = tr
    return out


def _parse_ss(raw):
    body = raw[len("ss://"):].split("#", 1)[0]
    query = ""
    if "?" in body:
        body, query = body.split("?", 1)
    body = body.rstrip("/")

    if "@" in body:  # SIP002: base64(method:pass)@host:port  或明文 method:pass@host:port
        userinfo, hostport = body.rsplit("@", 1)
        try:
            userinfo = _b64decode(userinfo)
        except Exception:
            userinfo = unquote(userinfo)
    else:  # 旧格式：base64(method:pass@host:port)
        decoded = _b64decode(body)
        userinfo, hostport = decoded.rsplit("@", 1)

    method, password = userinfo.split(":", 1)
    host, port = hostport.rsplit(":", 1)
    if "plugin=" in query:
        raise ValueError("暂不支持带 plugin 的 Shadowsocks 节点")
    return {
        "type": "shadowsocks",
        "tag": "proxy",
        "server": host.strip("[]"),
        "server_port": int(port),
        "method": method,
        "password": password,
    }


def _parse_hysteria2(u):
    p = parse_qs(u.query)
    out = {
        "type": "hysteria2",
        "tag": "proxy",
        "server": u.hostname,
        "server_port": u.port or 443,
        "password": unquote(u.username or "") + (":" + unquote(u.password) if u.password else ""),
        "tls": {
            "enabled": True,
            "server_name": _q(p, "sni") or u.hostname,
            "insecure": _truthy(_q(p, "insecure", "0")),
        },
    }
    alpn = _split_csv(_q(p, "alpn"))
    if alpn:
        out["tls"]["alpn"] = alpn
    if _q(p, "obfs"):
        out["obfs"] = {"type": _q(p, "obfs"), "password": _q(p, "obfs-password")}
    return out


def share_link_to_outbound(link: str) -> dict:
    link = link.strip()
    scheme = link.split("://", 1)[0].lower()
    if scheme == "vless":
        return _parse_vless(urlparse(link))
    if scheme == "vmess":
        return _parse_vmess(link)
    if scheme == "trojan":
        return _parse_trojan(urlparse(link))
    if scheme == "ss":
        return _parse_ss(link)
    if scheme in ("hysteria2", "hy2"):
        return _parse_hysteria2(urlparse(link))
    raise ValueError(f"不支持的代理协议: {scheme}://")


def build_singbox_config(outbound: dict, port: int = LOCAL_PORT) -> dict:
    return {
        "log": {"level": "warn", "timestamp": True},
        "inbounds": [{
            "type": "mixed",
            "tag": "in",
            "listen": LOCAL_HOST,
            "listen_port": port,
        }],
        "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        "route": {"final": "proxy"},
    }


# ---------------------------------------------------------------- 进程管理

def _find_singbox():
    path = os.environ.get("SING_BOX_PATH") or shutil.which("sing-box")
    if not path:
        raise RuntimeError(
            "未找到 sing-box 可执行文件。请安装 sing-box 并加入 PATH，或设置环境变量 SING_BOX_PATH"
            "（GitHub Actions 中由 workflow 自动安装）"
        )
    return path


def _wait_port(host, port, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


class ProxyHandle:
    """start_proxy 的返回值：playwright_proxy 直接喂给 launch(proxy=...)；stop() 关闭子进程"""

    def __init__(self, playwright_proxy, process=None, workdir=None, logfile=None):
        self.playwright_proxy = playwright_proxy
        self._proc = process
        self._workdir = workdir
        self._logfile = logfile

    def stop(self):
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        if self._workdir:
            shutil.rmtree(self._workdir, ignore_errors=True)


def start_proxy(proxy_url: str, log=print) -> ProxyHandle:
    """
    根据 PROXY_URL 启动代理并返回 ProxyHandle。
    - http/https/socks5 链接：直接交给 Chromium，不起子进程
    - vless/vmess/trojan/ss/hysteria2：生成配置，启动本地 sing-box
    """
    proxy_url = proxy_url.strip()
    scheme = proxy_url.split("://", 1)[0].lower()

    u0 = urlparse(proxy_url)
    # Chromium 不支持带账号密码的 SOCKS5，这种情况交给 sing-box 中转
    socks_with_auth = scheme in ("socks", "socks5", "socks5h") and bool(u0.username)

    if scheme in DIRECT_SCHEMES and not socks_with_auth:
        u = u0
        server = f"{'socks5' if scheme.startswith('socks') else scheme}://{u.hostname}:{u.port}"
        cfg = {"server": server}
        if u.username:
            cfg["username"] = unquote(u.username)
            cfg["password"] = unquote(u.password or "")
        log(f"使用 {scheme} 代理（直连模式，无需 sing-box）")
        return ProxyHandle(cfg)

    if socks_with_auth:
        outbound = {"type": "socks", "tag": "proxy", "server": u0.hostname, "server_port": u0.port or 1080,
                    "version": "5", "username": unquote(u0.username), "password": unquote(u0.password or "")}
    else:
        outbound = share_link_to_outbound(proxy_url)
    log(f"已解析代理协议: {outbound['type']}"
        f"{' + reality' if outbound.get('tls', {}).get('reality') else ''}"
        f"{' + ' + outbound['transport']['type'] if outbound.get('transport') else ''}")

    singbox = _find_singbox()
    workdir = tempfile.mkdtemp(prefix="singbox_")
    cfg_path = os.path.join(workdir, "config.json")
    log_path = os.path.join(workdir, "sing-box.log")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(build_singbox_config(outbound), f, ensure_ascii=False, indent=2)

    logfile = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen([singbox, "run", "-c", cfg_path], stdout=logfile, stderr=subprocess.STDOUT)
    handle = ProxyHandle({"server": f"socks5://{LOCAL_HOST}:{LOCAL_PORT}"}, proc, workdir, logfile)

    if not _wait_port(LOCAL_HOST, LOCAL_PORT) or proc.poll() is not None:
        logfile.flush()
        try:
            with open(log_path, encoding="utf-8") as f:
                tail = f.read()[-1500:]
        except Exception:
            tail = ""
        handle.stop()
        raise RuntimeError(f"sing-box 启动失败。日志:\n{tail}")

    log(f"本地代理已就绪: socks5://{LOCAL_HOST}:{LOCAL_PORT}")
    return handle
