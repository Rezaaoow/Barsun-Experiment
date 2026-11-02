# درخواست بررسی مسیر ورودی HTTPS — بارسان

این متن پیش‌نویس بررسی فنی است و برای پشتیبانی ارسال نشده است.

Staging با LiteSpeed reverse proxy به Uvicorn دو worker روی 127.0.0.1:8766، PostgreSQL مستقل و حداکثر ۸ thread هم‌زمان sync در هر worker اجرا می‌شود. CPU/RAM پلن تغییر نکرده است. کاربر تأیید کرده شرکت‌کنندگان نهایی اینترنت شخصی و IPهای جدا دارند؛ بنابراین تست تک‌IP با ۶۰۰ اتصال را نباید با ۶۰۰ IP واقعی یکسان دانست.

پنجرهٔ آزمون بیرونی به وقت تهران: 2026-11-03T01:13:19+03:30 تا 2026-11-03T01:18:13+03:30.

در این پنجره ۲۸۰۶۱ تلاش HTTP، ۲۰۳ status=0، p95 حدود ۷٫۰۷ ثانیه ثبت شد و آزمون با timeout پایش اصلی متوقف شد. در Uvicorn، ۲۷۹۵۵ درخواست رسیده همگی ۲۰۰ بودند؛ p95 مدت ASGI برابر ۴٫۵۶۱ms، حداکثر ۱۱٫۳۸۳ms و حداکثر in-flight هر worker سه بود. pool p95 برابر ۰٫۰۳۱ms بود. فاصلهٔ ارسال client تا ورود ASGI با لحاظ اختلاف ثابت ساعت، در tail چندین ثانیه افزایش داشت.

لطفاً با log و counter همان بازه مشخص کنید درخواست‌های دیررس یا گمشده در کدام لایه مانده‌اند:

- صف listener، TLS و proxy، سقف کل اتصال‌ها و Max SSL Connections.
- Per-Client Throttling، connection soft/hard limit، grace/banned period و DynReqPerSec برای IP مبدأ تست؛ این مقادیر با ظرفیت کل کاربران یکسان نیستند.
- علت block/drop در LiteSpeed، ModSecurity و firewall؛ وضعیت conntrack/SYN backlog و retransmission اگر counter مربوط وجود دارد.
- مقدار و fault واقعی NPROC، EP، PMEM و IO همین حساب. RLIMIT_NPROC شل با سقف واقعی CloudLinux برابر فرض نشود.

نمونهٔ traceهای بدون اطلاعات هویتی و summary تایمینگ در PR #27 و شواهد عملیات وجود دارد. دسترسی SSH این حساب به /proc/lve/list مجاز نبود و CLI محلی cPanel نیز به binary لازم دسترسی نداشت؛ تغییر در تنظیمات مدیریتی حدس زده نشده است.

براساس مستند رسمی LiteSpeed، DynReqPerSec و MaxConnPerClient در پیکربندی virtual host قرار می‌گیرند؛ افزودن تصادفی آن‌ها به .htaccess راهکار پشتیبانی‌شده فرض نشده است. خاموش‌کردن کلی WAF/anti-DDoS یا افزایش RAM/تغییر زبان، نتیجهٔ این شواهد نیست. اگر درخواست‌های واقعی از IPهای جدا از سقف هر IP عبور نمی‌کنند، به‌جای بالا بردن بی‌دلیل آن سقف، آزمون چندمبدأ و ظرفیت کل مسیر سنجیده شود.

منابع: https://docs.litespeedtech.com/lsws/configuration/ و https://docs.litespeedtech.com/lsws/cp/cpanel/antiddos/ و https://docs.cloudlinux.com/cloudlinuxos/limits/

نتیجهٔ تکمیلی: HTTPS عمومی از داخل هاست با ۶۰۰/۶۰۰ تکمیل، صفر خطا و p95 حدود ۲۸۸ms موفق شد. آزمون دو مبدأ نیز ۶۰۰/۶۰۰ و صفر خطا بود، با p95 بیرونی ۱۲۰۳ms. این شواهد ظرفیت کل مسیر را از سیاست/کیفیت مبدأ بیرونی تفکیک می‌کنند؛ محدودیت مشخصی بدون log میزبان مقصر اعلام نشده است.
