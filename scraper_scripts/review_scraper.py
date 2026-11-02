"""
Google Maps Hospital Review Scraper
===================================
Scrapes reviews (rating + comment) for hospitals across Indonesia.
Max 60 most recent reviews per hospital, up to 5 years back.

Features:
- Stealth mode with playwright-stealth
- Cookie/consent dialog handling
- Multiple fallback strategies for finding review tab
- JS-based review extraction (resilient to class name changes)
- Smart scrolling with auto-detection of scrollable container
- Browser restart every N hospitals to avoid detection
- Incremental CSV save with resume support
- Proper logging to file + console
"""

import time
import os
import csv
import random
import re
import logging
import glob
from datetime import datetime
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from playwright_stealth import Stealth

# ============================================================
# CONFIGURATION
# ============================================================
current_time = datetime.now()
date_str = current_time.strftime("%Y%m%d")
datetime_str = current_time.strftime("%Y%m%d_%H%M%S")

INPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "processed", "merged_data_rumah_sakit.csv")
OUTPUT_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "processed", f"hospital_reviews_gmaps_{date_str}.csv")
DEBUG_DIR = os.path.join(os.path.dirname(__file__), "..", "debug")
LOG_FILE = os.path.join(DEBUG_DIR, f"log_{datetime_str}.txt")
SCREENSHOT_DIR = os.path.join(DEBUG_DIR, "screenshots")

MAPS_URL = "https://www.google.com/maps"
BATCH_SIZE = 1000               # Max hospitals per run
MAX_REVIEWS_PER_HOSPITAL = 60   # Max reviews to collect per hospital
MAX_YEARS_AGO = 5               # Stop if review is older than this
HEADLESS = True
BROWSER_RESTART_EVERY = 50      # Restart browser every N hospitals

# Ensure directories exist
os.makedirs(SCREENSHOT_DIR, exist_ok=True)
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# ============================================================
# LOGGING
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler()
    ]
)
log = logging.getLogger("review_scraper")

# ============================================================
# USER AGENT ROTATION
# ============================================================
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
]


# ============================================================
# UTILITY FUNCTIONS
# ============================================================
def random_delay(min_s=1.5, max_s=3.5):
    """Sleep for a random duration to mimic human behavior."""
    time.sleep(random.uniform(min_s, max_s))


def is_older_than_limit(date_text):
    """
    Check if relative date string (e.g. '6 years ago', '6 tahun lalu')
    is older than MAX_YEARS_AGO.
    """
    if not date_text:
        return False
    date_text = date_text.lower()
    match = re.search(r'(\d+)\s+(years?|tahun)', date_text)
    if match:
        return int(match.group(1)) > MAX_YEARS_AGO
    return False


def save_review(review_data):
    """Save a single review row to CSV incrementally."""
    file_exists = os.path.isfile(OUTPUT_FILE)
    fieldnames = ['hospital_id', 'hospital_name', 'author', 'rating', 'date', 'comment', 'latitude', 'longitude', 'scraping_timestamp']
    with open(OUTPUT_FILE, mode='a', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, delimiter=';')
        if not file_exists:
            writer.writeheader()
        writer.writerow(review_data)


def get_scraped_ids():
    """Get set of hospital codes already scraped to enable resume."""
    scraped_ids = set()
    output_dir = os.path.dirname(OUTPUT_FILE)
    
    # Check all potential output files
    file_pattern = os.path.join(output_dir, "hospital_reviews_gmaps*.csv")
    for file_path in glob.glob(file_pattern):
        try:
            with open(file_path, mode='r', encoding='utf-8') as f:
                reader = csv.DictReader(f, delimiter=';')
                for row in reader:
                    if 'hospital_id' in row:
                        scraped_ids.add(row['hospital_id'])
        except Exception as e:
            log.warning(f"Failed to read IDs from {file_path}: {e}")
            
    return scraped_ids


