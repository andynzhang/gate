#!/usr/bin/env python3
"""
VPN Gate SSTP 节点检测流水线
============================
流程:
  1. 获取 VPN Gate 原始节点 (官方 api/iphone CSV, 失败时依次回退多个 JSON 镜像)
  2. 只保留「带 TCP 入口」的中继 = SSTP 可用节点
     (OpenVPN 配置里 proto tcp + remote <ip> <port>; UDP-only 中继无法走 SSTP/xray 链, 直接丢弃)
  3. 按 host+port+protocol 去重
  4. 探测可用 Cloudflare Worker, 再并发 GET {WORKER}/check?sstp=vpn:vpn@host:port
     (单节点 HTTP 成功 != 节点可用; 以 Worker 返回 JSON 的 success 字段为准)
  5. 保留 success=true 的节点, 按国家分组, 生成 public/data.json + public/index.html
  6. 网页端 (GitHub Pages) 读取 data.json 展示
退出码:
  0 = 正常完成 (允许部分节点检测失败)
  1 = 硬性失败 (数据源全挂 / 解析不出 SSTP 节点 / Worker 完全不可达 / 程序异常)
     这些情况绝不允许"假成功"
"""

[46 lines collapsed]

    "VPNGATE_MIRROR",
    (
        "https://cdn.jsdelivr.net/gh/bagussatoto/Vpngate-Scraper-API@main/json/data.json",
        "https://fastly.jsdelivr.net/gh/bagussatoto/Vpngate-Scraper-API@main/json/data.json",
        "https://cdn.jsdelivr.net/gh/fdciabdul/Vpngate-Scraper-API@main/json/data.json",
        "https://raw.githubusercontent.com/bagussatoto/Vpngate-Scraper-API/refs/heads/main/json/data.json",
        "https://raw.githubusercontent.com/fdciabdul/Vpngate-Scraper-API/refs/heads/main/json/data.json",

[72 lines collapsed]

# 第 1 步: 获取 VPN Gate 原始节点
# ---------------------------------------------------------------------------
def _http_get(url, timeout=None):
    t = timeout or HTTP_TIMEOUT
    if not isinstance(t, tuple):
        # 连接超时单独缩短, 避免官方站/已删镜像卡住 60s 才轮询下一个源
        t = (min(12, float(t)), float(t))
    return requests.get(
        url,
        timeout=timeout or HTTP_TIMEOUT,
        timeout=t,
        headers={"User-Agent": "Mozilla/5.0 (compatible; gate-checker)"},
    )

[233 lines collapsed]

    out["checked_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out["exit"] = None
    out["residential"] = "unknown"
    try:
        r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
        if r.status_code != 200:
            out["error"] = f"HTTP {r.status_code}"
    last_err = None
    # 公共 Worker 可能 429/5xx; 只对这类瞬时错误短重试, 避免把 90s 超时再乘 3
    for attempt in range(3):
        try:
            r = session.get(url, timeout=CHECK_TIMEOUT, headers={"User-Agent": "Mozilla/5.0 (gate-checker)"})
            if r.status_code in (429, 502, 503, 520, 522, 524) and attempt < 2:
                time.sleep(1.5 * (attempt + 1))
                last_err = f"HTTP {r.status_code}"
                continue
            if r.status_code != 200:
                out["error"] = f"HTTP {r.status_code}"
                out["worker_error"] = True
                return out
            j = r.json()
            ok = bool(j.get("success"))
            out["success"] = ok
            out["status"] = "success" if ok else "failed"
            out["latency_ms"] = j.get("responseTime")
            out["colo"] = j.get("colo")
            out["error"] = (None if ok else (j.get("error") or j.get("message") or "check failed"))
            # SSTP 版 Worker: exit 在顶层; 新版 CheckSocks5 把国家/城市放在 location 里
            exit_info = j.get("exit") or {}
            if exit_info:
                asn = exit_info.get("asn") or {}
                loc = exit_info.get("location") or {}
                org = asn.get("org") or asn.get("name") or ""
                out["exit"] = {
                    "ip": exit_info.get("ip"),
                    "country": exit_info.get("country") or loc.get("country"),
                    "country_code": exit_info.get("country_code") or loc.get("country_code"),
                    "city": exit_info.get("city") or loc.get("city"),
                    "continent": exit_info.get("continent") or loc.get("continent"),
                    "asn": asn.get("asn"),
                    "org": org,
                    "type": asn.get("type"),
                    "is_datacenter": exit_info.get("is_datacenter"),
                }
                out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))
            else:
                out["residential"] = classify_network(out["host"], None, None)
            return out
        except Exception as exc:
            last_err = f"{type(exc).__name__}: {exc}"
            if attempt < 2 and "Timeout" not in type(exc).__name__:
                time.sleep(1.0 * (attempt + 1))
                continue
            out["error"] = last_err
            out["worker_error"] = True
            return out
        j = r.json()
        ok = bool(j.get("success"))
        out["success"] = ok
        out["status"] = "success" if ok else "failed"
        out["latency_ms"] = j.get("responseTime")
        out["colo"] = j.get("colo")
        out["error"] = (None if ok else (j.get("error") or j.get("message") or "check failed"))
        # SSTP 版 Worker: 顶层直接返回 exit, 含真实 is_datacenter 标志 + 嵌套 asn 对象
        exit_info = j.get("exit") or {}
        if exit_info:
            asn = exit_info.get("asn") or {}
            org = asn.get("org") or asn.get("name") or ""
            out["exit"] = {
                "ip": exit_info.get("ip"),
                "country": exit_info.get("country"),
                "country_code": exit_info.get("country_code"),
                "city": exit_info.get("city"),
                "continent": exit_info.get("continent"),
                "asn": asn.get("asn"),
                "org": org,
                "type": asn.get("type"),
                "is_datacenter": exit_info.get("is_datacenter"),
            }
            out["residential"] = classify_network(out["host"], org, exit_info.get("is_datacenter"))
        else:
            out["residential"] = classify_network(out["host"], None, None)
        return out
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
        out["worker_error"] = True
        return out
    out["error"] = last_err or "worker retry exhausted"
    out["worker_error"] = True
    return out
def check_all(nodes, session):

[349 lines collapsed]
