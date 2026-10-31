# گزارش ممیزی کارایی و آزمون بار — رویداد ۶۰۰ شرکت‌کننده

## وضعیت این گزارش

این سند ممیزی کد و دو پاس بهینه‌سازی Backend در شاخهٔ `perf/600-user-event-readiness` را ثبت می‌کند. تغییرهای اجرایی در CI روی SQLite و PostgreSQL 16 تست شده‌اند؛ آخرین اجرای معتبر شامل ۶۱ تست SQLite و PostgreSQL smoke/concurrency test برای ۲۴ شرکت‌کننده بوده است. بااین‌حال benchmark زمان‌دار و load test واقعی ۶۰۰ VU روی staging هنوز اجرا نشده‌اند؛ بنابراین این گزارش ظرفیت ۶۰۰ نفر را تأیید نمی‌کند.

## مشخصات هاست اعلام‌شده

| منبع | مقدار |
|---|---:|
| CPU | ۲ هسته |
| RAM | ۴ گیگابایت |
| فضای فایل/هاست | ۱۵ گیگابایت |
| فضای دیتابیس | ۱۵ گیگابایت |

هنوز مشخص نیست CPU و RAM اعلام‌شده فقط به حساب/برنامه تعلق دارند یا PostgreSQL و سایر سرویس‌های هاست نیز از همین سهم استفاده می‌کنند. همچنین تعداد processهای Passenger، سقف Entry Processes/NPROC، سقف اتصال PostgreSQL، محدودیت I/O و وضعیت اشتراکی یا اختصاصی CPU باید از کنترل‌پنل یا میزبان دریافت شوند.

**ارزیابی مقدماتی:** دو هسته و ۴GB RAM برای یک API سبک می‌تواند ۶۰۰ نشست کاربری را در صورت پخش بودن درخواست‌ها پشتیبانی کند، اما burst هم‌زمان، محدودیت thread/process در Passenger و دیتابیس می‌توانند به صف و timeout منجر شوند. تا پیش از آزمون، این پیکربندی را برای رویداد قطعی ۶۰۰نفره تأییدشده نمی‌دانیم. ظرفیت ۱۵GB دیتابیس برای داده‌های یک رویداد با این ابعاد احتمالاً از نظر حجم خام کافی است؛ ولی IOPS، latency دیسک و سقف اتصال را نشان نمی‌دهد.

## معماری فعلی مشاهده‌شده

- Backend یک برنامهٔ FastAPI/Python با handlerهای عمدتاً synchronous و دسترسی synchronous به PostgreSQL از طریق psycopg است.
- روی PostgreSQL از connection pool کتابخانهٔ `psycopg_pool` استفاده می‌شود.
- تنظیمات پیش‌فرض کد: `DB_POOL_MIN_SIZE=1`، `DB_POOL_MAX_SIZE=4`، `DB_POOL_TIMEOUT=5s`، `DB_POOL_MAX_WAITING=128` و `DB_CONNECT_TIMEOUT=5s`.
- اندازهٔ pool به‌ازای هر process است؛ بنابراین سقف بالقوهٔ اتصال برنامه تقریباً `DB_POOL_MAX_SIZE × تعداد Passenger processها` است. تعداد processهای واقعی از repository قابل استنتاج نیست.
- دیتابیس production باید PostgreSQL باشد؛ SQLite فقط برای تست محلی مناسب است.
- ساختار اصلی داده‌ها، کلیدهای اصلی و unique constraintها برای lookupهای رایج وجود دارند. افزودن index بدون بررسی query plan می‌تواند writeها را کند کند؛ index جدید فقط پس از مشاهدهٔ `EXPLAIN (ANALYZE, BUFFERS)` روی دیتابیس staging اضافه شود.

## گلوگاه‌ها و هزینه‌های درخواست

### ۱. احراز هویت در مسیر پرتکرار

این کنترل‌ها برای هر درخواست participant ضروری‌اند: تطبیق invitation token و participant code، ثبت رضایت، و فعال بودن run/module مربوط. در پاس اول، token/code و consent در lookup واحد ترکیب شد. در Ultimatum نیز در پاس دوم، lookup هویت، رضایت، stage/run فعال، نوع instrument و ردیف trial در یک SELECT ترکیب شده‌اند؛ بنابراین دیگر برای resolve/events چند SELECT متوالی برای همین کنترل‌ها اجرا نمی‌شود. این تغییر کنترل امنیتی را حذف نمی‌کند و صرفاً round-trip تکراری را کاهش می‌دهد.

### ۲. Heartbeat زمان‌سنجی

