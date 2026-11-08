#!/usr/bin/env python3
"""Obsidian のノート（notes/）から ブログページ（blog/）を生成するスクリプト。

使い方:
    pip install -r scripts/requirements.txt
    python scripts/build_blog.py

- notes/ 以下の .md ファイルが 1 記事になります（_ や . で始まるフォルダ・ファイルは対象外）。
- フロントマター（Obsidian の「プロパティ」）で title / date / tags / description /
  slug / draft を指定できます。draft: true または publish: false の記事は公開されません。
- [[ウィキリンク]]・![[画像埋め込み]]・==ハイライト==・%%コメント%%・> [!note] コールアウト
  に対応しています。
- blog/ フォルダは毎回まるごと作り直されます。直接編集しないでください。
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

import markdown
import yaml

ROOT = Path(__file__).resolve().parent.parent
NOTES = ROOT / "notes"
OUT = ROOT / "blog"
SITE_URL = "https://" + (ROOT / "CNAME").read_text(encoding="utf-8").strip() if (ROOT / "CNAME").exists() else ""
BLOG_TITLE = "OK Room ブログ"
BLOG_DESC = "おけいの日々の気づき、心理学のこと、YUKINE の石のこと。こころと人生をととのえるヒントを綴っています。"

ASSET_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".avif", ".mp3", ".m4a", ".mp4", ".pdf"}


@dataclass
class Post:
    src: Path
    title: str
    date: dt.date
    slug: str
    tags: list[str] = field(default_factory=list)
    description: str = ""
    body_md: str = ""
    html: str = ""

    @property
    def url(self) -> str:
        return quote(self.slug) + ".html"


# ---------------------------------------------------------------- helpers

def is_hidden(path: Path) -> bool:
    rel = path.relative_to(NOTES)
    return any(p.startswith((".", "_")) for p in rel.parts)


def split_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("﻿")
    if text.startswith("---"):
        m = re.match(r"^---\s*\n(.*?)\n---\s*(?:\n|$)", text, re.S)
        if m:
            try:
                meta = yaml.safe_load(m.group(1)) or {}
            except yaml.YAMLError as e:
                print(f"  ! フロントマターを読めませんでした: {e}", file=sys.stderr)
                meta = {}
            return (meta if isinstance(meta, dict) else {}), text[m.end():]
    return {}, text


def to_date(value, src: Path) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        s = value.strip().replace("/", "-").replace(".", "-")
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%dT%H:%M:%S"):
            try:
                return dt.datetime.strptime(s, fmt).date()
            except ValueError:
                pass
    # 日付がないときは git の最初のコミット日 → 今日 の順で使う
    try:
        out = subprocess.run(
            ["git", "log", "--diff-filter=A", "--follow", "--format=%cs", "--", str(src)],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.split()
        if out:
            return dt.date.fromisoformat(out[-1])
    except Exception:
        pass
    return dt.date.today()


def to_list(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = re.split(r"[,\s]+", value)
    return [str(v).lstrip("#").strip() for v in value if str(v).strip()]


def make_slug(name: str) -> str:
    s = unicodedata.normalize("NFKC", name).strip()
    s = re.sub(r"[\s/\\?#%&]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    return s or "post"


def esc(s: str) -> str:
    return html.escape(str(s), quote=True)


def plain_excerpt(md_text: str, n: int = 110) -> str:
    t = re.sub(r"```.*?```", "", md_text, flags=re.S)
    t = re.sub(r"!\[\[[^\]]*\]\]|!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[\[([^\]|]*\|)?([^\]]*)\]\]", r"\2", t)
    t = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^\s*>\s*\[![^\]]*\][+-]?", "", t, flags=re.M)
    t = re.sub(r"[#>*_`=~\-|]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t[:n] + ("…" if len(t) > n else "")


# ---------------------------------------------------------------- obsidian syntax

def convert_obsidian(md_text: str, link_map: dict[str, Post], assets: dict[str, str]) -> str:
    # %%コメント%% を削除
    md_text = re.sub(r"%%.*?%%", "", md_text, flags=re.S)

    # ![[画像.png|300]] 埋め込み
    def embed(m: re.Match) -> str:
        target, _, opt = m.group(1).partition("|")
        target = target.strip()
        name = Path(target).name
        if name in assets:
            width = f' width="{esc(opt)}"' if opt.strip().isdigit() else ""
            alt = esc(Path(name).stem if not opt or opt.strip().isdigit() else opt)
            src = esc(assets[name])
            ext = Path(name).suffix.lower()
            if ext in {".mp3", ".m4a"}:
                return f'<audio controls src="{src}"></audio>'
            if ext == ".mp4":
                return f'<video controls src="{src}"{width}></video>'
            if ext == ".pdf":
                return f'<a href="{src}">{esc(name)}</a>'
            return f'<img src="{src}" alt="{alt}"{width} loading="lazy">'
        post = link_map.get(target.split("#")[0].lower())
        if post:  # ノートの埋め込みはリンクとして扱う
            return f'[{post.title}]({post.url})'
        print(f"  ! 埋め込み先が見つかりません: {target}", file=sys.stderr)
        return ""

    md_text = re.sub(r"!\[\[([^\]]+)\]\]", embed, md_text)

    # [[ノート名#見出し|表示名]] ウィキリンク
    def wikilink(m: re.Match) -> str:
        target, _, alias = m.group(1).partition("|")
        page, _, heading = target.partition("#")
        label = alias or (heading if not page else page)
        post = link_map.get(page.strip().lower()) if page else None
        if post:
            anchor = "#" + quote(make_slug(heading).lower()) if heading else ""
            return f"[{label}]({post.url}{anchor})"
        if not page and heading:
            return f"[{label}](#{quote(make_slug(heading).lower())})"
        # 非公開・未作成のノートへのリンクは文字だけ残す
        return f'<span class="wikilink-missing">{esc(label)}</span>'

    md_text = re.sub(r"(?<!!)\[\[([^\]]+)\]\]", wikilink, md_text)

    # ==ハイライト==
    md_text = re.sub(r"(?<![=\w])==(?=\S)(.+?)(?<=\S)==(?!=)", r"<mark>\1</mark>", md_text)
    return md_text


CALLOUT_ICONS = {
    "note": "📝", "info": "💡", "tip": "🌱", "hint": "🌱", "important": "✨", "success": "🌷",
    "check": "🌷", "done": "🌷", "question": "❓", "help": "❓", "faq": "❓", "warning": "⚠️",
    "caution": "⚠️", "attention": "⚠️", "danger": "❗", "error": "❗", "example": "📎",
    "quote": "💬", "cite": "💬", "abstract": "📋", "summary": "📋", "tldr": "📋", "todo": "☑️",
}


def convert_callouts(body_html: str) -> str:
    pat = re.compile(r"<blockquote>\s*<p>\[!(\w+)\][+-]?[ \t]*([^\n<]*)(?:<br\s*/?>)?\s*", re.S)

    def repl(m: re.Match) -> str:
        kind = m.group(1).lower()
        title = m.group(2).strip() or kind.capitalize()
        icon = CALLOUT_ICONS.get(kind, "📝")
        return (f'<blockquote class="callout callout-{esc(kind)}"><p class="callout-title">'
                f'<span aria-hidden="true">{icon}</span> {title}</p><p>')

    out = pat.sub(repl, body_html)
    return out.replace("<p></p>", "")


def rewrite_asset_srcs(body_html: str, assets: dict[str, str]) -> str:
    def repl(m: re.Match) -> str:
        attr, url = m.group(1), m.group(2)
        if re.match(r"^([a-z]+:|/|#|data:)", url, re.I):
            return m.group(0)
        name = Path(url.split("?")[0]).name
        from urllib.parse import unquote
        name = unquote(name)
        if name in assets:
            return f'{attr}="{esc(assets[name])}"'
        return m.group(0)

    return re.sub(r'(src|href)="([^"]+)"', repl, body_html)


# ---------------------------------------------------------------- templates

HEART = ('<svg class="dot" viewBox="0 0 40 40" aria-hidden="true"><circle cx="20" cy="20" r="20" fill="#b7a4e6"/>'
         '<path d="M20 30c-6-4-10-7.5-10-12a5.4 5.4 0 0 1 10-2 5.4 5.4 0 0 1 10 2c0 4.5-4 8-10 12z" fill="#fff"/></svg>')
LINE_ICON = ('<svg class="li" viewBox="0 0 24 24" aria-hidden="true"><path d="M12 2C6.48 2 2 5.66 2 10.16c0 4.03 3.55 7.4 8.35 8.04.33.07.77.22.88.5.1.26.07.66.03.92l-.14.86c-.04.26-.2 1.02.9.56 1.1-.46 5.94-3.5 8.1-5.99C21.4 13.4 22 11.86 22 10.16 22 5.66 17.52 2 12 2z"/></svg>')

CSS = """
:root{--bg:#FFF7FB;--surface:#fff;--ink:#6b6280;--ink-soft:#9d95ac;--lav:#b7a4e6;--lav-deep:#7d6bc0;
--peach-deep:#f0a184;--tint-lav:#f1ecfc;--tint-peach:#fff0e8;--tint-mint:#e9f8f1;--mint-deep:#5cbf99;
--tint-rose:#fdeef4;--rose-deep:#c26f8e;--yellow:#ffe1a6;--border:#f0e7f5;
--shadow:0 14px 34px rgba(150,120,200,.14);--shadow-sm:0 8px 20px rgba(150,120,200,.10);}
*{box-sizing:border-box;}
body{margin:0;min-height:100vh;display:flex;flex-direction:column;font-family:"M PLUS Rounded 1c",system-ui,sans-serif;color:var(--ink);background:var(--bg);line-height:1.95;-webkit-font-smoothing:antialiased;}
footer{margin-top:auto;}
.round{font-family:"Zen Maru Gothic",serif;}
.wrap{width:min(1040px,92%);margin-inline:auto;}
.narrow{width:min(740px,92%);margin-inline:auto;}
a{color:var(--lav-deep);}
img{max-width:100%;height:auto;}
header.site{position:sticky;top:0;z-index:50;background:rgba(255,247,251,.86);backdrop-filter:blur(10px);border-bottom:1px solid var(--border);}
.nav{display:flex;align-items:center;justify-content:space-between;padding:14px 0;gap:16px;}
.brand{font-family:"Zen Maru Gothic",serif;font-weight:700;font-size:1.3rem;color:var(--lav-deep);text-decoration:none;display:flex;align-items:center;gap:9px;}
.brand .dot{width:26px;height:26px;flex:none;}
.brand small{display:block;font-family:"M PLUS Rounded 1c";font-weight:400;font-size:.58rem;letter-spacing:.16em;color:var(--ink-soft);margin-top:1px;}
.nav-links{display:flex;gap:22px;align-items:center;}
.nav-links a{text-decoration:none;font-size:.88rem;color:var(--ink-soft);}
.nav-links a:hover{color:var(--lav-deep);}
@media(max-width:760px){.nav-links a:not(.btn-line-sm){display:none;}}
.btn-line-sm{display:inline-flex;align-items:center;gap:9px;background:#06C755;color:#fff!important;text-decoration:none;border-radius:999px;font-weight:700;box-shadow:0 8px 18px rgba(6,199,85,.3);padding:10px 20px;font-size:.85rem;}
.li{width:20px;height:20px;flex:none;fill:#fff;}
.hero{padding:64px 0 40px;text-align:center;background:radial-gradient(620px 360px at 85% 0%,var(--tint-peach) 0%,transparent 60%),radial-gradient(620px 420px at 10% 10%,var(--tint-lav) 0%,transparent 60%);}
.eyebrow{display:inline-block;background:#fff;color:var(--lav-deep);border:1px solid var(--border);border-radius:999px;padding:6px 18px;font-size:.76rem;font-weight:500;margin-bottom:18px;box-shadow:var(--shadow-sm);letter-spacing:.12em;}
.hero h1{font-family:"Zen Maru Gothic",serif;color:var(--lav-deep);font-size:clamp(1.6rem,4.4vw,2.4rem);line-height:1.5;margin:0 0 12px;}
.hero p{margin:0 auto;max-width:600px;color:var(--ink-soft);font-size:.95rem;}
.tagbar{display:flex;flex-wrap:wrap;justify-content:center;gap:8px;margin:28px 0 0;}
.chip{display:inline-block;border:1px solid var(--border);background:#fff;color:var(--ink-soft);border-radius:999px;padding:4px 14px;font-size:.78rem;text-decoration:none;cursor:pointer;font-family:inherit;}
.chip.on,.chip:hover{background:var(--tint-lav);color:var(--lav-deep);border-color:var(--lav);}
.posts{display:grid;gap:20px;padding:20px 0 70px;}
.card{display:block;text-decoration:none;color:inherit;background:var(--surface);border:1px solid var(--border);border-radius:26px;padding:24px 28px;box-shadow:var(--shadow-sm);transition:transform .2s ease,box-shadow .2s ease;}
.card:hover{transform:translateY(-4px);box-shadow:var(--shadow);}
.card time{font-size:.78rem;color:var(--ink-soft);letter-spacing:.06em;}
.card h2{font-family:"Zen Maru Gothic",serif;color:var(--lav-deep);font-size:1.25rem;margin:2px 0 6px;line-height:1.6;}
.card p{margin:0;font-size:.9rem;color:var(--ink-soft);line-height:1.8;}
.card .tags{margin-top:10px;display:flex;flex-wrap:wrap;gap:6px;}
.card .tags span{font-size:.7rem;background:var(--tint-lav);color:var(--lav-deep);border-radius:999px;padding:2px 10px;}
.empty{text-align:center;color:var(--ink-soft);padding:40px 0;}
article{background:var(--surface);border:1px solid var(--border);border-radius:30px;box-shadow:var(--shadow-sm);padding:40px clamp(20px,5vw,52px);margin:40px auto 30px;}
article header time{font-size:.82rem;color:var(--ink-soft);letter-spacing:.06em;}
article h1{font-family:"Zen Maru Gothic",serif;color:var(--lav-deep);font-size:clamp(1.5rem,4vw,2rem);line-height:1.5;margin:4px 0 12px;}
article .tags{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:24px;}
.post-body{font-size:1rem;overflow-wrap:anywhere;}
.post-body h2,.post-body h3,.post-body h4{font-family:"Zen Maru Gothic",serif;color:var(--lav-deep);line-height:1.5;margin:2em 0 .6em;}
.post-body h2{font-size:1.35rem;padding-bottom:6px;border-bottom:2px dotted var(--border);}
.post-body h3{font-size:1.15rem;}
.post-body img{border-radius:18px;display:block;margin:1.2em auto;}
.post-body mark{background:linear-gradient(transparent 55%,var(--yellow) 55%);color:inherit;padding:0 2px;}
.post-body blockquote{margin:1.4em 0;padding:12px 20px;border-left:4px solid var(--lav);background:var(--tint-lav);border-radius:0 16px 16px 0;}
.post-body blockquote p{margin:.4em 0;}
.post-body .callout{border-left-color:var(--lav-deep);}
.post-body .callout-title{font-weight:700;color:var(--lav-deep);}
.post-body .callout-tip,.post-body .callout-success,.post-body .callout-check,.post-body .callout-done,.post-body .callout-hint{background:var(--tint-mint);border-left-color:var(--mint-deep);}
.post-body .callout-tip .callout-title,.post-body .callout-success .callout-title,.post-body .callout-check .callout-title,.post-body .callout-done .callout-title,.post-body .callout-hint .callout-title{color:var(--mint-deep);}
.post-body .callout-warning,.post-body .callout-caution,.post-body .callout-attention,.post-body .callout-danger,.post-body .callout-error{background:var(--tint-peach);border-left-color:var(--peach-deep);}
.post-body .callout-warning .callout-title,.post-body .callout-caution .callout-title,.post-body .callout-attention .callout-title,.post-body .callout-danger .callout-title,.post-body .callout-error .callout-title{color:var(--peach-deep);}
.post-body .callout-quote,.post-body .callout-cite,.post-body .callout-important{background:var(--tint-rose);border-left-color:var(--rose-deep);}
.post-body .callout-quote .callout-title,.post-body .callout-cite .callout-title,.post-body .callout-important .callout-title{color:var(--rose-deep);}
.post-body code{background:var(--tint-lav);border-radius:6px;padding:1px 6px;font-size:.9em;}
.post-body pre{background:#fbf8ff;border:1px solid var(--border);border-radius:14px;padding:14px 18px;overflow-x:auto;}
.post-body pre code{background:none;padding:0;}
.post-body table{border-collapse:collapse;width:100%;display:block;overflow-x:auto;margin:1.2em 0;}
.post-body th,.post-body td{border:1px solid var(--border);padding:8px 12px;text-align:left;}
.post-body th{background:var(--tint-lav);color:var(--lav-deep);}
.post-body hr{border:none;border-top:2px dotted var(--border);margin:2em 0;}
.post-body .wikilink-missing{color:var(--ink-soft);}
.post-body ul.task-list{list-style:none;padding-left:1em;}
.post-nav{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;margin:0 auto 60px;font-size:.9rem;}
.post-nav a{text-decoration:none;background:#fff;border:1px solid var(--border);border-radius:999px;padding:10px 20px;box-shadow:var(--shadow-sm);}
footer{background:var(--lav-deep);color:#efe9fb;padding:40px 0 28px;text-align:center;font-size:.85rem;}
footer a{color:#fff;}
footer .note{color:#c7bce8;font-size:.76rem;margin-top:12px;}
"""


def page(title: str, description: str, body: str, *, canonical: str, og_type: str = "website") -> str:
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{esc(title)}</title>
<meta name="description" content="{esc(description)}">
<meta property="og:title" content="{esc(title)}">
<meta property="og:description" content="{esc(description)}">
<meta property="og:type" content="{og_type}">
<meta property="og:url" content="{esc(canonical)}">
<link rel="canonical" href="{esc(canonical)}">
<link rel="alternate" type="application/rss+xml" title="{esc(BLOG_TITLE)}" href="feed.xml">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=M+PLUS+Rounded+1c:wght@300;400;500;700&family=Zen+Maru+Gothic:wght@400;500;700&display=swap" rel="stylesheet">
<link rel="stylesheet" href="blog.css">
</head>
<body>
<header class="site">
  <div class="wrap nav">
    <a href="../index.html" class="brand round">{HEART}OK Room<small>こころをととのえる時間</small></a>
    <nav class="nav-links">
      <a href="../index.html#psychology">心理学</a>
      <a href="../index.html#yukine">YUKINE</a>
      <a href="../life-design-essence.html">ラジオ番組</a>
      <a href="index.html">ブログ</a>
      <a class="btn-line-sm" href="https://lin.ee/xT0FdTS" target="_blank" rel="noopener">{LINE_ICON}LINEで相談</a>
    </nav>
  </div>
</header>
{body}
<footer>
  <div class="wrap">
    <div><a href="../index.html">ホーム</a> ｜ <a href="index.html">ブログ</a> ｜ <a href="../course.html">VCA心理学講座</a> ｜ <a href="../life-compass.html">Life Compass Session</a> ｜ <a href="../life-design-essence.html">ラジオ番組</a> ｜ <a href="https://lin.ee/xT0FdTS" target="_blank" rel="noopener">公式LINE</a></div>
    <p class="note">© OK Room</p>
  </div>
</footer>
</body>
</html>
"""


def tag_chips(tags: list[str]) -> str:
    return "".join(f'<span class="chip">#{esc(t)}</span>' for t in tags)


def render_post(post: Post, prev: Post | None, nxt: Post | None) -> str:
    nav = '<nav class="post-nav narrow">'
    nav += f'<a href="{prev.url}">← {esc(prev.title)}</a>' if prev else "<span></span>"
    nav += f'<a href="{nxt.url}">{esc(nxt.title)} →</a>' if nxt else '<a href="index.html">記事一覧へ</a>'
    nav += "</nav>"
    body = f"""
<main class="narrow">
  <article>
    <header>
      <time datetime="{post.date.isoformat()}">{post.date.strftime('%Y.%m.%d')}</time>
      <h1>{esc(post.title)}</h1>
      <div class="tags">{tag_chips(post.tags)}</div>
    </header>
    <div class="post-body">
{post.html}
    </div>
  </article>
</main>
{nav}
"""
    return page(f"{post.title} ｜ {BLOG_TITLE}", post.description, body,
                canonical=f"{SITE_URL}/blog/{post.url}", og_type="article")


def render_index(posts: list[Post]) -> str:
    all_tags = sorted({t for p in posts for t in p.tags})
    chips = "".join(f'<button class="chip" type="button" data-tag="{esc(t)}">#{esc(t)}</button>' for t in all_tags)
    tagbar = (f'<div class="tagbar"><button class="chip on" type="button" data-tag="">すべて</button>{chips}</div>'
              if all_tags else "")
    cards = []
    for p in posts:
        tags = "".join(f"<span>#{esc(t)}</span>" for t in p.tags)
        cards.append(
            f'<a class="card" href="{p.url}" data-tags="{esc(json.dumps(p.tags, ensure_ascii=False))}">'
            f'<time datetime="{p.date.isoformat()}">{p.date.strftime("%Y.%m.%d")}</time>'
            f'<h2>{esc(p.title)}</h2><p>{esc(p.description)}</p>'
            f'{f"<div class=tags>{tags}</div>" if tags else ""}</a>')
    listing = "\n".join(cards) if cards else '<p class="empty">まだ記事がありません。</p>'
    body = f"""
<section class="hero">
  <div class="wrap">
    <span class="eyebrow">BLOG ／ ブログ</span>
    <h1 class="round">{esc(BLOG_TITLE)}</h1>
    <p>{esc(BLOG_DESC)}</p>
    {tagbar}
  </div>
</section>
<main class="narrow posts">
{listing}
</main>
<script>
document.querySelectorAll('.tagbar .chip').forEach(function(btn){{
  btn.addEventListener('click', function(){{
    var tag = btn.dataset.tag;
    document.querySelectorAll('.tagbar .chip').forEach(function(b){{ b.classList.toggle('on', b === btn); }});
    document.querySelectorAll('.posts .card').forEach(function(card){{
      var tags = JSON.parse(card.dataset.tags || '[]');
      card.style.display = (!tag || tags.indexOf(tag) !== -1) ? '' : 'none';
    }});
  }});
}});
</script>
"""
    return page(BLOG_TITLE, BLOG_DESC, body, canonical=f"{SITE_URL}/blog/")


def render_feed(posts: list[Post]) -> str:
    items = []
    for p in posts[:30]:
        link = f"{SITE_URL}/blog/{p.url}"
        pub = dt.datetime.combine(p.date, dt.time(9, 0), tzinfo=dt.timezone(dt.timedelta(hours=9)))
        items.append(f"""  <item>
    <title>{esc(p.title)}</title>
    <link>{esc(link)}</link>
    <guid>{esc(link)}</guid>
    <pubDate>{pub.strftime('%a, %d %b %Y %H:%M:%S %z')}</pubDate>
    <description>{esc(p.description)}</description>
  </item>""")
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
  <title>{esc(BLOG_TITLE)}</title>
  <link>{esc(SITE_URL)}/blog/</link>
  <description>{esc(BLOG_DESC)}</description>
  <language>ja</language>
{chr(10).join(items)}
</channel>
</rss>
"""


# ---------------------------------------------------------------- main

def main() -> int:
    if not NOTES.exists():
        print("notes/ フォルダがありません。", file=sys.stderr)
        return 1

    # 画像などの添付ファイル（ファイル名 → blog/ からの相対パス）
    assets: dict[str, str] = {}
    asset_files: dict[str, Path] = {}
    for f in sorted(NOTES.rglob("*")):
        if f.is_file() and f.suffix.lower() in ASSET_EXTS and not f.relative_to(NOTES).parts[0].startswith("."):
            if f.name in asset_files:
                print(f"  ! 同じ名前の添付ファイルが複数あります（後のものを使います）: {f.name}", file=sys.stderr)
            asset_files[f.name] = f
            assets[f.name] = "assets/" + quote(f.name)

    # 記事を読み込む
    posts: list[Post] = []
    for f in sorted(NOTES.rglob("*.md")):
        if is_hidden(f):
            continue
        meta, body = split_frontmatter(f.read_text(encoding="utf-8"))
        if meta.get("draft") is True or meta.get("publish") is False:
            print(f"  - 下書きのためスキップ: {f.relative_to(NOTES)}")
            continue
        title = str(meta.get("title") or f.stem)
        post = Post(
            src=f,
            title=title,
            date=to_date(meta.get("date") or meta.get("created"), f),
            slug=make_slug(str(meta.get("slug") or f.stem)),
            tags=to_list(meta.get("tags")),
            body_md=body,
        )
        post.description = str(meta.get("description") or plain_excerpt(body))
        posts.append(post)

    # slug の重複チェック
    seen: dict[str, Post] = {}
    for p in posts:
        if p.slug in seen:
            n = 2
            while f"{p.slug}-{n}" in seen:
                n += 1
            print(f"  ! URL が重複したため変更: {p.slug} → {p.slug}-{n}", file=sys.stderr)
            p.slug = f"{p.slug}-{n}"
        seen[p.slug] = p

    posts.sort(key=lambda p: (p.date, p.title), reverse=True)

    # [[リンク]] 解決用（ファイル名・タイトル・エイリアスで引けるように）
    link_map: dict[str, Post] = {}
    for p in posts:
        link_map[p.src.stem.lower()] = p
        link_map[str(p.src.relative_to(NOTES).with_suffix("")).lower()] = p
        link_map.setdefault(p.title.lower(), p)

    md = markdown.Markdown(
        extensions=["extra", "nl2br", "sane_lists", "toc"],
        extension_configs={"toc": {"slugify": lambda v, sep: make_slug(v).lower()}},
        output_format="html",
    )
    for p in posts:
        src = convert_obsidian(p.body_md, link_map, assets)
        # タスクリスト
        src = re.sub(r"^(\s*[-*+]) \[ \] ", r"\1 ☐ ", src, flags=re.M)
        src = re.sub(r"^(\s*[-*+]) \[[xX]\] ", r"\1 ☑ ", src, flags=re.M)
        md.reset()
        out = md.convert(src)
        out = convert_callouts(out)
        p.html = rewrite_asset_srcs(out, assets)

    # 出力（blog/ を作り直す）
    if OUT.exists():
        shutil.rmtree(OUT)
    (OUT / "assets").mkdir(parents=True)
    for name, f in asset_files.items():
        shutil.copy2(f, OUT / "assets" / name)
    (OUT / "blog.css").write_text(CSS.strip() + "\n", encoding="utf-8")
    for i, p in enumerate(posts):
        newer = posts[i - 1] if i > 0 else None
        older = posts[i + 1] if i + 1 < len(posts) else None
        (OUT / f"{p.slug}.html").write_text(render_post(p, older, newer), encoding="utf-8")
    (OUT / "index.html").write_text(render_index(posts), encoding="utf-8")
    (OUT / "feed.xml").write_text(render_feed(posts), encoding="utf-8")

    print(f"✔ {len(posts)} 件の記事を blog/ に書き出しました。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
