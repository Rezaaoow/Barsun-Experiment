# مهاجرت cPanel از `lifetrade` به `barsan_experiment`

این تغییر نام در کد مخزن به‌تنهایی کامل نمی‌شود. مقادیر **Application root** و **Application URL** در cPanel تنظیمات میزبانی‌اند و باید در رابط cPanel تغییر یا به‌صورت یک برنامهٔ موازی ایجاد شوند. راه امن‌تر این است که ابتدا برنامهٔ جدید را کنار برنامهٔ قدیمی بالا بیاورید و پس از تأیید کامل، ترافیک و لینک‌ها را منتقل کنید. cPanel برای ثبت Python app فیلدهای Application root، Application URL، startup file و entry point را جداگانه تنظیم می‌کند. راهنمای رسمی: https://support.cpanel.net/hc/en-us/articles/360057849753-How-to-register-an-application-with-Python-Selector

## نام‌گذاری پیشنهادی

- **Application root:** `barsan_experiment` (نام پوشه در حساب میزبانی؛ underscore قابل استفاده است)
- **Application URL:** `barsan-experiment` (بخش URL، معمولاً با hyphen)
- **URL نهایی نمونه:** `https://DOMAIN.example/barsan-experiment`
- **Application startup file / entry point:** همان مقادیر برنامهٔ موجود را حفظ کنید؛ نام فایل را حدس نزنید. اگر برنامهٔ فعلی `passenger_wsgi.py` و `application` دارد، همان تنظیمات را در برنامهٔ جدید استفاده کنید.
- **Passenger log file:** بهتر است مسیر لاگ جداگانه‌ای برای برنامهٔ جدید تعریف شود تا خطاهای دو برنامه با هم مخلوط نشوند.

`DOMAIN.example` نمونه است؛ آن را با دامنهٔ واقعی خودتان جایگزین کنید.

## قبل از شروع

1. از PostgreSQL یک backup مستقل و قابل‌بازیابی بگیرید. علاوه بر آن، مسیر برنامهٔ فعلی و فایل‌های تنظیمات را کپی کنید.
2. از cPanel یادداشت یا تصویر تهیه کنید: Python version، startup file، entry point، environment variables و log file فعلی.
3. بررسی کنید که کلید SSH به مخزن خصوصی GitHub دسترسی دارد و نسخهٔ مورد استقرار همان commit تأییدشده در GitHub است.
4. فهرست لینک‌های دعوتی را که قبلاً ساخته‌اید خصوصی نگه دارید. اگر لینک‌های قدیمی باید کار کنند، برنامهٔ قبلی را تا پایان مهاجرت خاموش نکنید.

## ایجاد برنامهٔ جدید

1. در cPanel، بخش **Setup Python App** یا **Python Selector** را باز کنید.
2. به‌جای ویرایش/حذف برنامهٔ فعلی، ابتدا یک Python application جدید بسازید.
3. Application root را `barsan_experiment` و Application URL را `barsan-experiment` تنظیم کنید.
4. Python version، startup file و entry point را مطابق برنامهٔ فعلی انتخاب کنید. محل فایل‌های کد و مسیرهای مورد استفاده در startup را با root جدید هماهنگ کنید.
5. وابستگی‌ها را در virtualenv برنامهٔ جدید نصب کنید.
6. environment variables برنامهٔ جدید را تنظیم کنید:
   - `DATABASE_URL`: همان PostgreSQL مورد تأیید، پس از بررسی نام دیتابیس و مجوز کاربر.
   - `ADMIN_TOKEN`: همان secret مدیریتی یا یک secret جدید امن.
   - `PUBLIC_EXPERIMENT_URL`: آدرس کامل برنامهٔ جدید، مثلاً `https://DOMAIN.example/barsan-experiment`.
   - بقیهٔ متغیرهایی را که برنامهٔ فعلی واقعاً نیاز دارد حفظ کنید، به‌خصوص `STUDY_CODE` و هر تنظیم محیطی مرتبط.
7. کد را در root جدید مستقر کنید و فایل‌های مورد استفادهٔ برنامه مثل `app/index.html` و `app/admin-links.html` را نیز منتشر کنید.
8. بعد از تغییر فایل‌ها، راه‌اندازی مجدد را از cPanel انجام دهید. در Passenger نیز معمولاً ایجاد/به‌روزرسانی فایل `tmp/restart.txt` داخل root برنامه باعث restart در درخواست بعدی می‌شود؛ راهنمای رسمی: https://support.cpanel.net/hc/en-us/articles/1500002443862-How-to-restart-an-application-that-s-using-cPanel-s-Application-Manager

## چک‌لیست قبل از انتقال لینک‌ها

همهٔ موارد را روی URL جدید بررسی کنید:

- `/api/health` پاسخ سالم بدهد و اتصال PostgreSQL را تأیید کند.
- `/admin` و `/admin/links` باز شوند و بدون توکن، عملیات مدیریتی رد شود.
- ساخت یک لینک آزمایشی مستقل برای هر ماژول، consent، identify و اجرای آزمایشی مرحله کار کنند.
- ساخته‌شدن لینک‌ها، دامنه و مسیر `/barsan-experiment` را داخل URL داشته باشد و به `/lifetrade` برنگردد.
- خروجی CSV جداگانه و ZIP کامل دانلود شود و `scope=main` و `scope=test` مخلوط نباشند.
- reset را فقط در فضای تست و با عبارت تأیید صحیح آزمایش کنید؛ پاک‌سازی دادهٔ اصلی را در این مرحله اجرا نکنید.
- یک اجرای end-to-end از پرسشنامه تا Ultimatum را با کد تست انجام دهید؛ سپس داده‌ها و خروجی‌ها را با هم تطبیق دهید.

## لینک‌های قدیمی و انتقال نهایی

لینک‌های قبلی به مسیر `/lifetrade` اشاره می‌کنند. تغییر Application URL در cPanel معمولاً به معنی تغییر URL لینک‌های از پیش توزیع‌شده نیست؛ برای همین، برنامهٔ قدیمی را تا زمانی که روشن شود لینک‌های قدیمی دیگر لازم نیستند فعال نگه دارید. اگر باید لینک‌های قبلی حفظ شوند، یک redirect کنترل‌شده یا دورهٔ هم‌زیستی لازم است، اما فقط پس از اطمینان از اینکه مسیر قدیمی به همان دیتابیس و همان سرویس مربوط است. از راه‌اندازی دو نسخه با یک `STUDY_CODE` روی دو دیتابیس متفاوت خودداری کنید.

این repository change مسیرها را قابل‌حمل‌تر می‌کند، ولی به cPanel دسترسی ندارد و خودش نمی‌تواند Application root یا Application URL را ویرایش کند.
