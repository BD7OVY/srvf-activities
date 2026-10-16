#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRVF 活动报名代理服务

存在的原因：GitHub Pages 是 https 静态页，而论坛（srvf.cn）只有 http 且无
CORS 头，浏览器无法直接从前端登录/报名。本服务作为中转：

    浏览器(https) --账号密码--> 本代理 --登录+报名--> srvf.cn (http)

安全模型（重要）：
  * 账号密码仅在当次请求内存中使用，用于登录论坛；**不写日志、不落盘、不缓存**；
  * 登录后的 cookie 同样只在当次请求内存中存在，请求结束即丢弃；
  * 全服务无任何持久化存储。

接口（均仅接受 POST，JSON body）：
  POST /api/apply   {"username":"..","password":"..","tid":"19428","message":".."}
     -> {"ok":true,"msg":"报名成功","nickname":".."}
     -> {"ok":false,"msg":"失败原因"}

仅用 Python 标准库；监听 $PORT（默认 8080），绑定 0.0.0.0。
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import http.cookiejar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock

# ---------------- 配置 ----------------
BASE_URL = "http://www.srvf.cn"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
PORT = int(os.environ.get("PORT", "8080"))

MESSAGE_MAX = 200          # 论坛报名留言上限（与页面 checkform 一致）
RATE_LIMIT = 5             # 同一 IP 每分钟最多请求次数（防密码爆破/滥用）
RATE_WINDOW = 60

# ---------------- 频率限制 ----------------
_rate_lock = Lock()
_rate_hits = {}


def rate_ok(ip):
    now = time.time()
    with _rate_lock:
        hits = [t for t in _rate_hits.get(ip, []) if now - t < RATE_WINDOW]
        if len(hits) >= RATE_LIMIT:
            _rate_hits[ip] = hits
            return False
        hits.append(now)
        _rate_hits[ip] = hits
        return True


# ---------------- 论坛会话 ----------------
class ForumSession:
    """一次性的论坛会话：登录 + 操作，全部在内存中，不持久化"""

    def __init__(self):
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))
        self.opener.addheaders = [("User-Agent", UA)]

    def get(self, url, timeout=25):
        req = urllib.request.Request(url, headers={"Referer": BASE_URL + "/"})
        with self.opener.open(req, timeout=timeout) as resp:
            return resp.read().decode("gbk", "replace")

    def post(self, url, data, timeout=25):
        body = urllib.parse.urlencode(data).encode("gbk")
        req = urllib.request.Request(
            url, data=body,
            headers={"Referer": BASE_URL + "/",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return resp.read().decode("gbk", "replace")
        except urllib.error.HTTPError as e:
            return e.read().decode("gbk", "replace")

    # ---- 登录 ----
    def login(self, username, password, answer=""):
        """返回 (ok, msg_or_nickname)"""
        try:
            html = self.get(BASE_URL + "/member.php?mod=logging&action=login")
        except Exception as e:
            return False, "无法连接论坛登录页：%s" % e
        fh = re.search(r'name="formhash" value="([0-9a-f]+)"', html)
        lh = re.search(r"loginhash=([A-Za-z0-9]+)", html)
        if not fh or not lh:
            return False, "登录页结构异常，暂时无法登录"
        data = {
            "formhash": fh.group(1),
            "referer": BASE_URL + "/./",
            "loginfield": "username",
            "username": username,
            "password": password,
            "questionid": "0",
            "answer": answer,
            "cookietime": "2592000",
            "quickforward": "yes",
            "handlekey": "login",
        }
        try:
            resp = self.post(
                BASE_URL + "/member.php?mod=logging&action=login"
                "&loginsubmit=yes&loginhash=" + lh.group(1), data)
        except Exception as e:
            return False, "登录请求失败：%s" % e
        # 登录成功的标志：cookie 中出现 xxx_auth
        if not any(c.name.endswith("_auth") for c in self.cj):
            msg = extract_message(resp) or "用户名或密码错误（或需安全提问/验证码）"
            return False, msg
        return True, ""


def extract_message(html):
    """从 Discuz '提示信息' 页提取提示文本"""
    for pat in (r'<div[^>]*id="messagetext"[^>]*>(.*?)</div>',
                r'<div class="alert_info"[^>]*>(.*?)</div>',
                r'<p class="[^"]*alert[^"]*"[^>]*>(.*?)</p>'):
        m = re.search(pat, html, re.S)
        if m:
            txt = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", m.group(1))
            txt = re.sub(r"<[^>]+>", " ", txt)
            txt = re.sub(r"\s+", " ", txt).strip()
            # 去掉 "提示信息 xxx" 前缀
            txt = re.sub(r"^提示信息\s*", "", txt)
            if txt:
                return txt[:200]
    return ""


def clean_text(s):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s or "")).strip()


