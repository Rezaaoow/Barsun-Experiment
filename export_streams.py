"""Bounded ZIP output; reuse synchronous CSV iterators without full-file buffering."""
from collections import deque
import zipfile
from starlette.responses import StreamingResponse


def export_response(content, **kwargs):
    response = StreamingResponse(content, **kwargs)
    response.export_iterator = iter(content)
    return response


class _ZipSink:
    def __init__(self):
        self.pending = deque()
        self.position = 0

    def write(self, body):
        if body:
            self.pending.append(bytes(body))
            self.position += len(body)
        return len(body)

    def tell(self):
        return self.position

    def flush(self):
        pass

    def drain(self):
        while self.pending:
            yield self.pending.popleft()


def zip_stream(files):
    sink = _ZipSink()
    with zipfile.ZipFile(sink, mode='w', compression=zipfile.ZIP_DEFLATED) as archive:
        for name, factory in files:
            with archive.open(name, mode='w', force_zip64=True) as entry:
                # Send an entry header before performing that export's queries.
                yield from sink.drain()
                response = factory()
                chunks = response.export_iterator if isinstance(response, StreamingResponse) else iter(response)
                try:
                    for chunk in chunks:
                        entry.write(chunk.encode('utf-8') if isinstance(chunk, str) else chunk)
                        yield from sink.drain()
                finally:
                    close = getattr(chunks, 'close', None)
                    if close:
                        close()
                del chunks, response
            yield from sink.drain()
    yield from sink.drain()
