
"""
module_websearch.py

Web Search Module for TARS-AI Application.

This module provides functionality for performing web searches using Selenium WebDriver. 
It supports multiple search engines and allows for extracting specific content, links, 
and structured data from search results.
"""

# === Standard Libraries ===
import os
import re
import sys
import json
from contextlib import contextmanager
from html import unescape
from pathlib import Path
from urllib.parse import quote
import requests
import atexit
from datetime import datetime
from dotenv import load_dotenv

try:
    from selenium import webdriver
    from selenium.webdriver.common.by import By
    from selenium.webdriver.chrome.options import Options as ChromeOptions
    from selenium.webdriver.chrome.service import Service as ChromeService
    from selenium.webdriver.support.ui import WebDriverWait
    from selenium.webdriver.support import expected_conditions as EC
except ImportError:
    webdriver = None
    By = None
    ChromeOptions = None
    ChromeService = None
    WebDriverWait = None
    EC = None

from modules.module_messageQue import queue_message

BASE_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BASE_DIR.parent
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(BASE_DIR / ".env", override=False)

NAVER_ENDPOINTS = {
    "blog": "/v1/search/blog.json",
    "news": "/v1/search/news.json",
    "shopping": "/v1/search/shop.json",
    "web": "/v1/search/webkr.json",
    "knowledge": "/v1/search/kin.json",
    "cafe": "/v1/search/cafearticle.json",
    "book": "/v1/search/book.json",
    "image": "/v1/search/image.json",
    "local": "/v1/search/local.json",
}


def _strip_html(text):
    text = re.sub(r"<[^>]+>", "", text or "")
    return unescape(text).strip()


def _naver_credentials():
    client_id = os.getenv("NAVER_CLIENT_ID", "").strip()
    client_secret = os.getenv("NAVER_CLIENT_SECRET", "").strip()
    return client_id, client_secret


def _brave_api_key():
    return os.getenv("BRAVE_SEARCH_API_KEY", "").strip()


def is_naver_search_available():
    client_id, client_secret = _naver_credentials()
    return bool(client_id and client_secret)


def is_brave_search_available():
    return bool(_brave_api_key())

# === Helper Functions ===
# Silence logs to suppress unnecessary outputs
@contextmanager
def silence_log():
    """
    Context manager to suppress unnecessary console logs during driver initialization.
    """
    old_stdout, old_stderr = sys.stdout, sys.stderr
    try:
        with open(os.devnull, "w") as devnull:
            sys.stdout, sys.stderr = devnull, devnull
            yield
    finally:
        sys.stdout, sys.stderr = old_stdout, old_stderr


def initialize_driver():
    """
    Initialize the Selenium WebDriver using Chromium.

    Returns:
    - WebDriver: Configured Selenium WebDriver instance.
    """
    if webdriver is None:
        raise RuntimeError("Selenium is not installed; browser search fallback is unavailable.")

    options = ChromeOptions()
    options.add_argument("--headless")  # Run in headless mode
    options.add_argument("--no-sandbox")  # Bypass OS security model
    options.add_argument("--disable-dev-shm-usage")  # Overcome limited resource problems
    options.add_argument("--disable-infobars")
    options.add_argument("--lang=en-GB")

    service = ChromeService(executable_path="/usr/bin/chromedriver")  # Path to the chromedriver
    return webdriver.Chrome(service=service, options=options)


driver = None


def get_driver():
    """Start Selenium only when a scraping fallback is actually needed."""
    global driver
    if driver is None:
        driver = initialize_driver()
    return driver

def quit_driver():
    """
    Quit the WebDriver instance when the script ends.
    """
    if driver:
        driver.quit()

def save_debug():
    """
    Save the current page source for debugging purposes.
    """
    debug_path = BASE_DIR / "engine" / "debug.html"
    debug_path.parent.mkdir(parents=True, exist_ok=True)
    with open(debug_path, "w", encoding='utf-8') as f:
        f.write(get_driver().page_source)

def wait_for_element(element_id: str, delay: int = 10):
    """Wait for an element with a specific ID to be present."""
    try:
        WebDriverWait(get_driver(), delay).until(EC.presence_of_element_located((By.ID, element_id)))
    except Exception:
        queue_message(f"ERROR: Element with ID '{element_id}' not found.")
        
def extract_text(selector):
    """
    Extract text content from elements matching the specified CSS selector.

    Parameters:
    - selector (str): The CSS selector for the elements.

    Returns:
    - str: Concatenated text content of matched elements.
    """
    return '\n'.join(el.text for el in get_driver().find_elements(By.CSS_SELECTOR, selector) if el and el.text).strip()

