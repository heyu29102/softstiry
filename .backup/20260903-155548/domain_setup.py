"""
Нормализация доменов и автогенерация nginx-конфига из domains.txt.
"""

import re
import subprocess
from urllib.parse import urlparse

import config

_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$", re.I)


def host_from_url(url):
    try:
        return (urlparse(url).hostname or "").lower().strip()
    except Exception:
        return ""


def normalize_domain_url(raw):
    """look-now.pro / https://look-now.pro/path → https://look-now.pro"""
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("empty")

    if not re.match(r"^https?://", raw, re.I):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower().strip()
    if not host or not _HOST_RE.match(host):
        raise ValueError(f"invalid host: {host or raw}")

    return f"https://{host}"


def parse_domain_input(text):
    """Вход: look-now.pro | https://... | label|domain → (label, https://host)"""
    text = (text or "").strip()
    if not text:
        raise ValueError("empty")

    label = ""
    rest = text
    if "|" in text:
        label, rest = text.split("|", 1)
        label = label.strip()
        rest = rest.strip()

    if not rest:
        raise ValueError("empty domain part")

    url = normalize_domain_url(rest)
    return label, url


def format_domain_line(label, url):
    url = normalize_domain_url(url)
    if label:
        return f"{label.strip()}|{url}"
    return url


def parse_stored_line(line):
    line = (line or "").strip()
    if not line:
        return "", ""
    if "|" in line:
        label, link = line.split("|", 1)
        return label.strip(), link.strip()
    return "", line.strip()


def line_host(line):
    _, link = parse_stored_line(line)
    if not link:
        return ""
    try:
        return host_from_url(normalize_domain_url(link))
    except ValueError:
        return host_from_url(link)


def hosts_from_lines(lines):
    hosts = []
    for line in lines:
        h = line_host(line)
        if h:
            hosts.append(h)
    return hosts


def expand_hosts_for_nginx(hosts):
    """apex + www.* в server_name (если в CF есть www-запись)."""
    out = set()
    for h in hosts:
        h = h.lower().strip()
        if not h:
            continue
        out.add(h)
        if h.startswith("www."):
            out.add(h[4:])
        else:
            out.add(f"www.{h}")
    return sorted(out)


def hosts_from_pool():
    hosts = config.domain_hosts_from_links(config.load_domain_links(config.DOMAINS_FILE))
    hosts += config.domain_hosts_from_links(config.load_domain_links(config.DOMAINS_CONTACTS_FILE))
    return list(dict.fromkeys(h for h in hosts if h))


def build_nginx_redirect_conf(hosts, upstream=None):
    upstream = upstream or config.NGINX_REDIRECT_UPSTREAM
    hosts = expand_hosts_for_nginx(hosts)
    if not hosts:
        raise ValueError("no hosts")

    names = " ".join(hosts)
    return (
        "# Автогенерация из domains.txt — правь пул в панели, не этот файл вручную\n"
        "# Cloudflare: origin HTTP 80 → redirect.py (без return 301 https на origin)\n"
        "server {\n"
        "    listen 80;\n"
        "    listen [::]:80;\n"
        f"    server_name {names};\n\n"
        "    location / {\n"
        f"        proxy_pass {upstream};\n"
        "        proxy_http_version 1.1;\n"
        "        proxy_set_header Host $host;\n"
        "        proxy_set_header X-Real-IP $remote_addr;\n"
        "        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;\n"
        "        proxy_set_header X-Forwarded-Proto $scheme;\n"
        "        proxy_set_header CF-Connecting-IP $http_cf_connecting_ip;\n"
        "        proxy_set_header CF-IPCountry $http_cf_ipcountry;\n"
        "    }\n"
        "}\n"
    )


def reload_nginx():
    cmd = (config.NGINX_RELOAD_CMD or "").strip()
    if not cmd:
        return False, "NGINX_RELOAD_CMD не задан — перезагрузи nginx вручную"

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )
        tail = (result.stdout or result.stderr or "").strip()
        if tail:
            return True, f"nginx reload ok ({tail[:120]})"
        return True, "nginx reload ok"
    except subprocess.CalledProcessError as error:
        err = (error.stderr or error.stdout or str(error)).strip()
        return False, f"nginx reload fail: {err[:300]}"
    except Exception as error:
        return False, f"nginx reload error: {error}"


def sync_nginx_redirect(auto_reload=None):
    """
    Пересобирает nginx-конфиг из domains.txt.
    Returns (ok, message).
    """
    hosts = hosts_from_pool()
    if not hosts:
        return False, "domains.txt пуст — nginx не обновлён"

    try:
        body = build_nginx_redirect_conf(hosts)
    except ValueError as error:
        return False, str(error)

    conf_path = config.NGINX_REDIRECT_CONF
    conf_path.parent.mkdir(parents=True, exist_ok=True)

    if conf_path.exists():
        backup = conf_path.with_suffix(conf_path.suffix + ".bak")
        try:
            backup.write_bytes(conf_path.read_bytes())
        except Exception:
            pass

    config.atomic_write(conf_path, body.encode("utf-8"))

    msg = f"nginx: {len(hosts)} доменов → {conf_path}"
    do_reload = auto_reload if auto_reload is not None else config.NGINX_AUTO_RELOAD
    if do_reload:
        reload_cmd = (config.NGINX_RELOAD_CMD or "").strip()
        if not reload_cmd:
            msg += "; reload: NGINX_RELOAD_CMD пуст"
            return True, msg
        try:
            subprocess.run(
                "nginx -t",
                shell=True,
                check=True,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except subprocess.CalledProcessError as error:
            err = (error.stderr or error.stdout or str(error)).strip()
            if conf_path.with_suffix(conf_path.suffix + ".bak").exists():
                conf_path.write_bytes(conf_path.with_suffix(conf_path.suffix + ".bak").read_bytes())
            return False, f"nginx -t fail, конфиг не применён: {err[:300]}"
        except subprocess.TimeoutExpired:
            return True, f"{msg}; nginx -t timeout — reload вручную: nginx -t && systemctl reload nginx"

        ok, reload_msg = reload_nginx()
        msg = f"{msg}; {reload_msg}"
        return ok, msg

    msg += "; reload пропущен (NGINX_AUTO_RELOAD=0)"
    return True, msg


def cloudflare_hint():
    ip = (config.SERVER_PUBLIC_IP or "").strip()
    if ip:
        return f"Cloudflare A-запись → <code>{ip}</code> (оранжевое облако ON)"
    return "Cloudflare: A-запись на IP сервера, оранжевое облако ON"


def russia_access_hint(host):
    """Подсказка если TLD часто не открывается в РФ (браузер ERR_TIMED_OUT)."""
    h = (host or "").lower().strip()
    risky_tlds = (".icu", ".cfd", ".sbs", ".bond", ".cyou", ".top", ".xyz")
    if any(h.endswith(t) for t in risky_tlds):
        return (
            "⚠️ <b>РФ:</b> зона <code>.icu</code> и похожие часто не открываются "
            "у части пользователей (таймаут в Chrome). Сервер/CF могут быть OK — "
            "проверь <code>look-now.pro</code> в том же браузере. "
            "Для РФ лучше <code>.pro</code> / <code>.info</code>."
        )
    return ""
