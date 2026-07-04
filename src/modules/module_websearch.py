
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
from contextlib import contextmanager
from html import unescape
from urllib.parse import quote
import requests
import atexit
from datetime import datetime

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


def is_naver_search_available():
    client_id, client_secret = _naver_credentials()
    return bool(client_id and client_secret)

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
    with open("engine/debug.html", "w", encoding='utf-8') as f:
        f.write(driver.page_source)

def wait_for_element(element_id: str, delay: int = 10):
    """Wait for an element with a specific ID to be present."""
    try:
        WebDriverWait(driver, delay).until(EC.presence_of_element_located((By.ID, element_id)))
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
    return '\n'.join(el.text for el in driver.find_elements(By.CSS_SELECTOR, selector) if el and el.text).strip()

def extract_links(selector):
    """
    Extract hyperlinks from elements matching the specified CSS selector.

    Parameters:
    - selector (str): The CSS selector for the elements.

    Returns:
    - list: List of extracted hyperlinks.
    """
    return [el.get_attribute('href') for el in driver.find_elements(By.CSS_SELECTOR, selector) if el and el.text]


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


def search_naver(query, category="auto", display=5):
    """
    Search Naver using the official Search API.

    category can be auto, news, shopping, web, knowledge, blog, cafe, book, image, local.
    """
    client_id, client_secret = _naver_credentials()
    if not client_id or not client_secret:
        raise RuntimeError("Naver Search API credentials are missing.")

    resolved_category = _naver_category_for_query(query, category)
    endpoint = NAVER_ENDPOINTS.get(resolved_category, NAVER_ENDPOINTS["web"])
    url = f"https://openapi.naver.com{endpoint}"
    headers = {
        "X-Naver-Client-Id": client_id,
        "X-Naver-Client-Secret": client_secret,
    }
    params = {
        "query": query,
        "display": max(1, min(int(display), 10)),
        "sort": "date" if resolved_category == "news" else "sim",
    }

    queue_message(f"INFO: Searching Naver {resolved_category} for: {query}")
    response = requests.get(url, headers=headers, params=params, timeout=8)
    response.raise_for_status()
    items = response.json().get("items", [])
    if not items:
        return "검색 결과가 비어 있습니다."

    lines = [f"Naver {resolved_category} search results for '{query}':"]
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
    """Single WebSearch tool entrypoint: prefer Naver API, fallback to Google scraping."""
    if is_naver_search_available():
        return search_naver(query, category="auto")
    queue_message("INFO: Naver credentials missing; falling back to Google search.")
    return search_google(query)


def search_news(query):
    """News tool entrypoint: prefer Naver News API, fallback to Google News scraping."""
    if is_naver_search_available():
        return search_naver(query, category="news")
    queue_message("INFO: Naver credentials missing; falling back to Google News search.")
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