def extract_links(selector):
    """
    Extract hyperlinks from elements matching the specified CSS selector.

    Parameters:
    - selector (str): The CSS selector for the elements.

    Returns:
    - list: List of extracted hyperlinks.
    """
    return [el.get_attribute('href') for el in get_driver().find_elements(By.CSS_SELECTOR, selector) if el and el.text]


def _naver_category_for_query(query, category="auto"):
    if category != "auto":
        return category

    text = (query or "").lower()
    if re.search(r"뉴스|속보|헤드라인|경기 결과|누가 이겼|score|news|result", text):
        return "news"
    if re.search(r"가격|제품|추천|구매|쇼핑|최저가|제습기|과자|shop|price|buy", text):
        return "shopping"
    if re.search(r"맛집|근처|주소|영업|병원|카페|local|near", text):
        return "local"
    if re.search(r"뜻|방법|원리|지식|뭐야|왜|어떻게|kin", text):
        return "knowledge"
    return "web"


def _naver_search_once(query, category, display):
    endpoint = NAVER_ENDPOINTS.get(category, NAVER_ENDPOINTS["web"])
    url = f"https://openapi.naver.com{endpoint}"
    client_id, client_secret = _naver_credentials()
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {
        "query": query,
        "display": max(1, min(int(display), 10)),
        "sort": "date" if category == "news" else "sim",
    }

    queue_message(f"INFO: Searching Naver {category} for: {query}")
    response = requests.get(url, headers=headers, params=params, timeout=8)
    response.raise_for_status()
    items = response.json().get("items", [])
    queue_message(f"INFO: Naver {category} returned {len(items)} results")
    return items


def _normalize_naver_item(item, category):
    title = _strip_html(item.get("title"))
    snippet = _strip_html(
        item.get("description")
        or item.get("link")
        or item.get("mallName")
        or item.get("address")
    )
    url = item.get("link") or item.get("originallink") or ""
    if category == "shopping":
        price = item.get("lprice") or item.get("hprice")
        mall = _strip_html(item.get("mallName"))
        extras = []
        if price:
            extras.append(f"price={price} KRW")
        if mall:
            extras.append(f"mall={mall}")
        if extras:
            snippet = f"{snippet} [{' ; '.join(extras)}]".strip()
    return {
        "provider": "naver",
        "category": category,
        "title": title,
        "snippet": snippet,
        "url": url,
    }


def search_brave_items(query, display=5):
    api_key = _brave_api_key()
    if not api_key:
        raise RuntimeError("Brave Search API key is missing.")

    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }
    params = {
        "q": query,
        "count": max(1, min(int(display), 10)),
        "country": "KR",
        "search_lang": "ko",
        "safesearch": "moderate",
    }

    queue_message(f"INFO: Searching Brave web for: {query}")
    response = requests.get(url, headers=headers, params=params, timeout=8)
    response.raise_for_status()
    raw_items = response.json().get("web", {}).get("results", [])
    queue_message(f"INFO: Brave web returned {len(raw_items)} results")
    return [
        {
            "provider": "brave",
            "category": "web",
            "title": _strip_html(item.get("title")),
            "snippet": _strip_html(item.get("description")),
            "url": item.get("url") or "",
        }
        for item in raw_items[:display]
    ]


def _naver_fallback_categories(category):
    if category == "news":
        return ["news", "web", "blog"]
    if category == "shopping":
        return ["shopping", "web", "blog"]
    if category == "knowledge":
        return ["knowledge", "web", "blog"]
    if category == "local":
        return ["local", "web", "blog"]
    return [category]


def _extract_json_object(text):
    match = re.search(r"\{.*\}", text or "", re.S)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def _load_raw_complete_llm():
    try:
        from modules.module_llm import raw_complete_llm
    except Exception:
        from module_llm import raw_complete_llm
    return raw_complete_llm


def _normalize_naver_category(category):
    category = (category or "").strip().lower()
    aliases = {
        "shop": "shopping",
        "shopping": "shopping",
        "product": "shopping",
        "products": "shopping",
        "kin": "knowledge",
        "knowledge": "knowledge",
        "webkr": "web",
        "web": "web",
        "news": "news",
        "blog": "blog",
        "cafe": "cafe",
        "book": "book",
        "image": "image",
        "local": "local",
    }
    return aliases.get(category, "web")