def parse_first_post_pid(html):
    m = re.search(r'<td class="t_f" id="postmessage_(\d+)"', html)
    return m.group(1) if m else None


def find_join_form(html):
    """在（已登录的）活动页中定位报名表单：
    返回 (action_url, hidden_fields, has_message_field)
    - 表单判据：内含 name="ijoin" 的按钮/input；
    - 已登录但已报名的页面通常不再有该表单。
    """
    for m in re.finditer(r"(?is)<form([^>]*)>(.*?)</form>", html):
        attrs, body = m.group(1), m.group(2)
        if "ijoin" not in body:
            continue
        am = re.search(r'action="([^"]+)"', attrs)
        action = am.group(1) if am else None
        if action and not action.startswith("http"):
            action = urllib.parse.urljoin(BASE_URL + "/", action)
        fields = {}
        for tag in re.findall(r"(?is)<input\b[^>]*>", body):
            if re.search(r'type="hidden"', tag, re.I):
                nm = re.search(r'name="([^"]+)"', tag)
                if not nm:
                    continue
                vm = re.search(r'value="([^"]*)"', tag)
                fields[nm.group(1)] = (vm.group(1) if vm else "").replace(
                    "&amp;", "&")
        has_msg = bool(re.search(r'(?is)<textarea[^>]*name="message"', body)) or \
            bool(re.search(r'(?is)<input[^>]*name="message"', body))
        return action, fields, has_msg
    return None, None, None


def find_join_link(html, tid):
    """备用：页面里可能有 GET 方式的报名入口（链接/按钮带 apply/join 与 tid）"""
    pat = re.compile(
        r'(?is)<a[^>]*href="([^"]*(?:apply|join)[^"]*)"[^>]*>(.{0,40}?)</a>')
    for m in pat.finditer(html):
        href, text = m.group(1), clean_text(m.group(2))
        if tid in href and re.search(r"报名|参加|我要", text):
            if not href.startswith("http"):
                href = urllib.parse.urljoin(BASE_URL + "/", href)
            return href
    return None


def already_joined(html, nickname):
    """登录后页面若显示'已报名/取消报名'等状态，尝试判断是否已报名。
    没有可靠信号时返回 None（未知）。"""
    if not nickname:
        return None
    # 报名名单里出现自己的昵称即视为已报名
    seg = html[:400000]
    for m in re.finditer(r"<p>%s</p>" % re.escape(nickname), seg):
        return True
    # 页面上出现“已报名”“取消报名”字样
    if re.search(r"(已报名|取消报名|已参加|退出活动)", clean_text(html)):
        # 该字样可能来自界面说明，仅在同时没有报名表单时采纳
        pass
    return None


def get_nickname(html):
    """从登录后页面提取当前用户昵称"""
    # 顶部用户菜单：<a href="home.php?mod=space&uid=xx">昵称</a>
    m = re.search(r'(?is)<a[^>]*href="home\.php\?mod=space[^"]*"[^>]*>([^<]{1,30})</a>', html)
    if m:
        return clean_text(m.group(1))
    # <title>昵称 - 深圳市公益救援...</title>
    m = re.search(r"(?is)<title>(.*?)(?:\s*[-–—]\s*|$)", html)
    if m:
        t = clean_text(m.group(1))
        if t and "公益救援" not in t and "论坛" not in t and len(t) <= 30:
            return t
    return ""


def do_login_check(username, password):
    """仅验证账号密码，返回 (ok, msg, nickname)"""
    if not username or not password:
        return False, "请填写论坛用户名和密码", ""
    sess = ForumSession()
    ok, info = sess.login(username, password)
    if not ok:
        return False, info, ""
    nickname = ""
    try:
        space = sess.get(BASE_URL + "/home.php?mod=space")
        nickname = get_nickname(space)
    except Exception:
        pass
    return True, "登录成功", nickname


