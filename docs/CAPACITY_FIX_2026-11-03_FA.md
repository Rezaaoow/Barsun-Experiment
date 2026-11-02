# اصلاح ظرفیت پس از آزمون native ASGI

هدف: رفع رقابت شروع اجرای یکسان و تعیین محل انتظار پیش از تغییر تنظیمات میزبان.

کد پایه: PR #25، e2b725cd0e1924d2dfcbb16f3cbb19d59496bd64. Staging: LiteSpeed → loopback Uvicorn دو worker؛ Production همچنان Passenger. گزارش قبلی در PR #26 ثبت شده است.

## تغییرات

- در لاگ بار قبلی ۱۶ خطای `RuntimeError: can't start new thread` پیدا شد. محدودیت واقعی NPROC حساب قابل خواندن نبود؛ خطای ساخت thread قطعی است، اما منشأ دقیق kernel/LVE هنوز اثبات نشده است. [CloudLinux](https://docs.cloudlinux.com/cloudlinuxos/limits/) threadها را در NPROC می‌شمارد و [Starlette](https://www.starlette.io/threadpool/) به‌طور پیش‌فرض تا ۴۰ thread در هر worker اجرا می‌کند. تنظیم اختیاری BARSAN_SYNC_THREADS=8 در launcher Staging تعداد درخواست‌های sync هم‌زمان هر worker را محدود می‌کند؛ event loop و صف درخواست‌ها فعال می‌مانند. پیش‌فرض برنامه بدون این env تغییر نمی‌کند.
- انتشار اول به‌دلیل بررسی health پیش از آماده‌شدن workerها برگشت خورد. در rollback، import هم‌زمان می‌توانست فایل dependency در حال بازگردانی را نبیند. روش بعدی سرویس را پیش از جابه‌جایی مجموعه فایل‌ها متوقف و برای readiness با retry محدود منتظر می‌ماند. کد قبلی و health Staging پس از rollback تأیید شدند.
- PostgreSQL: قفل تراکنشی شناسهٔ start_request_id و قفل ردیف assignment پیش از خواندن ledger/progress؛ SQLite: BEGIN IMMEDIATE پیش از خواندن.
- آزمون رقابتی درخواست یکسان، شناسهٔ تازه و حفظ run قبلی در SQLite و PostgreSQL مستقل CI.
- middleware خالص ASGI و ContextVar برای اندازه‌گیری درخواست و DB؛ پیش‌فرض خاموش، فعال‌سازی فقط Staging. لاگ خصوصی و چرخشی Supervisor، بدون هویت یا payload.
- بستهٔ runtime اکنون request_metrics.py را هم لازم دارد. launcher مقید به hash باید همراه فایل برنامه به hash نسخهٔ تست‌شده به‌روزرسانی شود؛ restart با Supervisor انجام می‌شود.

## محدودیت فعلی

health اصلی در شروع این نوبت بدون بار timeout داشت. بار سنگین تا تأیید سلامت پایه یا رفع علت متوقف است. هیچ reset یا حذف داده‌ای مجاز نیست و تست PostgreSQL smoke هرگز روی دیتابیس اصلی یا Staging اجرا نمی‌شود؛ فقط دیتابیس disposable CI.
