"""One admin bulk response per runtime; participants never enter this queue."""
import asyncio
import fcntl
import os
import secrets
import logging
import time
from anyio.to_thread import run_sync
from starlette.responses import JSONResponse


def take_slot(path):
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT,0o600)
    handle=os.fdopen(descriptor,'a+b')
    try:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return handle
    except BlockingIOError:
        handle.close()
        return None
    except BaseException:
        handle.close()
        raise


class BulkExportMiddleware:
    def __init__(self,app,lock_path,token_getter,busy_checker):
        self.app=app;self.lock_path=lock_path;self.token_getter=token_getter;self.busy_checker=busy_checker

    async def __call__(self,scope,receive,send):
        path=scope.get('path','')
        bulk=scope['type']=='http' and scope.get('method')=='GET' and '/api/admin/' in path and path.endswith(('.csv','.zip'))
        if not bulk:
            return await self.app(scope,receive,send)
        configured=self.token_getter()
        supplied=dict(scope.get('headers',[])).get(b'x-admin-token',b'')
        if not configured or not secrets.compare_digest(supplied,configured.encode("utf-8")):
            return await self.app(scope,receive,send)
        try:
            recent_activity=await run_sync(self.busy_checker)
        except Exception as exc:
            logging.getLogger(__name__).warning("Bulk admission deferred: %s", type(exc).__name__)
            recent_activity=True
        lease=None if recent_activity else take_slot(self.lock_path)
        if lease is None:
            response=JSONResponse({'detail':'دریافت خروجی در انتظار نوبت است؛ اجرای شرکت‌کنندگان در اولویت قرار دارد.'},status_code=429,
                headers={'Retry-After':'2','X-Barsan-Export-Busy':'1','Cache-Control':'no-store'})
            return await response(scope,receive,send)
        pending=bytearray()
        first_body=True
        last_flush=time.monotonic()
        async def paced_send(message):
            nonlocal first_body,last_flush
            if message['type']!='http.response.body':
                return await send(message)
            pending.extend(message.get('body',b''))
            more=message.get('more_body',False)
            if first_body or not more or len(pending)>=65536 or time.monotonic()-last_flush>=.25:
                body=bytes(pending);pending.clear()
                await send({**message,'body':body})
                first_body=False;last_flush=time.monotonic()
                if more and body:await asyncio.sleep(.02)
        try:
            await self.app(scope,receive,paced_send)
        finally:
            lease.close()