رابط برای هر شرکت‌کنندهٔ فعال هر ۱۵ ثانیه یک درخواست `/api/experiment/time` ارسال می‌کند. مسیر Backend در پاس اول از read-then-write جداگانه به یک UPSERT اتمیک برای start و یک UPDATE اتمیک برای heartbeat/pause/complete تبدیل شد؛ محاسبهٔ elapsed داخل SQL انجام می‌شود و سقف ۴۵ ثانیه و semantics قبلی pause/heartbeat حفظ شده‌اند.

در صورت فعال بودن هم‌زمان ۶۰۰ مرورگر، همچنان حدود ۴۰ درخواست heartbeat در ثانیه ایجاد می‌شود؛ در ۳۰ دقیقه فعالیت کامل ۶۰۰ نفر، حدود ۷۲٬۰۰۰ heartbeat ثبت می‌شود. بهینه‌سازی round-trip دیتابیس را برای هر event به یک write statement کاهش می‌دهد، اما تعداد درخواست‌های HTTP را حذف نمی‌کند.

### ۳. پرسشنامه

هر یک از ۲۵ پاسخ به‌صورت یک درخواست مجزا ذخیره می‌شود. در مقیاس ۶۰۰ نفر، تکمیل کامل پرسشنامه می‌تواند ۱۵٬۰۰۰ درخواست ذخیره پاسخ بسازد. این مسیر برای هر پاسخ upsert پاسخ و به‌روزرسانی draft را انجام می‌دهد. نباید ذخیرهٔ پایدار پاسخ یا امکان بازیابی پس از refresh حذف شود.

### ۴. DD / Life-Trade

رابط فعلی پاسخ‌های trial را ابتدا در localStorage صف می‌کند و به‌صورت batch ارسال می‌کند؛ این یک تصمیم مثبت برای کاهش درخواست‌ها و جدا کردن تأخیر شبکه از زمان انتخاب است. اندازهٔ flush معمولاً از ۵ پاسخ شروع می‌شود و API تا ۲۵ پاسخ را در یک batch می‌پذیرد. حفظ idempotency و بازیابی صف محلی حیاتی است؛ حذف این صف یا ارسال غیرقابل‌تکرار پاسخ‌ها مجاز نیست.

### ۵. Ultimatum

هر شرکت‌کننده ۲ دور تمرینی و ۲۰ دور اصلی دارد. برای هر دور، مسیر عادی فعلی شامل:
- یک درخواست `prepare`؛
- یک درخواست `resolve`؛
- یک درخواست `events` برای زمان‌های پس از نتیجه.

در صورت تکمیل همهٔ ۲۲ دور توسط ۶۰۰ نفر، این الگو تا حدود ۳۹٬۶۰۰ درخواست HTTP ایجاد می‌کند، جدا از شروع run، پرسشنامه، DD/Life-Trade و heartbeat. مسیرهای prepare/resolve علاوه بر دسترسی به assignment و رضایت، queryهای اعتبارسنجی و queryهای ثبت/بازیابی نتیجه دارند. endpoint رخدادهای پس از نتیجه نیز پیش از رفتن به دور بعد در مرورگر await می‌شود؛ بنابراین latency آن مستقیماً می‌تواند در فاصلهٔ بین دورها دیده شود.

قید یکتای `(run_id, phase, block_code, trial_in_block)` و وضعیت‌های `prepared/completed` برای جلوگیری از ثبت تکراری/دوگانه بخشی از صحت داده‌اند و حفظ شده‌اند. در پاس اول، ردیف‌های run که prepare از قبل خوانده بود برای count/order/existing دوباره استفاده می‌شوند و aggregate امتیاز run/block با یک query گروه‌بندی‌شده انجام می‌شود. در پاس دوم، consent/run/instrument/trial برای resolve/events در یک read و consent/stage/draft/instrument برای prepare در یک read دریافت می‌شوند. محاسبهٔ امتیاز، منطق تصمیم AI، تعداد دورها و خروجی participant تغییر نکرده‌اند.

### ۶. گزارش‌ها و APIهای مدیریت

گزارش پیشرفت، CSV و ZIP می‌توانند تعداد زیادی ردیف را بخوانند یا در حافظه serialize کنند. این مسیرها معمولاً توسط تیم پژوهش استفاده می‌شوند و نباید در ساعات اجرای رویداد هم‌زمان با اوج مشارکت‌کننده‌ها، exportهای بزرگ اجرا شوند. این مورد بیشتر مسئلهٔ عملیاتی است تا مسیر پرتکرار participant.

