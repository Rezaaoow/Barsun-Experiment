# BarSan Experiment — راهنمای جامع انتقال دانش و عملیات

## ویرایش رابط شرکت‌کننده — ۴ نوامبر

مبنای کار PR #29 و منبع منتشرشدهٔ 83ae50134f1969d6fa3d3f594eb83ec263459b74 است. کار جاری روی متن و نمایش خوش‌آمدگویی/توضیحات و عنوان DD است؛ فهرست مراحل و fetch مخصوص نمایش آن مطابق درخواست پژوهشگر حذف می‌شوند. راهنمای معتبر این ویرایش docs/PARTICIPANT_COPY_20261104_FA.md است. مقصد انتشار فعلی Staging برای ادامهٔ بازبینی رابط است. وضعیت‌های قدیمی زیر تاریخی‌اند و باید از current-release.json دوباره بررسی شوند.

## وضعیت قطعی پس از انتشار — ۳ نوامبر ۲۰۲۶

هر دو محیط اکنون منبع ثابت `8e4838c54e3fcc82a1d1a25af337293c57b98551` را با native ASGI اجرا می‌کنند. PR #27 ادغام و CI منبع و main موفق شده‌اند. تطبیق هر 217 فایل با Git و دو محیط، HTML عمومی و سلامت سرویس‌ها تأیید شد؛ داده، توکن و کلید Production مستقل و محفوظ‌اند. 600 جریان نهایی Staging با صفر شکست/retry و p95=230.23ms کامل شد؛ 128 تست و Chrome/WebKit موفق. روی Production فقط بررسی انتشار انجام شد، بدون تست بار/اختلال/شرکت‌کنندهٔ مصنوعی.

مرجع تازه: [گزارش نهایی](FINAL_RELEASE_RESULTS_2026-11-03_FA.md) و [عملیات native](../ops/README_FINAL_NATIVE_FA.md). مسیر زنده از `runtime_source` در `current-release.json` هر محیط خوانده می‌شود؛ `/repo` آینه/ورودی fallback است. تنظیمات مؤثر در `config/app.env` خصوصی و launcher/Supervisor همان محیط‌اند؛ Application Manager/Passenger restart به‌تنهایی native را restart نمی‌کند. گزارش‌های قدیمیِ «Production هنوز Passenger» سابقهٔ پیش از این انتشارند.


## Current authorized release work — 2026-11-03

The user authorized final Staging fault/stability acceptance followed by Production promotion of the identical tested runtime. See `docs/FINAL_RELEASE_FA.md`. New runtime files include `client_timing.py` and `app/task_timing.js`; include them in pinned manifests. Native launchers will use immutable `deployments/final-<sha>/runtime` sources; `/repo` is mirrored after verification for legacy compatibility. Production remains unchanged until Staging acceptance is complete. Separate Production env/database/invitation key must be preserved. Never run load/fault tests on Production; only release verification. Two synthetic browser invitation fixtures are specifically authorized for the private Mac directory `/Users/rezaaoow/barsan-loadtest/final-release-20261103/`; no other fixture export is authorized by that approval. Actual release results supersede this in-progress note when recorded.


## وضعیت عملیاتی به‌روز — ۳ نوامبر ۲۰۲۶

Staging نسخهٔ ثابت `7af183881410faf5d089efefe44d71d43781d8ce` از PR #27 را با LiteSpeed proxy → Uvicorn دو worker → PostgreSQL مستقل اجرا می‌کند. در هر worker حداکثر هشت handler هم‌زمان sync و چهار اتصال pool داریم. کد، workflow، سؤال‌ها، randomization و UI پژوهش تغییر معنایی نکرده‌اند؛ PR #25 recovery/outbox در همین شاخه حضور دارد. فرایندهای قدیمی Passenger مربوط به Staging متوقف شده‌اند؛ restart این محیط با Supervisor است، نه restart.txt مربوط به Passenger.

دو آزمون کامل ۶۰۰جریانی با شمارش مستقل PostgreSQL موفق بودند: از داخل هاست از HTTPS عمومی، ۶۰۰ تکمیل و صفر خطا در ۵۸۰۵۴ درخواست، p95=۲۸۸ms؛ در اجرای هم‌زمان دو مبدأ (۲۰۰ Mac + ۴۰۰ داخل هاست)، ۶۰۰ تکمیل و صفر خطا در ۵۸۲۱۹ درخواست. p95 بخش بیرونی ۱۲۰۳ms بود و از هدف ۵۰۰ms عبور کرد؛ پذیرش کامل latency تأیید نشده است. آزمون تک‌IP بیرونی ۶۰۰ با guard متوقف شد. این‌ها آزمون API هستند، نه ۶۰۰ مرورگر یا IP واقعی. شرکت‌کنندگان نهایی اینترنت شخصی دارند.

[گزارش اصلاح، نتایج و حدود اعتبار آزمون](CAPACITY_FIX_2026-11-03_FA.md) و [عملیات Staging و ابزارها](../ops/README_CAPACITY_FA.md) مرجع فعلی‌اند. اطلاعات Passenger در ادامه برای Production و سابقه است. کد/تنظیمات Production منتقل نشده‌اند؛ فقط restart معمول بازیابی انجام شده است. هیچ PRی merge نشده است.