def plan_search_query(user_query, category_hint="auto"):
    """
    Ask the configured LLM to turn a natural-language question into search keywords.
    Falls back to the original query if planning fails.
    """
    fallback_category = _naver_category_for_query(user_query, category_hint)
    fallback = {
        "should_search": True,
        "query": (user_query or "").strip(),
        "category": fallback_category,
    }

    try:
        raw_complete_llm = _load_raw_complete_llm()
    except Exception as e:
        queue_message(f"WARNING: Search planner unavailable: {e}")
        return fallback

    prompt = f"""
You are a search query planner for a Korean voice assistant.
Return ONLY one JSON object with:
{{"should_search": true|false, "query": "...", "category": "news|shopping|web|knowledge|blog|local"}}

Rules:
- Convert the user's natural Korean question into concise search keywords.
- Preserve important proper nouns, dates, teams, product names, places, model names, and numbers.
- Remove conversational filler like "누가 이겼어", "알려줘", "추천해줘" unless it is needed as a keyword.
- For live/recent events, sports results, announcements, and headlines use category "news".
- For prices/products/models/reviews/purchases use category "shopping".
- For how-to, meanings, or general explanations use category "web" or "knowledge".
- If no external/current information is needed, set should_search false.
- Today's date is {datetime.now().strftime('%Y-%m-%d')}.
- Category hint from router: {category_hint}

User question: {user_query}
JSON:
""".strip()

    planned_text = raw_complete_llm(prompt)
    planned = _extract_json_object(planned_text)
    if not isinstance(planned, dict):
        queue_message("WARNING: Search planner returned invalid JSON; using original query.")
        return fallback

    if planned.get("should_search") is False:
        return {
            "should_search": False,
            "query": fallback["query"],
            "category": fallback["category"],
        }

    planned_query = str(planned.get("query") or "").strip()
    if not planned_query:
        planned_query = fallback["query"]
    planned_category = _normalize_naver_category(planned.get("category") or fallback_category)
    queue_message(f"INFO: Search planner query='{planned_query}' category={planned_category}")
    return {
        "should_search": True,
        "query": planned_query,
        "category": planned_category,
    }


def _format_search_items(items, limit=8):
    lines = []
    for index, item in enumerate(items[:limit], 1):
        line = (
            f"{index}. [{item.get('provider')}/{item.get('category')}] "
            f"{item.get('title')}"
        )
        if item.get("snippet"):
            line += f" - {item.get('snippet')}"
        if item.get("url"):
            line += f" ({item.get('url')})"
        lines.append(line)
    return "\n".join(lines)


def _extract_answer_from_results(user_query, plan, items):
    if not items:
        return {
            "answer_found": False,
            "answer": "",
            "confidence": "low",
            "evidence": [],
            "followup_context": {},
        }

    try:
        raw_complete_llm = _load_raw_complete_llm()
    except Exception as e:
        queue_message(f"WARNING: Answer extractor unavailable: {e}")
        return {
            "answer_found": True,
            "answer": "",
            "confidence": "low",
            "evidence": [],
            "followup_context": {},
        }

    result_text = _format_search_items(items, limit=8)
    prompt = f"""
You are an evidence-bound answer extractor for a Korean voice assistant.
Return ONLY one JSON object with:
{{
  "answer_found": true|false,
  "answer": "direct answer in Korean",
  "confidence": "high|medium|low",
  "evidence": ["short copied-or-paraphrased evidence 1", "evidence 2"],
  "followup_context": {{}}
}}

Rules:
- Use ONLY the search results below. Do not use memory or guess.
- If the results do not directly answer the user's question, set answer_found false.
- If sources disagree, say so in answer and set confidence medium or low.
- For sports results include winner, loser, and score when present.
- For product price questions include model name, price, and source/mall when present.
- Keep answer concise and factual. No persona, no jokes, no advice to sleep.

User question: {user_query}
Planned search: query="{plan.get('query')}", category="{plan.get('category')}"

Search results:
{result_text}

JSON:
""".strip()

    extracted_text = raw_complete_llm(prompt)
    extracted = _extract_json_object(extracted_text)
    if not isinstance(extracted, dict):
        queue_message("WARNING: Answer extractor returned invalid JSON.")
        return {
            "answer_found": False,
            "answer": "",
            "confidence": "low",
            "evidence": [],
            "followup_context": {},
        }
    return extracted


def _format_search_tool_result(user_query, plan, items, extracted):
    return (
        "SEARCH_TOOL_RESULT\n"
        f"Original question: {user_query}\n"
        f"Planned query: {plan.get('query')}\n"
        f"Planned category: {plan.get('category')}\n"
        f"Answer JSON: {json.dumps(extracted, ensure_ascii=False)}\n"
        "Search results:\n"
        f"{_format_search_items(items, limit=8)}\n"
        "Assistant rule: answer using Answer JSON only. If answer_found is false, say the search results did not clearly answer it. Do not invent missing facts."
    )


