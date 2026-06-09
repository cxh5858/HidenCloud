#!/usr/bin/env python3
"""
setup_proxy.py — 解析 PROXY_NODE 环境变量并生成 xray_config.json
支持协议: vless / vmess / trojan / shadowsocks / socks5
"""

import os
import sys
import json
import base64
from urllib.parse import parse_qs, unquote


def mask(s: str) -> str:
    """遮盖敏感字符串，只显示首尾各2位"""
    return f"{s[:2]}***{s[-2:]}" if len(s) > 4 else "****"


def safe_b64decode(s: str) -> bytes:
    """处理 base64 padding，兼容无 padding 的字符串"""
    padding = 4 - len(s) % 4
    if padding != 4:
        s += "=" * padding
    return base64.b64decode(s)


def build_base_config() -> dict:
    return {
        "log": {"loglevel": "warning"},
        "inbounds": [
            {"port": 1080, "listen": "127.0.0.1", "protocol": "socks", "settings": {"udp": True}},
            {"port": 1081, "listen": "127.0.0.1", "protocol": "http"},
        ],
        "outbounds": [],
    }


def parse_vless(content: str) -> dict:
    uuid, rest = content.split("@", 1)
    host_port, params_str = (rest.split("?", 1) + [""])[:2]
    address, port = (host_port.rsplit(":", 1) + ["443"])[:2] if ":" in host_port else (host_port, "443")

    params = parse_qs(params_str)
    security = params.get("security", ["none"])[0]
    network  = params.get("type",     ["tcp"])[0]
    sni      = params.get("sni",      [address])[0]
    fp       = params.get("fp",       ["chrome"])[0]
    flow     = params.get("flow",     [""])[0]
    pbk      = params.get("pbk",      [""])[0]
    host     = params.get("host",     [sni])[0]
    path     = unquote(params.get("path", ["/"])[0])

    print(f"[INFO] VLESS -> {mask(address)}:{port} (security: {security}, network: {network})")

    user = {"id": uuid, "encryption": "none"}
    if flow:
        user["flow"] = flow

    outbound = {
        "protocol": "vless",
        "settings": {
            "vnext": [{"address": address, "port": int(port), "users": [user]}]
        },
        "streamSettings": {
            "network": network,
            "security": security,
        },
    }

    if security == "reality":
        outbound["streamSettings"]["realitySettings"] = {
            "serverName": sni, "fingerprint": fp, "publicKey": pbk
        }
    elif security == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni, "fingerprint": fp, "allowInsecure": False
        }

    if network == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path, "headers": {"Host": host}
        }
    elif network == "grpc":
        outbound["streamSettings"]["grpcSettings"] = {
            "serviceName": params.get("serviceName", [""])[0]
        }

    return outbound


def parse_vmess(content: str) -> dict:
    try:
        vm = json.loads(safe_b64decode(content).decode("utf-8"))
    except Exception as e:
        print(f"[ERROR] VMess 解析失败: {e}")
        sys.exit(1)

    address = vm.get("add", "")
    port    = int(vm.get("port", 443))
    uuid    = vm.get("id", "")
    aid     = int(vm.get("aid", 0))
    network = vm.get("net", "tcp")
    tls     = vm.get("tls", "")
    sni     = vm.get("sni", "") or vm.get("host", address)
    host    = vm.get("host", address)
    path    = vm.get("path", "/")
    fp      = vm.get("fp", "chrome")

    print(f"[INFO] VMess -> {mask(address)}:{port} (network: {network}, tls: {tls})")

    outbound = {
        "protocol": "vmess",
        "settings": {
            "vnext": [{
                "address": address, "port": port,
                "users": [{"id": uuid, "alterId": aid, "security": "auto"}],
            }]
        },
        "streamSettings": {
            "network": network,
            "security": "tls" if tls == "tls" else "none",
        },
    }

    if tls == "tls":
        outbound["streamSettings"]["tlsSettings"] = {
            "serverName": sni, "fingerprint": fp, "allowInsecure": False
        }

    if network == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path, "headers": {"Host": host}
        }

    return outbound