> سند مرجع برای عامل هوش مصنوعی/توسعه‌دهنده‌ای که بدون تاریخچهٔ مکالمه وارد پروژه می‌شود. این snapshot بر اساس شواهد بررسی‌شده تا ۳۱ اکتبر ۲۰۲۶ است؛ قبل از هر عملیات، وضعیت فعلی GitHub، CI و هاست را دوباره بررسی کنید.

## 1. ترتیب شروع کار

1. `AGENTS.md` را بخوانید.
2. `README_FA.md` را بخوانید.
3. همین سند را بخوانید؛ به‌خصوص مسیرهای واقعی Passenger و سابقهٔ خطای deploy.
4. برای جزئیات رفتار پژوهش و schema به `EXPERIMENT_ARCHITECTURE.md` و `DATABASE_ARCHITECTURE.md` رجوع کنید.
5. برای تغییر URL عمومی فقط، `CPANEL_RENAME.md` را بررسی کنید.
6. قبل از تغییر، branch، `git status`، آخرین SHA شاخهٔ `main`، فایل‌های واقعی و CI همان SHA را بررسی کنید.

قاعدهٔ توسعهٔ اجباری پروژه: **اول README و مستند مرتبط را به‌روز کنید، سپس کد را تغییر دهید؛ مستندات، کد و تست مرتبط باید در یک commit/PR منسجم باشند.** بعد از تغییر، تست‌ها و CI همان SHA را بررسی کنید. GitHub merge مساوی deploy نیست و deploy مساوی live verification نیست.

## 2. ماهیت محصول

BarSan Experiment سامانهٔ پژوهشی آنلاین با Python/FastAPI، رابط Persian RTL و PostgreSQL برای production است. سه ماژول مستقل در یک workflow مشترک دارد:

1. **Questionnaire:** پرسشنامهٔ ۲۵ سؤالی؛ یک سؤال در هر صفحه؛ پاسخ‌ها با کد داخلی سؤال ذخیره می‌شوند و draft قابل بازیابی است.
2. **DD / Life-Trade:** تسک تأخیر در پاداش در برابر معاملهٔ زمان زندگی؛ داده‌های قدیمی این ماژول باید سازگار باقی بمانند.
3. **Ultimatum Game:** دو دور تمرین و چهار بلوک اصلی پنج‌دوری (۲۰ دور اصلی).

ترتیب اصلی مستندشده: `questionnaire → dd_lifetrade → ultimatum`. Backend مرجع نهایی identity، invitation، condition، consent، workflow sequence، active attempt، scoring و persistence است؛ به مقادیر حساس/شرطی ارسالی از مرورگر اعتماد نکنید.

پنل admin در `/admin` و `/admin/links` است و عملیات حساس آن با header `X-Admin-Token` محافظت می‌شود.

## 3. نقشهٔ فایل‌های مهم

| مسیر مخزن | کاربرد |
|---|---|
| `barsan_experiment.py` | Backend canonical، FastAPI، DB، APIها، workflow و homepage loader |
| `main.py` | ورودی سازگاری؛ `app` را از `barsan_experiment` می‌گیرد؛ cPanel فعلی همین import را انجام می‌دهد |
| `app.py` | ورودی سازگاری دوم برای `app:app` |
| `app/index.html` | رابط شرکت‌کننده و JavaScript ماژول‌ها |
| `app/admin-links.html` | پنل مدیریت لینک |
| `app/conditions.json` | پیکربندی condition |
| `app/Stimuli/` | محرک‌های تصویری DD/Life-Trade؛ runtime assets |
| `tests/test_api.py` | API regression |
| `tests/test_workflow.py` | workflow و assignment |
| `tests/test_ultimatum.py` | Ultimatum |
| `tests/test_admin_links.py` | admin links و تفکیک scope |
| `tests/test_participant_ui.py` | copy و UI regression |
| `tests/postgres_smoke.py` | PostgreSQL integration/concurrency |
| `.github/workflows/tests.yml` | CI |
| `requirements.txt`, `requirements-dev.txt` | runtime/dev dependencies |
| `EXPERIMENT_ARCHITECTURE.md` | جریان آزمایش |
| `DATABASE_ARCHITECTURE.md` | schema و روابط |
| `CPANEL_RENAME.md` | راهنمای تغییر URL |
| `README_FA.md` | راهنمای کلی |
| `AGENTS.md` | دستورالعمل AI agents |
| `docs/AI_HANDOFF_FA.md` | این راهنمای عملیاتی |

ممکن است در مخزن `lifetrade.sqlite3`، `__pycache__` و فایل‌های محلی/تولیدی وجود داشته باشد. وجود آن‌ها مجوز انتشار به production نیست. SQLite را به PostgreSQL کپی نکنید.

## 4. رفتار پژوهش و ملاحظات داده

