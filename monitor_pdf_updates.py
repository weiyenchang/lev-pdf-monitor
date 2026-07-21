#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
monitor_pdf_updates.py

只監控電動機車補助網下載頁面中，屬於以下三大類別的 PDF：
  1. 電動機車測試申請程序與相關表單
  2. 經濟部受認可電動機車申請表
  3. 電動機車能源補充設施補助作業流程及相關申請文件

對於「內容變更」的檔案，會擷取 PDF 文字並逐行比對，標示實際新增/刪除的內容。

每次執行都會：
  1. 更新 pdf_state.json       （每個 PDF 的雜湊 / Header 資訊）
  2. 更新 pdf_text_cache.json  （每個 PDF 最新擷取到的文字）
  3. 更新 history.json         （每日比對紀錄，含文字 diff）
  4. 更新 discovered_links_debug.json （本次在頁面上找到的「所有」PDF 連結
     與分類結果，供人工核對關鍵字規則是否正確 —— 非常建議第一次執行後
     打開這個檔案檢查一次）
  5. 重新產生 docs/index.html  （靜態頁面）
"""

import os
import io
import json
import hashlib
import difflib
import html as html_lib
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader

# ------------------------------------------------------------------
# 基本設定
# ------------------------------------------------------------------
TARGET_URL = "https://meid.nat.gov.tw/lev/regulations/download"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "pdf_state.json")
TEXT_CACHE_FILE = os.path.join(BASE_DIR, "pdf_text_cache.json")
HISTORY_FILE = os.path.join(BASE_DIR, "history.json")
DEBUG_FILE = os.path.join(BASE_DIR, "discovered_links_debug.json")
DOCS_DIR = os.path.join(BASE_DIR, "docs")
OUTPUT_HTML = os.path.join(DOCS_DIR, "index.html")

MAX_HISTORY_DAYS = 180
MAX_DIFF_LINES = 40

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0 Safari/537.36"
    )
}
REQUEST_TIMEOUT = 30
TW_TZ = timezone(timedelta(hours=8))

# ------------------------------------------------------------------
# 監控類別與比對關鍵字
#
# 分類方式：抓到每個 PDF 連結時，同時記錄該連結在頁面上「最近的標題／
# 段落文字」(heading)，然後用下面的關鍵字比對 heading + 連結文字(name)，
# 只要任一關鍵字出現在其中，就歸類到該類別、納入監控範圍。
#
# ⚠️ 第一次執行後，請務必打開 discovered_links_debug.json 核對：
#    - 有沒有「應該要被監控卻沒被抓到」的檔案 → 補上對應關鍵字
#    - 有沒有「不該被監控卻被抓進來」的檔案 → 關鍵字太寬鬆，需收斂
# ------------------------------------------------------------------
CATEGORY_KEYWORDS = {
    "電動機車測試申請程序與相關表單": [
        "測試申請", "測試程序", "測試作業", "測試表", "測試申請書",
    ],
    "經濟部受認可電動機車申請表": [
        "受認可電動機車申請", "受認可申請表", "電動機車認可申請",
        "受認可電動機車", "認可電動機車申請表",
    ],
    "電動機車能源補充設施補助作業流程及相關申請文件": [
        "能源補充設施", "充電站", "換電站", "能源補充", "設施補助",
    ],
}


def classify_link(name: str, heading: str):
    """回傳符合的類別名稱；不符合任何類別則回傳 None。"""
    combined = f"{heading} {name}"
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(kw in combined for kw in keywords):
            return category
    return None


# ------------------------------------------------------------------
# 1. 取得頁面上所有 PDF 連結，並標記最近的標題文字
# ------------------------------------------------------------------
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def fetch_all_pdf_links(page_url: str) -> list:
    """回傳頁面上『所有』PDF 連結，附上最近標題與分類結果（含未分類的）。"""
    resp = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    current_heading = ""

    for el in soup.find_all(True):
        if el.name in HEADING_TAGS:
            text = el.get_text(strip=True)
            if text:
                current_heading = text
            continue

        if el.name == "a" and el.get("href", "").strip().lower().endswith(".pdf"):
            href = el["href"].strip()
            abs_url = urljoin(page_url, href)
            name = el.get_text(strip=True) or os.path.basename(abs_url)
            category = classify_link(name, current_heading)
            results.append({
                "url": abs_url,
                "name": name,
                "heading": current_heading,
                "category": category,
                "matched": category is not None,
            })

    return results


def fetch_pdf_links(page_url: str) -> dict:
    """回傳「只保留三大類別」的 {url: {name, category}} 字典，同時把
    完整清單（含未分類）寫入 debug 檔，供人工核對。"""
    all_links = fetch_all_pdf_links(page_url)
    save_json(DEBUG_FILE, {
        "checked_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        "total_found": len(all_links),
        "total_matched": sum(1 for l in all_links if l["matched"]),
        "links": all_links,
    })

    matched = {
        l["url"]: {"name": l["name"], "category": l["category"]}
        for l in all_links if l["matched"]
    }
    return matched


# ------------------------------------------------------------------
# 2. 下載 / Hash / Header / 文字擷取
# ------------------------------------------------------------------
def get_head_info(url: str) -> dict:
    try:
        resp = requests.head(
            url, headers=HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
        )
        return {
            "content_length": resp.headers.get("Content-Length"),
            "last_modified": resp.headers.get("Last-Modified"),
            "etag": resp.headers.get("ETag"),
        }
    except requests.RequestException:
        return {"content_length": None, "last_modified": None, "etag": None}


def download_file(url: str) -> bytes:
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    return resp.content


def hash_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def extract_pdf_text(content: bytes) -> str:
    try:
        reader = PdfReader(io.BytesIO(content))
        return "\n".join((page.extract_text() or "") for page in reader.pages)
    except Exception:
        return ""


# ------------------------------------------------------------------
# 3. 狀態讀寫
# ------------------------------------------------------------------
def load_json(path: str, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: str, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ------------------------------------------------------------------
# 4. 文字 diff
# ------------------------------------------------------------------
def normalize_lines(text: str) -> list:
    return [line.strip() for line in text.splitlines() if line.strip()]


def make_text_diff(old_text: str, new_text: str):
    old_lines = normalize_lines(old_text)
    new_lines = normalize_lines(new_text)

    if old_lines == new_lines:
        return None

    raw_diff = list(difflib.unified_diff(old_lines, new_lines, lineterm=""))
    changes = [
        line for line in raw_diff
        if (line.startswith("+") or line.startswith("-"))
        and not line.startswith("+++") and not line.startswith("---")
    ]
    truncated = len(changes) > MAX_DIFF_LINES
    return {
        "lines": changes[:MAX_DIFF_LINES],
        "truncated": truncated,
        "total_changes": len(changes),
    }


# ------------------------------------------------------------------
# 5. 主要比對邏輯
# ------------------------------------------------------------------
def check_updates():
    old_state = load_json(STATE_FILE, {})
    old_text_cache = load_json(TEXT_CACHE_FILE, {})
    current_links = fetch_pdf_links(TARGET_URL)  # 已過濾成三大類別

    new_state = {}
    new_text_cache = {}
    added, removed, changed = [], [], []

    old_urls = set(old_state.keys())
    current_urls = set(current_links.keys())

    # --- 新增的檔案 ---
    for url in current_urls - old_urls:
        info = current_links[url]
        name, category = info["name"], info["category"]
        content = download_file(url)
        file_hash = hash_bytes(content)
        text = extract_pdf_text(content)
        head_info = get_head_info(url)

        new_state[url] = {
            "name": name, "category": category, "hash": file_hash,
            "content_length": head_info["content_length"],
            "last_modified": head_info["last_modified"],
            "etag": head_info["etag"],
            "checked_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        }
        new_text_cache[url] = text
        added.append({"name": name, "url": url, "category": category})

    # --- 消失的檔案（原本在監控範圍內，這次抓不到了）---
    for url in old_urls - current_urls:
        old_info = old_state[url]
        removed.append({
            "name": old_info.get("name", url),
            "url": url,
            "category": old_info.get("category"),
        })

    # --- 既有檔案：一律下載後用雜湊值判斷是否變更 ---
    #
    # 注意：這個網站有個特性——PDF 內容會變動，但 Content-Length /
    # Last-Modified / ETag 等 Header 資訊不會跟著變。因此不能再用
    # 「Header 沒變就跳過下載」這種快篩，否則會漏掉真正的內容變更。
    # 改成每次都下載、用檔案內容本身的 SHA-256 雜湊值來判斷有沒有變化，
    # Header 資訊只留作紀錄參考，不影響判斷邏輯。
    for url in current_urls & old_urls:
        info = current_links[url]
        name, category = info["name"], info["category"]
        old_info = old_state[url]

        content = download_file(url)
        file_hash = hash_bytes(content)
        head_info = get_head_info(url)  # 僅供記錄，不用來判斷是否略過

        if file_hash == old_info.get("hash"):
            new_state[url] = {
                "name": name, "category": category, "hash": file_hash,
                "content_length": head_info["content_length"],
                "last_modified": head_info["last_modified"],
                "etag": head_info["etag"],
                "checked_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
            }
            new_text_cache[url] = old_text_cache.get(url, "")
            continue

        new_text = extract_pdf_text(content)
        old_text = old_text_cache.get(url)

        new_state[url] = {
            "name": name, "category": category, "hash": file_hash,
            "content_length": head_info["content_length"],
            "last_modified": head_info["last_modified"],
            "etag": head_info["etag"],
            "checked_at": datetime.now(TW_TZ).isoformat(timespec="seconds"),
        }
        new_text_cache[url] = new_text

        if old_text is None:
            changed.append({
                "name": name, "url": url, "category": category,
                "diff": None, "note": "no_baseline",
            })
        else:
            diff = make_text_diff(old_text, new_text)
            if diff is None:
                continue
            if not new_text.strip() or not old_text.strip():
                changed.append({
                    "name": name, "url": url, "category": category,
                    "diff": None, "note": "no_text_layer",
                })
            else:
                changed.append({
                    "name": name, "url": url, "category": category,
                    "diff": diff, "note": None,
                })

    save_json(STATE_FILE, new_state)
    save_json(TEXT_CACHE_FILE, new_text_cache)
    return added, removed, changed


# ------------------------------------------------------------------
# 6. 歷史紀錄
# ------------------------------------------------------------------
def record_history(added, removed, changed) -> list:
    history = load_json(HISTORY_FILE, [])
    today = datetime.now(TW_TZ).strftime("%Y-%m-%d")
    now_str = datetime.now(TW_TZ).strftime("%H:%M")

    entry = {
        "date": today, "checked_at": now_str,
        "added": added, "removed": removed, "changed": changed,
        "has_update": bool(added or removed or changed),
    }

    history = [h for h in history if h["date"] != today]
    history.append(entry)
    history.sort(key=lambda h: h["date"], reverse=True)
    history = history[:MAX_HISTORY_DAYS]

    save_json(HISTORY_FILE, history)
    return history


# ------------------------------------------------------------------
# 7. HTML 產生
# ------------------------------------------------------------------
def esc(text: str) -> str:
    return html_lib.escape(text or "")


CATEGORY_SHORT = {
    "電動機車測試申請程序與相關表單": "測試申請",
    "經濟部受認可電動機車申請表": "受認可申請表",
    "電動機車能源補充設施補助作業流程及相關申請文件": "能源補充設施補助",
}


def category_badge(category: str) -> str:
    label = CATEGORY_SHORT.get(category, category or "未分類")
    return f'<span class="cat-badge">{esc(label)}</span>'


def render_diff_block(diff: dict) -> str:
    rows = []
    for line in diff["lines"]:
        sign = line[0]
        content = esc(line[1:].strip())
        tone = "diff-add" if sign == "+" else "diff-remove"
        marker = "+" if sign == "+" else "－"
        rows.append(f'<div class="diff-line {tone}"><span class="diff-marker">{marker}</span>{content}</div>')
    body = "\n".join(rows)
    footer = ""
    if diff["truncated"]:
        footer = f'<p class="diff-more">僅顯示前 {MAX_DIFF_LINES} 行差異，實際共 {diff["total_changes"]} 行變動，完整內容請開啟原始 PDF 比對。</p>'
    return f'<div class="diff-block">{body}</div>{footer}'


def render_simple_list(label: str, files: list, tone: str) -> str:
    if not files:
        return ""
    items = "\n".join(
        f'<li>{category_badge(f.get("category"))}<span class="fname">{esc(f["name"])}</span>'
        f'<a class="furl" href="{esc(f["url"])}" target="_blank" rel="noopener">開啟 PDF ↗</a></li>'
        for f in files
    )
    return f"""
      <div class="diff-group diff-group--{tone}">
        <p class="diff-label">{label}<span class="diff-count">{len(files)}</span></p>
        <ul class="file-list">{items}</ul>
      </div>"""


def render_changed_list(changed: list) -> str:
    if not changed:
        return ""
    blocks = []
    for c in changed:
        note_text = ""
        if c["note"] == "no_baseline":
            note_text = '<p class="diff-note">首次為此檔案建立文字快取，暫無先前版本可逐行比對，下次異動時即可看到差異。</p>'
        elif c["note"] == "no_text_layer":
            note_text = '<p class="diff-note">此檔案可能為掃描檔或無文字層，無法自動比對文字內容，請開啟原始 PDF 確認差異。</p>'

        diff_html = render_diff_block(c["diff"]) if c["diff"] else ""

        blocks.append(f"""
        <div class="changed-file">
          <div class="changed-file__head">
            {category_badge(c.get("category"))}
            <span class="fname">{esc(c["name"])}</span>
            <a class="furl" href="{esc(c["url"])}" target="_blank" rel="noopener">開啟 PDF ↗</a>
          </div>
          {note_text}
          {diff_html}
        </div>""")

    return f"""
      <div class="diff-group diff-group--changed">
        <p class="diff-label">內容更新<span class="diff-count">{len(changed)}</span></p>
        {"".join(blocks)}
      </div>"""


def render_entry(entry: dict) -> str:
    date_obj = datetime.strptime(entry["date"], "%Y-%m-%d")
    weekday_map = ["一", "二", "三", "四", "五", "六", "日"]
    weekday = weekday_map[date_obj.weekday()]

    has_update = entry["has_update"]
    stamp_class = "stamp--active" if has_update else "stamp--quiet"
    stamp_text = "異動" if has_update else "無異動"

    counts = []
    if entry["added"]:
        counts.append(f'新增 {len(entry["added"])}')
    if entry["removed"]:
        counts.append(f'移除 {len(entry["removed"])}')
    if entry["changed"]:
        counts.append(f'更新 {len(entry["changed"])}')
    count_text = "・".join(counts) if counts else "本日內容與前次相同"

    if has_update:
        body = (
            render_simple_list("新增檔案", entry["added"], "added")
            + render_simple_list("移除檔案", entry["removed"], "removed")
            + render_changed_list(entry["changed"])
        )
    else:
        body = '<p class="no-change">與前一次檢查內容一致，未發現新增、移除或更新的 PDF 檔案。</p>'

    return f"""
    <details class="ledger-entry" {"open" if has_update else ""}>
      <summary>
        <div class="entry-date">
          <span class="entry-day">{date_obj.day:02d}</span>
          <span class="entry-month">{date_obj.strftime("%Y年%m月")} ・ 星期{weekday}</span>
        </div>
        <div class="entry-mid">
          <span class="stamp {stamp_class}">{stamp_text}</span>
          <span class="entry-summary">{esc(count_text)}</span>
        </div>
        <span class="entry-time">檢查時間 {entry["checked_at"]}</span>
      </summary>
      <div class="entry-body">{body}</div>
    </details>"""


def render_html(history: list) -> str:
    total_days = len(history)
    update_days = sum(1 for h in history if h["has_update"])
    latest_check = history[0]["checked_at"] if history else "—"
    latest_date = history[0]["date"] if history else "—"

    entries_html = "\n".join(render_entry(h) for h in history) if history else \
        '<p class="empty">尚無歷史紀錄，請稍後再檢查。</p>'

    category_list_html = "".join(
        f'<li>{esc(cat)}</li>' for cat in CATEGORY_KEYWORDS.keys()
    )

    return f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>電動機車補助網 ・ PDF 異動公報</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Noto+Serif+TC:wght@500;700;900&family=Noto+Sans+TC:wght@400;500;700&family=IBM+Plex+Mono:wght@400;500&display=swap" rel="stylesheet">
<style>
  :root {{
    --paper: #EFEDE6; --paper-line: #DCD8CC; --ink: #1C2B3A; --ink-soft: #4B5A6B;
    --seal: #A6362C; --seal-soft: #E7D6D2; --sage: #4C6B54; --sage-soft: #DCE4DC;
    --amber: #8A6A2E; --amber-soft: #EAE0C8;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; background: var(--paper);
    background-image: repeating-linear-gradient(0deg, rgba(28,43,58,0.025) 0px, rgba(28,43,58,0.025) 1px, transparent 1px, transparent 3px);
    color: var(--ink); font-family: "Noto Sans TC", sans-serif; -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 780px; margin: 0 auto; padding: 56px 24px 120px; }}
  header.masthead {{ border-top: 3px solid var(--ink); border-bottom: 1px solid var(--ink); padding: 20px 0 18px; margin-bottom: 8px; }}
  .eyebrow {{ font-family: "IBM Plex Mono", monospace; font-size: 12px; letter-spacing: 0.12em; color: var(--ink-soft); display: flex; justify-content: space-between; margin-bottom: 10px; }}
  h1 {{ font-family: "Noto Serif TC", serif; font-weight: 900; font-size: 34px; line-height: 1.3; margin: 0 0 8px; }}
  .subtitle {{ font-size: 14px; color: var(--ink-soft); margin: 0 0 14px; }}
  .subtitle a {{ color: var(--ink-soft); }}
  .scope-list {{ margin: 0; padding-left: 18px; font-size: 12.5px; color: var(--ink-soft); line-height: 1.9; }}
  .stats-row {{
    display: flex; gap: 28px; margin: 28px 0 40px; padding: 16px 0;
    border-top: 1px solid var(--paper-line); border-bottom: 1px solid var(--paper-line);
    font-family: "IBM Plex Mono", monospace; flex-wrap: wrap;
  }}
  .stat .num {{ font-family: "Noto Serif TC", serif; font-size: 26px; font-weight: 700; display: block; }}
  .stat .label {{ font-size: 11px; color: var(--ink-soft); letter-spacing: 0.06em; }}
  .ledger-entry {{ border-bottom: 1px solid var(--paper-line); }}
  .ledger-entry summary {{
    list-style: none; cursor: pointer; display: grid; grid-template-columns: 96px 1fr auto;
    align-items: center; gap: 16px; padding: 18px 4px;
  }}
  .ledger-entry summary::-webkit-details-marker {{ display: none; }}
  .ledger-entry summary:hover {{ background: rgba(28,43,58,0.03); }}
  .entry-date {{ font-family: "Noto Serif TC", serif; }}
  .entry-day {{ font-size: 28px; font-weight: 700; display: block; line-height: 1; }}
  .entry-month {{ font-size: 11px; color: var(--ink-soft); display: block; margin-top: 4px; font-family: "IBM Plex Mono", monospace; }}
  .entry-mid {{ display: flex; align-items: center; gap: 12px; }}
  .stamp {{
    font-family: "Noto Serif TC", serif; font-weight: 700; font-size: 13px; width: 52px; height: 52px;
    border-radius: 50%; display: flex; align-items: center; justify-content: center;
    border: 2px solid currentColor; transform: rotate(-8deg); flex-shrink: 0;
  }}
  .stamp--active {{ color: var(--seal); background: var(--seal-soft); }}
  .stamp--quiet {{ color: var(--sage); background: var(--sage-soft); opacity: 0.75; }}
  .entry-summary {{ font-size: 14px; color: var(--ink-soft); }}
  .entry-time {{ font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-soft); white-space: nowrap; }}
  .entry-body {{ padding: 4px 4px 24px 112px; }}
  .no-change {{ font-size: 13px; color: var(--ink-soft); margin: 0; }}
  .diff-group {{ margin-bottom: 20px; }}
  .diff-label {{ font-family: "IBM Plex Mono", monospace; font-size: 12px; font-weight: 500; margin: 0 0 10px; display: flex; align-items: center; gap: 8px; }}
  .diff-count {{ background: var(--ink); color: var(--paper); border-radius: 10px; padding: 1px 8px; font-size: 11px; }}
  .diff-group--added .diff-label {{ color: var(--sage); }}
  .diff-group--removed .diff-label {{ color: var(--seal); }}
  .diff-group--changed .diff-label {{ color: var(--amber); }}
  .cat-badge {{
    font-family: "IBM Plex Mono", monospace; font-size: 10px; color: var(--ink-soft);
    border: 1px solid var(--paper-line); border-radius: 3px; padding: 1px 6px; white-space: nowrap;
  }}
  .file-list {{ list-style: none; margin: 0; padding: 0; }}
  .file-list li {{ display: flex; align-items: baseline; gap: 10px; padding: 8px 0; border-bottom: 1px dashed var(--paper-line); font-size: 13px; }}
  .fname {{ color: var(--ink); flex: 1; }}
  .furl {{ font-family: "IBM Plex Mono", monospace; font-size: 11px; color: var(--ink-soft); text-decoration: none; white-space: nowrap; flex-shrink: 0; }}
  .furl:hover {{ color: var(--seal); }}
  .changed-file {{ background: rgba(255,255,255,0.5); border: 1px solid var(--paper-line); border-radius: 4px; padding: 14px 16px; margin-bottom: 12px; }}
  .changed-file__head {{ display: flex; align-items: baseline; gap: 10px; margin-bottom: 10px; font-size: 13px; }}
  .changed-file__head .fname {{ flex: 1; }}
  .diff-note {{ font-size: 12px; color: var(--ink-soft); margin: 0; }}
  .diff-block {{ font-family: "IBM Plex Mono", monospace; font-size: 12.5px; line-height: 1.7; background: #fff; border-radius: 3px; overflow: hidden; border: 1px solid var(--paper-line); }}
  .diff-line {{ padding: 2px 12px; white-space: pre-wrap; word-break: break-word; }}
  .diff-marker {{ display: inline-block; width: 16px; font-weight: 700; }}
  .diff-add {{ background: var(--sage-soft); color: #2E4A34; }}
  .diff-remove {{ background: var(--seal-soft); color: #6E2620; text-decoration: line-through; text-decoration-color: rgba(110,38,32,0.4); }}
  .diff-more {{ font-size: 11px; color: var(--ink-soft); margin: 8px 0 0; }}
  footer {{ margin-top: 48px; padding-top: 16px; border-top: 1px solid var(--paper-line); font-size: 12px; color: var(--ink-soft); font-family: "IBM Plex Mono", monospace; }}
  @media (max-width: 600px) {{
    .ledger-entry summary {{ grid-template-columns: 64px 1fr; }}
    .entry-time {{ grid-column: 1 / -1; }}
    .entry-body {{ padding-left: 76px; }}
    h1 {{ font-size: 26px; }}
  }}
</style>
</head>
<body>
  <div class="wrap">
    <header class="masthead">
      <div class="eyebrow">
        <span>PDF 異動公報 ・ AUTO-GENERATED</span>
        <span>NO.{total_days:04d}</span>
      </div>
      <h1>電動機車補助網<br>指定類別 PDF 異動紀錄</h1>
      <p class="subtitle">
        每日自動比對
        <a href="{esc(TARGET_URL)}" target="_blank" rel="noopener">{esc(TARGET_URL)}</a>
        中下列三類文件的文字內容：
      </p>
      <ul class="scope-list">{category_list_html}</ul>
    </header>

    <div class="stats-row">
      <div class="stat"><span class="num">{total_days}</span><span class="label">追蹤天數</span></div>
      <div class="stat"><span class="num">{update_days}</span><span class="label">發現異動天數</span></div>
      <div class="stat"><span class="num">{latest_date}</span><span class="label">最近檢查日期</span></div>
      <div class="stat"><span class="num">{latest_check}</span><span class="label">最近檢查時間</span></div>
    </div>

    <main>
      {entries_html}
    </main>

    <footer>本頁面由 GitHub Actions 每日自動產生・僅監控指定三大類別文件・文字比對僅供快速預覽，正式內容請以原始 PDF 為準</footer>
  </div>
</body>
</html>"""


# ------------------------------------------------------------------
# 主程式
# ------------------------------------------------------------------
def main():
    added, removed, changed = check_updates()
    history = record_history(added, removed, changed)

    os.makedirs(DOCS_DIR, exist_ok=True)
    html_content = render_html(history)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_content)

    if added or removed or changed:
        print(f"發現異動：新增 {len(added)}、移除 {len(removed)}、更新 {len(changed)}")
    else:
        print("本次檢查無異動。")
    print(f"頁面已產生：{OUTPUT_HTML}")
    print(f"分類除錯清單已寫入：{DEBUG_FILE}（建議第一次執行後檢查）")


if __name__ == "__main__":
    main()