def parse_trojan(content: str) -> dict:
    password, rest = content.split("@", 1)
    host_port, params_str = (rest.split("?", 1) + [""])[:2]
    address, port = host_port.rsplit(":", 1) if ":" in host_port else (host_port, "443")

    params  = parse_qs(params_str)
    sni     = params.get("sni",  [address])[0]
    network = params.get("type", ["tcp"])[0]
    host    = params.get("host", [sni])[0]
    path    = unquote(params.get("path", ["/"])[0])
    fp      = params.get("fp",   ["chrome"])[0]

    print(f"[INFO] Trojan -> {mask(address)}:{port} (network: {network})")

    outbound = {
        "protocol": "trojan",
        "settings": {
            "servers": [{"address": address, "port": int(port), "password": password}]
        },
        "streamSettings": {
            "network": network,
            "security": "tls",
            "tlsSettings": {"serverName": sni, "fingerprint": fp, "allowInsecure": False},
        },
    }

    if network == "ws":
        outbound["streamSettings"]["wsSettings"] = {
            "path": path, "headers": {"Host": host}
        }

    return outbound


def parse_shadowsocks(content: str) -> dict:
    try:
        if "@" in content:
            # SIP002 格式: base64(method:password)@host:port
            encoded, server_part = content.split("@", 1)
            decoded = safe_b64decode(encoded).decode("utf-8")
            method, password = decoded.split(":", 1)
            address, port = server_part.split(":", 1)
        else:
            # 传统格式: base64(method:password@host:port)
            decoded = safe_b64decode(content).decode("utf-8")
            if "@" in decoded:
                user_part, server_part = decoded.split("@", 1)
                method, password = user_part.split(":", 1)
                address, port = server_part.split(":", 1)
            else:
                raise ValueError("无法识别的 Shadowsocks 格式")
    except Exception as e:
        print(f"[ERROR] Shadowsocks 解析失败: {e}")
        sys.exit(1)

    print(f"[INFO] Shadowsocks -> {mask(address)}:{port}")

    return {
        "protocol": "shadowsocks",
        "settings": {
            "servers": [{
                "address": address, "port": int(port),
                "method": method, "password": password,
            }]
        },
    }


def main():
    url = os.environ.get("PROXY_NODE", "").strip()
    if not url:
        print("[ERROR] PROXY_NODE 为空")
        sys.exit(1)

    # 去掉 fragment（# 之后的备注）
    url_clean = url.rsplit("#", 1)[0]

    if url_clean.startswith("vless://"):
        outbound = parse_vless(url_clean[8:])

    elif url_clean.startswith("vmess://"):
        outbound = parse_vmess(url_clean[8:])

    elif url_clean.startswith("trojan://"):
        outbound = parse_trojan(url_clean[9:])

    elif url_clean.startswith("ss://"):
        outbound = parse_shadowsocks(url_clean[5:])

    elif url_clean.startswith("socks5://") or url_clean.startswith("socks://"):
        content = url_clean.split("://")[1]
        server  = content.split("@")[1].split(":")[0] if "@" in content else content.split(":")[0]
        print(f"[INFO] SOCKS5 -> {mask(server)}")
        with open("use_external_socks.txt", "w") as f:
            f.write(url_clean)
        print("[INFO] ✅ 外部 SOCKS5 配置已写入 use_external_socks.txt")
        return

    else:
        protocol = url_clean.split("://")[0]
        print(f"[ERROR] 不支持的协议: {protocol}")
        sys.exit(1)

    config = build_base_config()
    config["outbounds"].append(outbound)

    with open("xray_config.json", "w") as f:
        json.dump(config, f, indent=2)

    print("[INFO] ✅ 代理配置已生成 xray_config.json")


if __name__ == "__main__":
    main()
