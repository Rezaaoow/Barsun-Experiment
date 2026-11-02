# راهنمای آزمون بار BarSan در staging

این ابزار برای آزمون backend با حداکثر ۶۰۰ شرکت‌کنندهٔ ساختگی طراحی شده است. سناریو هر شرکت‌کننده را از consent تا پایان هر سه ماژول اجرا می‌کند؛ فاصله‌های کوتاه پاسخ‌گویی پرسشنامه، batchهای Life-Trade و پنجره‌های انتظار Ultimatum را شبیه‌سازی می‌کند و heartbeatهای ۱۵ثانیه‌ای را در حین فعالیت می‌فرستد. با این حال، UI واقعی مرورگر و همهٔ رفتارهای انسانی را بازسازی نمی‌کند.

## هشدار عملیاتی

- این ابزار را فقط روی staging مستقل با دیتابیس دورریختنی اجرا کنید.
- از دامنهٔ زندهٔ my-experiment.ir یا هر subdomain آن استفاده نکنید؛ generator این میزبان را رد می‌کند.
- staging باید تنظیمات runtime مشابه production داشته باشد: Python، Passenger، PostgreSQL، pool و reverse proxy.
- آزمون را از یک کامپیوتر جدا از هاست اجرا کنید؛ اجرای k6 روی همان هاست CPU و RAM برنامه را مصرف می‌کند و نتیجه را مخدوش می‌کند.
- generator مقدار ADMIN_TOKEN را فقط از environment می‌خواند و هرگز چاپ نمی‌کند.
- قبل از ساخت لینک‌ها، health endpoint باید `ok=true` و دیتابیس PostgreSQL متصل را گزارش کند. k6 هم پیش از شروع VUها همین preflight را بررسی می‌کند.
- فایل participants.json حاوی invitation tokenهاست. فایل با permission محدود ساخته می‌شود و در همین پوشه gitignored است. آن را commit، ایمیل یا share نکنید.
- هر بار ۶۰۰ assignment جدید در STUDY_CODE تنظیم‌شده در staging ایجاد می‌شود. generator داده یا لینک‌های قدیمی را پاک نمی‌کند.
- پاسخ‌های پرسشنامه و trialهای Life-Trade و Ultimatum در دیتابیس staging نوشته می‌شوند؛ این‌ها دادهٔ پژوهشی واقعی نیستند و نباید وارد تحلیل اصلی شوند.

## تشخیص محدودیت‌های cPanel و PostgreSQL

ابتدا وارد ریشهٔ clone فعال برنامه در staging شوید (همان پوشه‌ای که `tests/load/` داخل آن است) و سپس فایل `tests/load/host_diagnostics.py` را با Python همان محیط اجرا کنید. مسیر `Application Root` الزاماً ریشهٔ import برنامه نیست؛ در cPanel فعلی پروژه، کد فعال از زیرپوشهٔ `repo/` بارگذاری می‌شود. برای staging، مسیر واقعی clone و virtualenv همان staging را به‌کار ببرید. این ابزار فقط اطلاعات را می‌خواند و این موارد را گزارش می‌کند:
- CPU affinity و quota قابل‌مشاهده، محدودیت RAM در cgroup؛
- تعداد processهای متعلق به UID جاری و سقف file descriptor/process؛
- مقادیر DB pool تنظیم‌شده در محیط؛
- نسخهٔ PostgreSQL، `max_connections`، connectionهای active/idle و چند تنظیم حافظهٔ دیتابیس.

تعداد processهای UID شامل processهای دیگر حساب cPanel و خود Terminal نیز می‌شود؛ لزوماً تعداد Passenger worker نیست. در نتیجه مقدار pool محاسبه‌شده صرفاً upper bound است، نه تعداد اتصال واقعی برنامه.

این ابزار snapshot لحظه‌ای از محدودیت‌های قابل‌مشاهدهٔ process جاری می‌گیرد، نه نمودار مصرف CPU/RAM/IO و نه شمارش قطعی Passenger workerها. Terminal ممکن است environment یکسانی با Passenger نداشته باشد؛ مقادیر `DB_POOL_*` و `DB_CONNECT_TIMEOUT` در خروجی فقط وقتی نمایندهٔ تنظیمات برنامه‌اند که همان مقادیر cPanel را به session ترمینال منتقل کرده باشید. اگر این متغیرها در cPanel تعریف نشده‌اند، مقدارهای پیش‌فرض کد گزارش می‌شوند. تعداد processهای UID نیز شامل سایر processهای حساب و Terminal است.