def search_combined(query, category="auto", display=5):
    plan = plan_search_query(query, category)
    if not plan.get("should_search", True):
        return "Search planner decided no external search is needed."

    planned_query = plan.get("query") or query
    resolved_category = _normalize_naver_category(plan.get("category")) or _naver_category_for_query(query, category)
    items = []
    tried = []

    if is_naver_search_available():
        for candidate in _naver_fallback_categories(resolved_category):
            tried.append(f"naver:{candidate}:{planned_query}")
            try:
                naver_items = _naver_search_once(planned_query, candidate, display)
            except Exception as e:
                queue_message(f"WARNING: Naver {candidate} search failed: {e}")
                naver_items = []
            items.extend(_normalize_naver_item(item, candidate) for item in naver_items)
            if items and candidate == resolved_category:
                break
    else:
        queue_message("INFO: Naver credentials missing; skipping Naver search.")

    if is_brave_search_available():
        tried.append(f"brave:web:{planned_query}")
        try:
            items.extend(search_brave_items(planned_query, display=display))
        except Exception as e:
            queue_message(f"WARNING: Brave search failed: {e}")
    else:
        queue_message("INFO: Brave Search API key missing; skipping Brave search.")

    if not items and planned_query != query:
        if is_naver_search_available():
            fallback_category = _naver_category_for_query(query, category)
            for candidate in _naver_fallback_categories(fallback_category):
                tried.append(f"naver:{candidate}:{query}")
                try:
                    naver_items = _naver_search_once(query, candidate, display)
                except Exception as e:
                    queue_message(f"WARNING: Naver fallback {candidate} search failed: {e}")
                    naver_items = []
                items.extend(_normalize_naver_item(item, candidate) for item in naver_items)
                if items:
                    break
        if is_brave_search_available():
            tried.append(f"brave:web:{query}")
            try:
                items.extend(search_brave_items(query, display=display))
            except Exception as e:
                queue_message(f"WARNING: Brave fallback search failed: {e}")

    # Keep prompt size bounded and remove exact duplicate URLs/titles.
    deduped = []
    seen = set()
    for item in items:
        key = (item.get("url") or item.get("title") or "").strip().lower()
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        deduped.append(item)
        if len(deduped) >= 10:
            break

    extracted = _extract_answer_from_results(query, plan, deduped)
    if not deduped:
        extracted = {
            "answer_found": False,
            "answer": "",
            "confidence": "low",
            "evidence": [f"No results. Tried: {', '.join(tried)}"],
            "followup_context": {},
        }
    return _format_search_tool_result(query, plan, deduped, extracted)


def search_naver(query, category="auto", display=5):
    """
    Search Naver using the official Search API.

    category can be auto, news, shopping, web, knowledge, blog, cafe, book, image, local.
    """
    client_id, client_secret = _naver_credentials()
    if not client_id or not client_secret:
        raise RuntimeError("Naver Search API credentials are missing.")

    plan = plan_search_query(query, category)
    if not plan.get("should_search", True):
        return "Search planner decided no external search is needed."

    planned_query = plan.get("query") or query
    resolved_category = _normalize_naver_category(plan.get("category")) or _naver_category_for_query(query, category)
    tried_categories = []
    tried_queries = []
    items = []
    used_category = resolved_category
    used_query = planned_query
    query_variants = [planned_query]
    if planned_query != query:
        query_variants.append(query)

    for candidate in _naver_fallback_categories(resolved_category):
        tried_categories.append(candidate)
        for query_variant in query_variants:
            tried_queries.append(query_variant)
            used_category = candidate
            used_query = query_variant
            items = _naver_search_once(query_variant, candidate, display)
            if items:
                break
        if items:
            break
    if not items:
        return (
            f"Naver search returned no results for '{query}'. "
            f"Tried categories: {', '.join(tried_categories)}. "
            f"Tried queries: {', '.join(tried_queries)}."
        )

    lines = [f"Naver {used_category} search results for '{used_query}':"]
    for index, item in enumerate(items[:display], 1):
        title = _strip_html(item.get("title"))
        summary = _strip_html(
            item.get("description")
            or item.get("link")
            or item.get("mallName")
            or item.get("address")
        )
        link = item.get("link") or item.get("originallink") or ""
        line = f"{index}. {title}"
        if summary:
            line += f" - {summary}"
        if link:
            line += f" ({link})"
        lines.append(line)

    return "\n".join(lines)


def search_web(query):
    """Single WebSearch tool entrypoint with planning, multi-provider search, and extraction."""
    if is_naver_search_available() or is_brave_search_available():
        return search_combined(query, category="auto")
    queue_message("INFO: Search API credentials missing; falling back to Google search.")
    return search_google(query)


