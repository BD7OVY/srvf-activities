#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SRVF 论坛活动抓取脚本（定时遍历 + 增量刷新）

数据源：深圳公益救援志愿者联合会论坛 · 活动版块
  http://www.srvf.cn/forum.php?mod=forumdisplay&fid=45

增量策略：
  1. 每次运行只扫描列表页前 SCAN_PAGES 页（默认 3 页 = 75 帖），
     遇到整页无新帖即提前停止（列表按最后回复排序，新帖必然浮在最前）；
  2. 只对“新发现的 tid”抓取详情页；
  3. 对“尚未结束的活动”定期重抓详情页（默认间隔 REFRESH_HOURS 小时），
     以便活动时间/地点/报名截止被编辑后能更新；
  4. 结束超过 KEEP_DAYS 天的活动归档剔除，控制 JSON 体积。

环境变量（均可选）：
  SCAN_PAGES     扫描列表页数，默认 3
  DELAY          请求间隔秒数，默认 1.0
  REFRESH_HOURS  未结束活动的详情重抓间隔，默认 6
  KEEP_DAYS      活动结束保留天数，默认 90

仅用 Python 标准库，GitHub Actions 无需安装依赖。
"""

import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from html import unescape
from pathlib import Path

# ---------------- 配置 ----------------
BASE_URL = "http://www.srvf.cn"
FORUM_URL = BASE_URL + "/forum.php?mod=forumdisplay&fid=45"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "data" / "activities.json"
STATE_PATH = ROOT / "scripts" / "state.json"

SCAN_PAGES = int(os.environ.get("SCAN_PAGES", "3"))
DELAY = float(os.environ.get("DELAY", "1.0"))
REFRESH_HOURS = float(os.environ.get("REFRESH_HOURS", "6"))
KEEP_DAYS = int(os.environ.get("KEEP_DAYS", "90"))

CONTENT_MAX = 600  # 正文摘要最大字符数

# ---------------- 基础工具 ----------------

def now_local():
    """本地时间（论坛时间为中国本地时间，不做时区转换）"""
    return datetime.now()


def now_iso():
    return now_local().strftime("%Y-%m-%dT%H:%M:%S+08:00")


def log(msg):
    print("[%s] %s" % (now_local().strftime("%H:%M:%S"), msg), flush=True)


def http_get(url, retries=2, timeout=30):
    """带 UA 的 GET，返回 bytes；失败重试后抛异常"""
    last_err = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, OSError) as e:
            last_err = e
            if attempt < retries:
                time.sleep(2 + attempt * 2)
    raise RuntimeError("GET %s 失败: %s" % (url, last_err))


def fetch_html(url):
    """抓取并转成 str（论坛为 GBK 编码）"""
    raw = http_get(url)
    for enc in ("gbk", "gb18030", "utf-8"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("gbk", errors="replace")


# 块级标签转为换行，行内标签直接删除（避免 <font> 拆字引入多余空格）
_BLOCK_TAGS = ("br", "div", "p", "tr", "td", "li", "ul", "ol", "table", "thead",
               "tbody", "h1", "h2", "h3", "h4", "h5", "h6", "dl", "dt", "dd",
               "blockquote", "pre", "hr", "center", "section", "article")


def clean_tags(fragment):
    """去 script/style/iframe、注释、编辑提示，块级标签转换行，压缩空白"""
    s = re.sub(r"(?is)<(script|style|iframe)[^>]*>.*?</\1>", " ", fragment)
    s = re.sub(r"(?is)<i class=\"pstatus\">.*?</i>", " ", s)  # "本帖最后由…编辑"
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)</?(?:%s)\b[^>]*>" % "|".join(_BLOCK_TAGS), "\n", s)
    s = re.sub(r"(?s)<[^>]+>", "", s)
    s = unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{2,}", "\n", s)
    return s.strip()


def norm_dt(text):
    """把 '2026-9-30 17:37' / '2026-9-30 17:37:45' 规范成 '2026-09-30 17:37'"""
    if not text:
        return None
    m = re.search(r"(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?"
                  r"(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", text)
    if not m:
        return None
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    hh = int(m.group(4) or 0)
    mi = int(m.group(5) or 0)
    try:
        datetime(y, mo, d, hh, mi)
    except ValueError:
        return None
    return "%04d-%02d-%02d %02d:%02d" % (y, mo, d, hh, mi)


# ---------------- 列表页解析 ----------------

def parse_list_page(html):
    """解析 forumdisplay 页面，返回帖子摘要列表"""
    threads = []
    # 每个 <tbody id="normalthread_xxx"> 出现两次（去重用 seen）
    seen = set()
    for m in re.finditer(r'<tbody id="normalthread_(\d+)">(.*?)</tbody>',
                         html, re.S):
        tid, block = m.group(1), m.group(2)
        if tid in seen:
            continue
        seen.add(tid)

        tm = re.search(r'class="s xst">(.*?)</a>', block, re.S)
        title = clean_tags(tm.group(1)) if tm else ""
        title = re.sub(r"\s+", " ", title).strip()

        am = re.search(r'<td class="by">\s*<cite>(.*?)</cite>', block, re.S)
        author = clean_tags(am.group(1)).strip() if am else ""

        # 发帖日期：<em><span title="2026-10-15">5&nbsp;小时前</span></em>
        # 或 <em><span>2026-9-30</span></em>
        dm = re.search(r"<em>(?:<a[^>]*>)?<span[^>]*>(.*?)</span>", block, re.S)
        post_date = ""
        if dm:
            span_html = dm.group(1)
            tm2 = re.search(r'title="(\d{4}-\d{1,2}-\d{1,2})', span_html)
            if tm2:
                post_date = tm2.group(1)
            else:
                post_date = clean_tags(span_html).strip()

        nm = re.search(r'<td class="num"><a[^>]*>(\d+)</a>', block, re.S)
        replies = int(nm.group(1)) if nm else 0
        vm = re.search(r"<em>(\d+)</em>", block, re.S)
        views = int(vm.group(1)) if vm else 0

        is_activity = "activitysmall" in block

        threads.append({
            "tid": tid,
            "title": title,
            "author": author,
            "post_date": post_date,
            "replies": replies,
            "views": views,
            "is_activity": is_activity,
        })
    return threads


# ---------------- 详情页解析 ----------------

def parse_act_block(html):
    """解析活动插件结构化信息块 <div class="act pbm cl">"""
    i = html.find('<div class="act pbm cl">')
    if i < 0:
        return {}
    blk = html[i:i + 6000]
    fields = {}
    for dt, dd in re.findall(r"<dt>(.*?)</dt>\s*<dd>(.*?)</dd>", blk, re.S):
        key = clean_tags(dt).strip().rstrip(":：")
        # 时间类字段保留原始 HTML（绝对时间可能藏在 span 的 title 属性里），
        # 其余字段存纯文本
        if key in ("开始时间", "活动时间"):
            fields[key] = dd
        else:
            val = re.sub(r"\s+", " ", clean_tags(dd)).strip()
            if key and key not in fields:
                fields[key] = val
    return fields


def extract_dt_from_dd(dd_raw):
    """从 dd 原始 HTML 提取绝对时间：优先 <span title="2026-10-13 15:00">，
    其次纯文本（插件会把近期时间显示成'前天 15:00'、'9 小时前'等相对时间）"""
    if not dd_raw:
        return None
    m = re.search(r'title="(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2}(?::\d{1,2})?)"',
                  dd_raw)
    if m:
        return norm_dt(m.group(1))
    return norm_dt(clean_tags(dd_raw))


def parse_post_time(html):
    """解析首帖发帖时间：'发表于 <em>2026-9-30 17:37</em>' 或
    '发表于 <span title="2026-10-14 12:00:46">昨天 12:00</span>'"""
    i = html.find("发表于")
    if i < 0:
        return None
    seg = html[i:i + 300]
    m = re.search(r'title="(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})', seg)
    if m:
        return norm_dt(m.group(1))
    m = re.search(r"(\d{4}-\d{1,2}-\d{1,2}\s+\d{1,2}:\d{2})", seg)
    return norm_dt(m.group(1)) if m else None


def parse_first_post(html):
    """提取首帖正文纯文本（去脚本样式与插件界面，压缩空白）"""
    m = re.search(r'<td class="t_f" id="postmessage_\d+">(.*?)</td>',
                  html, re.S)
    if not m:
        m = re.search(r'<div class="pcb">(.*?)</div>', html, re.S)
    if not m:
        return ""
    frag = m.group(1)
    # 活动插件会把真实正文再嵌一层同名 t_f（外层混有插件界面），取里层
    inner = re.search(r'<td class="t_f" id="postmessage_\d+">(.*)$', frag, re.S)
    if inner:
        frag = inner.group(1)
    txt = clean_tags(frag)
    if len(txt) > CONTENT_MAX:
        txt = txt[:CONTENT_MAX].rstrip() + "…"
    return txt


def parse_thread_page(html):
    """解析详情页，返回活动字段"""
    fields = parse_act_block(html)
    out = {}

    out["activity_type"] = fields.get("活动类型", "")
    out["location"] = fields.get("活动地点", "") or fields.get("集合地点", "")
    out["signup_deadline"] = norm_dt(fields.get("报名截止", ""))
    m = re.search(r"\d+", fields.get("已报名人数", ""))
    out["signup_count"] = int(m.group(0)) if m else None
    out["post_time"] = parse_post_time(html)

    # 开始时间：'2026-10-15 19:30' 或 '2026-10-15 19:30 至 2026-10-15 21:30 商定'
    # 注意插件可能把近期时间渲染成相对时间，绝对时间在 span 的 title 属性里
    raw_time = fields.get("开始时间", "") or fields.get("活动时间", "")
    start = end = None
    if raw_time:
        parts = re.split(r"至", clean_tags(raw_time))
        raw_parts = re.split(r"至", raw_time)
        if parts:
            start = extract_dt_from_dd(raw_parts[0])
        if len(parts) > 1:
            end = extract_dt_from_dd(raw_parts[1])
    tbd = "商定" in clean_tags(raw_time)
    out["activity_start"] = start
    out["activity_end"] = end
    out["time_tbd"] = tbd

    out["content"] = parse_first_post(html)
    return out


def guess_time_from_text(text, default_year=None):
    """兜底：从文本猜活动时间。支持 20261015 / 2026年10月15日 /
    10月15日 / 10月15日-16日，可带 HH:MM。返回 (start, end)"""
    if not text:
        return None, None
    year = default_year or now_local().year

    # 完整日期（带年份）
    m = re.search(r"(20\d{2})\s*[-/年]?\s*(\d{1,2})\s*[-/月]?\s*(\d{1,2})\s*日?",
                  text)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        # 仅月日，年份取发帖年
        m = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日", text)
        if not m:
            return None, None
        y, mo, d = year, int(m.group(1)), int(m.group(2))

    # 日期后就近找时间
    tail = text[m.end():m.end() + 24] if m else ""
    tm = re.search(r"(\d{1,2}):(\d{2})", tail)
    hh = int(tm.group(1)) if tm else 0
    mi = int(tm.group(2)) if tm else 0
    start = norm_dt("%04d-%02d-%02d %02d:%02d" % (y, mo, d, hh, mi))
    if not start:
        return None, None

    # 范围：'10月15日-16日' / '15-16日' / '10月15日-17日'
    end = None
    seg = text[m.start():m.end() + 40]
    rm = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*[-—~至]\s*(?:(\d{1,2})\s*月)?(\d{1,2})\s*日", seg)
    if rm:
        emo = int(rm.group(3)) if rm.group(3) else mo
        end = norm_dt("%04d-%02d-%02d 23:59" % (y, emo, int(rm.group(4))))
    return start, end


# ---------------- 状态与数据 ----------------

def load_json(path, default):
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            log("警告：%s 解析失败，按空状态处理" % path.name)
    return default


def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    tmp.replace(path)


def parse_dt(s):
    if not s:
        return None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


def main():
    t0 = time.time()
    log("SRVF 活动抓取开始（SCAN_PAGES=%d DELAY=%.1fs）" % (SCAN_PAGES, DELAY))

    state = load_json(STATE_PATH, {"version": 1, "runs": 0, "tids": {}})
    activities = {}
    data = load_json(DATA_PATH, {"activities": []})
    for a in data.get("activities", []):
        activities[str(a.get("tid"))] = a
    log("本地已有活动 %d 条" % len(activities))

    # ---- 1. 扫描列表页 ----
    stubs = []
    for page in range(1, SCAN_PAGES + 1):
        url = FORUM_URL + ("&page=%d" % page if page > 1 else "")
        log("扫描列表第 %d 页…" % page)
        html = fetch_html(url)
        page_threads = parse_list_page(html)
        if not page_threads:
            log("第 %d 页无解析结果，停止翻页" % page)
            break
        stubs.extend(page_threads)
        new_count = sum(1 for t in page_threads if t["tid"] not in activities)
        log("第 %d 页：%d 帖（新帖 %d）" % (page, len(page_threads), new_count))
        if new_count == 0:
            log("整页无新帖，提前结束翻页")
            break
        if page < SCAN_PAGES:
            time.sleep(DELAY)

    if not stubs:
        raise RuntimeError("列表页解析结果为空，站点结构可能已变化")

    # ---- 2. 更新列表可见帖子的基础信息 ----
    today = now_local().date()
    cutoff = today - timedelta(days=KEEP_DAYS)
    detail_queue = []  # 需要抓详情页的 tid

    for stub in stubs:
        tid = stub["tid"]
        old = activities.get(tid)
        if old is None:
            detail_queue.append(tid)
            continue
        # 已知帖子：刷新列表侧字段
        old["title"] = stub["title"] or old.get("title", "")
        old["author"] = stub["author"] or old.get("author", "")
        old["replies"] = stub["replies"]
        old["views"] = stub["views"]
        # 未结束的活动定期重抓详情
        end_dt = parse_dt(old.get("activity_end")) or parse_dt(old.get("activity_start"))
        if end_dt and end_dt.date() >= today:
            fetched = parse_dt(state["tids"].get(tid, {}).get("detail_fetched_at", ""))
            if not fetched or (now_local() - fetched) >= timedelta(hours=REFRESH_HOURS):
                detail_queue.append(tid)

    # ---- 3. 抓取详情页（新帖 + 待刷新的未结束活动）----
    log("需抓取详情页 %d 个" % len(detail_queue))
    for n, tid in enumerate(detail_queue, 1):
        try:
            log("[%d/%d] 抓取 tid=%s" % (n, len(detail_queue), tid))
            html = fetch_html(BASE_URL + "/forum.php?mod=viewthread&tid=" + tid)
            info = parse_thread_page(html)
        except Exception as e:  # 单个帖子失败不中断整体
            log("  tid=%s 抓取失败：%s" % (tid, e))
            continue

        stub = next((s for s in stubs if s["tid"] == tid), None)
        old = activities.get(tid, {})
        start = info.get("activity_start")
        end = info.get("activity_end")
        if not start:
            # 兜底：依次尝试标题、正文里的时间描述
            post_year = None
            pt = info.get("post_time") or (stub.get("post_date") if stub else None)
            if pt:
                ym = re.search(r"(20\d{2})", pt)
                post_year = int(ym.group(1)) if ym else None
            title = stub["title"] if stub else ""
            start, end = guess_time_from_text(title, post_year)
            if not start:
                cstart, cend = guess_time_from_text(info.get("content", ""), post_year)
                start, end = start or cstart, end or cend

        record = {
            "tid": tid,
            "title": (stub["title"] if stub else "") or old.get("title", ""),
            "author": (stub["author"] if stub else "") or old.get("author", ""),
            "post_time": info.get("post_time")
                         or (norm_dt(stub["post_date"] + " 00:00") if stub and stub.get("post_date") else None)
                         or old.get("post_time"),
            "activity_type": info.get("activity_type", "") or old.get("activity_type", ""),
            "activity_start": start,
            "activity_end": end,
            "time_tbd": bool(info.get("time_tbd")),
            "signup_deadline": info.get("signup_deadline") or old.get("signup_deadline"),
            "signup_count": info.get("signup_count"),
            "location": info.get("location", "") or old.get("location", ""),
            "replies": stub["replies"] if stub else old.get("replies", 0),
            "views": stub["views"] if stub else old.get("views", 0),
            "post_url": BASE_URL + "/forum.php?mod=viewthread&tid=" + tid,
            "content": info.get("content", ""),
            "first_seen": old.get("first_seen", now_iso()),
            "updated_at": now_iso(),
        }
        activities[tid] = record
        state["tids"][tid] = {"detail_fetched_at": now_iso()}
        if n < len(detail_queue):
            time.sleep(DELAY)

    # ---- 4. 归档剔除 + 排序输出 ----
    kept = {}
    for tid, a in activities.items():
        end_dt = parse_dt(a.get("activity_end")) or parse_dt(a.get("activity_start"))
        if end_dt is None or end_dt.date() >= cutoff:
            kept[tid] = a
        else:
            state["tids"].pop(tid, None)
    dropped = len(activities) - len(kept)
    if dropped:
        log("归档剔除 %d 条（结束早于 %s）" % (dropped, cutoff))

    result = sorted(
        kept.values(),
        key=lambda a: parse_dt(a.get("activity_start")) or datetime(1970, 1, 1),
        reverse=True,
    )

    generated = now_iso()
    out = {
        "generated_at": generated,
        "source": {
            "name": "深圳公益救援论坛 · 活动版块",
            "url": FORUM_URL,
        },
        "count": len(result),
        "activities": result,
    }
    save_json(DATA_PATH, out)
    state["runs"] = state.get("runs", 0) + 1
    state["last_run"] = generated
    save_json(STATE_PATH, state)

    log("完成：共 %d 条活动，本次运行 %.1fs" % (len(result), time.time() - t0))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("运行失败：%s" % e)
        sys.exit(1)
