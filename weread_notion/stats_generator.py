"""
统计页面生成器
为每本书生成仿微信读书 App 风格的 HTML 阅读统计页面，
托管到 GitHub Pages 后在 Notion 中以嵌入方式展示。
"""

import json
from pathlib import Path
from typing import Optional

STATS_DIR = Path("stats")

# ── HTML 模板 ─────────────────────────────────────────────────────────────────

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>阅读统计 - ${title}</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    background: #1a1a2e;
    color: #e0e0e0;
    min-height: 100vh;
    padding: 24px 20px;
    max-width: 480px;
    margin: 0 auto;
  }

  .header {
    text-align: center;
    margin-bottom: 28px;
  }
  .cover-img {
    width: 80px;
    height: 110px;
    object-fit: cover;
    border-radius: 6px;
    box-shadow: 0 4px 20px rgba(0,0,0,0.4);
    margin-bottom: 12px;
    background: #252542;
  }
  .header h1 {
    font-size: 20px;
    font-weight: 700;
    color: #fff;
    line-height: 1.4;
  }
  .header .author {
    font-size: 13px;
    color: #7a7a9a;
    margin-top: 4px;
  }

  /* 四格统计卡片 */
  .stat-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 28px;
  }
  .stat-card {
    background: #252542;
    border-radius: 14px;
    padding: 16px 14px;
    text-align: center;
  }
  .stat-card .icon { font-size: 18px; margin-bottom: 4px; }
  .stat-card .label { font-size: 12px; color: #7a7a9a; margin-bottom: 4px; }
  .stat-card .value { font-size: 20px; font-weight: 700; color: #5BB8F5; }
  .stat-card .detail { font-size: 11px; color: #5a5a7a; margin-top: 2px; }

  /* 每日阅读 */
  .section-title {
    font-size: 15px;
    font-weight: 700;
    color: #ffd54f;
    margin-bottom: 14px;
  }
  .section-subtitle {
    font-size: 11px;
    color: #5a5a7a;
    margin-bottom: 14px;
    margin-top: -10px;
  }

  .day-row {
    display: flex;
    align-items: center;
    margin-bottom: 12px;
    gap: 10px;
  }
  .day-label {
    font-size: 13px;
    font-weight: 600;
    color: #5BB8F5;
    min-width: 48px;
  }
  .bar-wrap {
    flex: 1;
    background: #252542;
    border-radius: 8px;
    height: 26px;
    overflow: hidden;
    position: relative;
  }
  .bar-fill {
    height: 100%;
    background: linear-gradient(90deg, #3a7bd5, #5BB8F5);
    border-radius: 8px;
    transition: width 0.6s ease;
    min-width: 4px;
  }
  .bar-duration {
    position: absolute;
    right: 8px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 11px;
    font-weight: 600;
    color: rgba(255,255,255,0.85);
    white-space: nowrap;
  }

  .no-data {
    text-align: center;
    color: #5a5a7a;
    font-size: 13px;
    padding: 20px 0;
  }

  .footer {
    text-align: center;
    margin-top: 36px;
    font-size: 11px;
    color: #3a3a5a;
  }
  .footer a { color: #5a5a7a; text-decoration: none; }
</style>
</head>
<body>
  <div class="header">
    <img class="cover-img" src="${cover}" alt="${title}" onerror="this.style.display='none'">
    <h1>${title}</h1>
    <div class="author">${author}</div>
  </div>

  <div class="stat-grid">
    <div class="stat-card">
      <div class="icon">⏱</div>
      <div class="label">累计阅读</div>
      <div class="value" id="totalTime">-</div>
      <div class="detail">本书累计阅读时长</div>
    </div>
    <div class="stat-card">
      <div class="icon">📖</div>
      <div class="label">阅读进度</div>
      <div class="value" id="progress">-</div>
      <div class="detail" id="startDate">-</div>
    </div>
    <div class="stat-card">
      <div class="icon">📝</div>
      <div class="label">笔记划线</div>
      <div class="value" id="notes">-</div>
      <div class="detail">划线 + 想法 + 评价</div>
    </div>
    <div class="stat-card">
      <div class="icon">🏆</div>
      <div class="label">单日最久</div>
      <div class="value" id="bestDay">-</div>
      <div class="detail" id="bestDate">-</div>
    </div>
  </div>

  <div class="section-title">📆 近期每日阅读</div>
  <div class="section-subtitle">本月每日阅读（所有书籍合计）</div>
  <div id="dailyContainer">
    <div class="no-data">暂无本月阅读数据</div>
  </div>

  <div class="footer">
    微信读书 · 自动同步生成 · <span id="genTime"></span>
  </div>

  <script>
    const DATA = ${json_data};

    function fmtHM(sec) {
      if (!sec || sec <= 0) return '-';
      const h = Math.floor(sec / 3600);
      const m = Math.floor((sec % 3600) / 60);
      if (h > 0 && m > 0) return h + '小时' + m + '分';
      if (h > 0) return h + '小时';
      return m + '分钟';
    }

    function fmtMinutes(sec) {
      if (!sec || sec <= 0) return '-';
      const m = Math.floor(sec / 60);
      if (m >= 60) return fmtHM(sec);
      return m + '分钟';
    }

    // 累计阅读时长
    document.getElementById('totalTime').textContent =
      DATA.totalReadSec > 0 ? fmtHM(DATA.totalReadSec) : '暂无数据';

    // 阅读进度
    document.getElementById('progress').textContent =
      DATA.progress > 0 ? DATA.progress + '%' : (DATA.progress === 100 ? '100%' : '未开始');

    // 笔记划线
    document.getElementById('notes').textContent = DATA.notes;

    // 单日最久
    if (DATA.bestDay) {
      document.getElementById('bestDay').textContent = fmtMinutes(DATA.bestDay.sec);
      document.getElementById('bestDate').textContent = DATA.bestDay.date;
    }

    // 首次阅读日期
    document.getElementById('startDate').textContent =
      DATA.startDate ? '首次阅读 ' + DATA.startDate : '';

    // 生成时间
    document.getElementById('genTime').textContent = DATA.generatedAt || '';

    // 每日阅读进度条
    const records = DATA.dailyRecords || [];
    if (records.length > 0) {
      const container = document.getElementById('dailyContainer');
      container.innerHTML = '';
      const maxSec = Math.max(...records.map(r => r.sec), 1);
      records.forEach(r => {
        const pct = Math.max((r.sec / maxSec * 100), 2);
        const row = document.createElement('div');
        row.className = 'day-row';
        row.innerHTML = '<div class="day-label">' + r.date.slice(-5) + '</div>'
          + '<div class="bar-wrap">'
          + '<div class="bar-fill" style="width:' + pct + '%"></div>'
          + '<span class="bar-duration">' + fmtMinutes(r.sec) + '</span>'
          + '</div>';
        container.appendChild(row);
      });
    }
  </script>
</body>
</html>"""


def format_seconds_hm(sec: int) -> str:
    """秒数 → 'X小时Y分钟'"""
    if sec <= 0:
        return "0分钟"
    h = sec // 3600
    m = (sec % 3600) // 60
    if h > 0 and m > 0:
        return f"{h}小时{m}分钟"
    elif h > 0:
        return f"{h}小时"
    return f"{m}分钟"


GITHUB_COVER_BASE = "https://raw.githubusercontent.com/a3100821009/weread-to-notion/main/covers"


def generate_book_stats(
    book_id: str,
    title: str,
    author: str = "",
    total_read_sec: int = 0,
    progress: int = 0,
    note_count: int = 0,
    review_count: int = 0,
    start_date: str = "",
    daily_records: Optional[list[dict]] = None,
    generated_at: str = "",
) -> str:
    """
    生成单本书的阅读统计 HTML 页面。

    参数：
    - book_id: 微信读书 bookId
    - title: 书名
    - author: 作者
    - total_read_sec: 本书累计阅读时长（秒）
    - progress: 阅读进度百分比（0-100）
    - note_count: 划线/想法/书签总数
    - review_count: 评价数（已合入 note_count）
    - start_date: 首次阅读日期（YYYY-MM-DD）
    - daily_records: 每日阅读记录 [{"date": "2026-06-01", "sec": 3720}, ...]
    - generated_at: 生成时间字符串

    返回完整的 HTML 字符串。
    """
    cover_url = f"{GITHUB_COVER_BASE}/{book_id}.jpg"
    notes = note_count + review_count

    # 找出单日最久
    best_day = None
    if daily_records:
        best = max(daily_records, key=lambda r: r.get("sec", 0))
        if best.get("sec", 0) > 0:
            best_day = {"date": best["date"][-5:] if len(best["date"]) >= 5 else best["date"],
                        "sec": best["sec"]}

    data = {
        "title": title,
        "author": author or "",
        "cover": cover_url,
        "totalReadSec": total_read_sec,
        "progress": progress,
        "notes": notes if notes > 0 else 0,
        "startDate": start_date,
        "bestDay": best_day,
        "dailyRecords": [{"date": r["date"], "sec": r.get("sec", r.get("readTime", 0))}
                         for r in (daily_records or [])],
        "generatedAt": generated_at,
    }

    html = HTML_TEMPLATE.replace("${title}", _esc(title))
    html = html.replace("${author}", _esc(author or ""))
    html = html.replace("${cover}", cover_url)
    html = html.replace("${json_data}", json.dumps(data, ensure_ascii=False))

    return html


def save_book_stats(
    book_id: str,
    title: str,
    author: str = "",
    total_read_sec: int = 0,
    progress: int = 0,
    note_count: int = 0,
    review_count: int = 0,
    start_date: str = "",
    daily_records: Optional[list[dict]] = None,
    generated_at: str = "",
) -> Optional[Path]:
    """
    生成并保存单本书的统计 HTML 到 stats/<bookId>.html。
    返回保存的文件路径，失败返回 None。
    """
    try:
        STATS_DIR.mkdir(exist_ok=True)
        html = generate_book_stats(
            book_id=book_id, title=title, author=author,
            total_read_sec=total_read_sec, progress=progress,
            note_count=note_count, review_count=review_count,
            start_date=start_date, daily_records=daily_records,
            generated_at=generated_at,
        )
        path = STATS_DIR / f"{book_id}.html"
        path.write_text(html, encoding="utf-8")
        return path
    except Exception as e:
        print(f"[stats_generator] 生成 {book_id} 统计页失败: {e}")
        return None


def _esc(s: str) -> str:
    """HTML 转义（只处理关键字符）"""
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