اگر `DATABASE_URL` در Terminal موجود نیست، آن را از Environment Variables برنامه در cPanel بردارید و در prompt مخفی وارد کنید؛ مقدار در command history قرار نمی‌گیرد و اسکریپت آن را چاپ نمی‌کند:

    (
      set +x
      read -r -s -p "DATABASE_URL (hidden input): " DATABASE_URL
      printf '\n'
      export DATABASE_URL
      STAGING_PYTHON="/path/to/staging/virtualenv/bin/python"
      "$STAGING_PYTHON" tests/load/host_diagnostics.py
    )

در صورت تنظیم بودن متغیر در محیط shell، فقط دستور Python کافی است. خروجی JSON را می‌توانید برای تحلیل ارسال کنید؛ در هر صورت قبل از ارسال بررسی کنید هیچ دادهٔ محرمانهٔ دیگری در خروجی یا Terminal اضافه نشده باشد.

## ۱. آماده‌سازی staging

یک کپی مستقل از برنامه و PostgreSQL بسازید. مطمئن شوید متغیرهای cPanel/Passenger روی staging به دیتابیس staging اشاره می‌کنند و STUDY_CODE اختصاصی staging است.

BASE_URL باید آدرس ریشهٔ برنامه باشد، نه مسیر /api؛ برای نمونه ساختاری مانند:
https://staging.example.org/lifetrade

در صورت استفاده از PUBLIC_EXPERIMENT_URL، آن را نیز روی همین آدرس staging تنظیم کنید تا لینک‌های دعوت به محیط درست اشاره کنند.

## ۲. ساخت invitationهای آزمایشی

Python 3.12 یا جدیدتر کافی است؛ وابستگی third-party اضافی برای generator وجود ندارد.

از ریشهٔ مخزن اجرا کنید:

    (
      set +x
      read -r -s -p "ADMIN_TOKEN (hidden input): " ADMIN_TOKEN
      printf '\n'
      export ADMIN_TOKEN
      export BASE_URL="https://staging.example.org/lifetrade"
      export LOAD_TEST_CONFIRM="I CONFIRM THIS IS A DEDICATED STAGING ENVIRONMENT"
      export LOAD_TEST_USERS=600
      export PARTICIPANTS_FILE="$PWD/tests/load/participants.json"
      python3 tests/load/create_load_test_users.py
    )

توکن مدیریت را در shell history یا اسکرین‌شات منتشر نکنید. بهتر است آن را در environment امن session قرار دهید، نه اینکه در فایل script یا repository بنویسید.

Generator ابتدا سلامت API و PostgreSQL را بررسی می‌کند و سپس لینک‌ها را در batchهای حداکثر ۵۰۰تایی ایجاد می‌کند. در صورت خطای batch میانی، فایل خصوصی با وضعیت partial باقی می‌ماند؛ آن را برای اجرای k6 استفاده نکنید. لینک‌های ساخته‌شده در staging باقی می‌مانند و باید از پنل staging مدیریت یا همراه با دیتابیس staging دورریخته شوند.

## ۳. اجرای k6

k6 را روی کامپیوتر جداگانه نصب کنید. سپس از ریشهٔ مخزن اجرا کنید:

    k6 run \
      -e BASE_URL="https://staging.example.org/lifetrade" \
      -e PARTICIPANTS_FILE="$PWD/tests/load/participants.json" \
      -e EXPECTED_USERS=600 \
      tests/load/k6_experiment.js

سناریو با ramp-up از ۱۰۰ به ۳۰۰ و سپس ۶۰۰ کاربر مجازی، یک اجرای کامل برای هر invitation انجام می‌دهد و بعد ظرفیت را در سطح ۶۰۰ VU برای سه دقیقه نگه می‌دارد. هر VU فقط از لینک اختصاصی خودش استفاده می‌کند.