# ============================================================
# BROWSER MANAGEMENT
# ============================================================
def create_browser(pw):
    """Create a new browser + context + page with stealth settings."""
    browser = pw.chromium.launch(headless=HEADLESS)
    ua = random.choice(USER_AGENTS)
    viewport_w = random.randint(1280, 1920)
    viewport_h = random.randint(800, 1080)

    context = browser.new_context(
        user_agent=ua,
        viewport={'width': viewport_w, 'height': viewport_h},
        locale='id-ID',
        timezone_id='Asia/Jakarta',
    )

    # Block heavy resources for speed (images, fonts, media)
    context.route(
        "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,eot,mp4,webm}",
        lambda route: route.abort()
    )

    page = context.new_page()
    Stealth().apply_stealth_sync(page)

    log.info(f"🌐 Browser created (UA: ...{ua[-30:]}, viewport: {viewport_w}x{viewport_h})")
    return browser, context, page


# ============================================================
# CONSENT / COOKIE HANDLING
# ============================================================
def accept_consent(page):
    """Handle Google consent/cookie dialog if it appears."""
    try:
        # Google consent buttons (various languages)
        selectors = [
            'button:has-text("Accept all")',
            'button:has-text("Terima semua")',
            'button:has-text("Setuju")',
            'button:has-text("Agree")',
            'form[action*="consent"] button:first-of-type',
        ]
        for sel in selectors:
            try:
                btn = page.locator(sel)
                if btn.count() > 0 and btn.first.is_visible(timeout=2000):
                    btn.first.click()
                    log.info("✅ Consent dialog accepted")
                    random_delay(2, 3)
                    return True
            except PlaywrightTimeout:
                continue
    except Exception:
        pass
    return False


# ============================================================
# REVIEW TAB NAVIGATION
# ============================================================
def _find_review_tab(page):
    """
    Try to find and return the Reviews/Ulasan tab element.
    Tabs can be either button or div with role="tab".
    Also checks aria-label which contains "Ulasan" after workaround.
    """
    tab_selectors = [
        '[role="tab"]:has-text("Ulasan")',
        '[role="tab"]:has-text("Reviews")',
        'button[role="tab"]:has-text("Ulasan")',
        'button[role="tab"]:has-text("Reviews")',
        '[role="tab"][aria-label*="Ulasan"]',
        '[role="tab"][aria-label*="Reviews"]',
    ]
    for sel in tab_selectors:
        try:
            tab = page.locator(sel)
            if tab.count() > 0 and tab.first.is_visible(timeout=2000):
                return tab.first
        except (PlaywrightTimeout, Exception):
            continue
    return None


def click_review_tab(page):
    """
    Navigate to the Reviews tab using the proven workaround flow:
    1. Try direct tab click
    2. If not found: scroll → click "Lihat semua" → click back → retry tab
    Returns True if successfully opened the reviews panel.
    """

    # --- Step 1: Try direct tab click ---
    tab = _find_review_tab(page)
    if tab:
        tab.click()
        random_delay(2, 3)
        log.info("📑 Reviews tab found directly")
        return True

    # --- Step 2: Workaround - Scroll → "Lihat semua" → Back → Retry ---
    log.info("⚠️ Tab Ulasan not found directly, trying workaround (Lihat semua → Kembali)...")
    try:
        # Scroll the side panel using mouse wheel (like original)
        page.mouse.move(200, 500)
        for _ in range(3):
            page.mouse.wheel(0, 500)
            time.sleep(1)

        # Find and click "Lihat semua" button
        lihat_semua = page.locator('button[aria-label="Lihat semua"]').or_(
                      page.locator('button:has-text("Lihat semua")')).or_(
                      page.locator('button[aria-label="See all"]')).or_(
                      page.locator('button:has-text("See all")'))

        if lihat_semua.count() > 0 and lihat_semua.first.is_visible(timeout=3000):
            log.info("✅ Found 'Lihat semua' button, clicking...")
            lihat_semua.first.click()
            random_delay(2, 3)

            # Click back button (arrow left)
            log.info("⬅️ Clicking back button...")
            back_btn = page.locator('button[aria-label="Kembali"]').or_(
                       page.locator('button[aria-label="Back"]'))
            if back_btn.count() > 0:
                back_btn.first.click()
                random_delay(2, 3)

                # Now try finding tab again - it should appear after workaround
                tab = _find_review_tab(page)
                if tab:
                    tab.click()
                    random_delay(2, 3)
                    log.info("📑 Reviews tab found after workaround")
                    return True
                else:
                    log.warning("⚠️ Tab still not found after workaround")
            else:
                log.warning("⚠️ Back button not found")
        else:
            log.warning("⚠️ 'Lihat semua' button not found")
    except Exception as e:
        log.warning(f"⚠️ Workaround failed: {e}")

    return False


