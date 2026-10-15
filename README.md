# SRVF 活动汇总

深圳公益救援志愿者联合会论坛（[活动版块 fid=45](http://www.srvf.cn/forum.php?mod=forumdisplay&fid=45)）的活动聚合页面。

**在线访问：<https://bd7ovy.github.io/srvf-activities/>**

## 功能

- 📋 自动聚合论坛活动版块的全部活动帖（标题、类型、活动时间、地点、报名截止、已报名人数、正文摘要）
- ⏰ 按**活动时间**或**发帖时间**排序（点击切换升/降序）
- 🗂 状态筛选：待办 / 未开始 / 进行中 / 已结束 / 全部
- 🔍 搜索标题、发帖人、地点
- 🔗 一键复制帖子链接（或标题+链接，方便转发到微信群）
- 📄 点击卡片查看活动详情与正文摘要，可跳转原帖报名

## 定时增量抓取原理

```
GitHub Actions（每 6 小时）
   └─ scripts/scrape.py
        1. 扫描列表页前 3 页（75 帖），整页无新帖即提前停止
        2. 只抓取「新发现帖子」的详情页
        3. 对「尚未结束」的活动定期重抓详情页（默认 6 小时间隔），
           以便组织方编辑时间/地点/报名截止后能同步更新
        4. 结束超过 90 天的活动归档剔除
        5. 生成 data/activities.json 并提交，GitHub Pages 自动更新
```

抓取脚本仅用 Python 标准库，无需安装依赖；请求间隔 1 秒，对论坛友好。

## 本地手动运行

```bash
python3 scripts/scrape.py            # 默认参数
SCAN_PAGES=5 python3 scripts/scrape.py   # 扫描 5 页列表
```

数据有变化后本地起个服务即可预览：

```bash
python3 -m http.server 8080
# 打开 http://127.0.0.1:8080
```

## 文件说明

| 文件 | 说明 |
| --- | --- |
| `index.html` | 前端页面（单文件，无外部依赖） |
| `scripts/scrape.py` | 抓取脚本（标准库） |
| `scripts/state.json` | 增量状态（已抓取 tid 及详情抓取时间） |
| `data/activities.json` | 生成的活动数据 |
| `.github/workflows/update.yml` | 定时任务配置 |

## 说明

- 本页面为志愿者个人制作的聚合工具，**非官方页面**，活动信息以论坛原帖为准。
- 论坛为 GBK 编码的 Discuz 站点，活动信息来自活动插件的结构化字段；插件会把近期时间显示为相对时间（如"前天 15:00"），脚本已处理其 `title` 属性中的绝对时间。
