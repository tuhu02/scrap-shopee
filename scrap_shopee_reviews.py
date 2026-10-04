"""
Pengumpul data produk laptop dan ulasan publik di Shopee Indonesia.

Perbaikan utama:
- Nama produk diprioritaskan dari Search API Shopee.
- DOM hanya digunakan sebagai fallback.
- Mencegah badge seperti "%" / "Rp" / "Diskon" menjadi nama produk.
- Data API dan DOM digabung berdasarkan itemid.
- Jika nama dari DOM salah, nama dari API dapat menggantikannya.
- product_name pada reviews.csv menggunakan nama produk final.
- Jika nama belum ditemukan, mencoba mengambil nama dari halaman produk.
- Produk dengan ulasan berteks kurang dari batas minimal dilewati.

Contoh:
    python scraper_shopee.py
    python scraper_shopee.py --keyword laptop --max-products 5 --reviews-per-product 10
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


# ============================================================
# KONFIGURASI
# ============================================================

BASE_URL = "https://shopee.co.id"

SEARCH_API_MARKERS = (
    "search_items",
    "search/search_items",
    "search_v2",
    "recommend_v2",
    "search_hint",
)

REVIEW_API_MARKERS = (
    "get_ratings",
    "product_ratings",
    "pdp/get",
)

ITEM_ID_PATTERN = re.compile(
    r"(?:-i\.|/product/)(\d+)\.(\d+)|/product/(\d+)/(\d+)"
)


# ============================================================
# ARGUMENT
# ============================================================

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Kumpulkan produk laptop dan ulasan Shopee."
    )

    parser.add_argument(
        "--keyword",
        default="laptop",
        help="Kata kunci pencarian",
    )

    parser.add_argument(
        "--max-products",
        type=int,
        default=100,
        help="Jumlah produk lolos yang ditargetkan",
    )

    parser.add_argument(
        "--reviews-per-product",
        type=int,
        default=10,
        help="Ulasan berteks yang diambil per produk",
    )

    parser.add_argument(
        "--min-reviews",
        type=int,
        default=None,
        help=(
            "Minimal ulasan berteks agar produk disimpan "
            "(default: sama dengan --reviews-per-product)"
        ),
    )

    parser.add_argument(
        "--candidate-multiplier",
        type=int,
        default=3,
        help=(
            "Kumpulkan kandidat produk sebanyak "
            "max-products x angka ini"
        ),
    )

    parser.add_argument(
        "--output-dir",
        default="data",
        help="Folder hasil",
    )

    parser.add_argument(
        "--headless",
        action="store_true",
        help="Jalankan tanpa jendela browser",
    )

    parser.add_argument(
        "--min-delay",
        type=float,
        default=2.5,
        help="Jeda minimum antar produk (detik)",
    )

    parser.add_argument(
        "--max-delay",
        type=float,
        default=5.0,
        help="Jeda maksimum antar produk (detik)",
    )

    parser.add_argument(
        "--isolated-profile",
        action="store_true",
        help="Pakai profil Chrome terpisah",
    )

    return parser.parse_args()


# ============================================================
# UTILITAS UMUM
# ============================================================

def sleep_jitter(min_delay: float, max_delay: float) -> None:
    time.sleep(random.uniform(min_delay, max_delay))


_EXTERNAL_BROWSER: subprocess.Popen[bytes] | None = None
_KEEP_BROWSER = False


def ensure_chromium() -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "playwright",
            "install",
            "chromium",
        ],
        check=True,
    )


def pick_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ============================================================
# VALIDASI NAMA PRODUK
# ============================================================

def clean_product_name(name: Any) -> str | None:
    """
    Membersihkan dan memvalidasi nama produk.

    Tujuannya supaya teks seperti:
        %
        Rp
        Diskon
        Terjual
        Baru
        10%
        Rp5.000.000

    tidak dianggap sebagai nama produk.
    """

    if name is None:
        return None

    if not isinstance(name, str):
        name = str(name)

    # Rapikan whitespace
    name = re.sub(r"\s+", " ", name).strip()

    if not name:
        return None

    # Hilangkan karakter pemisah yang tidak perlu
    name = name.strip("|•·")

    if not name:
        return None

    lower_name = name.lower()

    # --------------------------------------------------------
    # TEKS YANG JELAS BUKAN NAMA PRODUK
    # --------------------------------------------------------

    invalid_exact = {
        "%",
        "rp",
        "rp.",
        "rp ",
        "diskon",
        "promo",
        "baru",
        "terlaris",
        "terjual",
        "gratis ongkir",
        "cashback",
        "official",
        "mall",
    }

    if lower_name in invalid_exact:
        return None

    # --------------------------------------------------------
    # HANYA ANGKA / SIMBOL
    # --------------------------------------------------------

    if not re.search(r"[A-Za-zÀ-ÿ]", name):
        return None

    # --------------------------------------------------------
    # PERSENTASE SAJA
    # Contoh:
    # 10%
    # 50%
    # -20%
    # --------------------------------------------------------

    if re.fullmatch(r"[-+]?\d+(?:[.,]\d+)?\s*%", name):
        return None

    # --------------------------------------------------------
    # HARGA SAJA
    # Contoh:
    # Rp100.000
    # Rp 100.000
    # Rp1.500.000
    # --------------------------------------------------------

    if re.fullmatch(
        r"rp\s*[\d.,]+",
        lower_name,
    ):
        return None

    # --------------------------------------------------------
    # TERLALU PENDEK
    # --------------------------------------------------------

    if len(name) < 3:
        return None

    # --------------------------------------------------------
    # KATA-KATA BADGE
    # --------------------------------------------------------

    badge_patterns = [
        r"^\d+\s*%\s*$",
        r"^rp\s*[\d.,]+\s*$",
        r"^terjual\s+\d+",
        r"^diskon\s*\d*",
        r"^hemat\s+",
        r"^cashback\s+",
        r"^gratis\s+ongkir",
    ]

    for pattern in badge_patterns:
        if re.search(pattern, lower_name, re.IGNORECASE):
            return None

    return name


def is_valid_product_name(name: Any) -> bool:
    return clean_product_name(name) is not None


def product_name_quality(name: Any) -> int:
    """
    Memberikan nilai kualitas sederhana untuk nama produk.

    Semakin tinggi nilainya, semakin layak dianggap sebagai
    nama produk.
    """

    cleaned = clean_product_name(name)

    if not cleaned:
        return 0

    score = 0

    # Nama valid
    score += 10

    # Nama lebih panjang biasanya lebih informatif
    if len(cleaned) >= 10:
        score += 5

    if len(cleaned) >= 20:
        score += 5

    if len(cleaned) >= 40:
        score += 3

    # Ada angka + huruf, umum pada nama laptop
    if re.search(r"[A-Za-z]", cleaned) and re.search(r"\d", cleaned):
        score += 2

    # Nama dengan kata laptop biasanya relevan
    if "laptop" in cleaned.lower():
        score += 2

    return score


# ============================================================
# CHROME
# ============================================================

def find_browser_exe(kind: str) -> Path | None:
    program_files = Path(
        os.environ.get(
            "PROGRAMFILES",
            r"C:\Program Files",
        )
    )

    program_files_x86 = Path(
        os.environ.get(
            "PROGRAMFILES(X86)",
            r"C:\Program Files (x86)",
        )
    )

    local_app = Path(
        os.environ.get(
            "LOCALAPPDATA",
            "",
        )
    )

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
            [
                "tasklist",
                "/FI",
                "IMAGENAME eq chrome.exe",
                "/NH",
            ],
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
    with socket.socket(
        socket.AF_INET,
        socket.SOCK_STREAM,
    ) as sock:

        sock.settimeout(0.25)

        return (
            sock.connect_ex(
                ("127.0.0.1", port)
            )
            == 0
        )


def wait_for_cdp(
    p,
    port: int,
    proc: subprocess.Popen[bytes] | None,
) -> BrowserContext:

    last_error: Exception | None = None

    for _ in range(80):

        if proc is not None and proc.poll() is not None:
            raise PlaywrightError(
                "Chrome langsung tertutup. "
                "Tutup semua proses chrome.exe di Task Manager, "
                "lalu jalankan skrip lagi."
            )

        try:
            browser = p.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}"
            )

            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context()
            )

            return context

        except Exception as exc:
            last_error = exc
            time.sleep(0.4)

    hint = ""

    if not port_is_open(port):
        hint = (
            " Port debugging tidak aktif. "
            "Chrome versi baru menolak remote debugging "
            "pada profil default, atau masih ada Chrome lain "
            "yang sudah jalan."
        )

    raise PlaywrightError(
        f"Gagal tersambung ke Chrome: {last_error}.{hint}"
    )


def clear_profile_locks(profile_dir: Path) -> None:
    for name in (
        "SingletonLock",
        "SingletonCookie",
        "SingletonSocket",
        "lockfile",
    ):
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
        "Google Chrome masih berjalan di background "
        "(proses chrome.exe), meskipun jendela tidak terlihat. "
        "Tutup dari Task Manager, atau di PowerShell jalankan:\n"
        "  taskkill /IM chrome.exe /F\n"
        "Lalu jalankan skrip ini lagi."
    )


def real_chrome_user_data() -> Path:
    return (
        Path(
            os.environ.get(
                "LOCALAPPDATA",
                "",
            )
        )
        / "Google"
        / "Chrome"
        / "User Data"
    )


def last_used_chrome_profile(
    user_data: Path,
) -> str:

    local_state = user_data / "Local State"

    try:
        data = json.loads(
            local_state.read_text(
                encoding="utf-8"
            )
        )

        name = str(
            data.get(
                "profile",
                {},
            ).get(
                "last_used"
            )
            or "Default"
        )

        if (user_data / name).is_dir():
            return name

    except Exception:
        pass

    return "Default"


def _copy_file_quiet(
    src: Path,
    dest: Path,
) -> bool:

    try:
        dest.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        shutil.copy2(
            src,
            dest,
        )

        return True

    except OSError:
        return False


def _copy_tree_quiet(
    src: Path,
    dest: Path,
    skip_dirs: set[str],
) -> int:

    copied = 0

    if not src.is_dir():
        return 0

    for root, dirs, files in os.walk(src):

        dirs[:] = [
            name
            for name in dirs
            if name not in skip_dirs
        ]

        rel = Path(root).relative_to(src)

        for name in files:

            if _copy_file_quiet(
                Path(root) / name,
                dest / rel / name,
            ):
                copied += 1

    return copied


def seed_cdp_profile(dest: Path) -> str:
    """
    Salin cookie/login profil Chrome yang terakhir dipakai.
    """

    marker = dest / ".seeded-from-chrome"

    src = real_chrome_user_data()

    profile_name = (
        last_used_chrome_profile(src)
        if src.is_dir()
        else "Default"
    )

    if marker.exists():

        saved = marker.read_text(
            encoding="utf-8"
        ).strip()

        return saved or profile_name

    if not (src / profile_name).is_dir():

        dest.mkdir(
            parents=True,
            exist_ok=True,
        )

        print(
            "Profil Chrome harian tidak ditemukan; "
            "memakai profil kosong."
        )

        return "Default"

    dest.mkdir(
        parents=True,
        exist_ok=True,
    )

    print(
        f"Menyalin cookie/login dari profil Chrome "
        f"'{profile_name}'..."
    )

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

    dir_names = (
        "Network",
        "Local Storage",
        "Session Storage",
        "IndexedDB",
    )

    copied = 0

    _copy_file_quiet(
        src / "Local State",
        dest / "Local State",
    )

    _copy_file_quiet(
        src / "First Run",
        dest / "First Run",
    )

    for name in file_names:

        if _copy_file_quiet(
            src / profile_name / name,
            dest / profile_name / name,
        ):
            copied += 1

    for name in dir_names:

        copied += _copy_tree_quiet(
            src / profile_name / name,
            dest / profile_name / name,
            skip_dirs,
        )

    clear_profile_locks(dest)

    marker.write_text(
        profile_name,
        encoding="utf-8",
    )

    print(
        f"Salinan profil siap ({copied} file)."
    )

    return profile_name


def launch_real_chrome(
    p,
    output_dir: Path,
) -> BrowserContext:

    global _EXTERNAL_BROWSER
    global _KEEP_BROWSER

    exe = find_browser_exe("chrome")

    if exe is None:
        raise PlaywrightError(
            "Google Chrome tidak ditemukan."
        )

    if chrome_is_running():
        raise chrome_already_running_error()

    profile_dir = (
        output_dir / "chrome-cdp-session"
    ).resolve()

    profile_name = seed_cdp_profile(
        profile_dir
    )

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

    _EXTERNAL_BROWSER = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _KEEP_BROWSER = True

    try:
        context = wait_for_cdp(
            p,
            port,
            _EXTERNAL_BROWSER,
        )

    except Exception:
        stop_external_browser()
        raise

    print(
        "Chrome terbuka memakai salinan profil "
        "harian Anda."
    )

    return context


def launch_isolated_chrome(
    p,
    exe: Path,
    profile_dir: Path,
    headless: bool,
) -> BrowserContext:

    global _EXTERNAL_BROWSER
    global _KEEP_BROWSER

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

    _EXTERNAL_BROWSER = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    _KEEP_BROWSER = False

    try:
        return wait_for_cdp(
            p,
            port,
            _EXTERNAL_BROWSER,
        )

    except Exception:
        stop_external_browser()
        raise


def launch_context(
    p,
    headless: bool,
    output_dir: Path,
    isolated_profile: bool,
) -> BrowserContext:

    if not isolated_profile:
        return launch_real_chrome(
            p,
            output_dir,
        )

    profile_dir = (
        output_dir / "chrome-profile"
    ).resolve()

    profile_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    exe = find_browser_exe("chrome")

    if exe is None:
        raise PlaywrightError(
            "Google Chrome tidak ditemukan."
        )

    context = launch_isolated_chrome(
        p,
        exe,
        profile_dir,
        headless,
    )

    print(
        f"Chrome terpisah terbuka: {profile_dir}"
    )

    return context


# ============================================================
# JSON
# ============================================================

def safe_json(
    response: Response,
) -> Any | None:

    try:
        return response.json()
    except Exception:
        return None


def walk_item_dicts(
    node: Any,
    found: list[dict[str, Any]],
) -> None:

    if isinstance(node, dict):

        itemid = (
            node.get("itemid")
            or node.get("item_id")
        )

        shopid = (
            node.get("shopid")
            or node.get("shop_id")
        )

        name = (
            node.get("name")
            or node.get("title")
        )

        if itemid and shopid and name:
            found.append(node)

        for value in node.values():
            walk_item_dicts(
                value,
                found,
            )

    elif isinstance(node, list):

        for value in node:
            walk_item_dicts(
                value,
                found,
            )


# ============================================================
# NORMALISASI PRODUK
# ============================================================

def normalize_product(
    item: dict[str, Any],
) -> dict[str, Any] | None:

    itemid = (
        item.get("itemid")
        or item.get("item_id")
    )

    shopid = (
        item.get("shopid")
        or item.get("shop_id")
    )

    raw_name = (
        item.get("name")
        or item.get("title")
    )

    name = clean_product_name(
        raw_name
    )

    if not itemid or not shopid:
        return None

    # Produk tanpa nama tetap boleh dikumpulkan
    # karena nama dapat dicari dari DOM / halaman produk.
    price = item.get("price")

    if isinstance(price, (int, float)) and price > 1000:

        price_rp = int(price) // 100_000

    else:
        price_rp = price

    rating_info = (
        item.get("item_rating")
        or {}
    )

    rating = (
        rating_info.get("rating_star")
        if isinstance(
            rating_info,
            dict,
        )
        else None
    )

    rating_count = (
        rating_info.get("rating_count")
        if isinstance(
            rating_info,
            dict,
        )
        else None
    )

    if (
        isinstance(
            rating_count,
            list,
        )
        and rating_count
    ):
        rating_count = rating_count[0]

    return {
        "itemid": str(itemid),
        "shopid": str(shopid),
        "name": name,
        "price": price_rp,
        "sold": (
            item.get("historical_sold")
            or item.get("sold")
            or item.get("item_sold")
        ),
        "rating_star": rating,
        "rating_count": rating_count,
        "shop_name": (
            item.get("shop_name")
            or item.get("shopname")
        ),
        "url": (
            f"{BASE_URL}/product/"
            f"{shopid}/{itemid}"
        ),
    }


def extract_items_from_search_payload(
    payload: Any,
) -> list[dict[str, Any]]:

    raw: list[dict[str, Any]] = []

    walk_item_dicts(
        payload,
        raw,
    )

    products: dict[
        str,
        dict[str, Any],
    ] = {}

    for item in raw:

        normalized = normalize_product(
            item
        )

        if normalized:

            itemid = normalized["itemid"]

            # Kalau ada duplikat dari payload,
            # pilih yang nama produknya lebih baik.
            if itemid not in products:

                products[itemid] = normalized

            else:

                old = products[itemid]

                if (
                    product_name_quality(
                        normalized.get("name")
                    )
                    >
                    product_name_quality(
                        old.get("name")
                    )
                ):
                    old["name"] = normalized["name"]

                for key, value in normalized.items():

                    if old.get(key) in (
                        None,
                        "",
                    ) and value not in (
                        None,
                        "",
                    ):
                        old[key] = value

    return list(
        products.values()
    )


# ============================================================
# MERGE PRODUK API + DOM
# ============================================================

def merge_product(
    products: dict[str, dict[str, Any]],
    product: dict[str, Any],
    source: str = "unknown",
) -> None:
    """
    Menggabungkan produk berdasarkan itemid.

    API diprioritaskan untuk data terstruktur.
    DOM hanya digunakan sebagai fallback.

    Ini menggantikan penggunaan setdefault()
    yang sebelumnya menyebabkan nama '%' bertahan.
    """

    itemid = product.get("itemid")

    if not itemid:
        return

    itemid = str(itemid)

    incoming = dict(product)

    incoming_name = clean_product_name(
        incoming.get("name")
    )

    incoming["name"] = incoming_name

    # --------------------------------------------------------
    # PRODUK BELUM ADA
    # --------------------------------------------------------

    if itemid not in products:

        products[itemid] = incoming

        return

    # --------------------------------------------------------
    # PRODUK SUDAH ADA
    # --------------------------------------------------------

    existing = products[itemid]

    existing_name = clean_product_name(
        existing.get("name")
    )

    # --------------------------------------------------------
    # NAMA
    # --------------------------------------------------------

    existing_score = product_name_quality(
        existing_name
    )

    incoming_score = product_name_quality(
        incoming_name
    )

    # Nama incoming lebih bagus -> gunakan
    if incoming_score > existing_score:

        existing["name"] = incoming_name

    # Kalau existing kosong -> gunakan incoming
    elif not existing_name and incoming_name:

        existing["name"] = incoming_name

    # --------------------------------------------------------
    # FIELD LAIN
    # --------------------------------------------------------

    for key, value in incoming.items():

        if key == "name":
            continue

        if existing.get(key) in (
            None,
            "",
        ):

            if value not in (
                None,
                "",
            ):
                existing[key] = value

    # --------------------------------------------------------
    # API BIASANYA LEBIH DIPERCAYA
    # --------------------------------------------------------

    if source == "api":

        for key in (
            "price",
            "sold",
            "rating_star",
            "rating_count",
            "shop_name",
        ):

            if incoming.get(key) not in (
                None,
                "",
            ):
                existing[key] = incoming[key]


# ============================================================
# REVIEW
# ============================================================

def extract_reviews_from_payload(
    payload: dict[str, Any],
    product: dict[str, Any],
) -> list[dict[str, Any]]:

    reviews: list[
        dict[str, Any]
    ] = []

    data = (
        payload.get("data")
        or payload
    )

    if not isinstance(data, dict):
        return reviews

    ratings = (
        data.get("ratings")
        or data.get("item_rating_list")
        or []
    )

    if not isinstance(
        ratings,
        list,
    ):
        return reviews

    # Nama produk FINAL
    final_product_name = (
        clean_product_name(
            product.get("name")
        )
        or f"Produk {product['itemid']}"
    )

    for rating in ratings:

        if not isinstance(
            rating,
            dict,
        ):
            continue

        comment = (
            rating.get("comment")
            or rating.get("comment_text")
            or ""
        )

        comment = comment.strip()

        if not comment:
            continue

        variation = None

        product_items = (
            rating.get("product_items")
            or []
        )

        if (
            product_items
            and isinstance(
                product_items[0],
                dict,
            )
        ):

            variation = (
                product_items[0].get(
                    "model_name"
                )
            )

        ctime = (
            rating.get("ctime")
            or rating.get("mtime")
        )

        created_at = None

        if isinstance(
            ctime,
            (int, float),
        ):

            try:
                created_at = (
                    datetime.fromtimestamp(
                        int(ctime),
                        tz=timezone.utc,
                    ).isoformat()
                )
            except Exception:
                created_at = None

        reviews.append(
            {
                "itemid": product["itemid"],
                "shopid": product["shopid"],
                "product_name": final_product_name,
                "product_url": product["url"],
                "cmtid": rating.get("cmtid"),
                "username": (
                    rating.get("author_username")
                    or rating.get("author")
                ),
                "rating_star": rating.get(
                    "rating_star"
                ),
                "comment": comment,
                "variation": variation,
                "like_count": rating.get(
                    "like_count"
                ),
                "created_at": created_at,
            }
        )

    return reviews


# ============================================================
# POPUP / BLOCK
# ============================================================

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

            loc = page.locator(
                selector
            ).first

            if (
                loc.count()
                and loc.is_visible(
                    timeout=800
                )
            ):
                loc.click(
                    timeout=1000
                )

        except Exception:
            continue


def is_login_wall(
    page: Page,
) -> bool:

    try:

        html = page.content()

        markers = (
            "Masuk Diperlukan",
            "belum masuk",
            "Log In",
            "login?next=",
        )

        return any(
            marker in html
            for marker in markers
        )

    except Exception:
        return False


def is_captcha_wall(
    page: Page,
) -> bool:

    try:

        url = page.url.lower()

        if (
            "/verify/captcha" in url
            or "anti_bot" in url
        ):
            return True

        body = page.inner_text(
            "body",
            timeout=1500,
        )

        return (
            "Silakan Coba Lagi Nanti"
            in body
            or "Verifikasi gagal"
            in body
        )

    except Exception:
        return False


def is_hard_verify_fail(
    page: Page,
) -> bool:

    try:

        body = page.inner_text(
            "body",
            timeout=1500,
        )

        return (
            "Silakan Coba Lagi Nanti"
            in body
            or "Verifikasi gagal"
            in body
        )

    except Exception:
        return False


def has_product_links(
    page: Page,
) -> bool:

    try:

        return (
            page.locator(
                "a[href*='-i.'], "
                "a[href*='/product/']"
            ).count()
            > 0
        )

    except Exception:
        return False


def wait_until_search_looks_ready(
    page: Page,
) -> None:

    print()
    print(
        "Halaman verifikasi Shopee bukan error skrip: "
        "akses sempat ditolak."
    )

    print(
        "Jangan klik 'Coba Lagi' berulang kali."
    )

    print(
        "Di jendela Chrome:"
    )

    print(
        "  - Jika tertulis 'Coba Lagi Nanti': "
        "tunggu beberapa saat."
    )

    print(
        "  - Login jika diminta."
    )

    print(
        "  - Selesaikan captcha jika muncul."
    )

    print(
        "  - Buka pencarian 'laptop' sampai "
        "kartu produk terlihat."
    )

    try:

        input(
            "Tekan Enter setelah daftar produk tampil... "
        )

    except EOFError:

        print(
            "Input tidak tersedia; menunggu..."
        )

        deadline = (
            time.time() + 1200
        )

        while (
            time.time() < deadline
            and not has_product_links(page)
        ):
            time.sleep(5)


def wait_if_blocked(
    page: Page,
    timeout_sec: int = 180,
) -> None:

    deadline = (
        time.time() + timeout_sec
    )

    warned = False

    while time.time() < deadline:

        if has_product_links(page):
            return

        if (
            is_captcha_wall(page)
            or is_login_wall(page)
            or not has_product_links(page)
        ):

            if not warned:

                if is_hard_verify_fail(page):

                    print(
                        "Shopee menolak verifikasi. "
                        "Jangan spam tombol 'Coba Lagi'."
                    )

                elif is_captcha_wall(page):

                    print(
                        "Shopee menampilkan "
                        "verifikasi anti-bot."
                    )

                else:

                    print(
                        "Halaman pencarian belum "
                        "menampilkan produk."
                    )

                warned = True

                if not is_hard_verify_fail(page):

                    try:

                        page.get_by_role(
                            "button",
                            name=re.compile(
                                r"Log In|Masuk",
                                re.I,
                            ),
                        ).first.click(
                            timeout=2000
                        )

                    except Exception:
                        pass

            time.sleep(5)

            continue

        return


# ============================================================
# NAVIGASI
# ============================================================

def goto_resilient(
    page: Page,
    url: str,
) -> Page:

    try:

        page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=90_000,
        )

        return page

    except (
        PlaywrightTimeout,
        PlaywrightError,
    ) as exc:

        print(
            f"    Navigasi bermasalah "
            f"({exc.__class__.__name__}: "
            f"{str(exc)[:120]}). "
            "Mencoba lagi..."
        )

        try:

            page.reload(
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            return page

        except Exception:

            context = page.context

            try:
                page.close()
            except Exception:
                pass

            new_page = context.new_page()

            new_page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=90_000,
            )

            return new_page


def dump_debug(
    page: Page,
    output_dir: Path,
    name: str,
) -> None:

    debug_dir = (
        output_dir / "debug"
    )

    debug_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    try:

        page.screenshot(
            path=str(
                debug_dir / f"{name}.png"
            ),
            full_page=False,
        )

        (
            debug_dir / f"{name}.html"
        ).write_text(
            page.content(),
            encoding="utf-8",
        )

        print(
            f"    Debug disimpan: "
            f"{debug_dir / name}.png"
        )

    except Exception as exc:

        print(
            f"    Gagal menyimpan debug: {exc}"
        )


# ============================================================
# PARSE LINK PRODUK
# ============================================================

def parse_product_href(
    href: str,
    name: str = "",
) -> dict[str, Any] | None:

    if not href:
        return None

    match = re.search(
        r"-i\.(\d+)\.(\d+)",
        href,
    )

    if match:

        shopid = match.group(1)
        itemid = match.group(2)

    else:

        match = re.search(
            r"/product/(\d+)/(\d+)",
            href,
        )

        if not match:
            return None

        shopid = match.group(1)
        itemid = match.group(2)

    clean = href.split("?")[0]

    if clean.startswith("/"):

        clean = BASE_URL + clean

    elif not clean.startswith("http"):

        clean = (
            f"{BASE_URL}/product/"
            f"{shopid}/{itemid}"
        )

    return {
        "itemid": itemid,
        "shopid": shopid,
        "name": clean_product_name(name),
        "price": None,
        "sold": None,
        "rating_star": None,
        "rating_count": None,
        "shop_name": None,
        "url": clean,
    }


# ============================================================
# EKSTRAK PRODUK DARI DOM
# ============================================================

def collect_products_from_dom(
    page: Page,
) -> list[dict[str, Any]]:

    rows = page.evaluate(
        """
        () => {
            const out = [];

            for (const a of document.querySelectorAll("a[href]")) {

                const href =
                    a.getAttribute("href") || "";

                if (
                    !href.includes("-i.")
                    && !href.includes("/product/")
                ) {
                    continue;
                }

                const lines =
                    (a.innerText || "")
                    .split("\\n")
                    .map(x => x.trim())
                    .filter(Boolean);

                out.push({
                    href,
                    lines
                });
            }

            return out;
        }
        """
    )

    products: dict[
        str,
        dict[str, Any],
    ] = {}

    for row in rows or []:

        href = row.get(
            "href",
            "",
        )

        lines = row.get(
            "lines",
            [],
        )

        if not isinstance(
            lines,
            list,
        ):
            lines = []

        # ----------------------------------------------------
        # CARI BARIS YANG PALING MUNGKIN NAMA PRODUK
        # ----------------------------------------------------

        candidate_names = []

        for line in lines:

            cleaned = clean_product_name(
                line
            )

            if cleaned:

                candidate_names.append(
                    cleaned
                )

        if candidate_names:

            # Ambil kandidat dengan kualitas
            # tertinggi.
            name = max(
                candidate_names,
                key=product_name_quality,
            )

        else:

            name = ""

        parsed = parse_product_href(
            href,
            name,
        )

        if not parsed:
            continue

        itemid = parsed["itemid"]

        # Jika duplikat, simpan nama dengan
        # kualitas terbaik.
        if itemid not in products:

            products[itemid] = parsed

        else:

            old_name = products[itemid].get(
                "name"
            )

            if (
                product_name_quality(
                    parsed.get("name")
                )
                >
                product_name_quality(
                    old_name
                )
            ):
                products[itemid]["name"] = (
                    parsed["name"]
                )

    return list(
        products.values()
    )


# ============================================================
# AMBIL NAMA DARI HALAMAN PRODUK
# ============================================================

def extract_product_name_from_page(
    page: Page,
) -> str | None:

    candidates: list[str] = []

    # --------------------------------------------------------
    # TITLE
    # --------------------------------------------------------

    try:

        title = page.title()

        if title:
            candidates.append(title)

    except Exception:
        pass

    # --------------------------------------------------------
    # META OG:TITLE
    # --------------------------------------------------------

    try:

        meta_title = page.locator(
            "meta[property='og:title']"
        ).get_attribute(
            "content"
        )

        if meta_title:
            candidates.append(
                meta_title
            )

    except Exception:
        pass

    # --------------------------------------------------------
    # META DESCRIPTION TIDAK DIGUNAKAN
    # karena sering bukan nama produk.
    # --------------------------------------------------------

    # --------------------------------------------------------
    # ELEMEN HEADING
    # --------------------------------------------------------

    selectors = [
        "h1",
        "[data-testid='pdp-product-name']",
        ".qaNIZv",
        "[class*='product-name']",
        "[class*='product-title']",
    ]

    for selector in selectors:

        try:

            loc = page.locator(
                selector
            ).first

            if loc.count():

                text_value = (
                    loc.inner_text(
                        timeout=1000
                    )
                )

                if text_value:
                    candidates.append(
                        text_value
                    )

        except Exception:
            continue

    # --------------------------------------------------------
    # PILIH NAMA TERBAIK
    # --------------------------------------------------------

    valid_candidates = []

    for candidate in candidates:

        cleaned = clean_product_name(
            candidate
        )

        if cleaned:
            valid_candidates.append(
                cleaned
            )

    if not valid_candidates:
        return None

    return max(
        valid_candidates,
        key=product_name_quality,
    )


# ============================================================
# SEARCH PRODUK
# ============================================================

def collect_products_from_search(
    page: Page,
    keyword: str,
    max_products: int,
    output_dir: Path,
) -> tuple[
    Page,
    list[dict[str, Any]],
]:

    products: dict[
        str,
        dict[str, Any],
    ] = {}

    def on_response(
        response: Response,
    ) -> None:

        url = response.url.lower()

        if response.status != 200:
            return

        interesting = (
            any(
                marker in url
                for marker in SEARCH_API_MARKERS
            )
            or "search" in url
        )

        if not interesting:
            return

        payload = safe_json(
            response
        )

        if payload is None:
            return

        for item in extract_items_from_search_payload(
            payload
        ):

            merge_product(
                products,
                item,
                source="api",
            )

    page.on(
        "response",
        on_response,
    )

    print(
        "Membuka beranda Shopee dulu..."
    )

    try:

        page = goto_resilient(
            page,
            BASE_URL,
        )

    except PlaywrightError as exc:

        print(
            f"  Gagal membuka beranda: {exc}"
        )

    close_popups(page)

    if (
        is_captcha_wall(page)
        or is_login_wall(page)
        or not has_product_links(page)
    ):
        wait_until_search_looks_ready(
            page
        )

    # --------------------------------------------------------
    # AMBIL DOM BERANDA
    # --------------------------------------------------------

    if has_product_links(page):

        for item in collect_products_from_dom(
            page
        ):

            merge_product(
                products,
                item,
                source="dom",
            )

        print(
            f"  Produk dari halaman yang sudah "
            f"terbuka: {len(products)}"
        )

    # --------------------------------------------------------
    # SEARCH
    # --------------------------------------------------------

    page_no = 0
    empty_streak = 0

    while (
        len(products) < max_products
        and page_no < 12
    ):

        search_url = (
            f"{BASE_URL}/search"
            f"?keyword={quote_plus(keyword)}"
            f"&page={page_no}"
        )

        print(
            f"Membuka pencarian '{keyword}' "
            f"halaman {page_no + 1} ..."
        )

        before = len(products)

        try:

            page = goto_resilient(
                page,
                search_url,
            )

        except PlaywrightError as exc:

            print(
                f"  Gagal membuka halaman: {exc}"
            )

            page_no += 1

            continue

        try:

            page.on(
                "response",
                on_response,
            )

        except Exception:
            pass

        close_popups(page)

        wait_if_blocked(
            page,
            timeout_sec=(
                600
                if page_no == 0
                else 90
            ),
        )

        page.wait_for_timeout(
            5000
        )

        # Scroll untuk memicu lazy loading
        for _ in range(8):

            page.mouse.wheel(
                0,
                1600,
            )

            page.wait_for_timeout(
                700
            )

        # ----------------------------------------------------
        # AMBIL DATA DOM
        # ----------------------------------------------------

        dom_products = (
            collect_products_from_dom(
                page
            )
        )

        for item in dom_products:

            merge_product(
                products,
                item,
                source="dom",
            )

        gained = (
            len(products)
            - before
        )

        print(
            f"  Produk terkumpul: "
            f"{len(products)}/{max_products} "
            f"(+{gained})"
        )

        if gained == 0:

            empty_streak += 1

            dump_debug(
                page,
                output_dir,
                f"search_page_{page_no + 1}",
            )

            if empty_streak >= 3:

                print(
                    "  Tiga halaman berturut-turut "
                    "kosong. Berhenti mencari."
                )

                break

        else:

            empty_streak = 0

        page_no += 1

        sleep_jitter(
            1.8,
            3.2,
        )

    try:

        page.remove_listener(
            "response",
            on_response,
        )

    except Exception:
        pass

    # --------------------------------------------------------
    # FILTER NAMA YANG TIDAK VALID
    # --------------------------------------------------------

    final_products = []

    for product in products.values():

        product["name"] = clean_product_name(
            product.get("name")
        )

        final_products.append(
            product
        )

    return (
        page,
        final_products,
    )


# ============================================================
# SCRAPE REVIEW PRODUK
# ============================================================

def collect_reviews_for_product(
    page: Page,
    product: dict[str, Any],
    reviews_per_product: int,
    min_delay: float,
    max_delay: float,
) -> tuple[
    Page,
    list[dict[str, Any]],
]:

    collected: dict[
        str,
        dict[str, Any],
    ] = {}

    def on_response(
        response: Response,
    ) -> None:

        if not any(
            marker in response.url
            for marker in REVIEW_API_MARKERS
        ):
            return

        payload = safe_json(
            response
        )

        if not isinstance(
            payload,
            dict,
        ):
            return

        for review in extract_reviews_from_payload(
            payload,
            product,
        ):

            key = str(
                review.get("cmtid")
                or review["comment"][:80]
            )

            collected[key] = review

    page.on(
        "response",
        on_response,
    )

    print(
        f"  Membuka produk "
        f"{product['itemid']}: "
        f"{product.get('name') or '(nama belum ditemukan)'}"
    )

    try:

        page = goto_resilient(
            page,
            product["url"],
        )

        try:

            page.on(
                "response",
                on_response,
            )

        except Exception:
            pass

    except PlaywrightError:

        print(
            "    Gagal membuka halaman produk, "
            "dilewati."
        )

        try:

            page.remove_listener(
                "response",
                on_response,
            )

        except Exception:
            pass

        return (
            page,
            [],
        )

    close_popups(page)

    wait_if_blocked(
        page,
        timeout_sec=60,
    )

    page.wait_for_timeout(
        2500
    )

    # --------------------------------------------------------
    # PERBAIKI NAMA PRODUK JIKA MASIH KOSONG
    # --------------------------------------------------------

    current_name = clean_product_name(
        product.get("name")
    )

    if not current_name:

        page_name = (
            extract_product_name_from_page(
                page
            )
        )

        if page_name:

            product["name"] = page_name

            print(
                f"    Nama produk diperbaiki: "
                f"{page_name[:100]}"
            )

    # --------------------------------------------------------
    # SCROLL
    # --------------------------------------------------------

    for _ in range(12):

        page.mouse.wheel(
            0,
            1600,
        )

        page.wait_for_timeout(
            450
        )

        if (
            len(collected)
            >= reviews_per_product
        ):
            break

    # --------------------------------------------------------
    # TAB REVIEW
    # --------------------------------------------------------

    for label in (
        "Ulasan",
        "Penilaian",
        "Semua",
    ):

        try:

            tab = page.get_by_text(
                label,
                exact=False,
            ).first

            if tab.is_visible(
                timeout=800
            ):

                tab.click(
                    timeout=1500
                )

                page.wait_for_timeout(
                    1500
                )

                break

        except Exception:
            continue

    # --------------------------------------------------------
    # NEXT REVIEW PAGE
    # --------------------------------------------------------

    next_selectors = [
        "button.shopee-icon-button--right",
        "button[aria-label='Next']",
        "nav button:last-child",
    ]

    clicks = 0

    while (
        len(collected)
        < reviews_per_product
        and clicks < 6
    ):

        clicked = False

        for selector in next_selectors:

            try:

                btn = page.locator(
                    selector
                ).first

                if (
                    btn.count()
                    and btn.is_enabled()
                    and btn.is_visible(
                        timeout=500
                    )
                ):

                    btn.click(
                        timeout=1500
                    )

                    clicked = True

                    page.wait_for_timeout(
                        1800
                    )

                    break

            except Exception:
                continue

        if not clicked:
            break

        clicks += 1

    try:

        page.remove_listener(
            "response",
            on_response,
        )

    except Exception:
        pass

    sleep_jitter(
        min_delay,
        max_delay,
    )

    reviews = list(
        collected.values()
    )[:reviews_per_product]

    # --------------------------------------------------------
    # PASTIKAN SEMUA REVIEW MENGGUNAKAN
    # NAMA PRODUK FINAL
    # --------------------------------------------------------

    final_name = (
        clean_product_name(
            product.get("name")
        )
        or f"Produk {product['itemid']}"
    )

    for review in reviews:

        review["product_name"] = (
            final_name
        )

        review["product_url"] = (
            product["url"]
        )

    print(
        f"    Ulasan berteks: "
        f"{len(reviews)}"
    )

    return (
        page,
        reviews,
    )


# ============================================================
# SAVE
# ============================================================

def save_outputs(
    output_dir: Path,
    products: list[dict[str, Any]],
    reviews: list[dict[str, Any]],
) -> None:

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # Pastikan nama produk bersih sebelum disimpan
    for product in products:

        product["name"] = (
            clean_product_name(
                product.get("name")
            )
            or f"Produk {product['itemid']}"
        )

    for review in reviews:

        review["product_name"] = (
            clean_product_name(
                review.get(
                    "product_name"
                )
            )
            or f"Produk {review['itemid']}"
        )

    # --------------------------------------------------------
    # CSV
    # --------------------------------------------------------

    pd.DataFrame(
        products
    ).to_csv(
        output_dir / "products.csv",
        index=False,
        encoding="utf-8-sig",
    )

    pd.DataFrame(
        reviews
    ).to_csv(
        output_dir / "reviews.csv",
        index=False,
        encoding="utf-8-sig",
    )

    # --------------------------------------------------------
    # JSON
    # --------------------------------------------------------

    (
        output_dir / "products.json"
    ).write_text(
        json.dumps(
            products,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    (
        output_dir / "reviews.json"
    ).write_text(
        json.dumps(
            reviews,
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print(
        f"\nTersimpan. "
        f"Produk: {len(products)} | "
        f"Ulasan: {len(reviews)}"
    )

    print(
        f"File: "
        f"{output_dir / 'products.csv'} "
        f"dan "
        f"{output_dir / 'reviews.csv'}"
    )


# ============================================================
# MAIN
# ============================================================

def main() -> int:

    args = parse_args()

    output_dir = Path(
        args.output_dir
    )

    output_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    # --------------------------------------------------------
    # VALIDASI ARGUMEN
    # --------------------------------------------------------

    min_reviews = (
        args.min_reviews
        if args.min_reviews is not None
        else args.reviews_per_product
    )

    if min_reviews > args.reviews_per_product:

        print(
            f"--min-reviews ({min_reviews}) "
            f"lebih besar dari "
            f"--reviews-per-product "
            f"({args.reviews_per_product}); "
            f"minimal diturunkan menjadi "
            f"{args.reviews_per_product}."
        )

        min_reviews = (
            args.reviews_per_product
        )

    candidate_target = (
        args.max_products
        * max(
            args.candidate_multiplier,
            1,
        )
    )

    # --------------------------------------------------------
    # PLAYWRIGHT
    # --------------------------------------------------------

    with sync_playwright() as p:

        try:

            context = launch_context(
                p,
                args.headless,
                output_dir,
                args.isolated_profile,
            )

        except PlaywrightError as exc:

            print(str(exc))

            stop_external_browser()

            return 1

        try:

            page = (
                context.pages[0]
                if context.pages
                else context.new_page()
            )

            try:

                page, candidates = (
                    collect_products_from_search(
                        page,
                        args.keyword,
                        candidate_target,
                        output_dir,
                    )
                )

                if not candidates:

                    dump_debug(
                        page,
                        output_dir,
                        "search_empty",
                    )

                    print(
                        "Tidak ada produk yang "
                        "terkumpul."
                    )

                    return 1

                print(
                    f"\nKandidat: "
                    f"{len(candidates)} produk | "
                    f"Target lolos: "
                    f"{args.max_products} | "
                    f"Minimal ulasan: "
                    f"{min_reviews}"
                )

                # ------------------------------------------------
                # DEBUG NAMA PRODUK
                # ------------------------------------------------

                print(
                    "\nNama kandidat yang ditemukan:"
                )

                for no, candidate in enumerate(
                    candidates,
                    start=1,
                ):

                    print(
                        f"  {no}. "
                        f"{candidate.get('name') or '(kosong)'}"
                    )

                print()

                all_reviews: list[
                    dict[str, Any]
                ] = []

                kept_products: list[
                    dict[str, Any]
                ] = []

                skipped = 0

                # ------------------------------------------------
                # PROSES PRODUK
                # ------------------------------------------------

                for idx, product in enumerate(
                    candidates,
                    start=1,
                ):

                    print(
                        f"[{idx}/{len(candidates)}] "
                        f"lolos: "
                        f"{len(kept_products)}/"
                        f"{args.max_products}"
                    )

                    page, reviews = (
                        collect_reviews_for_product(
                            page,
                            product,
                            args.reviews_per_product,
                            args.min_delay,
                            args.max_delay,
                        )
                    )

                    if len(reviews) < min_reviews:

                        skipped += 1

                        print(
                            f"    Dilewati: "
                            f"hanya {len(reviews)} "
                            f"ulasan "
                            f"(minimal "
                            f"{min_reviews})."
                        )

                        continue

                    # --------------------------------------------
                    # PASTIKAN NAMA PRODUK VALID
                    # --------------------------------------------

                    final_name = (
                        clean_product_name(
                            product.get("name")
                        )
                    )

                    if not final_name:

                        final_name = (
                            f"Produk "
                            f"{product['itemid']}"
                        )

                    product["name"] = (
                        final_name
                    )

                    # --------------------------------------------
                    # UPDATE NAMA DI SEMUA REVIEW
                    # --------------------------------------------

                    for review in reviews:

                        review[
                            "product_name"
                        ] = final_name

                    kept_products.append(
                        product
                    )

                    all_reviews.extend(
                        reviews
                    )

                    # --------------------------------------------
                    # SAVE BERKALA
                    # --------------------------------------------

                    if (
                        len(kept_products)
                        % 5
                        == 0
                    ):

                        save_outputs(
                            output_dir,
                            kept_products,
                            all_reviews,
                        )

                    if (
                        len(kept_products)
                        >= args.max_products
                    ):

                        print(
                            "Target produk terpenuhi."
                        )

                        break

                # ------------------------------------------------
                # PERINGATAN
                # ------------------------------------------------

                if (
                    len(kept_products)
                    < args.max_products
                ):

                    print(
                        f"\nPeringatan: hanya "
                        f"{len(kept_products)} "
                        f"dari "
                        f"{args.max_products} "
                        f"produk yang memenuhi "
                        f"minimal "
                        f"{min_reviews} ulasan "
                        f"({skipped} dilewati)."
                    )

                    print(
                        "Naikkan "
                        "--candidate-multiplier "
                        "atau turunkan "
                        "--min-reviews."
                    )

                # ------------------------------------------------
                # SAVE FINAL
                # ------------------------------------------------

                save_outputs(
                    output_dir,
                    kept_products,
                    all_reviews,
                )

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


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    sys.exit(main())