def search_news(query):
    """News tool entrypoint with planning, multi-provider search, and extraction."""
    if is_naver_search_available() or is_brave_search_available():
        return search_combined(query, category="news")
    queue_message("INFO: Search API credentials missing; falling back to Google News search.")
    return search_google_news(query)

# === Search Functions ===
def search_query(url, query, content_selector, link_selector=None):
    """
    Perform a web search and extract content and links.

    Parameters:
    - url (str): Base search URL.
    - query (str): Search query.
    - content_selector (str): CSS selector for text content.
    - link_selector (str, optional): CSS selector for links.

    Returns:
    - tuple: Extracted text content and links (if applicable).
    """
    get_driver().get(url + query)
    wait_for_element('res')  # Wait for page to load

    content = extract_text(content_selector)
    links = extract_links(link_selector) if link_selector else []
    return content, links

def search_google(query):
    """
    Perform a Google search and extract featured snippets, knowledge panels, and snippets.

    Parameters:
    - query (str): The search query.

    Returns:
    - tuple: Extracted content and links.
    """
    queue_message(f"INFO: Searching Google for: {query}")
    get_driver().get("https://google.com/search?hl=en&q=" + query)
    wait_for_element('res')
    save_debug()

    text = ""
    # Featured snippets
    text += extract_text('.wDYxhc')
    queue_message(f"INFO: Featured snippets: {text}")
    # Knowledge panels
    text += extract_text('.hgKElc')
    queue_message(f"INFO: Knowledge panels: {text}")
    # Page snippets
    text += extract_text('.r025kc.lVm3ye')
    queue_message(f"INFO: Page snippets: {text}")
    # Additional selectors for compatibility
    text += extract_text('.yDYNvb.lyLwlc')

    return text

def search_google_news(query):
    """
    Perform a search on Google News and extract news snippets.

    Parameters:
    - query (str): The search query.

    Returns:
    - tuple: Extracted content and links.
    """
    queue_message(f"INFO: Fetching Google News for: {query}")
    return search_query(
        "https://google.com/search?hl=en&gl=us&tbm=nws&q=",
        query,
        ".dURPMd"
    )

def search_duckduckgo(query):
    """
    Perform a search on DuckDuckGo and extract results.

    Parameters:
    - query (str): The search query.

    Returns:
    - tuple: Extracted content and links.
    """
    queue_message(f"INFO: Searching DuckDuckGo for: {query}")
    return search_query(
        "https://duckduckgo.com/?kp=-2&kl=wt-wt&q=",
        query,
        '[data-result="snippet"]'
    )

def search_mojeek(query):
    """
    Perform a search on Mojeek and extract results.

    Parameters:
    - query (str): The search query.

    Returns:
    - tuple: Extracted content and links.
    """
    queue_message(f"INFO: Searching Mojeek for: {query}")
    base_url = "https://www.mojeek.com/search?q="
    full_url = base_url + query

    # Load the page
    get_driver().get(full_url)
    wait_for_element("results")  # Wait for results container to load

    # Extract search result snippets
    content = extract_text(".result-title, .result-desc")  # Titles and descriptions
    links = extract_links(".result-title > a")  # Result links

    return content

def search_mojeek_summary(query):
    """
    Perform a search on Mojeek and extract the summary text. Includes debugging information.

    Parameters:
    - query (str): The search query.

    Returns:
    - str: The extracted summary text, or an error message if not found.
    """
    queue_message(f"INFO: Searching Mojeek for: {query}")
    base_url = "https://www.mojeek.com/search?q="
    full_url = base_url + query

    try:
        # Load the Mojeek search results page
        get_driver().get(full_url)

        # Save page source for debugging
        save_debug()

        # Print all elements with 'id=kalid' for verification
        elements = driver.find_elements(By.ID, "kalid")
        if not elements:
            queue_message("DEBUG: No elements with ID 'kalid' found.")
        else:
            for el in elements:
                queue_message("DEBUG: Found element with ID 'kalid':", el.get_attribute("outerHTML"))

        # Wait for the summary box to load
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div#kalid.infobox-right.llm-ib"))
        )

        # Extract the summary content
        summary = extract_text("div#kalid.infobox-right.llm-ib div.content")
        queue_message("INFO: Summary extracted successfully.")
        return summary

    except Exception as e:
        queue_message(f"ERROR: Unable to extract summary. {e}")
        return "Summary not found. Check debug.html for details."


# === Cleanup ===
atexit.register(quit_driver)
