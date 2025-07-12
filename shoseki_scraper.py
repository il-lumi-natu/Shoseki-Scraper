#!/usr/bin/env python3
"""
Shoseki Weekly Rankings Scraper with Sales Estimation

- Fetches the latest Weekly Shoseki article from category 6.
- Parses the baseline "おおまかな実売目安" line to build a piece‑wise linear
  sales estimator.
- Extracts the 1‑500 ranking table that lives in <div class="entry_body">.
- Retrieves official English (or Romaji) titles in *batched* queries to
  AniList's GraphQL API (≤ 50 aliases per request).
- Falls back to a synchronous machine translation (deep‑translator) for
  titles missing on AniList.

Dependencies:
    pip install requests beautifulsoup4 lxml deep-translator tqdm
"""

from __future__ import annotations
import re
import requests
from bs4 import BeautifulSoup
from typing import List, Tuple, Dict, Optional
from deep_translator import GoogleTranslator
from tqdm import tqdm
from argparse import ArgumentParser
import calendar

WEEKLY_CATEGORY_URL = "http://shosekiranking.blog.fc2.com/blog-category-6.html"
MONTHLY_CATEGORY_URL = "http://shosekiranking.blog.fc2.com/blog-category-4.html"
UA_HEADER = {"User-Agent": "Mozilla/5.0"}
ALIAS_BATCH = 50  # safe alias limit per AniList query


# ---------------------------------------------------------------------------
# 1.  Generic helpers
# ---------------------------------------------------------------------------

def _get_soup(url: str) -> BeautifulSoup:
    """Return a BeautifulSoup parsed representation of *url*."""
    resp = requests.get(url, headers=UA_HEADER, timeout=10)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "lxml")


def _latest_article_url(use_monthly: bool = False) -> Tuple[str, str]:
    """Grab the first blog‑entry‑####.html link from the category page.
    
    Args:
        use_monthly: If True, use monthly category URL, otherwise weekly
        
    Returns:
        Tuple of (article_url, category_type)
    """
    category_url = MONTHLY_CATEGORY_URL if use_monthly else WEEKLY_CATEGORY_URL
    category_type = "monthly" if use_monthly else "weekly"
    
    soup = _get_soup(category_url)
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if re.search(r"blog-entry-\d+\.html$", href):
            return href, category_type
    raise RuntimeError("No article link found on category page.")


# ---------------------------------------------------------------------------
# 2.  JP → EN title resolution (AniList batched + MT fallback)
# ---------------------------------------------------------------------------

def _clean_for_lookup(jp_title: str) -> str:
    """Strip trailing volumes and spaces so AniList search hits."""
    return re.sub(r"\s+\d+\s*$", "", jp_title).strip()


def _query_anilist_batch(jp_titles: List[str]) -> Dict[str, Optional[str]]:
    """Return {jp_title: english | romaji | None} via one GraphQL request."""
    alias_blocks: List[str] = []
    for i, jp in enumerate(jp_titles):
        esc = _clean_for_lookup(jp).replace('"', r'\"')
        alias_blocks.append(
            f"""
            manga_{i}: Page(perPage: 1) {{
              pageInfo {{ total }}
              results: media(type: MANGA, search: \"{esc}\") {{
                title {{ english romaji }}
              }}
            }}"""
        )
    query = "query {\n" + "\n".join(alias_blocks) + "\n}"

    try:
        r = requests.post(
            "https://graphql.anilist.co",
            json={"query": query},
            headers=UA_HEADER,
            timeout=8,
        )
        r.raise_for_status()
        data = r.json()["data"]
    except Exception:
        return {t: None for t in jp_titles}

    out: Dict[str, Optional[str]] = {}
    for i, original in enumerate(jp_titles):
        node = data.get(f"manga_{i}")
        if node and node.get("results"):
            titles = node["results"][0]["title"]
            out[original] = titles.get("english") or titles.get("romaji")
        else:
            out[original] = None
    return out


def _machine_translate(jp_title: str) -> str:
    """Synchronous fallback translator using deep‑translator."""
    try:
        return GoogleTranslator(source="ja", target="en").translate(jp_title)
    except Exception:
        return jp_title


def _jp_to_en(jp_title: str, cache: Dict[str, str]) -> str:
    """Return an English (or fallback) title, caching results."""
    if jp_title in cache:
        return cache[jp_title]
    en = _machine_translate(jp_title)
    cache[jp_title] = en
    return en


# ---------------------------------------------------------------------------
# 3.  Baseline parsing & estimator
# ---------------------------------------------------------------------------

def _parse_baseline(text: str) -> List[Tuple[int, int]]:
    match = re.search(r"※[^\n]*おおまかな実売目安[^\n]*\n?(.*)", text)
    if not match:
        raise RuntimeError("Baseline line not found.")
    pairs = re.findall(r"(\d+)位(\d+)", match.group(1))
    if not pairs:
        raise RuntimeError("No baseline rank/sales pairs found.")
    return sorted([(int(r), int(v)) for r, v in pairs])


def _make_estimator(breakpoints: List[Tuple[int, int]]):
    def estimator(rank: int) -> int:
        if not 1 <= rank <= breakpoints[-1][0]:
            raise ValueError(f"Rank must be between 1 and {breakpoints[-1][0]}")
        if rank <= breakpoints[0][0]:
            return -1
        for r, v in breakpoints:
            if rank == r:
                return v
        for (r1, v1), (r2, v2) in zip(breakpoints, breakpoints[1:]):
            if r1 < rank < r2:
                return int(v1 + (v2 - v1) * (rank - r1) / (r2 - r1))
        return breakpoints[-1][1]
    
    return estimator