# ============================================================
# SORT BY NEWEST
# ============================================================
def sort_by_newest(page):
    """Try to sort reviews by 'Newest' / 'Terbaru'."""
    try:
        # Find the sort/menu button
        sort_selectors = [
            'button[aria-label*="Urutkan"]',
            'button[aria-label*="Sort"]',
            'button[data-value="sort"]',
            'button[aria-label*="sort"]',
        ]
        for sel in sort_selectors:
            try:
                btn = page.locator(sel)
                if btn.count() > 0 and btn.first.is_visible(timeout=3000):
                    btn.first.click()
                    random_delay(1, 2)

                    # Click "Terbaru" / "Newest" in dropdown
                    newest_selectors = [
                        'div[role="menuitemradio"]:has-text("Terbaru")',
                        'div[role="menuitemradio"]:has-text("Newest")',
                        'div[role="menuitem"]:has-text("Terbaru")',
                        'div[role="menuitem"]:has-text("Newest")',
                        'li[role="menuitemradio"]:has-text("Terbaru")',
                        'li[role="menuitemradio"]:has-text("Newest")',
                    ]
                    for nsel in newest_selectors:
                        try:
                            newest = page.locator(nsel)
                            if newest.count() > 0 and newest.first.is_visible(timeout=2000):
                                newest.first.click()
                                log.info("⚖️ Sorted by newest")
                                random_delay(2, 3)
                                return True
                        except (PlaywrightTimeout, Exception):
                            continue
                    break  # Sort button found but menu item failed
            except (PlaywrightTimeout, Exception):
                continue

        log.warning("⚖️ Sort button not found, using default order")
        return False
    except Exception as e:
        log.warning(f"⚖️ Sort failed: {e}")
        return False


# ============================================================
# SCROLL REVIEWS PANEL
# ============================================================
def scroll_reviews_panel(page):
    """
    Scroll the reviews container using JS.
    Uses multiple strategies to find the scrollable element.
    """
    scrolled = page.evaluate("""
        () => {
            // Strategy 1: Well-known Google Maps class combo
            let container = document.querySelector('.m6QErb.DxyBCb.kA9KIf.dS8AEf');

            // Strategy 2: Find scrollable ancestor of review cards
            if (!container) {
                const card = document.querySelector('div[data-review-id]') ||
                             document.querySelector('.jftiEf');
                if (card) {
                    let el = card.parentElement;
                    while (el && el !== document.body) {
                        const style = getComputedStyle(el);
                        if ((style.overflowY === 'auto' || style.overflowY === 'scroll') &&
                            el.scrollHeight > el.clientHeight + 50) {
                            container = el;
                            break;
                        }
                        el = el.parentElement;
                    }
                }
            }

            // Strategy 3: Any large scrollable div inside role="main"
            if (!container) {
                const main = document.querySelector('div[role="main"]');
                if (main) {
                    const divs = main.querySelectorAll('div');
                    for (const div of divs) {
                        const style = getComputedStyle(div);
                        if (div.scrollHeight > div.clientHeight + 200 &&
                            (style.overflowY === 'auto' || style.overflowY === 'scroll')) {
                            container = div;
                            break;
                        }
                    }
                }
            }

            if (container) {
                container.scrollBy(0, 2000);
                return true;
            }
            return false;
        }
    """)
    if not scrolled:
        # Fallback: mouse wheel
        page.mouse.move(400, 500)
        page.mouse.wheel(0, 2000)