### Assignment و invitation
- Frame/Control در پنل مدیریت به کدهای A/B نگاشت می‌شوند: `Frame → A` و `Control → B` (بر اساس README و کد موجود، در هر تغییر مجدداً بررسی شود).
- برای هر assignment لینک token opaque ساخته می‌شود. condition از query parameter مرورگر پذیرفته نمی‌شود؛ از دیتابیس resolve می‌شود.
- token خام برای اعتبارسنجی به‌صورت SHA-256 hash نگهداری می‌شود و نسخهٔ URL قابل‌بازیابی با Fernet رمزنگاری می‌شود.
- شرکت‌کننده باید invitation token و participant code متناظر را وارد کند.
- رضایت با consent version و timestamp سمت سرور ثبت می‌شود و دسترسی به مراحل نیازمند رضایت معتبر است.

### Attempts و exports
- پاسخ پرسشنامه با question code داخلی در `questionnaire_responses` ذخیره می‌شود؛ draft برای ادامه قابل بازیابی است.
- تلاش ناقص DD/Life-Trade یا Ultimatum با حذف داده حل نمی‌شود: attempt قبلی `superseded` می‌شود و attempt جدید ساخته می‌شود. دادهٔ قبلی برای audit حفظ می‌شود.
- stage completion در سرور نگهداری می‌شود؛ stage تکمیل‌شده نباید دوباره اجرا شود.
- دادهٔ Ultimatum در `ultimatum_trials` است و نباید با `lifetrade_trials` مخلوط شود.
- test studyها با prefix `barsan-test-` از main جدا هستند. synthetic prerequisite rows با `test_prerequisite=true` مشاهدهٔ رفتاری واقعی نیستند. خروجی‌ها را با `scope=main|test` جدا نگه دارید.

### Ultimatum: فرض‌های موقت
مستندات فعلی چهار بلوک proposer/agent، responder/agent، proposer/coworker و responder/coworker را شرح می‌دهند؛ هر بلوک پنج دور اصلی دارد و دو دور تمرین جدا هستند. پیشنهاد ربات در نقش پیشنهاددهنده از نرمال گسسته با میانگین ۵ و پارامتر انحراف معیار ۴، محدود به اعداد صحیح ۰ تا ۱۰ نمونه‌گیری می‌شود؛ انحراف معیار واقعی توزیع نهایی پس از گسسته‌سازی و محدودسازی حدود ۲٫۷۹ است. الگوریتم پاسخ ربات در نقش پاسخ‌دهنده همچنان همان مدل دوجمله‌ای موقت (`n=10`، احتمال موفقیت برابر سهم پیشنهادی به ربات تقسیم بر ۱۰، و پذیرش در صورت حداقل ۵ موفقیت) است و با این تغییر عوض نمی‌شود. این پارامترها فرض‌های عملیاتی موقت‌اند و باید با پژوهشگر نهایی شوند. در شرط coworker رابط فرد حاضر را نشان می‌دهد اما تصمیم در کد فعلی توسط الگوریتم سمت سرور تولید می‌شود. هیچ عامل یا توسعه‌دهنده‌ای نباید این دستکاری را اخلاقاً تأییدشده معرفی کند یا بدون پژوهشگر تغییر دهد. تأیید پروتکل، الگوریتم و debriefing با مسئول پژوهش/کمیتهٔ اخلاق است.

## 5. معماری واقعی cPanel / Passenger — حیاتی

### مسیرهای تأییدشده

| مورد | مقدار مشاهده‌شده |
|---|---|
| Public URL | `https://my-experiment.ir/lifetrade/` |
| API health | `https://my-experiment.ir/lifetrade/api/health` |
| Passenger Application Root | `/home/myexperi/lifetrade` |
| Git clone | `/home/myexperi/lifetrade/repo` |
| Passenger bootstrap | `/home/myexperi/lifetrade/passenger_wsgi.py` |
| active `.htaccess` | `/home/myexperi/public_html/lifetrade/.htaccess` |
| Python virtualenv | `/home/myexperi/virtualenv/lifetrade/3.12/bin/python` |
| Restart trigger | `/home/myexperi/lifetrade/tmp/restart.txt` |
| adapter log | `/home/myexperi/lifetrade/adapter-debug.log` |
| deploy SSH key path | `/home/myexperi/.ssh/lifetrade_deploy` — مسیر را بدانید، محتوا را هرگز نخوانید/چاپ نکنید |
| PostgreSQL DB name | `myexperi_lifetrade_study` — نام DB است، نه connection string کامل |

### چرا Application Root مسیر کد فعال نیست؟

فایل واقعی `passenger_wsgi.py` روی هاست مشاهده شده و حاوی این منطق است:

```python
APP_ROOT = "/home/myexperi/lifetrade/repo"
sys.path.insert(0, APP_ROOT)

from a2wsgi import ASGIMiddleware
from main import app as fastapi_app
```

در نتیجه Passenger bootstrap را از Application Root پیدا می‌کند، اما bootstrap ماژول `main` را از کلون `repo` import می‌کند. `main.py` نیز app را از `barsan_experiment.py` می‌گیرد. در Backend مقدار `ROOT` از مسیر خود `barsan_experiment.py` محاسبه می‌شود؛ بنابراین HTML و Stimuli هم از `repo/app/` خوانده می‌شوند.