## تغییرات اجرایی انجام‌شده

دو پاس Backend در همین شاخه پیاده‌سازی و در CI تأیید شده‌اند:

- token/code و consent در lookup واحد؛
- اعتبارسنجی active run و instrument در یک join برای مسیرهای مربوط؛
- زمان‌سنجی اتمیک با UPSERT/UPDATE در هر دو SQLite و PostgreSQL، با حفظ سقف ۴۵ ثانیه و semantics قبلی؛
- استفادهٔ مجدد از ردیف‌های همان run در prepare به‌جای queryهای count/order/existing جداگانه؛
- تجمیع مجموع امتیاز run و block در یک query گروه‌بندی‌شده؛
- ادغام lookup لینک، consent، stage/run فعال، instrument و trial برای resolve/events؛
- ادغام lookup لینک/رضایت و دریافت stage/draft/instrument برای prepare؛
- تست‌های رگرسیون برای حفظ خطاهای 403/404/409، رفتار heartbeat، زمان‌سنجی و idempotency.

آخرین CI روی SHA فعلی: ۶۱ تست SQLite موفق، PostgreSQL 16 smoke test و تست هم‌زمانی ۲۴ شرکت‌کننده موفق. این تأیید، correctness و سازگاری دو دیتابیس را نشان می‌دهد؛ benchmark زمان‌دار و ظرفیت واقعی ۶۰۰ VU همچنان تأیید نشده‌اند.

## برآورد بار نسخهٔ فعلی k6

سناریوی اجرایی حاضر برای هر شرکت‌کننده حدود ۱۵۰ درخواست API ایجاد می‌کند: ۲۵ پاسخ و ۲۵ draft پرسشنامه، ۳۰ trial در شش batch پنج‌تایی Life-Trade، ۲۲ دور Ultimatum با سه درخواست در هر دور، به‌علاوهٔ consent/identify، شروع و تکمیل مراحل و heartbeatهای زمان‌سنجی. تعداد heartbeatها به زمان انتظار تصادفی Ultimatum و latency درخواست‌ها وابسته است؛ بنابراین برای ۶۰۰ شرکت‌کننده حدود **۹۰٬۰۰۰ درخواست** برآورد می‌شود. این برآورد از رفتار اسکریپت است، نه نتیجهٔ اندازه‌گیری.

## مرحلهٔ تکمیلی query optimization

کاهش round-tripهای مسیرهای پرتکرار Ultimatum در کد پیاده‌سازی شده و روی SQLite و PostgreSQL تست شده است:

- ترکیب lookup لینک/کد، consent، stage/run فعال، instrument و trial در یک SELECT برای resolve و events؛
- ترکیب خواندن draft/status مرحله با بررسی instrument/run برای prepare؛
- حفظ ترتیب خطاهای 403/404/409، بررسی role و payload، unique constraint و idempotency resolve/events.

این تغییرها خواندن‌های تکراری را حذف می‌کنند، نه کنترل‌های دسترسی. query-count و latency در محیط CI از نظر کارکردی بررسی شده‌اند، اما هنوز benchmark زمان‌دار و load test واقعی اجرا نشده است.

## اولویت‌های باقیمانده

1. اندازه‌گیری baseline روی staging: latency هر endpoint، تعداد query، pool wait/timeout و منابع process/DB.
2. بررسی query plan مسیرهای پرتکرار با `EXPLAIN (ANALYZE, BUFFERS)` و ایجاد index فقط بر مبنای شواهد.
3. اندازه‌گیری و کاهش تأخیر رخدادهای پس از resolve، بدون از دست دادن timestamps؛ هر صف احتمالی باید retry، idempotency و بازیابی پس از refresh داشته باشد.
4. تنظیم pool فقط پس از مشخص شدن processهای Passenger و سقف PostgreSQL؛ افزایش کورکورانهٔ pool ممنوع.
5. اجرای آزمون بار واقعی ramp و burst با ۶۰۰ هویت آزمایشی روی staging، نه روی دادهٔ اصلی.
6. بررسی نتایج و تصمیم دربارهٔ نیاز به VPS/سرویس دیتابیس مستقل بر اساس CPU/RAM/IO/connectionهای واقعی.

## طرح آزمون بار پذیرش

آزمون باید روی staging با دیتابیس دورریز اجرا شود. به هیچ وجه روی دادهٔ اصلی یا هم‌زمان با اجرای رویداد تست سنگین انجام نشود.

