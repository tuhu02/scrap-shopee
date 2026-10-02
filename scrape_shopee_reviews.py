"""
Pengumpul data produk laptop dan ulasan publik di Shopee Indonesia.

Cara kerja:
- Membuka Google Chrome terpasang (bukan Chromium Playwright) seperti pengguna biasa.
- Menyimpan sesi di data/chrome-profile agar captcha cukup diselesaikan sekali.
- Menangkap JSON halaman pencarian + ulasan, plus cadangan dari tautan DOM.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

import pandas as pd
from playwright.sync_api import BrowserContext, Page, Response
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

BASE_URL = "https://shopee.co.id"
SEARCH_API_MARKERS = (
    "search_items",
    "search/search_items",
    "search_v2",
    "recommend_v2",
    "search_hint",
)
REVIEW_API_MARKERS = ("get_ratings", "product_ratings", "pdp/get")
ITEM_ID_PATTERN = re.compile(r"(?:-i\.|/product/)(\d+)\.(\d+)|/product/(\d+)/(\d+)")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Kumpulkan produk laptop dan ulasan Shopee.")
    parser.add_argument("--keyword", default="laptop", help="Kata kunci pencarian")
    parser.add_argument("--max-products", type=int, default=100, help="Jumlah produk target")
    parser.add_argument("--reviews-per-product", type=int, default=10, help="Ulasan berteks per produk")
    parser.add_argument("--output-dir", default="data", help="Folder hasil")
    parser.add_argument("--headless", action="store_true", help="Jalankan tanpa jendela browser")
    parser.add_argument("--min-delay", type=float, default=2.5, help="Jeda minimum antar produk (detik)")
    parser.add_argument("--max-delay", type=float, default=5.0, help="Jeda maksimum antar produk (detik)")
    parser.add_argument(
        "--isolated-profile",
        action="store_true",
        help="Pakai profil Chrome terpisah (lebih sering diblokir Shopee)",
    )
    return parser.parse_args()


def sleep_jitter(min_delay: float, max_delay: float) -> None:
    time.sleep(random.uniform(min_delay, max_delay))


_EXTERNAL_BROWSER: subprocess.Popen[bytes] | None = None
_KEEP_BROWSER = False


def ensure_chromium() -> None:
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def find_browser_exe(kind: str) -> Path | None:
    program_files = Path(os.environ.get("PROGRAMFILES", r"C:\Program Files"))
    program_files_x86 = Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"))
    local_app = Path(os.environ.get("LOCALAPPDATA", ""))
    if kind == "chrome":
        candidates = [
            program_files / "Google/Chrome/Application/chrome.exe",
            program_files_x86 / "Google/Chrome/Application/chrome.exe",
            local_app / "Google/Chrome/Application/chrome.exe",
        ]
    else:
        candidates = [
            program_files / "Microsoft/Edge/Application/msedge.exe",
            program_files_x86 / "Microsoft/Edge/Application/msedge.exe",
            local_app / "Microsoft/Edge/Application/msedge.exe",
        ]
    for path in candidates:
        if path.is_file():
            return path
    return None


def chrome_is_running() -> bool:
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq chrome.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
            check=False,
        )
        return "chrome.exe" in result.stdout.lower()
    except Exception:
        return False


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.25)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def wait_for_cdp(p, port: int, proc: subprocess.Popen[bytes] | None) -> BrowserContext:
    last_error: Exception | None = None
    for _ in range(80):
        if proc is not None and proc.poll() is not None:
            raise PlaywrightError(
                "Chrome langsung tertutup. Tutup semua proses chrome.exe di Task Manager, lalu jalankan skrip lagi."
            )
        try:
            browser = p.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            return context
        except Exception as exc:
            last_error = exc
            time.sleep(0.4)
    hint = ""
    if not port_is_open(port):
        hint = (
            " Port debugging tidak aktif. Chrome versi baru menolak remote debugging "
            "pada profil default, atau masih ada Chrome lain yang sudah jalan."
        )
    raise PlaywrightError(f"Gagal tersambung ke Chrome: {last_error}.{hint}")


def clear_profile_locks(profile_dir: Path) -> None:
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
        lock = profile_dir / name
        try:
            if lock.exists() or lock.is_symlink():
                lock.unlink()
        except OSError:
            continue


def stop_external_browser() -> None:
    global _EXTERNAL_BROWSER
    proc = _EXTERNAL_BROWSER
    _EXTERNAL_BROWSER = None
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except Exception:
        proc.kill()


def chrome_already_running_error() -> PlaywrightError:
    return PlaywrightError(
        "Google Chrome masih berjalan di background (proses chrome.exe), "
        "meskipun jendela tidak terlihat. Tutup dari Task Manager, atau di PowerShell jalankan:\n"
        "  taskkill /IM chrome.exe /F\n"
        "Lalu jalankan skrip ini lagi. Di Chrome: Settings > System > matikan "
        "'Continue running background apps when Google Chrome is closed'."
    )


def real_chrome_user_data() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"


def last_used_chrome_profile(user_data: Path) -> str:
    local_state = user_data / "Local State"
    try:
        data = json.loads(local_state.read_text(encoding="utf-8"))
        name = str(data.get("profile", {}).get("last_used") or "Default")
        if (user_data / name).is_dir():
            return name
    except Exception:
        pass
    return "Default"


def _copy_file_quiet(src: Path, dest: Path) -> bool:
    try:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return True
    except OSError:
        return False


def _copy_tree_quiet(src: Path, dest: Path, skip_dirs: set[str]) -> int:
    copied = 0
    if not src.is_dir():
        return 0
    for root, dirs, files in os.walk(src):
        dirs[:] = [name for name in dirs if name not in skip_dirs]
        rel = Path(root).relative_to(src)
        for name in files:
            if _copy_file_quiet(Path(root) / name, dest / rel / name):
                copied += 1
    return copied


def seed_cdp_profile(dest: Path) -> str:
    """Salin cookie/login profil Chrome yang terakhir dipakai. Mengembalikan nama folder profil."""
    marker = dest / ".seeded-from-chrome"
    src = real_chrome_user_data()
    profile_name = last_used_chrome_profile(src) if src.is_dir() else "Default"
    if marker.exists():
        saved = marker.read_text(encoding="utf-8").strip()
        return saved or profile_name
    if not (src / profile_name).is_dir():
        dest.mkdir(parents=True, exist_ok=True)
        print("Profil Chrome harian tidak ditemukan; memakai profil kosong.")
        return "Default"

    dest.mkdir(parents=True, exist_ok=True)
    print(f"Menyalin cookie/login dari profil Chrome '{profile_name}' (tanpa cache/sertifikat)...")
    skip_dirs = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "GrShaderCache",
        "ShaderCache",
        "DawnCache",
        "GraphiteDawnCache",
        "Service Worker",
        "BrowserMetrics",
        "Crashpad",
        "Safe Browsing",
        "Temp",
        "ScreenCaptureCache",
        "CertificateRevocation",
        "OptimizationHints",
        "optimization_guide_model_store",
        "component_crx_cache",
        "extensions_crx_cache",
    }
    file_names = (
        "Preferences",
        "Secure Preferences",
        "Cookies",
        "Cookies-journal",
        "Login Data",
        "Login Data-journal",
        "Web Data",
        "Web Data-journal",
        "History",
        "Visited Links",
        "TransportSecurity",
    )
    dir_names = ("Network", "Local Storage", "Session Storage", "IndexedDB")
    copied = 0
    _copy_file_quiet(src / "Local State", dest / "Local State")
    _copy_file_quiet(src / "First Run", dest / "First Run")
    for name in file_names:
        if _copy_file_quiet(src / profile_name / name, dest / profile_name / name):
            copied += 1
    for name in dir_names:
        copied += _copy_tree_quiet(src / profile_name / name, dest / profile_name / name, skip_dirs)
    clear_profile_locks(dest)
    marker.write_text(profile_name, encoding="utf-8")
    print(f"Salinan profil siap ({copied} file).")
    return profile_name


def launch_real_chrome(p, output_dir: Path) -> BrowserContext:
    global _EXTERNAL_BROWSER, _KEEP_BROWSER
    exe = find_browser_exe("chrome")
    if exe is None:
        raise PlaywrightError("Google Chrome tidak ditemukan.")
    if chrome_is_running():
        raise chrome_already_running_error()

    # Chrome 136+ menolak remote debugging pada profil default.
    # Solusi: salin cookie ke folder lain, lalu debug folder itu.
    profile_dir = (output_dir / "chrome-cdp-session").resolve()
    profile_name = seed_cdp_profile(profile_dir)
    port = pick_free_port()
    cmd = [
        str(exe),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        f"--profile-directory={profile_name}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
    ]
    _EXTERNAL_BROWSER = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _KEEP_BROWSER = True
    try:
        context = wait_for_cdp(p, port, _EXTERNAL_BROWSER)
    except Exception:
        stop_external_browser()
        raise
    print(
        "Chrome terbuka memakai salinan profil harian Anda. "
        "Jika Shopee minta login/captcha, selesaikan di jendela ini."
    )
    return context


def launch_isolated_chrome(p, exe: Path, profile_dir: Path, headless: bool) -> BrowserContext:
    global _EXTERNAL_BROWSER, _KEEP_BROWSER
    clear_profile_locks(profile_dir)
    port = pick_free_port()
    cmd = [
        str(exe),
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--profile-directory=ShopeeScraper",
        "--no-first-run",
        "--no-default-browser-check",
    ]
    if headless:
        cmd.append("--headless=new")
    _EXTERNAL_BROWSER = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    _KEEP_BROWSER = False
    try:
        return wait_for_cdp(p, port, _EXTERNAL_BROWSER)
    except Exception:
        stop_external_browser()
        raise


def launch_context(p, headless: bool, output_dir: Path, isolated_profile: bool) -> BrowserContext:
    if not isolated_profile:
        return launch_real_chrome(p, output_dir)

    profile_dir = (output_dir / "chrome-profile").resolve()
    profile_dir.mkdir(parents=True, exist_ok=True)
    exe = find_browser_exe("chrome")
    if exe is None:
        raise PlaywrightError("Google Chrome tidak ditemukan.")
    context = launch_isolated_chrome(p, exe, profile_dir, headless)
    print(f"Chrome terpisah terbuka: {profile_dir}")
    return context


def safe_json(response: Response) -> Any | None:
    try:
        return response.json()
    except Exception:
        return None


def walk_item_dicts(node: Any, found: list[dict[str, Any]]) -> None:
    if isinstance(node, dict):
        itemid = node.get("itemid") or node.get("item_id")
        shopid = node.get("shopid") or node.get("shop_id")
        name = node.get("name") or node.get("title")
        if itemid and shopid and name:
            found.append(node)
        for value in node.values():
            walk_item_dicts(value, found)
    elif isinstance(node, list):
        for value in node:
            walk_item_dicts(value, found)


def normalize_product(item: dict[str, Any]) -> dict[str, Any] | None:
    itemid = item.get("itemid") or item.get("item_id")
    shopid = item.get("shopid") or item.get("shop_id")
    name = item.get("name") or item.get("title")
    if not itemid or not shopid or not name:
        return None
    price = item.get("price")
    if isinstance(price, (int, float)) and price > 1000:
        price_rp = int(price) // 100_000
    else:
        price_rp = price
    rating_info = item.get("item_rating") or {}
    rating = rating_info.get("rating_star") if isinstance(rating_info, dict) else None
    rating_count = rating_info.get("rating_count") if isinstance(rating_info, dict) else None
    if isinstance(rating_count, list) and rating_count:
        rating_count = rating_count[0]
    return {
        "itemid": str(itemid),
        "shopid": str(shopid),
        "name": name,
        "price": price_rp,
        "sold": item.get("historical_sold") or item.get("sold") or item.get("item_sold"),
        "rating_star": rating,
        "rating_count": rating_count,
        "shop_name": item.get("shop_name") or item.get("shopname"),
        "url": f"{BASE_URL}/product/{shopid}/{itemid}",
    }


def extract_items_from_search_payload(payload: Any) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    walk_item_dicts(payload, raw)
    products: dict[str, dict[str, Any]] = {}
    for item in raw:
        normalized = normalize_product(item)
        if normalized:
            products[normalized["itemid"]] = normalized
    return list(products.values())


def extract_reviews_from_payload(payload: dict[str, Any], product: dict[str, Any]) -> list[dict[str, Any]]:
    reviews: list[dict[str, Any]] = []
    data = payload.get("data") or payload
    ratings = data.get("ratings") or data.get("item_rating_list") or []
    if not isinstance(ratings, list):
        return reviews
    for rating in ratings:
        if not isinstance(rating, dict):
            continue
        comment = (rating.get("comment") or rating.get("comment_text") or "").strip()
        if not comment:
            continue
        variation = None
        product_items = rating.get("product_items") or []
        if product_items and isinstance(product_items[0], dict):
            variation = product_items[0].get("model_name")
        ctime = rating.get("ctime") or rating.get("mtime")
        created_at = None
        if isinstance(ctime, (int, float)):
            created_at = datetime.fromtimestamp(int(ctime), tz=timezone.utc).isoformat()
        reviews.append(
            {
                "itemid": product["itemid"],
                "shopid": product["shopid"],
                "product_name": product["name"],
                "product_url": product["url"],
                "cmtid": rating.get("cmtid"),
                "username": rating.get("author_username") or rating.get("author"),
                "rating_star": rating.get("rating_star"),
                "comment": comment,
                "variation": variation,
                "like_count": rating.get("like_count"),
                "created_at": created_at,
            }
        )
    return reviews


def close_popups(page: Page) -> None:
    selectors = [
        "button:has-text('Nanti Saja')",
        "button:has-text('Nanti saja')",
        "button:has-text('Tutup')",
        "button:has-text('Close')",
        "button:has-text('OK')",
        "button:has-text('Mengerti')",
        ".shopee-popup__close-btn",
        ".home-popup__close-button",
        "[class*='close-btn']",
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector).first
            if loc.count() and loc.is_visible(timeout=800):
                loc.click(timeout=1000)
        except Exception:
            continue


def is_login_wall(page: Page) -> bool:
    try:
        html = page.content()
        markers = ("Masuk Diperlukan", "belum masuk", "Log In", "login?next=")
        return any(marker in html for marker in markers)
    except Exception:
        return False


def is_captcha_wall(page: Page) -> bool:
    try:
        url = page.url.lower()
        if "/verify/captcha" in url or "anti_bot" in url:
            return True
        body = page.inner_text("body", timeout=1500)
        return "Silakan Coba Lagi Nanti" in body or "Verifikasi gagal" in body
    except Exception:
        return False


def is_hard_verify_fail(page: Page) -> bool:
    try:
        body = page.inner_text("body", timeout=1500)
        return "Silakan Coba Lagi Nanti" in body or "Verifikasi gagal" in body
    except Exception:
        return False


def wait_until_search_looks_ready(page: Page) -> None:
    print()
    print("Halaman verifikasi Shopee bukan error skrip: akses sempat ditolak.")
    print("Jangan klik 'Coba Lagi' berulang kali (blokirnya makin lama).")
    print("Di jendela Chrome:")
    print("  - Jika tertulis 'Coba Lagi Nanti': biarkan 20-30 menit, atau buka tab baru ke https://shopee.co.id")
    print("  - Login jika diminta, selesaikan captcha jika muncul puzzle/gambar")
    print("  - Buka pencarian 'laptop' sampai kartu produk terlihat")
    print("Lalu kembali ke PowerShell.")
    try:
        input("Tekan Enter setelah daftar produk tampil... ")
    except EOFError:
        print("Input tidak tersedia; menunggu hingga 20 menit...")
        deadline = time.time() + 1200
        while time.time() < deadline and not has_product_links(page):
            time.sleep(5)


def has_product_links(page: Page) -> bool:
    try:
        return page.locator("a[href*='-i.'], a[href*='/product/']").count() > 0
    except Exception:
        return False


def wait_if_blocked(page: Page, timeout_sec: int = 180) -> None:
    deadline = time.time() + timeout_sec
    warned = False
    while time.time() < deadline:
        if has_product_links(page):
            return
        if is_captcha_wall(page) or is_login_wall(page) or not has_product_links(page):
            if not warned:
                if is_hard_verify_fail(page):
                    print(
                        "Shopee menolak verifikasi ('Coba Lagi Nanti'). "
                        "Jangan spam tombol itu. Buka tab baru ke https://shopee.co.id "
                        "atau tunggu 20-30 menit. "
                        f"Skrip tetap menunggu maksimal {max(timeout_sec // 60, 1)} menit..."
                    )
                elif is_captcha_wall(page):
                    print(
                        "Shopee menampilkan verifikasi anti-bot. "
                        "Selesaikan puzzle/captcha di jendela browser. "
                        f"Skrip menunggu maksimal {max(timeout_sec // 60, 1)} menit..."
                    )
                else:
                    print(
                        "Halaman pencarian belum menampilkan produk (login/captcha). "
                        "Selesaikan di jendela browser yang terbuka. "
                        f"Skrip menunggu maksimal {max(timeout_sec // 60, 1)} menit..."
                    )
                warned = True
                if not is_hard_verify_fail(page):
                    try:
                        page.get_by_role("button", name=re.compile(r"Log In|Masuk", re.I)).first.click(timeout=2000)
                    except Exception:
                        pass
            time.sleep(5)
            continue
        return


def goto_resilient(page: Page, url: str) -> Page:
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=90_000)
        return page
    except (PlaywrightTimeout, PlaywrightError) as exc:
        print(f"    Navigasi bermasalah ({exc.__class__.__name__}: {str(exc)[:120]}). Mencoba lagi...")
        try:
            page.reload(wait_until="domcontentloaded", timeout=60_000)
            return page
        except Exception:
            context = page.context
            try:
                page.close()
            except Exception:
                pass
            new_page = context.new_page()
            new_page.goto(url, wait_until="domcontentloaded", timeout=90_000)
            return new_page


def dump_debug(page: Page, output_dir: Path, name: str) -> None:
    debug_dir = output_dir / "debug"
    debug_dir.mkdir(parents=True, exist_ok=True)
    try:
        page.screenshot(path=str(debug_dir / f"{name}.png"), full_page=False)
        (debug_dir / f"{name}.html").write_text(page.content(), encoding="utf-8")
        print(f"    Debug disimpan: {debug_dir / name}.png")
    except Exception as exc:
        print(f"    Gagal menyimpan debug: {exc}")


def parse_product_href(href: str, name: str = "") -> dict[str, Any] | None:
    if not href:
        return None
    match = re.search(r"-i\.(\d+)\.(\d+)", href)
    if match:
        shopid, itemid = match.group(1), match.group(2)
    else:
        match = re.search(r"/product/(\d+)/(\d+)", href)
        if not match:
            return None
        shopid, itemid = match.group(1), match.group(2)
    clean = href.split("?")[0]
    if clean.startswith("/"):
        clean = BASE_URL + clean
    elif not clean.startswith("http"):
        clean = f"{BASE_URL}/product/{shopid}/{itemid}"
    return {
        "itemid": itemid,
        "shopid": shopid,
        "name": name or f"produk-{itemid}",
        "price": None,
        "sold": None,
        "rating_star": None,
        "rating_count": None,
        "shop_name": None,
        "url": clean,
    }


def collect_products_from_dom(page: Page) -> list[dict[str, Any]]:
    rows = page.evaluate(
        """() => {
            const out = [];
            for (const a of document.querySelectorAll("a[href]")) {
                const href = a.getAttribute("href") || "";
                if (!(href.includes("-i.") || href.includes("/product/"))) continue;
                out.push({href, name: (a.innerText || "").split("\\n")[0].trim()});
            }
            return out;
        }"""
    )
    products: dict[str, dict[str, Any]] = {}
    for row in rows or []:
        parsed = parse_product_href(row.get("href", ""), row.get("name", ""))
        if parsed:
            products[parsed["itemid"]] = parsed
    return list(products.values())


def collect_products_from_search(
    page: Page,
    keyword: str,
    max_products: int,
    output_dir: Path,
) -> tuple[Page, list[dict[str, Any]]]:
    products: dict[str, dict[str, Any]] = {}

    def on_response(response: Response) -> None:
        url = response.url.lower()
        if response.status != 200:
            return
        interesting = any(marker in url for marker in SEARCH_API_MARKERS) or "search" in url
        if not interesting:
            return
        payload = safe_json(response)
        if payload is None:
            return
        for item in extract_items_from_search_payload(payload):
            products[item["itemid"]] = item

    page.on("response", on_response)
    print("Membuka beranda Shopee dulu (bukan langsung pencarian)...")
    try:
        page = goto_resilient(page, BASE_URL)
    except PlaywrightError as exc:
        print(f"  Gagal membuka beranda: {exc}")
    close_popups(page)
    if is_captcha_wall(page) or is_login_wall(page) or not has_product_links(page):
        wait_until_search_looks_ready(page)

    if has_product_links(page):
        for item in collect_products_from_dom(page):
            products[item["itemid"]] = item
        print(f"  Produk dari halaman yang sudah terbuka: {len(products)}")

    page_no = 0
    empty_streak = 0
    while len(products) < max_products and page_no < 12:
        search_url = f"{BASE_URL}/search?keyword={quote_plus(keyword)}&page={page_no}"
        print(f"Membuka pencarian '{keyword}' halaman {page_no + 1} ...")
        before = len(products)
        try:
            page = goto_resilient(page, search_url)
        except PlaywrightError as exc:
            print(f"  Gagal membuka halaman: {exc}")
            page_no += 1
            continue

        # Pasang ulang listener jika halaman baru dibuat setelah crash.
        try:
            page.on("response", on_response)
        except Exception:
            pass

        close_popups(page)
        wait_if_blocked(page, timeout_sec=600 if page_no == 0 else 90)
        page.wait_for_timeout(5000)
        for _ in range(8):
            page.mouse.wheel(0, 1600)
            page.wait_for_timeout(700)

        for item in collect_products_from_dom(page):
            products.setdefault(item["itemid"], item)

        gained = len(products) - before
        print(f"  Produk terkumpul: {len(products)}/{max_products} (+{gained})")
        if gained == 0:
            empty_streak += 1
            dump_debug(page, output_dir, f"search_page_{page_no + 1}")
            if empty_streak >= 3:
                print("  Tiga halaman berturut-turut kosong. Berhenti mencari.")
                break
        else:
            empty_streak = 0
        page_no += 1
        sleep_jitter(1.8, 3.2)

    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    return page, list(products.values())[:max_products]


def collect_reviews_for_product(
    page: Page,
    product: dict[str, Any],
    reviews_per_product: int,
    min_delay: float,
    max_delay: float,
) -> tuple[Page, list[dict[str, Any]]]:
    collected: dict[str, dict[str, Any]] = {}

    def on_response(response: Response) -> None:
        if not any(marker in response.url for marker in REVIEW_API_MARKERS):
            return
        payload = safe_json(response)
        if not isinstance(payload, dict):
            return
        for review in extract_reviews_from_payload(payload, product):
            key = str(review.get("cmtid") or review["comment"][:80])
            collected[key] = review

    page.on("response", on_response)
    print(f"  Membuka produk {product['itemid']}: {product['name'][:70]}")
    try:
        page = goto_resilient(page, product["url"])
        try:
            page.on("response", on_response)
        except Exception:
            pass
    except PlaywrightError:
        print("    Gagal membuka halaman produk, dilewati.")
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass
        return page, []

    close_popups(page)
    wait_if_blocked(page, timeout_sec=60)
    page.wait_for_timeout(2500)

    for _ in range(12):
        page.mouse.wheel(0, 1600)
        page.wait_for_timeout(450)
        if len(collected) >= reviews_per_product:
            break

    for label in ("Ulasan", "Penilaian", "Semua"):
        try:
            tab = page.get_by_text(label, exact=False).first
            if tab.is_visible(timeout=800):
                tab.click(timeout=1500)
                page.wait_for_timeout(1500)
                break
        except Exception:
            continue

    next_selectors = [
        "button.shopee-icon-button--right",
        "button[aria-label='Next']",
        "nav button:last-child",
    ]
    clicks = 0
    while len(collected) < reviews_per_product and clicks < 6:
        clicked = False
        for selector in next_selectors:
            try:
                btn = page.locator(selector).first
                if btn.count() and btn.is_enabled() and btn.is_visible(timeout=500):
                    btn.click(timeout=1500)
                    clicked = True
                    page.wait_for_timeout(1800)
                    break
            except Exception:
                continue
        if not clicked:
            break
        clicks += 1

    try:
        page.remove_listener("response", on_response)
    except Exception:
        pass
    sleep_jitter(min_delay, max_delay)
    reviews = list(collected.values())[:reviews_per_product]
    print(f"    Ulasan berteks: {len(reviews)}")
    return page, reviews


def save_outputs(output_dir: Path, products: list[dict[str, Any]], reviews: list[dict[str, Any]]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(products).to_csv(output_dir / "products.csv", index=False, encoding="utf-8-sig")
    pd.DataFrame(reviews).to_csv(output_dir / "reviews.csv", index=False, encoding="utf-8-sig")
    (output_dir / "products.json").write_text(json.dumps(products, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "reviews.json").write_text(json.dumps(reviews, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nTersimpan. Produk: {len(products)} | Ulasan: {len(reviews)}")
    print(f"File: {output_dir / 'products.csv'} dan {output_dir / 'reviews.csv'}")


def main() -> int:
    args = parse_args()
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        try:
            context = launch_context(p, args.headless, output_dir, args.isolated_profile)
        except PlaywrightError as exc:
            print(str(exc))
            stop_external_browser()
            return 1
        try:
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page, products = collect_products_from_search(page, args.keyword, args.max_products, output_dir)
                if not products:
                    dump_debug(page, output_dir, "search_empty")
                    print("Tidak ada produk yang terkumpul. Lihat data/debug dan selesaikan captcha di jendela browser skrip.")
                    return 1

                all_reviews: list[dict[str, Any]] = []
                for idx, product in enumerate(products, start=1):
                    print(f"[{idx}/{len(products)}]")
                    page, reviews = collect_reviews_for_product(
                        page,
                        product,
                        args.reviews_per_product,
                        args.min_delay,
                        args.max_delay,
                    )
                    all_reviews.extend(reviews)
                    if idx % 5 == 0:
                        save_outputs(output_dir, products[:idx], all_reviews)

                save_outputs(output_dir, products, all_reviews)
            finally:
                if not _KEEP_BROWSER:
                    try:
                        context.close()
                    except Exception:
                        pass
        finally:
            if not _KEEP_BROWSER:
                stop_external_browser()
    return 0


if __name__ == "__main__":
    sys.exit(main())
