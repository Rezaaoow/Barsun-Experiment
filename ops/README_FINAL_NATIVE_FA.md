# عملیات جاری native — پس از انتشار ۳ نوامبر ۲۰۲۶

کد فعال هر دو محیط: `8e4838c54e3fcc82a1d1a25af337293c57b98551`، ادغام main: `b7821078df1b4afdec928978d14b3f06866a621f`. گزارش قطعی و محدودیت آزمون در [گزارش نهایی](../docs/FINAL_RELEASE_RESULTS_2026-11-03_FA.md) است. گزارش‌های قدیمی Passenger برای سابقه/بازگشت‌اند.

## محل واقعی اجرا

| مورد | Staging | Production |
|---|---|---|
| URL | https://staging.my-experiment.ir/lifetrade/ | https://my-experiment.ir/lifetrade/ |
| ریشهٔ محیط | `/home/myexperi/lifetrade-staging` | `/home/myexperi/lifetrade` |
| منبع فعلی | `deployments/final-8e4838c54e3f/runtime` زیر ریشه | همان مسیر نسبی زیر ریشهٔ اصلی |
| مرجع منبع/نسخه | `current-release.json` زیر ریشه | همان |
| تنظیمات خصوصی | `config/app.env`، mode 0600 | همان، با مقادیر مستقل اصلی |
| Supervisor config | `native-asgi-trial-20261102/supervisor.ini` | `native-asgi/supervisor.ini` |
| Python/ابزار کنترل | `/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/` | `/home/myexperi/virtualenv/lifetrade/3.12/bin/` |
| پورت داخلی | `127.0.0.1:8766` | `127.0.0.1:8767` |
| routing عمومی | `/home/myexperi/public_html/staging/lifetrade/.htaccess` | `/home/myexperi/public_html/lifetrade/.htaccess` |

`/repo` در هر محیط آینهٔ همان فایل‌ها و ورودی سازگاری Passenger است؛ محل import زندهٔ native نیست. فایل زنده را مستقیماً ویرایش نکن. از `runtime_source` در current-release.json استفاده کن و نسخهٔ ثابت جدید با hash و Stage acceptance بساز. header خانوادهٔ `X-Barsan-Runtime: uvicorn-final-native` شناسهٔ commit نیست.

## وضعیت و restart واقعی

```sh
/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl -c /home/myexperi/lifetrade-staging/native-asgi-trial-20261102/supervisor.ini status barsan-native
/home/myexperi/virtualenv/lifetrade/3.12/bin/supervisorctl -c /home/myexperi/lifetrade/native-asgi/supervisor.ini status barsan-native
```

پس از تغییر مجاز تنظیمات، `restart` به‌جای `status` همان محیط اجرا و سلامت API/صفحهٔ عمومی بررسی شود. لمس restart.txt یا دکمهٔ restart مربوط به Passenger، Uvicorn را restart نمی‌کند. وضعیت RUNNING به‌تنهایی اثبات سلامت یا نسخه نیست. سلامت باید PostgreSQL متصل را نشان دهد و hash منبع با manifest بخواند.

هر دو سرویس autorestart و cron مستقل @reboot دارند؛ reboot واقعی هاست انجام/آزمایش نشده است. log rotation فعال است. متن کامل log خصوصی یا env را چاپ نکن؛ برای بررسی آزمون فقط فیلدهای آماری مشخص گزارش ساختاریافته را بخوان.

## متغیرهای مؤثر

مقادیر محرمانه فقط از فایل خصوصی همان محیط خوانده می‌شوند؛ مقدار ADMIN_TOKEN یا DATABASE_URL یا کلید را وارد Git/گزارش/چت نکن. Production از مقادیر قدیمی خود استفاده می‌کند و توکن فعلی مدیریت عوض نشده است.

نام‌ها: `ADMIN_TOKEN`، `DATABASE_URL`، `STUDY_CODE`، `STUDY_NAME`، `PUBLIC_EXPERIMENT_URL`، `INVITATION_TOKEN_KEY_FILE`، `DB_POOL_MIN_SIZE`، `DB_POOL_MAX_SIZE`، `DB_POOL_TIMEOUT`، `DB_POOL_MAX_WAITING` و `DB_CONNECT_TIMEOUT`.