# ============================================================
# EXPAND TRUNCATED REVIEWS
# ============================================================
def expand_review_texts(page):
    """Click all 'More'/'Lainnya'/'Selengkapnya' buttons to reveal full text."""
    try:
        page.evaluate("""
            () => {
                // Google Maps "More" buttons inside review cards
                // Try various known class names and text patterns
                const selectors = [
                    'button.w8nwRe.kyuRq',
                    'button.M77dve',
                ];
                for (const sel of selectors) {
                    document.querySelectorAll(sel).forEach(btn => {
                        try { btn.click(); } catch(e) {}
                    });
                }

                // Fallback: find buttons by text content
                document.querySelectorAll('button').forEach(btn => {
                    const text = btn.textContent.trim().toLowerCase();
                    if (text === 'lainnya' || text === 'more' ||
                        text === 'selengkapnya' || text === 'see more') {
                        // Only click small buttons (the "More" buttons are small)
                        if (btn.offsetWidth < 200) {
                            try { btn.click(); } catch(e) {}
                        }
                    }
                });
            }
        """)
        time.sleep(0.5)
    except Exception:
        pass


# ============================================================
# EXTRACT REVIEWS (JS-based for resilience)
# ============================================================
def extract_reviews_from_page(page):
    """
    Extract all currently visible review data using JavaScript.
    Uses multiple fallback selectors for each field to handle
    Google Maps' obfuscated and frequently-changing class names.
    """
    return page.evaluate("""
        () => {
            const reviews = [];
            const seen = new Set();

            // Primary: data-review-id is more stable than class names
            let cards = document.querySelectorAll('div[data-review-id]');
            // Fallback: known class
            if (cards.length === 0) cards = document.querySelectorAll('div.jftiEf');

            cards.forEach((card, idx) => {
                // Deduplicate by data-review-id
                const reviewId = card.getAttribute('data-review-id') || '';
                if (reviewId && seen.has(reviewId)) return;
                if (reviewId) seen.add(reviewId);

                // Skip nested cards (only process outermost)
                if (card.parentElement && card.parentElement.closest('div[data-review-id]')) return;

                try {
                    // ---- AUTHOR ----
                    let author = '';
                    // Try known class selectors
                    const authorSelectors = ['.d4r55', '.WNxzHc'];
                    for (const sel of authorSelectors) {
                        const el = card.querySelector(sel);
                        if (el && el.textContent.trim()) {
                            author = el.textContent.trim();
                            break;
                        }
                    }
                    // Fallback: profile button text
                    if (!author) {
                        const profileBtn = card.querySelector('button[data-review-id]') ||
                                           card.querySelector('a[data-href*="contrib"]');
                        if (profileBtn) {
                            const divs = profileBtn.querySelectorAll('div');
                            for (const d of divs) {
                                if (d.textContent.trim().length > 1 &&
                                    d.children.length === 0) {
                                    author = d.textContent.trim();
                                    break;
                                }
                            }
                        }
                    }

                    // ---- RATING ----
                    let rating = '';
                    // Stars use role="img" with descriptive aria-label
                    const ratingEl = card.querySelector('[role="img"][aria-label*="bintang"]') ||
                                     card.querySelector('[role="img"][aria-label*="star"]') ||
                                     card.querySelector('.kvMYJc') ||
                                     card.querySelector('.kvMY6c');
                    if (ratingEl) {
                        const ariaLabel = ratingEl.getAttribute('aria-label');
                        if (ariaLabel) {
                            // Extract just the number: "5 bintang" -> "5", "4 stars" -> "4"
                            const numMatch = ariaLabel.match(/(\\d)/);
                            rating = numMatch ? numMatch[1] : ariaLabel;
                        }
                    }

                    // ---- DATE ----
                    let dateText = '';
                    const dateSelectors = ['.rsqaWe', '.rS3nS', '.xRkPPb'];
                    for (const sel of dateSelectors) {
                        const el = card.querySelector(sel);
                        if (el && el.textContent.trim()) {
                            dateText = el.textContent.trim();
                            break;
                        }
                    }
                    // Fallback: scan spans for date-like text
                    if (!dateText) {
                        const dateKeywords = ['ago', 'lalu', 'yang', 'year', 'month',
                                              'week', 'day', 'tahun', 'bulan', 'minggu', 'hari'];
                        const spans = card.querySelectorAll('span');
                        for (const s of spans) {
                            const t = s.textContent.trim().toLowerCase();
                            if (dateKeywords.some(kw => t.includes(kw)) && t.length < 50) {
                                dateText = s.textContent.trim();
                                break;
                            }
                        }
                    }

                    // ---- COMMENT ----
                    let comment = '';
                    const commentSelectors = ['.wiI7ic', '.MyEned', '.review-full-text'];
                    for (const sel of commentSelectors) {
                        const el = card.querySelector(sel);
                        if (el && el.textContent.trim()) {
                            comment = el.textContent.trim();
                            break;
                        }
                    }
                    // Fallback: find longest text block in the card
                    if (!comment) {
                        let maxLen = 0;
                        const allSpans = card.querySelectorAll('span');
                        for (const span of allSpans) {
                            const txt = span.textContent.trim();
                            if (txt.length > maxLen && txt.length > 15 &&
                                !txt.includes('bintang') && !txt.includes('star') &&
                                txt !== author && txt !== dateText) {
                                comment = txt;
                                maxLen = txt.length;
                            }
                        }
                    }

                    // Only include if we got at least author or rating
                    if (author || rating) {
                        reviews.push({
                            author: author,
                            rating: rating,
                            date: dateText,
                            comment: comment.replace(/\\n/g, ' ').replace(/\\s+/g, ' ').trim()
                        });
                    }
                } catch(e) {
                    // Skip malformed card
                }
            });

            return reviews;
        }
    """)