ساختار واقعی:

```text
/home/myexperi/lifetrade/                 # Passenger Application Root
├── passenger_wsgi.py                     # bootstrap خارج Git؛ بدون دلیل تغییر نده
├── tmp/restart.txt                       # restart trigger
├── adapter-debug.log                     # PID/request/status/chunk logs
└── repo/                                 # Python import root فعال
    ├── main.py
    ├── app.py
    ├── barsan_experiment.py
    ├── requirements.txt
    └── app/
        ├── index.html                    # HTML فعال شرکت‌کننده
        ├── admin-links.html
        ├── conditions.json
        └── Stimuli/
```

`.htaccess` در `/home/myexperi/public_html/lifetrade/` در snapshot چنین تنظیمی داشت:

```apache
PassengerAppRoot "/home/myexperi/lifetrade"
PassengerBaseURI "/lifetrade"
PassengerPython "/home/myexperi/virtualenv/lifetrade/3.12/bin/python"
```

دایرکتوری `/home/myexperi/public_html/lifetrade/` در بررسی انجام‌شده فقط `.htaccess` داشت و فایل index استاتیک دیده نشد؛ صفحه از Passenger ارائه می‌شود.

### مسیر انتشار درست و غلط

| کار | درست | اشتباه |
|---|---|---|
| Backend | `/home/myexperi/lifetrade/repo/barsan_experiment.py` | `/home/myexperi/lifetrade/barsan_experiment.py` |
| entry point | `/home/myexperi/lifetrade/repo/main.py` | `/home/myexperi/lifetrade/main.py` |
| participant HTML | `/home/myexperi/lifetrade/repo/app/index.html` | `/home/myexperi/lifetrade/app/index.html` |
| app assets | `/home/myexperi/lifetrade/repo/app/` | `/home/myexperi/lifetrade/app/` |

**هر deploy به parent Application Root در این میزبانی می‌تواند فایل‌های غیر فعال را عوض کند و سایت را به‌روز نکند.** این تفاوت مهم‌ترین علت خطای deploy قبلی بوده است.

### Homepage cache و restart

در `barsan_experiment.py` تابع `load_homepage()` با `@lru_cache(maxsize=1)` تعریف شده و `ROOT/app/index.html` را می‌خواند. تغییر فایل بدون restart ممکن است در پاسخ Passenger دیده نشود. بعد از انتشار:

```bash
touch /home/myexperi/lifetrade/tmp/restart.txt
```

### Adapter log

`passenger_wsgi.py` از `a2wsgi.ASGIMiddleware` استفاده می‌کند و در این فایل log می‌نویسد:

```bash
tail -n 100 /home/myexperi/lifetrade/adapter-debug.log
```

اطلاعات مشاهده‌شده: PID، event-loop state، method، `SCRIPT_NAME`، `PATH_INFO`، response status و اندازهٔ chunk. اگر صفحه زنده قدیمی است، log و PID را با درخواست تازه تطبیق دهید؛ صرفاً cache را مقصر فرض نکنید.

## 6. محیط و secrets

| متغیر | معنا |
|---|---|
| `DATABASE_URL` | connection string کامل PostgreSQL؛ secret |
| `ADMIN_TOKEN` | توکن API مدیریت؛ secret |
| `STUDY_CODE` | شناسه مطالعه؛ default `main-study` |
| `PUBLIC_EXPERIMENT_URL` | base URL ساخت لینک دعوت؛ در snapshot مقدارش خالی نبود، اما دوباره بررسی شود |
| `DB_POOL_MIN_SIZE`, `DB_POOL_MAX_SIZE` | تنظیمات اندازه pool |
| `DB_POOL_TIMEOUT`, `DB_POOL_MAX_WAITING` | timeout/صف pool |
| `DB_CONNECT_TIMEOUT` | timeout اتصال DB |
| `SQLITE_BUSY_TIMEOUT_MS` | timeout SQLite |

Python مجازی cPanel مسیر بالا و Python 3.12 است؛ CI نیز 3.12 دارد. `requirements.txt` شامل FastAPI، Uvicorn، psycopg، psycopg-pool، a2wsgi و cryptography است؛ نسخهٔ دقیق را همیشه از فایل فعلی بخوان. `requirements-dev.txt` شامل pytest و httpx است.

`Procfile` و `render.yaml` برای اجرای Uvicorn/Render هستند؛ این‌ها نحوهٔ اجرای Passenger cPanel را تعیین نمی‌کنند. فقط اگر requirements تغییر کرده، dependency را در virtualenv واقعی نصب کن.

ممنوع: درخواست/چاپ/ثبت مقدار secretها، invitation token خام، SSH private key، Fernet key. فایل `.barsan_invitation_tokens.key` باید بیرون Git و web root باقی بماند؛ از دست رفتنش می‌تواند URLهای رمزنگاری‌شدهٔ قدیمی را غیرقابل بازیابی کند.

## 7. ایمنی دیتابیس