در نسخهٔ فعلی سناریوی k6، هر شرکت‌کننده ۱۱۲ درخواست ثابت تولید می‌کند: ۲۵ ذخیرهٔ پاسخ (که همان درخواست draft مرحله را هم به‌روز می‌کند)، ۳۰ trial در شش batch پنج‌تایی Life-Trade، ۲۲ دور Ultimatum با سه درخواست در هر دور، consent/identify و شروع/تکمیل مراحل. با زمان‌های انتظار شبیه‌سازی‌شده حدود ۱۳ heartbeat دیگر برای هر نفر انتظار می‌رود؛ بنابراین برآورد معمول حدود ۱۲۵ درخواست کل به‌ازای هر نفر و حدود ۷۵٬۰۰۰ درخواست برای ۶۰۰ نفر است. هر بار حرکت برگشت در پرسشنامه نیز یک draft write اضافه می‌کند. این ارقام تخمین اسکریپت‌اند و benchmark اندازه‌گیری‌شده نیستند.

## ۴. دو پروفایل بار

هر profile باید با یک cohort جدا اجرا شود. generator برای هر اجرا شناسهٔ تصادفی به participant code اضافه می‌کند؛ بنابراین می‌توانید دو فایل مستقل بسازید.

**Ramp-up تدریجی:** `LOAD_PROFILE=ramp`؛ از ۱۰۰ به ۳۰۰ و سپس ۶۰۰ VU می‌رسد و ظرفیت را سه دقیقه در سطح ۶۰۰ نگه می‌دارد. دستور بالا همین profile را اجرا می‌کند.

**Burst تقریباً هم‌زمان:** cohort دوم را با فایل جدا بسازید:

    BASE_URL="https://staging.example.org/lifetrade" \
    ADMIN_TOKEN="..." \
    LOAD_TEST_CONFIRM="I CONFIRM THIS IS A DEDICATED STAGING ENVIRONMENT" \
    LOAD_TEST_USERS=600 \
    PARTICIPANTS_FILE="$PWD/tests/load/participants-burst.json" \
    python3 tests/load/create_load_test_users.py

سپس اجرا کنید:

    k6 run \
      -e BASE_URL="https://staging.example.org/lifetrade" \
      -e PARTICIPANTS_FILE="$PWD/tests/load/participants-burst.json" \
      -e EXPECTED_USERS=600 \
      -e LOAD_PROFILE=burst \
      tests/load/k6_experiment.js

پروفایل burst از executor نوع per-vu-iterations با ۶۰۰ VU و یک iteration برای هر VU استفاده می‌کند و برای تکمیل حداکثر ۸ دقیقه فرصت دارد. ramp-up و burst را هم‌زمان اجرا نکنید؛ آن‌ها را در دو اجرای مستقل و با دو cohort جدا بسنجید.

## ۵. معنای سناریو و محدودیت‌ها

این آزمون UI مرورگر، رندر، موبایل، زمان تصمیم‌گیری و انتظارهای عمدی بازی را شبیه‌سازی نمی‌کند. همچنین APIها را سریع‌تر از رفتار طبیعی کاربران پشت سر هم فراخوانی می‌کند؛ بنابراین یک آزمون فشار backend است، نه شبیه‌سازی کامل تجربهٔ انسانی.

در طول اجرا این داده‌ها را از cPanel/میزبان و PostgreSQL ثبت کنید:
- CPU و RAM مصرفی، IO/IOPS و محدودیت‌های CloudLinux؛
- تعداد Passenger processها و restart/crashها؛
- تعداد connectionهای PostgreSQL، connection pool wait/timeout و صف درخواست‌ها؛
- HTTP error rate، p50، p95، p99 و تعداد شرکت‌کنندگان تکمیل‌شده؛
- تعداد رکوردها و صحت کامل شدن ۲۵ پاسخ پرسشنامه، ۳۰ trial آزمایشی DD و ۲۲ دور Ultimatum برای هر شرکت‌کننده.

## ۶. معیار اولیه

k6 در این فایل این thresholdها را اعمال می‌کند:
- خطای HTTP کمتر از ۰٫۱٪؛
- p95 زمان درخواست کمتر از ۵۰۰ms؛
- p99 کمتر از ۱٫۵s؛
- تکمیل تمام شرکت‌کنندگان مورد انتظار.

عبور از thresholdهای k6 به‌تنهایی برای تأیید ظرفیت کافی نیست. اگر درخواست‌ها موفق باشند ولی CPU/RAM، pool یا دیتابیس به سقف برسند، tail latency افزایش یابد یا رکوردی ناقص/تکراری باشد، آزمون پذیرفته نیست.

پس از هر اجرا summary کامل k6 و نمودارهای resource monitoring را ذخیره کنید. هیچ آزمون بار سنگینی را هم‌زمان با رویداد واقعی اجرا نکنید.
