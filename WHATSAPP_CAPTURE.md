# Pencatatan pengeluaran lewat WhatsApp

Setelah OTP diverifikasi di aplikasi, nomor yang sama bisa mengirim chat,
foto struk, atau VN. Backend mengotorisasi pesan sebelum n8n mengambil media,
memakai ekstraksi Cashly, lalu memeriksa ulang koneksi sebelum menyimpan.
Hasil lengkap langsung tercatat. Kalau ada detail yang kurang, jawab pertanyaan
Mochi lewat chat dalam 30 menit. Selesaikan klarifikasi sebelum mengirim media baru.

Backend mencatat sumber WhatsApp dan jenis input asal. Satu pesan hanya bisa
menghasilkan satu batch pengeluaran. Job dan balasan terkait memakai transaksi
DB yang sama dengan pengeluaran. Teks sementara dan draft disimpan terenkripsi.
n8n mengirim media ke backend untuk validasi dan ekstraksi. Job menyimpan hasil
dan status pemrosesan tanpa menyimpan foto atau rekaman.

Untuk memahami pertanyaan susulan di luar konteks, chat WhatsApp menggunakan
hingga dua pasang pesan dan balasan terkait dari nomor yang sama. Konteks ini
tersimpan terenkripsi, berlaku 10 menit, dan ikut dihapus saat disconnect.

## Aktivasi staging

1. Build image backend terbaru dan jalankan migration sebelum menyalakan fitur.

   ```sh
   docker compose -f compose.staging.yaml build backend
   docker compose -f compose.staging.yaml run --rm backend python -m cashly db-upgrade
   ```

2. Di database staging, jalankan grant dari `sql/runtime_role.sql` sebagai admin.
   Jika role runtime sudah disiapkan untuk linking dan history, cukup grant baru ini.

   ```sql
   GRANT SELECT, INSERT, UPDATE ON cashly.whatsapp_capture_jobs TO cashly_runtime;
   ```

3. Di canvas Cashly WA, pastikan **Konfigurasi Staging** dan **Konfigurasi Recovery**
   memiliki `linking_backend_ready: true` dan `expense_processing_enabled: true`.
   Pakai credential **Cashly Backend** yang sama untuk ketiga node pemrosesan.
   Simpan dan publish workflow staging yang URL-nya dipakai webhook Evolution.
4. Isi `WHATSAPP_RECORDING_ENABLED=true` di `.env.staging` privat. Buat ulang
   container backend untuk memuat env terbaru, lalu jalankan `db-check`.

   ```sh
   docker compose -f compose.staging.yaml up -d --no-deps backend
   docker compose -f compose.staging.yaml exec backend python -m cashly db-check
   ```

5. Uji dari nomor yang sudah terverifikasi dengan sebuah chat berjumlah jelas,
   satu foto struk, dan satu VN pendek. Cek balasan serta nominal di aplikasi.
   Lalu uji chat yang kurang nominal, balas pertanyaannya, dan pastikan pengeluaran
   baru muncul setelah lengkap. Dari nomor yang sudah disconnect, pastikan
   pesan berikutnya meminta linking.

Scheduler n8n mengambil balasan yang siap, termasuk balasan untuk job kedaluwarsa.
Jika proses terputus sebelum menyimpan, bot meminta pengguna mengirim ulang.
Pengiriman berstatus `unknown` perlu diperiksa di Evolution sebelum diulang.
Untuk menjeda pencatatan, set `WHATSAPP_RECORDING_ENABLED=false`, buat ulang
container backend, dan biarkan pengaturan linking aktif.