- PostgreSQL برای production است؛ SQLite برای local/test.
- DB name شناخته‌شده `myexperi_lifetrade_study` به‌تنهایی `DATABASE_URL` نیست.
- `init_db()` هنگام import/startup اجرا می‌شود؛ schema initialization فعلی از `CREATE TABLE IF NOT EXISTS` و `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` استفاده می‌کند. هر تغییر باید دقیقاً بررسی شود.
- Rollback فایل‌ها migration/DDL را rollback نمی‌کند.
- `POST /api/admin/maintenance/reset` destructive است؛ فقط پس از backup مستقل، preview scoped و تأیید صریح.
- در deploy عادی هیچ `DROP`، `DELETE`، reset، re-seed یا انتقال SQLite اجرا نکن.
- جدول‌های اصلی شامل `studies`، `participants`، `study_participations`، `experiment_assignments`، `experiment_consents`، `experiment_stage_progress`، `study_workflows`، `experiment_workflow_progress`، `experiment_time_tracking`، `instrument_runs`، `lifetrade_trials`، `questionnaire_responses` و `ultimatum_trials` هستند. جزئیات دقیق در `DATABASE_ARCHITECTURE.md`.

## ممیزی ظرفیت و عملکرد رویداد ۶۰۰نفره

بهینه‌سازی‌های Backend در PR #17 و کاهش درخواست‌های تکراری پرسشنامه در PR #24 روی `main` ادغام شده‌اند. آخرین commit ادغام‌شده `0598aca3b6f73cf089f4522fc4416fbb079bf02a` است؛ CI روی این commit با ۷۲ تست SQLite، JavaScript/k6 validation، PostgreSQL 16 smoke test و تست هم‌زمانی ۲۴ شرکت‌کننده موفق شد ([run 41459908685](https://github.com/Rezaaoow/barsan-experiment/actions/runs/41459908685)). آزمون ramp-up staging با ۶۰۰ VU در ۲ نوامبر ۲۰۲۶ اجرا شد و با اشباع هشت پردازش LSAPI ناموفق بود؛ بنابراین ظرفیت هاست تأیید نشده است. ابزار read-only تشخیص منابع در `tests/load/host_diagnostics.py` باقی است؛ پیش از تغییر تنظیمات میزبان یا pool، سقف‌های Python LSAPI/Passenger و CloudLinux و ظرفیت PostgreSQL باید از میزبان دریافت شوند.

قبل از اعلام آمادگی production، گزارش کارایی و آزمون بار را بخوانید: docs/PERFORMANCE_AND_LOAD_TESTING_FA.md. یافته‌های قطعی کد را با محدودیت‌های ناشناختهٔ هاست قاطی نکنید: تعداد processهای Passenger، memory/CPU limit، سقف connection دیتابیس و reverse-proxy concurrency باید از cPanel/provider گرفته و با آزمایش staging سنجیده شوند. تعداد ۶۰۰ کاربر مرورگر با ۶۰۰ request هم‌زمان در هر لحظه یکسان نیست، اما burst اولیه و مرحله‌های ذخیرهٔ پرسشنامه باید عمداً در سناریوی load test پوشش داده شوند.

تغییرات performance نباید protocol، تعداد trialها، ترتیب ماژول‌ها، randomization، scoring، RT semantics یا ظاهر participant UI را تغییر دهند. خروجی موفق CI نیز به‌تنهایی اثبات ظرفیت واقعی ۶۰۰ شرکت‌کننده نیست.


## نتیجهٔ آزمون واقعی ۶۰۰ شرکت‌کننده — ۲ نوامبر ۲۰۲۶

آزمون staging با ۶۰۰ دعوت‌نامه و ramp-up سه‌مرحله‌ای در ۹۰ ثانیه ناموفق بود. لاگ Passenger عبارت `Reached max children process limit: 6, extra: 2, current: 8, busy: 8` را ۱۹۸ بار ثبت کرد؛ در اوج اجرا هر ۸ پردازش LSAPI مشغول بود. حداقل ۵۲ پاسخ 503، تعداد ۱۰۴ پیام شکست درخواست و ۵۱ پیام timeout گزارش شد؛ شمارنده‌ها هم‌پوشانی دارند و نرخ خطای نهایی قابل محاسبه نیست. هیچ‌یک از ۶۰۰ شرکت‌کننده تمام سه ماژول را کامل نکردند. پس از توقف بار، health staging و production در سه بررسی متوالی HTTP 200 بود. هیچ باری روی production اجرا نشد و دعوت‌نامه‌های آزمایشی غیرفعال شدند. این آزمون ظرفیت ۶۰۰ نفر را تأیید نمی‌کند.

گلوگاه مشهود، سقف پردازش‌های وب Passenger/LSAPI است؛ افزایش pool دیتابیس به‌تنهایی آن را حل نمی‌کند. پیش از هر آزمون بعدی، میزبان باید سقف LSAPI children و سهمیه‌های CloudLinux شامل Entry Processes، NPROC، CPU و PMEM را اعلام و دربارهٔ افزایش ظرفیت متناسب با ۲ vCPU و ۴GB RAM تصمیم‌گیری کند. تعداد processها و سقف اتصال PostgreSQL باید قبل از هر تغییر در pool مشخص شود. در صورت غیرقابل‌افزایش بودن این سقف، انتقال به VPS یا محیط application hosting با ظرفیت کنترل‌شده باید ارزیابی شود.

