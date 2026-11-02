# عملیات ظرفیت Staging

این پوشه snapshot ابزارهای واقعاً استفاده‌شده در آزمون ۳ نوامبر ۲۰۲۶ است. هدف‌ها به Staging همین پروژه و loopback همان runtime محدودند؛ Production هدف هیچ workload نیست.

- کد فعال: `7af183881410faf5d089efefe44d71d43781d8ce`؛ تغییرات پس از آن در این پوشه/گزارش‌ها، انتشار خودکار runtime نیستند.
- مدیریت سرویس در `/home/myexperi/lifetrade-staging/native-asgi-trial-20261102/`؛ دادهٔ آزمون در `/home/myexperi/lifetrade-staging/test-runs/capacity-fix-20261103/`.
- `native_asgi_launcher.py` باید به نام `launch_native.py` در پوشهٔ مدیریت قرار گیرد. به hash هر دو فایل `barsan_experiment.py` و `request_metrics.py` مقید است؛ هنگام تغییر کد، guard و بسته باید هماهنگ و پس از CI همان SHA منتشر شوند.
- دو worker، حداکثر هشت handler هم‌زمان sync در هر worker، pool حداکثر چهار اتصال در هر worker. `BARSAN_SYNC_THREADS=0` یا unset رفتار قبلی limiter را حفظ می‌کند؛ تنظیم هشت‌تایی فقط در launcher Staging فعال است.
- `BARSAN_REQUEST_METRICS=1` فقط برای تشخیص فعال است. لاگ شامل body، query string، پارامتر SQL، کد شرکت‌کننده یا invitation نیست. Supervisor برای stdout حداکثر ۲۰MB و دو backup دارد.
- restart: `/home/myexperi/virtualenv/lifetrade-staging/3.12/bin/supervisorctl -c /home/myexperi/lifetrade-staging/native-asgi-trial-20261102/supervisor.ini restart barsan-native`.
- پس از restart تا readiness واقعی با retry محدود منتظر بمانید؛ RUNNING بودن manager به‌تنهایی آماده‌بودن workerها را ثابت نمی‌کند. برای جابه‌جایی dependency جدید، سرویس ابتدا متوقف شود؛ rollback نیز پیش از بازیابی مجموعهٔ فایل‌ها سرویس را متوقف کند.

## ابزارها

`check_client_recovery.mjs` توابع واقعی outboxهای `app/index.html` را با storage و شبکهٔ کنترل‌شده اجرا می‌کند و در CI قرار دارد. تست مرورگر یا ذخیرهٔ واقعی IndexedDB نیست.

`k6-capacity-fix.js` سناریوی کامل پرسشنامه، DD و Ultimatum را با think-time و انتظارهای دورها اجرا می‌کند. هر VU یک دعوت‌نامهٔ یکتا، start_request_id پایدار برای retry، event ID یکتا، batch پنج‌تایی و حداکثر سه تلاش در هر چرخه دارد. فایل fixture خصوصی باید پیش از اجرا آماده باشد؛ نمونهٔ واقعی یا token در Git نگهداری نمی‌شود. این driver، پس از سه خطای heartbeat می‌تواند جریان را متوقف کند؛ مرورگر اصلی رخدادهای کمکی را در outbox نگه می‌دارد، بنابراین شمارندهٔ تکمیل driver و وضعیت دیتابیس جدا سنجیده می‌شوند.

`k6-diagnostic.js` همان سناریو را برای دو مقصد ثابت HTTPS Staging یا `127.0.0.1:8766` می‌پذیرد. آزمون on-host با `run-on-host-dual.py` از binary رسمی k6 نصب‌شده در پوشهٔ خصوصی آزمون، با GOMAXPROCS=2 و guard حافظه/سلامت استفاده می‌کند؛ خود generator از منابع حساب مصرف می‌کند. فایل binary وارد مخزن نمی‌شود. نسخهٔ Linux v2.3.0 از release رسمی Grafana دریافت و SHA256 آن با digest API رسمی تطبیق داده شد.

`run-capacity-dual.py` بخش ۲۰۰نفرهٔ آزمون دو مبدأ را روی Mac خود کاربر اجرا می‌کند؛ fixture مصنوعی باید فقط با مجوز مربوط به همان انتقال، خصوصی و با مجوز 0600 باشد. ساخت لینک‌های جدید، scope مطالعهٔ مستقل و صحت cohort پیش از هر اجرا کنترل شود. fixture قدیمی را برای اجرای تازه به کار نبرید. helper مسیرها snapshot همین نوبت است؛ تغییر نام cohort نیازمند بررسی guardهای اسکریپت است.

`analyze_request_metrics.py` پنجرهٔ زمانی مشخص را از لاگ‌های چرخشی خلاصه می‌کند. تعداد درخواست‌های رسیده به ASGI با تعداد تلاش client برابر فرض نمی‌شود. اختلاف ساعت client/server در delta ورودی وجود دارد؛ دامنهٔ تغییر و تطبیق traceها را تحلیل کنید و مقدار مطلق را به‌عنوان زمان queue منتشر نکنید.

## معیار تفسیر

برای پذیرش، تکمیل client و شمارش مستقل PostgreSQL هر دو لازم‌اند: برای هر run مصنوعی ۲۵ پرسشنامه، ۳۰ DD و ۲۲ UT، با وضعیت completed مرحله و run. صرفاً صفرشدن خطای HTTP یا سلامت `/api/health` کافی نیست. p95/p99 دو مبدأ را میانگین نگیرید؛ تعداد درخواست‌ها و خطاها جمع‌پذیرند، quantileها نیستند.

نتیجهٔ اینترنت از یک IP را از ظرفیت ۶۰۰ شرکت‌کننده با اینترنت شخصی جدا کنید. چند مبدأ نیز معادل ۶۰۰ شبکه یا مرورگر واقعی نیست. جمع‌آوری واقعی پیش از پایلوت مرورگر/شبکهٔ نماینده و تصمیم پژوهشگر آغاز نشود. دادهٔ مصنوعی پس از آزمون غیرفعال می‌شود و برای ممیزی حفظ می‌شود؛ هیچ reset یا حذف کلی لازم نیست.
