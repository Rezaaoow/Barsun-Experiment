# راهنمای آزمون بار BarSan در staging

این ابزار برای آزمون backend با حداکثر ۶۰۰ شرکت‌کنندهٔ ساختگی طراحی شده است. سناریو هر شرکت‌کننده را از consent تا پایان هر سه ماژول اجرا می‌کند و عمداً بدون زمان‌های انتظار انسانی جلو می‌رود تا فشار API و دیتابیس را به‌شکل متمرکز بسنجد.

## هشدار عملیاتی

- این ابزار را فقط روی staging مستقل با دیتابیس دورریختنی اجرا کنید.
- از دامنهٔ زندهٔ my-experiment.ir یا هر subdomain آن استفاده نکنید؛ generator این میزبان را رد می‌کند.
- staging باید تنظیمات runtime مشابه production داشته باشد: Python، Passenger، PostgreSQL، pool و reverse proxy.
- آزمون را از یک کامپیوتر جدا از هاست اجرا کنید؛ اجرای k6 روی همان هاست CPU و RAM برنامه را مصرف می‌کند و نتیجه را مخدوش می‌کند.
- generator مقدار ADMIN_TOKEN را فقط از environment می‌خواند و هرگز چاپ نمی‌کند.
- فایل participants.json حاوی invitation tokenهاست. فایل با permission محدود ساخته می‌شود و در همین پوشه gitignored است. آن را commit، ایمیل یا share نکنید.
- هر بار ۶۰۰ assignment جدید در STUDY_CODE تنظیم‌شده در staging ایجاد می‌شود. generator داده یا لینک‌های قدیمی را پاک نمی‌کند.
- پاسخ‌های پرسشنامه و trialهای Life-Trade و Ultimatum در دیتابیس staging نوشته می‌شوند؛ این‌ها دادهٔ پژوهشی واقعی نیستند و نباید وارد تحلیل اصلی شوند.

## ۱. آماده‌سازی staging

یک کپی مستقل از برنامه و PostgreSQL بسازید. مطمئن شوید متغیرهای cPanel/Passenger روی staging به دیتابیس staging اشاره می‌کنند و STUDY_CODE اختصاصی staging است.

BASE_URL باید آدرس ریشهٔ برنامه باشد، نه مسیر /api؛ برای نمونه ساختاری مانند:
https://staging.example.org/lifetrade

در صورت استفاده از PUBLIC_EXPERIMENT_URL، آن را نیز روی همین آدرس staging تنظیم کنید تا لینک‌های دعوت به محیط درست اشاره کنند.

## ۲. ساخت invitationهای آزمایشی

Python 3.12 یا جدیدتر کافی است؛ وابستگی third-party اضافی برای generator وجود ندارد.

از ریشهٔ مخزن اجرا کنید:

    BASE_URL="https://staging.example.org/lifetrade" \
    ADMIN_TOKEN="..." \
    LOAD_TEST_CONFIRM="I CONFIRM THIS IS A DEDICATED STAGING ENVIRONMENT" \
    LOAD_TEST_USERS=600 \
    PARTICIPANTS_FILE="$PWD/tests/load/participants.json" \
    python3 tests/load/create_load_test_users.py

توکن مدیریت را در shell history یا اسکرین‌شات منتشر نکنید. بهتر است آن را در environment امن session قرار دهید، نه اینکه در فایل script یا repository بنویسید.

Generator لینک‌ها را در batchهای حداکثر ۵۰۰تایی ایجاد می‌کند. در صورت خطای batch میانی، فایل خصوصی با وضعیت partial باقی می‌ماند؛ آن را برای اجرای k6 استفاده نکنید. لینک‌های ساخته‌شده در staging باقی می‌مانند و باید از پنل staging مدیریت یا همراه با دیتابیس staging دورریخته شوند.

## ۳. اجرای k6

k6 را روی کامپیوتر جداگانه نصب کنید. سپس از ریشهٔ مخزن اجرا کنید:

    k6 run \
      -e BASE_URL="https://staging.example.org/lifetrade" \
      -e PARTICIPANTS_FILE="$PWD/tests/load/participants.json" \
      -e EXPECTED_USERS=600 \
      tests/load/k6_experiment.js

سناریو با ramp-up از ۱۰۰ به ۳۰۰ و سپس ۶۰۰ کاربر مجازی، یک اجرای کامل برای هر invitation انجام می‌دهد و بعد ظرفیت را در سطح ۶۰۰ VU برای سه دقیقه نگه می‌دارد. هر VU فقط از لینک اختصاصی خودش استفاده می‌کند.

هر شرکت‌کننده حدود ۱۱۱ درخواست API تولید می‌کند: ۲۵ پاسخ پرسشنامه، ۳۰ trial در دو batch Life-Trade، ۲۲ دور Ultimatum با سه درخواست در هر دور، به‌علاوهٔ consent/identify، شروع و تکمیل مراحل و ۹ درخواست time tracking (سه start، سه heartbeat و سه complete). برای ۶۰۰ نفر حدود ۶۶٬۶۰۰ درخواست است. این تعداد برآورد استاتیک از خود سناریوست، نه اندازه‌گیری انجام‌شده روی هاست.

## ۴. معنای سناریو و محدودیت‌ها

این آزمون UI مرورگر، رندر، موبایل، زمان تصمیم‌گیری و انتظارهای عمدی بازی را شبیه‌سازی نمی‌کند. همچنین APIها را سریع‌تر از رفتار طبیعی کاربران پشت سر هم فراخوانی می‌کند؛ بنابراین یک آزمون فشار backend است، نه شبیه‌سازی کامل تجربهٔ انسانی.

در طول اجرا این داده‌ها را از cPanel/میزبان و PostgreSQL ثبت کنید:
- CPU و RAM مصرفی، IO/IOPS و محدودیت‌های CloudLinux؛
- تعداد Passenger processها و restart/crashها؛
- تعداد connectionهای PostgreSQL، connection pool wait/timeout و صف درخواست‌ها؛
- HTTP error rate، p50، p95، p99 و تعداد شرکت‌کنندگان تکمیل‌شده؛
- تعداد رکوردها و صحت کامل شدن ۲۵ پاسخ پرسشنامه، ۳۰ trial آزمایشی DD و ۲۲ دور Ultimatum برای هر شرکت‌کننده.

## ۵. معیار اولیه

k6 در این فایل این thresholdها را اعمال می‌کند:
- خطای HTTP کمتر از ۰٫۱٪؛
- p95 زمان درخواست کمتر از ۵۰۰ms؛
- p99 کمتر از ۱٫۵s؛
- تکمیل تمام شرکت‌کنندگان مورد انتظار.

عبور از thresholdهای k6 به‌تنهایی برای تأیید ظرفیت کافی نیست. اگر درخواست‌ها موفق باشند ولی CPU/RAM، pool یا دیتابیس به سقف برسند، tail latency افزایش یابد یا رکوردی ناقص/تکراری باشد، آزمون پذیرفته نیست.

پس از هر اجرا summary کامل k6 و نمودارهای resource monitoring را ذخیره کنید. هیچ آزمون بار سنگینی را هم‌زمان با رویداد واقعی اجرا نکنید.