launcher این انتشار دو worker، sync limit=8، DB pool min/max=1/4، timeout=5، max_waiting=128، connect_timeout=5، `BARSAN_REQUEST_METRICS=1` و `BARSAN_EXPORT_IDLE_SECONDS=90` را اعمال می‌کند. این‌ها الزاماً معادل Application Manager نیستند؛ تغییر فایل env به‌تنهایی جای تغییر override آگاهانهٔ launcher را نمی‌گیرد. فعال‌بودن حفاظت خروجی در آزمون نهایی از انتظار واقعی و تکمیل هر دو دریافت تأیید شد.

کلید دعوت Production همان فایل خصوصی home قبلی است؛ Staging کلید مستقل دارد. هیچ‌گاه کلید/env/DB Stage را به اصلی کپی نکن. تنها دارایی عمومی Stimuli به‌عنوان کد رابط از انتشار ثابت Stage کپی شده است.

## آزمون و انتشار بعدی

آزمون‌های بار، fault و مسیر مصنوعی فقط Staging هستند. روی Production فقط بررسی لازم انتشار انجام شود. منابع حساب دو محیط مشترک‌اند؛ هم‌زمان با جمع‌آوری اصلی، روی Stage بار سنگین جدا اجرا نشود. دریافت کامل CSV/ZIP عمداً در زمان فعالیت اخیر تسک‌ها منتظر است؛ پایش پیشرفت همچنان قابل استفاده است.

ابزارهای ops این نوبت مسیر، cohort و artifact مشخص دارند و one-shot هستند. دوباره‌اجرا نکن و assertها را صرفاً برای عبور حذف نکن. دعوت‌نامه‌های cohortهای بار روی هاست خصوصی ماندند. فقط همان دو حساب مصنوعی با تأیید کاربر به پوشهٔ خصوصی Mac منتقل شدند؛ لینک‌هایشان در پایان غیرفعال و داده‌ها حفظ شدند. آماده‌سازی بازآزمایی فقط همان دو لینک، snapshot و audit دارد و مجوز reset دیگر داده‌ها نیست.

برای انتشار جدید: SHA و CI دقیق را کنترل کن، runtime و ۲۰۳ دارایی عمومی را به منبع ثابت جدید ببر، backup و manifest بساز، Stage را تأیید کن، main را بعد از merge دوباره بررسی کن و همان فایل‌های ثابت را با env/کلید/DB مستقل اصلی منتشر کن. استقرار مجدد Production که native موجود دارد باید Supervisor موجود را آگاهانه به منبع جدید منتقل کند؛ اسکریپت این نوبت مهاجرت اولیهٔ Passenger را توصیف می‌کند و نباید کورکورانه دوباره اجرا شود.

## پشتیبان و بازگشت این انتشار

زیر `deployments/final-8e4838c54e3f/` هر محیط: `database-before.dump` و `backup/` شامل سورس قبلی، routing، تنظیمات قبلیِ موجود و کلید. `pg_restore --list` بررسی شد؛ restore کامل یا reboot واقعی اجرا نشده است. پشتیبان در همان هاست و خارج از web/Git است.

در Production، `packages-before.json`، wheelهای قبلی و target و نتیجهٔ همسان‌سازی چهار کتابخانه نیز خصوصی نگه داشته شده‌اند. توکن و کلید با ۸ پردازش قبلی تطبیق داشتند. ۸ LSWGI قدیمی فقط پس از سلامت native با SIGTERM کنار رفتند؛ منبع native اکنون مستقل است.

Rollback باید routing/source/launcher/service و در صورت لزوم کتابخانه‌های ثبت‌شده را هماهنگ برگرداند، سپس سلامت عمومی/PG را بررسی کند. دادهٔ تازه را با restore یک dump قدیمی جایگزین نکن. DDL افزایشی خودکار با rollback فایل پاک نمی‌شود. هیچ DROP، reset، حذف کلی، rsync --delete یا کپی SQLite روی PostgreSQL مجاز نشده است.