## بهینه‌سازی autosave پرسشنامه

Endpoint ذخیرهٔ هر پاسخ، هم پاسخ نرمال‌شده و هم draft فعلی را ذخیره می‌کند. اکنون این endpoint پارامتر اختیاری `step_id` را می‌پذیرد و draft را در رکورد progress همان workflow نیز همگام می‌کند؛ بنابراین رابط شرکت‌کننده پس از هر پاسخ یا حرکت رو به جلو نیازی به ارسال درخواست draft دوم ندارد. ذخیرهٔ draft جداگانه برای حرکت به سؤال قبلی باقی مانده است. هنگام ورود مجدد ممکن است آخرین سؤال پاسخ‌داده‌شده دوباره نمایش داده شود، اما پاسخ‌های ذخیره‌شده حفظ می‌شوند. در جریان عادی ۲۵سؤالی، این تغییر تا ۲۵ درخواست تکراری برای هر شرکت‌کننده حذف می‌کند، بدون تغییر سؤال‌ها، اعتبارسنجی، ترتیب ماژول‌ها یا شرط تکمیل.

## 8. سابقهٔ خطای deploy — حتماً قبل از deploy بخوان

در تلاش قبلی، اسکریپت runtime files را در `/home/myexperi/lifetrade/` کپی کرد. این اشتباه بود، چون `passenger_wsgi.py` از `/home/myexperi/lifetrade/repo/` import می‌کند.

نتایج مشاهده‌شده:
- دریافت commit و staging موفق شد.
- فایل‌ها به مسیر parent Application Root رفتند، نه مسیر فعال clone.
- API health HTTP 200 و `database_connected=true` بود؛ این به معنی درست بودن HTML فعال نبود.
- صفحهٔ زنده همچنان welcome copy قدیمی را برمی‌گرداند و markerهای `ut-final-score-box` و متن جدید پایان تمرین را نداشت.
- live HTML verification شکست خورد و script فایل‌های parent را از backup restore کرد.
- بررسی `passenger_wsgi.py` مسیر active runtime را قطعی کرد: `/home/myexperi/lifetrade/repo/`.

بنابراین deploy بعدی باید در `repo/` باشد. snapshot آخر ۳۱ اکتبر ۲۰۲۶ هنوز live page قدیمی را نشان می‌داد؛ این سند **ادعا نمی‌کند** که انتشار درست بعدی انجام شده است. وضعیت live را با درخواست تازه verify کنید.

## 9. Runbook انتشار امن

### قبل از انتشار
- SHA فعلی `main` را بخوان و commit/PR مقصد را بررسی کن.
- CI موفق همان SHA را تأیید کن.
- مسیرهای cPanel و `passenger_wsgi.py` را دوباره بررسی کن؛ اگر تغییر کرده‌اند، این سند را نیز به‌روزرسانی کن.
- از runtime فعال در `/home/myexperi/lifetrade/repo/` backup بگیر.
- اگر schema تغییر می‌کند، backup مستقل PostgreSQL بگیر.
- بررسی کن `requirements.txt` تغییر کرده یا خیر.

### بستهٔ runtime
برای کد فعلی عموماً این‌ها از commit دقیق منتشر می‌شوند:
- `app.py`
- `main.py`
- `barsan_experiment.py`
- `requirements.txt` (نصب dependency فقط در صورت نیاز)
- کل `app/` شامل `index.html`، `admin-links.html`، `conditions.json` و `Stimuli/`

از commit SHA دقیق archive بساز؛ کل working tree را کورکورانه کپی نکن. `tests/` و docs در runtime لازم نیستند. `lifetrade.sqlite3`، `.git/`، `__pycache__/`، secrets، `passenger_wsgi.py`، `.htaccess` و virtualenv را در release package وارد نکن. از `rsync --delete` استفاده نکن مگر فایل‌های local بررسی شده باشند.

### توالی انتشار
1. stage directory بساز و archive دقیق SHA را استخراج کن.
2. فایل‌های ضروری و markerهای نسخه را پیش از انتشار بررسی کن.
3. از فایل‌های فعال `repo/` backup قابل خواندن بگیر.
4. runtime files را در **`/home/myexperi/lifetrade/repo/`** جایگزین کن.
5. app assets را همگام کن؛ فایل local را بدون بررسی حذف نکن.
6. `touch /home/myexperi/lifetrade/tmp/restart.txt`.
7. `/lifetrade/api/health` را بررسی کن؛ JSON باید `ok: true` و `database_connected: true` داشته باشد.
8. HTML زنده را با query یکتا و `Cache-Control: no-cache` بگیر و markerهای مشخص commit همان release را بررسی کن.
9. اگر هر check شکست خورد، backup runtime را restore و Passenger را restart کن؛ ابتدا علت را پیدا کن، سپس retry.

### Health / Live checks

