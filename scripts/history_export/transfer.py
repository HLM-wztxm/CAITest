"""Chunked ZIP transfer, reused from the standalone history runner."""
import base64
import hashlib
import os
from common import Failure

CHUNK_SIZE = 256 * 1024


async def save_zip(handle, destination, expected_size, expected_hash):
    partial = destination / 'history.zip.part'
    target = destination / 'history.zip'
    digest = hashlib.sha256()
    partial_created = target_created = completed = False
    try:
        with partial.open('xb') as stream:
            partial_created = True
            for offset in range(0, expected_size, CHUNK_SIZE):
                encoded = await handle.evaluate(r"""(result, range) => {
                  const bytes = result.zip.subarray(range[0], range[1]);
                  let binary = '';
                  for (let i = 0; i < bytes.length; i += 8192)
                    binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
                  return btoa(binary);
                }""", [offset, offset + CHUNK_SIZE])
                data = base64.b64decode(encoded, validate=True)
                if len(data) != min(CHUNK_SIZE, expected_size - offset):
                    raise Failure('ZIP_TRANSFER_SIZE_MISMATCH')
                digest.update(data)
                stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if digest.hexdigest() != expected_hash:
            raise Failure('ZIP_TRANSFER_HASH_MISMATCH')
        # Exclusive destination creation also protects against an external writer.
        with target.open('xb') as stream:
            target_created = True
            with partial.open('rb') as source:
                while chunk := source.read(CHUNK_SIZE):
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
        completed = True
        return target
    finally:
        if partial_created:
            partial.unlink(missing_ok=True)
        if target_created and not completed:
            target.unlink(missing_ok=True)