# ============================================================
# SCRAPE SINGLE HOSPITAL
# ============================================================
def scrape_reviews_for_hospital(page, hospital_id, hospital_name, hospital_loc):
    """
    Scrape reviews for a single hospital.
    Returns a list of review dicts, empty list if no reviews,
    or None if reviews tab could not be found.
    """
    query = f"{hospital_name} {hospital_loc}"
    search_url = f"{MAPS_URL}/search/{query.replace(' ', '+')}"

    log.info(f"🔗 Navigating: {search_url[:90]}...")
    page.goto(search_url, wait_until="domcontentloaded", timeout=60000)
    random_delay(3, 5)

    # Handle consent on first load
    accept_consent(page)

    # --- Handle search results list vs direct place page ---
    try:
        results = page.locator('a.hfpxzc')
        if results.count() > 0:
            all_results = results.all()
            best_idx = 0
            for i, res in enumerate(all_results[:5]):
                try:
                    label = (res.get_attribute('aria-label') or '').strip()
                    if label and hospital_name.lower() in label.lower():
                        best_idx = i
                        log.info(f"🎯 Matched result #{i}: {label[:60]}")
                        break
                except Exception:
                    continue

            all_results[best_idx].click()
            random_delay(3, 5)
    except Exception as e:
        log.debug(f"Search result handling: {e}")

    # --- Navigate to reviews tab ---
    if not click_review_tab(page):
        log.warning(f"⚠️ Reviews tab not found for {hospital_name}")
        return None

    # --- Extract coordinates ---
    latitude = ""
    longitude = ""
    try:
        # Give a small moment for data to load
        random_delay(1, 2)
        
        # Method 1: Check JSON-LD data
        coords = page.evaluate("""() => {
            const scripts = document.querySelectorAll('script[type="application/ld+json"]');
            for (const s of scripts) {
                try {
                    const data = JSON.parse(s.textContent);
                    if (data.geo && data.geo.latitude) {
                        return { lat: data.geo.latitude, long: data.geo.longitude };
                    }
                } catch(e) {}
            }
            
            // Method 2: Check for meta tags
            const metaLat = document.querySelector('meta[itemprop="latitude"]');
            const metaLong = document.querySelector('meta[itemprop="longitude"]');
            if (metaLat && metaLong) {
                return { lat: metaLat.getAttribute('content'), long: metaLong.getAttribute('content') };
            }

            // Method 3: Look into static map images in the source
            const images = document.querySelectorAll('img');
            for (const img of images) {
                const src = img.src;
                if (src.includes('maps.googleapis.com/maps/api/staticmap')) {
                    const match = src.match(/center=(-?\d+\.\d+)%2C(-?\d+\.\d+)/);
                    if (match) return { lat: match[1], long: match[2] };
                }
            }
            return null;
        }""")
        
        if coords:
            latitude = str(coords['lat'])
            longitude = str(coords['long'])
        else:
            # Method 4: URL Regex
            current_url = page.url
            match_bang = re.search(r'!3d(-?\d+\.\d+)!4d(-?\d+\.\d+)', current_url)
            match_at = re.search(r'@(-?\d+\.\d+),(-?\d+\.\d+)', current_url)
            
            if match_bang:
                latitude = match_bang.group(1)
                longitude = match_bang.group(2)
            elif match_at:
                latitude = match_at.group(1)
                longitude = match_at.group(2)
            
        if latitude:
            log.info(f"📍 Coordinates found: {latitude}, {longitude}")
        else:
            log.warning(f"⚠️ Could not find coordinates for {hospital_name}")
    except Exception as e:
        log.debug(f"Failed to extract coordinates: {e}")

    # --- Sort by newest ---
    sort_by_newest(page)

    # --- Scroll and extract reviews ---
    collected_reviews = []
    prev_count = 0
    stale_count = 0        # How many scrolls produced no new reviews
    max_scrolls = 25       # Safety limit

    for scroll_num in range(max_scrolls):
        # Expand any truncated review texts
        expand_review_texts(page)

        # Extract all currently visible reviews
        current_reviews = extract_reviews_from_page(page)
        current_count = len(current_reviews)

        # Check if we've reached our limit
        if current_count >= MAX_REVIEWS_PER_HOSPITAL:
            collected_reviews = current_reviews[:MAX_REVIEWS_PER_HOSPITAL]
            log.info(f"📊 Reached max {MAX_REVIEWS_PER_HOSPITAL} reviews at scroll #{scroll_num}")
            break

        # Check if oldest visible review exceeds our time limit
        if current_reviews:
            last_date = current_reviews[-1].get('date', '')
            if is_older_than_limit(last_date):
                collected_reviews = [
                    r for r in current_reviews
                    if not is_older_than_limit(r.get('date', ''))
                ]
                log.info(f"⏳ Age limit reached ({last_date}), keeping {len(collected_reviews)} reviews")
                break

        # Check if scrolling is still loading new reviews
        if current_count == prev_count:
            stale_count += 1
            if stale_count >= 3:
                collected_reviews = current_reviews
                log.info(f"📊 No new reviews after {stale_count} scrolls (total: {current_count})")
                break
        else:
            stale_count = 0

        prev_count = current_count
        collected_reviews = current_reviews

        # Scroll for more
        scroll_reviews_panel(page)
        random_delay(1.5, 2.5)

    return [
        {**r, 'latitude': latitude, 'longitude': longitude} 
        for r in collected_reviews[:MAX_REVIEWS_PER_HOSPITAL]
    ]