```bash
curl -i --max-time 20 https://my-experiment.ir/lifetrade/api/health
```

```bash
curl -sS -L --compressed --max-time 30 \
  -H 'Cache-Control: no-cache, no-store' \
  -o /tmp/barsan-live.html \
  -w 'HTTP %{http_code} | %{size_download} bytes\n' \
  "https://my-experiment.ir/lifetrade/?deploy_check=UNIQUE_VALUE"

grep -F 'ut-final-score-box' /tmp/barsan-live.html
grep -F 'دور‌های تمرینی به پایان رسید' /tmp/barsan-live.html
```

Markerها را با محتوای همان commit تطبیق بده؛ welcome title به‌تنهایی کافی نیست چون نسخهٔ قدیمی نیز نام بارسان را نشان می‌داد. HTTP 200 به‌تنهایی deploy موفق را ثابت نمی‌کند.

### اگر live page قدیمی ماند
1. `grep` و `sha256sum` روی `/home/myexperi/lifetrade/repo/app/index.html`.
2. بررسی import pathهای `repo/main.py` و `repo/barsan_experiment.py`.
3. restart با `tmp/restart.txt`.
4. بررسی `tail -n 100 /home/myexperi/lifetrade/adapter-debug.log`.
5. تطبیق PID، `SCRIPT_NAME`، `PATH_INFO` و response status.
6. خواندن مجدد `.htaccess`؛ آن را بی‌دلیل ویرایش نکن.
7. تا روشن‌شدن علت، cache را تنها فرضیه ندان و فایل را چندبار کپی نکن.

در cPanel Terminal تمام اسکریپت‌هایی را که `set -e` و `exit` دارند داخل subshell یعنی `( ... )` اجرا کن تا failure ترمینال تعاملی را نبندد. trap باید stage/line/exit code را اعلام کند و پس از شروع انتشار backup را restore کند.

## 10. CI و تست

Workflow: `.github/workflows/tests.yml`

1. Python 3.12.
2. نصب `requirements.txt` و `requirements-dev.txt`.
3. استخراج inline JS از `app/index.html` و `app/admin-links.html` و `node --check`.
4. `pytest -q` (SQLite/API/regression).
5. `tests/postgres_smoke.py` روی PostgreSQL 16.

در CI run مربوط به merge commit `82d35ced5e63873178511a05334dcecb6f3a8d9c` در ۳۱ اکتبر ۲۰۲۶، conclusion موفق بود و snapshot تست SQLite شامل ۴۶ تست موفق بود. این اطلاعات تاریخی‌اند؛ CI commit جاری را دوباره بررسی کن.

Local:

```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
pytest -q
python tests/postgres_smoke.py
```

PostgreSQL smoke test فقط با دیتابیس disposable تست اجرا شود، نه connection string production.

## 11. API و پنل admin

Admin header:

```text
X-Admin-Token: <ADMIN_TOKEN>
```

Endpointهای مستندشده:
- `GET /api/health`
- `POST /api/admin/assignments`
- `GET /api/admin/assignments`
- `POST /api/admin/assignments/status`
- `POST /api/admin/assignments/delete-inactive` (destructive)
- `POST /api/admin/assignments/reissue-link`
- `POST /api/admin/test-links`
- `POST /api/admin/module-test-links`
- `GET /api/admin/progress?scope=main|test`
- `GET /api/admin/questionnaire/export.csv?scope=main|test`
- `GET /api/admin/export.csv?scope=main|test`
- `GET /api/admin/ultimatum/export.csv?scope=main|test`
- `GET /api/admin/export-all.zip?scope=main|test`
- `GET /api/admin/maintenance/preview?scope=main|test`
- `POST /api/admin/maintenance/reset` (destructive)

پنل `/admin` و `/admin/links` است. خروجی main و test را مخلوط نکن. synthetic prerequisiteهای test، behavioral data واقعی نیستند.

## 12. رابط شرکت‌کننده و copy مصوب

صفحهٔ خوش‌آمدگویی شامل عنوان «به برنامهٔ پژوهشی بارسان خوش آمدید»، فهرست شماره‌دار مراحل واقعی شرکت‌کننده و دکمهٔ «ادامه» است. هیچ پاراگراف توضیحی اضافه یا شرح زیرعنوان برای مراحل نشان داده نمی‌شود. فهرست باید بر اساس ترتیب واقعی workflow همان invitation ساخته شود؛ ترتیب را از `GET /api/experiment/workflow?invite=...` بخوانید، نه از یک آرایهٔ ثابت در Frontend. اگر `legacy_default=true` باشد، endpoint دو گام پرسشنامه و DD/Life-Trade را گزارش می‌کند، اما جریان قدیمی پس از آن Ultimatum را نیز خودکار آغاز می‌کند؛ بنابراین عنوان بازی باشگاه رانندگان بارسان را به انتهای فهرست اضافه کنید، مگر اینکه از قبل حضور داشته باشد. لینک تست مستقل فقط همان ماژول را نشان می‌دهد.

عنوان‌های مجاز برای participant UI:
- `questionnaire` → «پرسشنامه»
- `dd_lifetrade` → «بازی انتخاب»
- `ultimatum`، `ultimatum_frame` و `ultimatum_control` → «بازی باشگاه رانندگان بارسان»