# ---------------------------------------------------------------------------
# 4.  Ranking row extraction
# ---------------------------------------------------------------------------
ROW_RE = re.compile(
    r"""^\s*(\d{1,3})\s*              # rank (1‑500)
        <[^>]+>\s*\d+\s*</a>\s*       # ISBN link
        ([^<]+?)\s+                     # title text
        (?:\S+\s+){2}\d{4}\.\d{2}\.\d{2} # publisher, author, date
    $""",
    re.VERBOSE,
)


def _extract_rank_list(soup: BeautifulSoup) -> List[Tuple[int, str, Optional[int]]]:
    entry = soup.find("div", class_="entry_body")
    if not entry:
        return []
    rows = entry.decode_contents().split("<br/>")
    out: List[Tuple[int, str, Optional[int]]] = []
    for raw in rows:
        m = ROW_RE.match(raw.strip())
        if m:
            title = m.group(2).strip()
            # Clean up title: remove trailing spaces and replace full-width spaces
            title = title.replace("　", " ").strip()

            # Clean up title: Replace Japanese Latin characters (Unicode full-width) with ASCII
            title = re.sub(r"[Ａ-Ｚａ-ｚ]", lambda x: chr(ord(x.group(0)) - 0xFEE0), title)
            title = re.sub(r"[０-９]", lambda x: str(int(x.group(0))), title)

            # Extract volume number if present at end of title
            vol_match = re.search(r"\s(\d+)$", title)
            volume = int(vol_match.group(1)) if vol_match else None

            out.append((int(m.group(1)), title, volume))
    return out


def _extract_date_info(soup: BeautifulSoup, is_monthly: bool) -> Dict[str, Optional[str]]:
    header = soup.find("h2", class_="entry_header")
    jp_date = None
    year = None
    week = None
    month = None
    en_date = None
    if header:
        text = header.text
        if is_monthly:
            # Example: 2025年5月 漫画ランキング ...
            m = re.search(r"(\d{4})年(\d{1,2})月", text)
            if m:
                year = m.group(1)
                month = m.group(2)
                jp_date = f"{year}年{month}月"
                # English translation

                en_month = calendar.month_name[int(month)]
                en_date = f"{en_month} {year}"
        else:
            # Example: 2025年6/30-7/6 漫画ランキング ...
            m = re.search(r"(\d{4})年(\d{1,2})/(\d{1,2})-(\d{1,2})/(\d{1,2})", text)
            if m:
                year = m.group(1)
                start_month = m.group(2)
                start_day = m.group(3)
                end_month = m.group(4)
                end_day = m.group(5)
                jp_date = f"{year}年{start_month}/{start_day}-{end_month}/{end_day}"
                en_date = f"{calendar.month_name[int(start_month)]} {start_day} - {calendar.month_name[int(end_month)]} {end_day}, {year}"
                week = f"{start_month}/{start_day}-{end_month}/{end_day}"
    return {
        "jp_date": jp_date,
        "en_date": en_date,
        "year": year,
        "month": month,
        "week": week
    }


# ---------------------------------------------------------------------------
# 5.  High‑level workflow
# ---------------------------------------------------------------------------

def scrape_latest_weekly_and_estimate(limit: int = 500, use_monthly: bool = False) -> Dict[str, any]:
    post_url, category_type = _latest_article_url(use_monthly)
    soup = _get_soup(post_url)
    article_text = soup.get_text("\n", strip=True)
    date_info = _extract_date_info(soup, use_monthly)
    estimator = _make_estimator(_parse_baseline(article_text))
    rank_lines = _extract_rank_list(soup)
    print(f"Found {len(rank_lines)} ranks in the article.")

    # Batch AniList queries
    unique_titles = list({title for _, title, _ in rank_lines})
    en_map: Dict[str, Optional[str]] = {}
    source_map: Dict[str, str] = {}
    for i in range(0, len(unique_titles), ALIAS_BATCH):
        batch_result = _query_anilist_batch(unique_titles[i : i + ALIAS_BATCH])
        for jp, en in batch_result.items():
            en_map[jp] = en
            source_map[jp] = "anilist" if en else None

    # Build cache for quick JP→EN
    cache = {jp: en for jp, en in en_map.items() if en}

    results: List[Dict[str, any]] = []
    for rank, jp, volume in tqdm(rank_lines, desc="Processing ranks"):
        if rank > limit:
            continue
        en_title = en_map.get(jp)
        if en_title:
            source = "anilist"
        else:
            en_title = _machine_translate(jp)
            source = "machine_translation"
        results.append({
            "rank": rank,
            "jp_title": jp,
            "en_title": en_title,
            "en_source": source,
            "volume": volume,
            "estimated_sales": estimator(rank)
        })
    return {
        "category_type": category_type,
        "date_info": date_info,
        "total_entries": len(results),
        "rankings": results
    }


# ---------------------------------------------------------------------------
# 6.  CLI demo
# ---------------------------------------------------------------------------
if __name__ == "__main__":

    parser = ArgumentParser(description="Scrape Shoseki rankings and estimate sales.")
    parser.add_argument(
        "--limit", type=int, default=500, help="Limit the number of rankings to scrape."
    )
    parser.add_argument(
        "--monthly", action="store_true", help="Use monthly rankings instead of weekly."
    )

    args = parser.parse_args()
    data = scrape_latest_weekly_and_estimate(limit=args.limit, use_monthly=args.monthly)

    file_name = "shoseki_monthly_ranking.json" if args.monthly else "shoseki_weekly_ranking.json"

    with open(file_name, "w", encoding="utf-8") as f:
        import json
        json.dump(data, f, ensure_ascii=False, indent=2)
