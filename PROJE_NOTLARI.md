# Proje Teknik Notları

Son incelenen kaynak dosyaları:
- `planner(20261113-191122).py` — 4.899 satır; ana masaüstü/web arayüzü ve proje yöneticisi.
- `pipeline.py` — 1.023 satır; ses/video işleme ve nihai MP4 üretim motoru.

> Bu belge kaynak kodun yerine geçmez. Kod değişikliği yapılacağı zaman ilgili güncel `.py` dosyası esas alınmalıdır. Bu incelemede kaynak dosyalarda değişiklik yapılmadı.

## 1. Genel mimari

`planner.py` ana uygulamadır. Python içinde yerel HTTP sunucusu açar (`127.0.0.1`, varsayılan port `8765`) ve Edge/Chrome/Brave uygulama penceresinde arayüzü gösterir. Metni sahnelere ayırma, stok video arama/seçme/indirme, ses ve görsel yükleme, proje kaydetme, video üretimini başlatma ve çıktıları yönetme işlerini yürütür.

`pipeline.py` üretim motorudur. Planner tarafından alt süreç olarak çalıştırılır. Ses dosyasını faster-whisper ile kelime zamanlarına ayırır; varsa `inputs/metin.txt` ile metni hizalar; videoları eşleştirir, dikey klipleri hazırlar, klipleri birleştirir ve ASS altyazıyı videoya gömerek MP4 üretir.

## 2. İki dosyanın bağlantısı

- `planner.py` içindeki `do_run()` fonksiyonu önce `do_write(scenes)` çağırır.
- Bu işlem sahne eşleşmelerini `inputs/eslesme.txt`, metni `inputs/metin.txt` dosyasına yazar.
- Ardından `pipeline.py` dosyasını `subprocess.Popen(...)` ile çalıştırır.
- Üretim komutunda `--match kelime`, `--min-scene 1.0`, seçilen Whisper modeli, altyazı stili/rengi, altyazı alt boşluğu ve çıktı yolu gönderilir.
- Üretim çıktısı varsayılan olarak kök klasörde `output_video.mp4` olur; planner bunu ayrıca `ciktilar/` klasörüne kopyalayabilir.
- İki `.py` dosyası aynı klasörde bulunmalıdır.

## 3. Ana akış

1. Kullanıcı planner arayüzüne metin ve ses ekler.
2. Planner metni sahnelere böler; sahne başına arama terimleri üretir ve Pexels/Pixabay vb. kaynaklardan klip arar.
3. Kullanıcı sahnelere video seçer; eşleşme dosyası oluşturulur.
4. Kullanıcı üretimi başlatır.
5. Planner, pipeline'ı alt süreç olarak çalıştırır ve işlem günlüklerini arayüzde gösterir.
6. Pipeline videoyu üretir; planner tamamlanan çıktıyı arşivler.

## 4. Dosyaların sorumlulukları

### planner.py
- Yerel web sunucusu ve HTML/JavaScript arayüzü.
- Sahne bölme, kanca (hook) düzeni, tetikleyici kelime ve arama sorgusu üretimi.
- LLM sağlayıcı sırası: API anahtarı varsa Claude, ardından Groq, ardından Cloudflare.
- Pexels/Pixabay ve diğer stok kaynakları için arama/indirme yardımcıları.
- Proje durumu, ses, görsel/referans görseli ve kapak yönetimi.
- Video üretimini başlatma/durdurma, günlükleri izleme, çıktı kopyalama.
- İngilizce dönüştürme ve video kalite kontrolüyle ilgili ek işlevler.

### pipeline.py
- FFmpeg/FFprobe kontrolü.
- faster-whisper ile Türkçe kelime zaman damgaları.
- Ses/model/prompt kombinasyonuna göre transkript önbelleği (`_cache/`).
- `eslesme.txt` ve video dosya adlarından kelime eşleştirmesi.
- Sahneleri kliplere dönüştürme, dikey 1080x1920 / 30 FPS işleme.
- ASS altyazı üretimi ve ses/video son birleştirmesi.

## 5. Önemli dosya ve klasörler

- `inputs/` — giriş dosyaları.
- `inputs/videos/` — kullanılacak video klipleri.
- `inputs/audio.*` — seslendirme.
- `inputs/metin.txt` — seslendirme metni/altyazı metni.
- `inputs/eslesme.txt` — video dosyaları ile tetikleyici kelimelerin eşleşmesi.
- `inputs/font.ttf` — isteğe bağlı altyazı fontu.
- `inputs/thumbnail.png` — onaylanan kapak görseli.
- `planner_keys.json` — API anahtarları (gizli tutulmalı, paylaşılmamalı).
- `planner_project.json` — planner proje durumu.
- `_cache/` — pipeline transkript önbelleği.
- `_temp/` — geçici video/altyazı işleme dosyaları.
- `output_video.mp4` — son üretim dosyası.
- `output_video_kapaksiz.mp4` — planner'ın kapak ekleme akışında kullandığı ham çıktı.
- `ciktilar/` — tamamlanan videoların arşivi.
- `english_conversion/` — İngilizce dönüştürme çalışma alanı.

## 6. Değişiklik yaparken korunacak kurallar

- `planner.py` ve `pipeline.py` aynı klasörde kalmalı.
- Planner'ın `do_run()` komut satırı argümanları ile pipeline'ın `parse_args()` seçenekleri uyumlu tutulmalı.
- `do_write()` tarafından yazılan `inputs/metin.txt` ve `inputs/eslesme.txt` biçimleri pipeline'ın beklediği biçimle uyumlu olmalı.
- `tr_fold()` / `stem_match()` gibi Türkçe eşleştirme mantıkları iki dosyada uyumlu tutulmalı.
- Çalışan özellikler talep edilmedikçe kaldırılmamalı; değişiklikler küçük ve hedefli yapılmalı.
- API anahtarları, kişisel dosyalar ve kullanıcı videoları kaynak kod değişikliği için gereksiz yere paylaşılmamalı.
- Değişiklik sonrası en azından Python sözdizimi kontrolü yapılmalı; mümkünse kullanıcının Windows 11 ortamında gerçek üretim testi yapılmalı.

## 7. Çalışma yöntemi

Her yeni istekte:
1. Önce hangi dosya/fonksiyonun etkilendiğini belirle.
2. Mümkünse yalnızca ilgili fonksiyonu değiştir; tüm dosyayı baştan yazma.
3. Diğer dosyayla olan arayüzü/argümanları kontrol et.
4. Değişiklikleri ve test sonucunu kısa bir kayıt olarak ekle.
5. Kullanıcıya güncel dosyaları ve neyin değiştiğini açıkça bildir.

## 8. Önemli sınır

Bu not dosyası proje yapısını ve entegrasyonu hatırlatır; 5.000 satıra yakın `planner.py` kodunun tamamını içermez. Gerçek kod düzenlemesinde güncel kaynak dosyalarına erişim gerekir. Aynı sohbet içinde ekli dosyalar üzerinden devam edilebilir; yeni ve ayrı bir sohbette kaynak dosyalar erişilebilir değilse güncel dosyaların yeniden eklenmesi gerekir.
