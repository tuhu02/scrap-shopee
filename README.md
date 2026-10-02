# Scrap Shopee

Skrip Python untuk mengumpulkan data produk laptop dan ulasan berteks dari halaman publik Shopee Indonesia.

Secara default, skrip mengumpulkan **100 produk laptop** dan **10 ulasan berteks per produk**. Data disimpan dalam format CSV dan JSON untuk keperluan skripsi atau analisis sentimen.

## Persyaratan

- Windows (skrip memakai `tasklist` dan lokasi `chrome.exe`)
- Python 3.10+
- Google Chrome terpasang
- Akun Shopee (login mungkin diminta saat pencarian)

## Persiapan

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## Penggunaan

**Tutup semua jendela Google Chrome terlebih dahulu** (cek juga Task Manager). Jika masih ada proses `chrome.exe`, skrip akan berhenti:

```powershell
taskkill /IM chrome.exe /F
```

Jalankan dengan konfigurasi default (kata kunci `laptop`, 100 produk, 10 ulasan per produk):

```powershell
python scrape_shopee_reviews.py
```

Contoh dengan kata kunci dan jumlah data tertentu:

```powershell
python scrape_shopee_reviews.py `
    --keyword "laptop gaming" `
    --max-products 100 `
    --reviews-per-product 10
```

Chrome akan terbuka dengan salinan profil harian Anda. Jika Shopee meminta login atau captcha, selesaikan secara manual di jendela tersebut, lalu ikuti instruksi di terminal.

### Opsi

| Opsi                    | Default  | Keterangan                                                        |
| ----------------------- | -------- | ----------------------------------------------------------------- |
| `--keyword`             | `laptop` | Kata kunci pencarian                                              |
| `--max-products`        | `100`    | Jumlah produk target                                              |
| `--reviews-per-product` | `10`     | Jumlah ulasan berteks per produk                                  |
| `--output-dir`          | `data`   | Folder penyimpanan hasil                                          |
| `--min-delay`           | `2.5`    | Jeda minimum antar produk (detik)                                 |
| `--max-delay`           | `5.0`    | Jeda maksimum antar produk (detik)                                |
| `--isolated-profile`    | nonaktif | Memakai profil Chrome terpisah (lebih sering diblokir Shopee)     |
| `--headless`            | nonaktif | Tanpa jendela browser (hanya berlaku dengan `--isolated-profile`) |

## Hasil

Hasil disimpan di folder `data/`:

- `products.csv` / `products.json`
- `reviews.csv` / `reviews.json`

Setiap 5 produk, file disimpan ulang agar progres tidak hilang jika skrip terhenti.

## Catatan penting

- Hormati ketentuan penggunaan Shopee dan gunakan skrip ini hanya untuk keperluan akademik.
- Gunakan jeda antar halaman dan jangan menjalankan terlalu banyak permintaan sekaligus.
- Shopee sering menampilkan captcha. Jangan gunakan `--headless` sampai Anda yakin akses tidak terblokir.
- Jika muncul pesan "Coba Lagi Nanti", jangan klik berulang kali. Tunggu 20-30 menit.
- Hanya ulasan yang memiliki teks yang disimpan; rating tanpa komentar dilewati.
- Jika tampilan halaman berubah, selector tombol ulasan bisa gagal. Skrip tetap mencoba menangkap JSON `get_ratings` dari network.
- Folder `data/chrome-cdp-session` berisi salinan cookie dan login Chrome Anda. **Jangan di-commit atau dibagikan.**