def do_apply(username, password, tid, message):
    """执行 登录→报名 全流程，返回 (ok, msg, nickname)"""
    if not username or not password:
        return False, "请填写论坛用户名和密码", ""
    if not re.fullmatch(r"\d{1,10}", tid or ""):
        return False, "活动 ID 不合法", ""
    message = (message or "").strip()[:MESSAGE_MAX]

    sess = ForumSession()
    ok, info = sess.login(username, password)
    if not ok:
        return False, info, ""
    nickname = ""

    # 抓活动页（登录态）
    try:
        html = sess.get(BASE_URL + "/forum.php?mod=viewthread&tid=" + tid)
    except Exception as e:
        return False, "无法打开活动页：%s" % e, ""
    nickname = get_nickname(html)

    # 报名截止检查（页面文案）
    if "报名已截止" in clean_text(html) or "报名已经截止" in clean_text(html):
        return False, "该活动报名已截止", nickname

    action, fields, has_msg = find_join_form(html)
    if not action:
        # 没有报名表单：尝试 GET 报名链接模式
        link = find_join_link(html, tid)
        if link:
            try:
                resp = sess.get(link)
            except Exception as e:
                return False, "报名请求失败：%s" % e, nickname
            msg = extract_message(resp)
            if re.search(r"成功|已报名|已提交|感谢", msg):
                return True, "报名成功！请留意论坛审核结果", nickname
            return False, msg or "报名未成功，请到原帖确认", nickname
        # 可能已报名 / 名额满 / 不允许报名
        joined = already_joined(html, nickname)
        if joined:
            return True, "您已报名该活动（无需重复报名）", nickname
        txt = clean_text(html)
        if re.search(r"剩余名额[：:]\s*0", txt):
            return False, "名额已满", nickname
        if "已经取消" in txt or "报名通道已关闭" in txt:
            return False, "该活动报名通道已关闭", nickname
        return False, "未找到报名入口（可能已报名、名额已满或报名已截止）", nickname

    # 组装报名表单
    if "formhash" not in fields:
        fh = re.search(r'name="formhash" value="([0-9a-f]+)"', html)
        if fh:
            fields["formhash"] = fh.group(1)
    if "tid" not in fields:
        fields["tid"] = tid
    pid = parse_first_post_pid(html)
    if pid and "pid" not in fields:
        fields["pid"] = pid
    if has_msg or "message" in fields:
        fields["message"] = message
    fields["ijoin"] = "true"
    fields["applysubmit"] = "yes"

    try:
        resp = sess.post(action, fields)
    except Exception as e:
        return False, "报名请求失败：%s" % e, nickname

    msg = extract_message(resp)
    # 报名成功后 Discuz 通常返回提示页；失败会给出原因
    if msg:
        if re.search(r"已经报名|已报名|重复", msg):
            return True, "您已报名该活动", nickname
        if re.search(r"成功|已提交|感谢参与", msg):
            return True, "报名成功！请留意论坛审核结果", nickname
        return False, msg, nickname
    # 无提示文本时，回查活动页判断是否已出现在名单中
    try:
        html2 = sess.get(BASE_URL + "/forum.php?mod=viewthread&tid=" + tid)
        if already_joined(html2, nickname):
            return True, "报名成功！请留意论坛审核结果", nickname
    except Exception:
        pass
    return False, "报名结果未知，请到原帖确认", nickname


# ---------------- HTTP 服务 ----------------
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # 静音默认访问日志（避免记录路径参数）
        pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/health"):
            self._json({"ok": True, "service": "srvf-signup-proxy"})
        else:
            self._json({"ok": False, "msg": "Not found"}, 404)

    def do_POST(self):
        path = self.path.rstrip("/")
        if path not in ("/api/apply", "/api/login"):
            self._json({"ok": False, "msg": "Not found"}, 404)
            return
        ip = self.client_address[0]
        if not rate_ok(ip):
            self._json({"ok": False,
                        "msg": "请求过于频繁，请 1 分钟后再试"}, 429)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(min(length, 64 * 1024))
            data = json.loads(raw.decode("utf-8"))
        except Exception:
            self._json({"ok": False, "msg": "请求体不是合法 JSON"}, 400)
            return
        try:
            if path == "/api/login":
                ok, msg, nickname = do_login_check(
                    data.get("username", ""), data.get("password", ""))
            else:
                ok, msg, nickname = do_apply(
                    data.get("username", ""),
                    data.get("password", ""),
                    str(data.get("tid", "")),
                    data.get("message", ""),
                )
        except Exception as e:
            self._json({"ok": False, "msg": "服务内部错误：%s" % e}, 500)
            return
        out = {"ok": ok, "msg": msg}
        if nickname:
            out["nickname"] = nickname
        self._json(out)


def main():
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("srvf-signup-proxy listening on 0.0.0.0:%d" % PORT, flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