برای variantهای Ultimatum هیچ نام Frame/Control یا description دریافتی از API را در صفحه نشان ندهید. در DD/Life-Trade نیز عنوان‌های داخلی انگلیسی مانند «5-Trial DD Task» و «Life-Trade Reward» نباید در title صفحهٔ دستورالعمل شرکت‌کننده ظاهر شوند؛ عنوان هر دو «بازی انتخاب» است.

### وسط‌چین عمودی صفحه‌های آزمایش

عنوان و متن اصلی صفحه‌های راهنما باید به‌عنوان یک گروه در مرکز عمودی کارت قرار بگیرند؛ نوار پیشرفت می‌تواند بالا و دکمه‌ها پایین بمانند. در پرسشنامه، عنوان دستور و عبارت سؤال با گزینه‌ها در فضای میانی کارت قرار می‌گیرند. در Ultimatum، topbar/امتیاز بالا باقی می‌ماند و عنوان به‌همراه محتوای اصلی در ناحیهٔ میانی کارت قرار می‌گیرد. در موبایل نیز محتوای کوتاه در مرکز viewport قرار گیرد و محتوای بلند در همان ناحیه قابل scroll باشد؛ centering نباید باعث بریده‌شدن بالای صفحه شود. این رفتار را در DD/Life-Trade، پرسشنامه و Ultimatum تست کنید.

Copy مهم:
- معرفی: «شما در این بازی برای کسب امتیاز باشگاه رانندگان با یک بازیکن دیگر بازی می‌کنید.»
- نقش‌ها: «پیشنهاد دهنده» و «پاسخ دهنده».
- آموزش تمرین: «قبل از شروع بازی اصلی، دو دور تمرینی اجرا می‌شود اما امتیازها و داده‌های این دو دور در امتیاز نهایی و برنامه پژوهشی لحاظ نمی‌شوند.»
- پایان تمرین: «دور‌های تمرینی به پایان رسید. الان بازی اصلی شروع می‌شود.»
- خلاصه: «در ۵ دور قبلی شما:» به‌همراه دو شاخص امتیاز به‌دست‌آمده و امتیاز ازدست‌رفته.
- دکمهٔ گذار: «دور بعدی».
- صفحهٔ پایانی: یک عنوان «امتیاز نهایی» و باکس امتیاز بسیار بزرگ.
- پیام پایان: «بارسانی عزیز، بازی و برنامه ‌پژوهشی به پایان رسید. امتیاز شما از طریق کد یکتای شما به حساب کاربری‌تان در باشگاه رانندگان بارسان منتقل خواهد شد.»

UI فارسی/RTL، متن‌های تعاملی وسط‌چین و عددهای امتیاز خوانا و بزرگ باشند. حذف یک پاراگراف خاص consent به معنی حذف توضیحات اخلاق پژوهش و اختیار مشارکت نیست. برای copy تغییر یافته، `tests/test_participant_ui.py` را نیز به‌روز کنید.

## 13. فرض نکنید

- GitHub merge همان cPanel deploy نیست.
- HTTP 200 به معنی DB health یا version-correct HTML نیست.
- Passenger Application Root همان Python import root نیست.
- `/home/myexperi/lifetrade/app/` مسیر HTML فعال نیست.
- نام دیتابیس همان `DATABASE_URL` نیست.
- `Procfile` / `render.yaml` پیکربندی Passenger نیستند.
- rollback فایل‌ها DDL دیتابیس را rollback نمی‌کند.
- healthcheck موفق به معنی تأیید پروتکل اخلاقی، UX کامل یا آماده‌بودن برای جمع‌آوری نهایی نیست.
- دسترسی GitHub به معنی SSH مستقیم به هاست نیست؛ عامل AI باید از کاربر خروجی command بگیرد و هرگز secret درخواست نکند.

## 14. Snapshot تاریخی و موارد باز

تا ۳۱ اکتبر ۲۰۲۶:
- `main` روی merge commit مربوط به PR شمارهٔ ۱۳ با SHA `82d35ced5e63873178511a05334dcecb6f3a8d9c` بود؛ پیش از اقدام جدید SHA/CI را دوباره بخوانید.
- PR شمارهٔ ۱۳ شامل README، UI و UI tests بود؛ کد Backend را تغییر نداد.
- CI مربوط به merge موفق شد.
- تلاش انتشار قبلی فایل‌ها را در parent Application Root گذاشت؛ live verification شکست خورد و backup restore شد.
- آخرین HTML زندهٔ مشاهده‌شده هنوز copy قدیمی را داشت. مسیر صحیح runtime کشف شده است؛ **انتشار موفق به `repo/` و تأیید زندهٔ پس از آن هنوز نباید مفروض گرفته شود.**
- API health و اتصال DB یک بار موفق بررسی شده بود؛ این را جایگزین live HTML verification نکنید.

هر عامل بعدی باید پس از تغییر main، SHA/CI و وضعیت deploy را با شواهد جدید به‌روز کند.