سناریوها:
- ۶۰۰ نفر در یک burst کوتاه برای باز کردن صفحه، consent، identify و start؛
- ۶۰۰ نفر با ramp-up کنترل‌شده برای تشخیص سقف throughput؛
- ورود هم‌زمان به مرحلهٔ پرسشنامه و burst ذخیرهٔ پاسخ‌ها؛
- ادامهٔ کامل workflow تا DD/Life-Trade و Ultimatum؛
- قطع موقت شبکه و retry/refresh برای بررسی عدم گم‌شدن یا دوبرابرشدن داده‌ها؛
- اجرای ۶۰۰ نشست همراه heartbeat برای حداقل ۱۵ تا ۳۰ دقیقه؛
- اجرای جداگانهٔ export مدیریتی برای ارزیابی تداخل منابع.

شاخص‌های پذیرش اولیه (قابل تنظیم پس از baseline):
- خطای 5xx/timeout کمتر از ۰٫۱٪ و بدون از دست رفتن پاسخ؛
- p95 درخواست‌های تعاملی زیر ۵۰۰ms و p99 زیر ۱٫۵s، به‌جز زمان انتظار عمدی داخل خود پروتکل؛
- صفر timeout از connection pool و صفر صف ماندگار؛
- صحت کامل تعداد trialها، ترتیب blockها، امتیازها و شناسه‌های یکتا؛
- بدون restart/crash برنامه و بدون رشد پیوستهٔ RAM در طول آزمون.

یک اجرای موفق load test باید گزارش k6 و هم‌زمان آمار CPU/RAM/IO، process و connectionهای PostgreSQL را ذخیره کند. تستی که فقط HTTP 200 می‌گیرد اما از دست رفتن trial، duplicate، یا تأخیرهای tail را کنترل نمی‌کند، آزمون پذیرش نیست.

## اطلاعات میزبان که هنوز باید مشخص شود

- تعداد Passenger application processها و threadها/حداکثر هم‌زمانی هر process؛
- آیا PostgreSQL روی همان سهم ۲ vCPU/۴GB اجرا می‌شود یا سرویس/ماشین جداست؛
- مقدار `max_connections` دیتابیس و سقف اتصال اختصاص‌یافته به این برنامه؛
- محدودیت‌های CloudLinux/cPanel شامل CPU, PMEM, Entry Processes, NPROC, IO و IOPS؛
- نوع و نسخهٔ PostgreSQL و محل دیتابیس؛
- latency واقعی از هاست به دیتابیس.

## ابزار اجرای آزمون بار

مخزن اکنون ابزار محلی staging-only برای ساخت ۶۰۰ assignment و یک سناریوی k6 برای workflow کامل دارد:
- `tests/load/create_load_test_users.py` — بررسی health/PostgreSQL، سپس ساخت لینک‌ها با ADMIN_TOKEN محیطی و خروجی خصوصی؛
- `tests/load/k6_experiment.js` — health preflight، دو profile مستقل ramp و burst تا ۶۰۰ VU، heartbeat و اجرای سه ماژول از طریق API؛
- `tests/load/host_diagnostics.py` — snapshot read-only از CPU/RAM quota، process limits، pool و PostgreSQL؛
- `tests/load/README_FA.md` — روش اجرا، ایمنی و تفسیر نتایج.

وجود ابزار به معنی اجرای آزمون یا تأیید ظرفیت نیست. فایل کاربران تولیدی و tokenها نباید commit یا برای محیط اصلی استفاده شوند.

## وضعیت اجرا

| مرحله | وضعیت |
|---|---|
| ممیزی ساختار Backend و APIها | بررسی اولیه انجام شده |
| ممیزی UI و نحوهٔ ارسال درخواست‌ها | بررسی اولیه انجام شده |
| ثبت مشخصات اسمی هاست | ثبت شده؛ جزییات Passenger و DB منتظر خروجی diagnostics |
| بهینه‌سازی کد اجرایی | دو پاس Backend در PR #17 پیاده‌سازی و در CI تست شده‌اند: query consolidation، time tracking اتمیک و grouped aggregation |
| تست رگرسیون روی نسخهٔ بهینه‌شده | موفق: ۶۱ تست SQLite، smoke test PostgreSQL 16 و تست هم‌زمانی ۲۴ شرکت‌کننده روی SHA فعلی PR #17 |
| ابزار load test با ۶۰۰ VU | آماده: دو profile مجزای ramp و burst؛ هنوز اجرا نشده |
| تأیید آمادگی production | انجام نشده |

هیچ‌یک از اعداد ظرفیت این سند نباید به‌عنوان benchmark اندازه‌گیری‌شده معرفی شود.