# ============================================================
# MAIN SCRAPER LOOP
# ============================================================
def main():
    log.info("=" * 60)
    log.info(f"🚀 Google Maps Review Scraper Started")
    log.info(f"   Max reviews/hospital: {MAX_REVIEWS_PER_HOSPITAL}")
    log.info(f"   Max age: {MAX_YEARS_AGO} years")
    log.info(f"   Batch size: {BATCH_SIZE}")
    log.info(f"   Browser restart every: {BROWSER_RESTART_EVERY} hospitals")
    log.info("=" * 60)

    scraped_ids = get_scraped_ids()
    log.info(f"🔄 Resume: {len(scraped_ids)} hospitals already scraped")

    # Load hospital list
    with open(INPUT_FILE, mode='r', encoding='utf-8') as f:
        reader = csv.DictReader(f, delimiter=';')
        hospitals = list(reader)
    log.info(f"📋 Total hospitals in database: {len(hospitals)}")

    processed_count = 0
    success_count = 0
    fail_count = 0
    start_time = datetime.now()

    with sync_playwright() as pw:
        browser, context, page = create_browser(pw)

        # Initial navigation to accept cookies
        try:
            page.goto(MAPS_URL, wait_until="domcontentloaded", timeout=60000)
            random_delay(2, 4)
            accept_consent(page)
        except Exception as e:
            log.warning(f"Initial Maps load warning: {e}")

        for rs in hospitals:
            if rs['kode'] in scraped_ids:
                continue

            if processed_count >= BATCH_SIZE:
                log.info(f"🛑 Batch size {BATCH_SIZE} reached. Stopping.")
                break

            # --- Restart browser periodically ---
            if processed_count > 0 and processed_count % BROWSER_RESTART_EVERY == 0:
                log.info(f"🔄 Restarting browser (every {BROWSER_RESTART_EVERY} hospitals)...")
                try:
                    browser.close()
                except Exception:
                    pass
                random_delay(5, 10)
                browser, context, page = create_browser(pw)
                try:
                    page.goto(MAPS_URL, wait_until="domcontentloaded", timeout=60000)
                    random_delay(2, 4)
                    accept_consent(page)
                except Exception:
                    pass

            hospital_id = rs['kode']
            hospital_name = rs['nama'].strip()
            hospital_loc = f"{rs['kab']}, {rs['nama_prop']}"

            log.info(f"\n🏥 [{processed_count + 1}] {hospital_name} ({hospital_id})")

            try:
                reviews = scrape_reviews_for_hospital(
                    page, hospital_id, hospital_name, hospital_loc
                )

                if reviews is None:
                    # Reviews tab not found
                    fail_count += 1
                    save_review({
                        'hospital_id': hospital_id,
                        'hospital_name': hospital_name,
                        'author': 'NO_REVIEWS_TAB',
                        'rating': '',
                        'date': '',
                        'comment': 'Reviews tab not found'
                    })
                elif len(reviews) == 0:
                    log.info(f"📭 No reviews found for {hospital_name}")
                    save_review({
                        'hospital_id': hospital_id,
                        'hospital_name': hospital_name,
                        'author': 'NO_REVIEWS',
                        'rating': '',
                        'date': '',
                        'comment': 'No reviews available',
                        'latitude': '',
                        'longitude': '',
                        'scraping_timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    })
                else:
                    scraping_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                    for review in reviews:
                        save_review({
                            'hospital_id': hospital_id,
                            'hospital_name': hospital_name,
                            'author': review.get('author', ''),
                            'rating': review.get('rating', ''),
                            'date': review.get('date', ''),
                            'comment': review.get('comment', ''),
                            'latitude': review.get('latitude', ''),
                            'longitude': review.get('longitude', ''),
                            'scraping_timestamp': scraping_ts
                        })
                    success_count += 1
                    log.info(f"✅ Saved {len(reviews)} reviews for {hospital_name}")

            except Exception as e:
                fail_count += 1
                log.error(f"❌ Error for {hospital_name}: {e}")
                try:
                    page.screenshot(
                        path=os.path.join(SCREENSHOT_DIR, f"error_{hospital_id}.png")
                    )
                except Exception:
                    pass
                save_review({
                    'hospital_id': hospital_id,
                    'hospital_name': hospital_name,
                    'author': 'ERROR',
                    'rating': '',
                    'date': '',
                    'comment': str(e)[:200]
                })

            processed_count += 1
            # Random delay between hospitals
            random_delay(3, 7)

        try:
            browser.close()
        except Exception:
            pass

    elapsed = datetime.now() - start_time
    log.info("\n" + "=" * 60)
    log.info("🏁 Scraping Complete!")
    log.info(f"   Processed : {processed_count}")
    log.info(f"   Successful: {success_count}")
    log.info(f"   Failed    : {fail_count}")
    log.info(f"   Duration  : {elapsed}")
    log.info(f"   Output    : {OUTPUT_FILE}")
    log.info("=" * 60)


if __name__ == "__main__":
    main()
